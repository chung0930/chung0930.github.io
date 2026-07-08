#!/usr/bin/env python3
"""
內網音樂伺服器 v3（單一檔案）
啟動：pip3 install flask --break-system-packages && python3 music_server.py
音樂目錄：/sdcard/music-data  或  MUSIC_DIR=路徑 python3 music_server.py
埠：1979
"""
import os, json, hashlib, threading, time, struct, base64, re, io
from flask import Flask, jsonify, request, Response, abort

# ══════════════════════════════════════════════
# 設定
# ══════════════════════════════════════════════
MUSIC_DIR   = os.environ.get('MUSIC_DIR', '/sdcard/music-data')
PLAYLISTS_F = os.environ.get('PLAYLISTS_FILE', '/tmp/music_playlists_v3.json')
SUPPORTED   = {'.mp3','.flac','.m4a','.mp4','.aac','.ogg','.oga','.wav','.m4b'}

app           = Flask(__name__)
music_library = {}
albums        = {}
artists       = {}
playlists     = {}
library_lock  = threading.RLock()
scan_status   = {'scanning':False,'progress':0,'total':0,'done':False,'error':''}

# ══════════════════════════════════════════════
# 純 Python 音訊標籤解析器
# ══════════════════════════════════════════════
def _ss(b):
    return (b[0]<<21)|(b[1]<<14)|(b[2]<<7)|b[3]

def _txt(data, enc=0):
    try:
        if enc==0: return data.rstrip(b'\x00').decode('latin-1',errors='replace')
        if enc==1:
            bom=data[:2]
            d=data.rstrip(b'\x00\x00')
            return d.decode('utf-16' if bom in(b'\xff\xfe',b'\xfe\xff') else 'utf-16-le',errors='replace')
        if enc==2: return data.rstrip(b'\x00\x00').decode('utf-16-be',errors='replace')
        if enc==3: return data.rstrip(b'\x00').decode('utf-8',errors='replace')
    except: pass
    return data.rstrip(b'\x00').decode('utf-8',errors='replace')

def _clean(s):
    if not s: return ''
    return re.sub(r'[\ufffd\x00-\x08\x0b\x0c\x0e-\x1f]','',s).strip()

def _read_id3v1(f):
    try:
        f.seek(-128,2); t=f.read(128)
        if t[:3]!=b'TAG': return {}
        r={}
        for k,a,b2 in[('title',3,33),('artist',33,63),('album',63,93)]:
            v=t[a:b2].rstrip(b'\x00').decode('latin-1','replace').strip()
            if v: r[k]=v
        y=t[93:97].rstrip(b'\x00').decode('latin-1','replace').strip()
        if y: r['year']=y
        return r
    except: return {}

def _read_id3v2(f):
    res={}; cov=None
    try:
        f.seek(0); h=f.read(10)
        if h[:3]!=b'ID3': return res,cov
        ver=h[3]; flags=h[5]; tsz=_ss(h[6:10])
        pos=10
        if flags&0x40 and ver>=3:
            eb=f.read(4)
            esz=_ss(eb) if ver==4 else struct.unpack('>I',eb)[0]
            f.seek(esz-4,1); pos+=esz
        end=10+tsz
        while pos<end-10:
            f.seek(pos)
            if ver==2:
                fh=f.read(6)
                if len(fh)<6 or fh[:3]==b'\x00\x00\x00': break
                fid=fh[:3].decode('latin-1','replace')
                fsz=struct.unpack('>I',b'\x00'+fh[3:6])[0]; pos+=6
            else:
                fh=f.read(10)
                if len(fh)<10 or fh[:4]==b'\x00\x00\x00\x00': break
                try: fid=fh[:4].decode('latin-1','replace')
                except: break
                fsz=_ss(fh[4:8]) if ver==4 else struct.unpack('>I',fh[4:8])[0]
                pos+=10
            if fsz<=0 or fsz>tsz: pos+=1; continue
            fd=f.read(fsz); pos+=fsz
            if not fd: break
            enc=fd[0] if fd else 0
            maps={
                ('TIT2','TT2'):'title',('TPE1','TP1'):'artist',
                ('TALB','TAL'):'album',('TPE2','TP2'):'album_artist',
                ('TCON','TCO'):'genre',('TRCK','TRK'):'track',
            }
            for keys,field in maps.items():
                if fid in keys: res[field]=_txt(fd[1:],enc); break
            if fid in('TYER','TYE','TDRC'):
                y=_txt(fd[1:],enc).strip(); res['year']=y[:4] if y else ''
            elif fid in('APIC','PIC') and cov is None:
                try:
                    if fid=='APIC':
                        ni=fd.index(b'\x00',1); di=ni+2
                        di=fd.index(b'\x00\x00',di)+2 if enc in(1,2) else fd.index(b'\x00',di)+1
                        cov=fd[di:]
                    else:
                        di=5
                        di=fd.index(b'\x00\x00',di)+2 if enc in(1,2) else fd.index(b'\x00',di)+1
                        cov=fd[di:]
                except: pass
    except: pass
    return res,cov

def _read_mp4(f):
    res={}; cov=None
    def _atoms(f,end):
        d={}
        while f.tell()<end:
            try:
                h=f.read(8)
                if len(h)<8: break
                sz=struct.unpack('>I',h[:4])[0]
                try: nm=h[4:8].decode('latin-1','replace')
                except: nm=''
                if sz==1:
                    e=f.read(8)
                    if len(e)<8: break
                    sz=struct.unpack('>Q',e)[0]-8
                elif sz==0: sz=end-f.tell()+8
                if sz<8: break
                cs=f.tell(); d[nm]=(cs,sz-8); f.seek(cs+sz-8)
            except: break
        return d
    try:
        f.seek(0,2); fsz=f.tell(); f.seek(0)
        ms=me=None; pos=0
        while pos<fsz:
            f.seek(pos); h=f.read(8)
            if len(h)<8: break
            sz=struct.unpack('>I',h[:4])[0]; nm=h[4:8]
            if sz==1:
                e=f.read(8); sz=struct.unpack('>Q',e)[0]
            elif sz==0: sz=fsz-pos
            if nm==b'moov': ms=pos+8; me=pos+sz; break
            pos+=max(sz,8)
        if ms is None: return res,cov
        f.seek(ms); ma=_atoms(f,me)
        if 'udta' not in ma: return res,cov
        us,usz=ma['udta']; f.seek(us); ua=_atoms(f,us+usz)
        if 'meta' not in ua: return res,cov
        ms2,msz=ua['meta']; f.seek(ms2+4); met=_atoms(f,ms2+msz)
        if 'ilst' not in met: return res,cov
        ils,ilsz=met['ilst']; f.seek(ils); il=_atoms(f,ils+ilsz)
        tm={'\xa9nam':'title','\xa9ART':'artist','\xa9alb':'album','\xa9day':'year',
            '\xa9gen':'genre','aART':'album_artist','trkn':'track','covr':'cover'}
        for an,tk in tm.items():
            if an not in il: continue
            s2,s2sz=il[an]; f.seek(s2); sa=_atoms(f,s2+s2sz)
            if 'data' not in sa: continue
            ds,dssz=sa['data']; f.seek(ds+8); v=f.read(dssz-8)
            if tk=='cover': cov=v
            elif tk=='track':
                try: res['track']=str(struct.unpack('>H',v[2:4])[0])
                except: pass
            elif tk=='year': res['year']=v.decode('utf-8','replace').strip()[:4]
            else: res[tk]=v.decode('utf-8','replace').strip()
    except: pass
    return res,cov

def _read_flac(f):
    res={}; cov=None
    try:
        f.seek(0)
        if f.read(4)!=b'fLaC': return res,cov
        while True:
            bh=f.read(4)
            if len(bh)<4: break
            bt=bh[0]&0x7F; last=bh[0]&0x80
            bs=struct.unpack('>I',b'\x00'+bh[1:4])[0]; bd=f.read(bs)
            if bt==4:
                vl=struct.unpack('<I',bd[:4])[0]; p=4+vl
                cc=struct.unpack('<I',bd[p:p+4])[0]; p+=4
                for _ in range(cc):
                    cl=struct.unpack('<I',bd[p:p+4])[0]; p+=4
                    c=bd[p:p+cl].decode('utf-8','replace'); p+=cl
                    if '=' in c:
                        k,v=c.split('=',1); k=k.upper().strip(); v=v.strip()
                        km={'TITLE':'title','ARTIST':'artist','ALBUM':'album','DATE':'year',
                            'GENRE':'genre','ALBUMARTIST':'album_artist','TRACKNUMBER':'track'}
                        if k in km: res[km[k]]=v if k!='DATE' else v[:4]
            elif bt==6 and cov is None:
                ml=struct.unpack('>I',bd[4:8])[0]
                dp=8+ml; dl=struct.unpack('>I',bd[dp:dp+4])[0]
                ip=dp+4+dl+16; il=struct.unpack('>I',bd[ip:ip+4])[0]
                cov=bd[ip+4:ip+4+il]
            if last: break
    except: pass
    return res,cov

def _read_ogg(f):
    res={}
    try:
        f.seek(0)
        for _ in range(20):
            ph=f.read(27)
            if len(ph)<27 or ph[:4]!=b'OggS': break
            sc=ph[26]; st=f.read(sc); pd=f.read(sum(st))
            if len(pd)>7 and pd[0]==0x03 and pd[1:7]==b'vorbis':
                vl=struct.unpack('<I',pd[7:11])[0]; p=11+vl
                cc=struct.unpack('<I',pd[p:p+4])[0]; p+=4
                for _ in range(cc):
                    if p+4>len(pd): break
                    cl=struct.unpack('<I',pd[p:p+4])[0]; p+=4
                    c=pd[p:p+cl].decode('utf-8','replace'); p+=cl
                    if '=' in c:
                        k,v=c.split('=',1); k=k.upper()
                        km={'TITLE':'title','ARTIST':'artist','ALBUM':'album','DATE':'year',
                            'GENRE':'genre','TRACKNUMBER':'track'}
                        if k in km: res[km[k]]=v[:4] if k=='DATE' else v
                break
    except: pass
    return res,None

def _dur_mp3(f,fsz):
    try:
        f.seek(0); d=f.read(min(65536,fsz))
        for i in range(len(d)-3):
            if d[i]==0xFF and (d[i+1]&0xE0)==0xE0:
                layer=(d[i+1]>>1)&3; bi=(d[i+2]>>4)&0xF
                bt={3:[0,32,40,48,56,64,80,96,112,128,160,192,224,256,320,0]}
                if layer in bt and 0<bi<15:
                    br=bt[layer][bi]*1000
                    if br>0:
                        f.seek(0); h=f.read(10)
                        ts=_ss(h[6:10])+10 if h[:3]==b'ID3' else 0
                        return int((fsz-ts)*8/br)
    except: pass
    return 0

def _dur_flac(f):
    try:
        f.seek(4); bh=f.read(4)
        if bh[0]&0x7F==0:
            si=f.read(34)
            sr=((struct.unpack('>I',si[10:14])[0])>>12)&0xFFFFF
            ts=struct.unpack('>Q',b'\x00\x00'+si[13:21])[0]&0xFFFFFFFFFFF
            if sr>0: return int(ts/sr)
    except: pass
    return 0

def read_tags(fp):
    ext=os.path.splitext(fp)[1].lower()
    res={}; cov=None
    try:
        with open(fp,'rb') as f:
            if ext=='.mp3':
                res,cov=_read_id3v2(f)
                if not res.get('title'):
                    for k,v in _read_id3v1(f).items():
                        if not res.get(k): res[k]=v
                res['duration']=_dur_mp3(f,os.path.getsize(fp))
            elif ext in('.m4a','.mp4','.aac','.m4b','.m4p'):
                res,cov=_read_mp4(f); res.setdefault('duration',0)
            elif ext=='.flac':
                res,cov=_read_flac(f); res['duration']=_dur_flac(f)
            elif ext in('.ogg','.oga'):
                res,cov=_read_ogg(f); res.setdefault('duration',0)
            else:
                res.setdefault('duration',0)
    except: pass
    for k in('title','artist','album','album_artist','year','genre','track'):
        if k in res: res[k]=_clean(str(res[k]))
    if cov:
        res['cover_b64']=base64.b64encode(cov).decode('ascii')
        res['cover_mime']='image/jpeg' if cov[:2]==bytes([0xFF,0xD8]) else 'image/png'
    return res

# ══════════════════════════════════════════════
# 藝人名正規化（修正 regex bug）
# ══════════════════════════════════════════════
_ART_SEP = re.compile(r'[\n\r/／、,，&＆]|(?i:feat\.?)|(?i:ft\.?)|(?i:vs\.?)|(?i:x(?=\s))')

def _norm_artist(name):
    if not name: return 'Unknown Artist',['Unknown Artist']
    parts=[p.strip() for p in _ART_SEP.split(name) if p.strip()]
    seen=set(); uniq=[]
    for p in parts:
        lp=p.lower()
        if lp not in seen: seen.add(lp); uniq.append(p)
    return (uniq[0],uniq) if uniq else (name.strip(),[name.strip()])

# ══════════════════════════════════════════════
# 掃描
# ══════════════════════════════════════════════
def _sid(fp): return hashlib.md5(fp.encode()).hexdigest()[:16]

def scan_library():
    global music_library,albums,artists,scan_status
    scan_status={'scanning':True,'progress':0,'total':0,'done':False,'error':''}
    all_files=[]; seen=set()
    try:
        for root,dirs,files in os.walk(MUSIC_DIR):
            dirs.sort()
            for fn in sorted(files):
                if os.path.splitext(fn)[1].lower() not in SUPPORTED: continue
                fp=os.path.realpath(os.path.join(root,fn))
                if fp in seen: continue
                seen.add(fp); all_files.append(fp)
    except Exception as e:
        scan_status.update({'done':True,'scanning':False,'error':str(e)}); return
    scan_status['total']=len(all_files)
    print(f"Found {len(all_files)} audio files")
    nl={}; na={}; nar={}
    for i,fp in enumerate(all_files):
        scan_status['progress']=i+1
        try:
            sid=_sid(fp); fn=os.path.basename(fp)
            tags=read_tags(fp)
            title  =tags.get('title','').strip() or os.path.splitext(fn)[0]
            raw_art=tags.get('artist','').strip() or 'Unknown Artist'
            album  =tags.get('album','').strip() or 'Unknown Album'
            raw_aa =tags.get('album_artist','').strip()
            year   =tags.get('year','').strip()
            genre  =tags.get('genre','').strip()
            dur    =tags.get('duration',0)
            cb64   =tags.get('cover_b64','')
            cmime  =tags.get('cover_mime','image/jpeg')
            trk_s  =tags.get('track','')
            try: trk=int(str(trk_s).split('/')[0]) if trk_s else 0
            except: trk=0
            d_art,_   =_norm_artist(raw_art)
            d_aa,aa_l =_norm_artist(raw_aa) if raw_aa else (d_art,[d_art])
            # 音質識別
            fsz=os.path.getsize(fp); ext2=os.path.splitext(fp)[1].lower()
            if ext2=='.flac': qual='FLAC'
            elif ext2=='.wav': qual='WAV'
            elif ext2 in('.m4a','.mp4','.m4b','.aac'): qual='AAC'
            elif ext2 in('.ogg','.oga'): qual='OGG'
            else:
                # MP3：估算最接近的標準位元率
                if dur and dur>3:
                    raw_kbps=int((fsz*8)/(dur*1000))
                    std=[32,40,48,56,64,80,96,112,128,160,192,224,256,320]
                    closest=min(std,key=lambda x:abs(x-raw_kbps))
                    qual=f'{closest}kbps'
                else:
                    qual='MP3'
            nl[sid]={
                'id':sid,'title':title,'artist':d_art,'raw_artist':raw_art,
                'album':album,'album_artist':d_aa,'year':year,'track':trk,
                'genre':genre,'duration':dur,'filepath':fp,'quality':qual,
                'has_cover':bool(cb64),'cover_b64':cb64,'cover_mime':cmime,
            }
            akey=hashlib.md5(f"{d_aa}||{album}".encode()).hexdigest()[:12]
            if akey not in na:
                na[akey]={'id':akey,'name':album,'artist':d_aa,'year':year,
                          'songs':[],'cover_song_id':sid if cb64 else None}
            na[akey]['songs'].append(sid)
            if cb64 and not na[akey]['cover_song_id']: na[akey]['cover_song_id']=sid
            if year and not na[akey]['year']: na[akey]['year']=year
            for art in aa_l:
                if art not in nar:
                    nar[art]={'name':art,'albums':set(),'songs':[],'cover_song_id':None}
                nar[art]['albums'].add(akey)
                nar[art]['songs'].append(sid)
                if cb64 and not nar[art]['cover_song_id']: nar[art]['cover_song_id']=sid
        except Exception as e:
            print(f"Error {fp}: {e}")
    for a in na.values():
        a['songs'].sort(key=lambda s:nl.get(s,{}).get('track',999))
    for a in nar.values():
        a['albums']=list(a['albums'])
    with library_lock:
        music_library=nl; albums=na; artists=nar
    scan_status.update({'done':True,'scanning':False})
    print(f"Scan done: {len(nl)} songs, {len(na)} albums, {len(nar)} artists")

# ══════════════════════════════════════════════
# 歌單
# ══════════════════════════════════════════════
def load_playlists():
    global playlists
    try:
        if os.path.exists(PLAYLISTS_F):
            with open(PLAYLISTS_F,'r',encoding='utf-8') as f: playlists=json.load(f)
    except: playlists={}

def save_playlists():
    try:
        with open(PLAYLISTS_F,'w',encoding='utf-8') as f:
            json.dump(playlists,f,ensure_ascii=False,indent=2)
    except Exception as e: print(f"Playlist save: {e}")

# ══════════════════════════════════════════════
# Flask Routes
# ══════════════════════════════════════════════
def _strip(s): return {k:v for k,v in s.items() if k not in('cover_b64','filepath','raw_artist')}

@app.route('/')
def index(): return Response(INDEX_HTML,mimetype='text/html; charset=utf-8')

@app.route('/api/scan',methods=['POST'])
def api_scan():
    if scan_status.get('scanning'): return jsonify({'status':'already_scanning'})
    threading.Thread(target=scan_library,daemon=True).start()
    return jsonify({'status':'started'})

@app.route('/api/scan/status')
def api_scan_status(): return jsonify(scan_status)

@app.route('/api/library')
def api_library():
    with library_lock:
        s=sorted([_strip(x) for x in music_library.values()],key=lambda x:(x.get('artist',''),x.get('album',''),x.get('track',0)))
    return jsonify(s)

@app.route('/api/albums')
def api_albums():
    with library_lock:
        r=[{'id':a['id'],'name':a['name'],'artist':a['artist'],'year':a['year'],
            'song_count':len(a['songs']),'cover_song_id':a.get('cover_song_id')} for a in albums.values()]
    return jsonify(sorted(r,key=lambda x:(x.get('artist',''),x.get('name',''))))

@app.route('/api/albums/<aid>')
def api_album(aid):
    with library_lock:
        if aid not in albums: return jsonify({'error':'not found'}),404
        a=albums[aid]
        songs=[_strip(music_library[s]) for s in a['songs'] if s in music_library]
    return jsonify({'id':a['id'],'name':a['name'],'artist':a['artist'],'year':a['year'],
                    'songs':songs,'cover_song_id':a.get('cover_song_id')})

@app.route('/api/artists')
def api_artists():
    with library_lock:
        r=[{'name':n,'album_count':len(a['albums']),'song_count':len(a['songs']),
            'cover_song_id':a.get('cover_song_id')} for n,a in artists.items()]
    return jsonify(sorted(r,key=lambda x:x['name'].lower()))

@app.route('/api/artists/<path:name>')
def api_artist(name):
    with library_lock:
        if name not in artists: return jsonify({'error':'not found'}),404
        art=artists[name]
        art_albums=[{'id':albums[ai]['id'],'name':albums[ai]['name'],'year':albums[ai]['year'],
                     'song_count':len(albums[ai]['songs']),'cover_song_id':albums[ai].get('cover_song_id')}
                    for ai in art['albums'] if ai in albums]
    art_albums.sort(key=lambda x:x.get('year','') or '')
    return jsonify({'name':name,'albums':art_albums,'cover_song_id':art.get('cover_song_id')})

@app.route('/api/song/<sid>/cover')
def api_cover(sid):
    with library_lock: s=music_library.get(sid)
    if not s or not s.get('cover_b64'): abort(404)
    data=base64.b64decode(s['cover_b64'])
    return Response(data,mimetype=s.get('cover_mime','image/jpeg'),
                    headers={'Cache-Control':'public,max-age=86400','Content-Length':str(len(data))})

@app.route('/api/song/<sid>/stream')
def api_stream(sid):
    with library_lock: s=music_library.get(sid)
    if not s: abort(404)
    fp=s.get('filepath','')
    if not fp or not os.path.exists(fp): abort(404)
    ext=os.path.splitext(fp)[1].lstrip('.')
    ct={'mp3':'audio/mpeg','flac':'audio/flac','m4a':'audio/mp4','mp4':'audio/mp4',
        'aac':'audio/aac','ogg':'audio/ogg','oga':'audio/ogg','wav':'audio/wav','m4b':'audio/mp4'}.get(ext,'audio/mpeg')
    fsz=os.path.getsize(fp)
    rh=request.headers.get('Range')
    if rh:
        m=rh.strip().replace('bytes=','').split('-')
        st=int(m[0]) if m[0] else 0
        en=int(m[1]) if len(m)>1 and m[1] else fsz-1
        en=min(en,fsz-1); ln=en-st+1
        def gen():
            with open(fp,'rb') as f:
                f.seek(st); rem=ln
                while rem>0:
                    d=f.read(min(65536,rem))
                    if not d: break
                    rem-=len(d); yield d
        return Response(gen(),206,mimetype=ct,headers={'Content-Range':f'bytes {st}-{en}/{fsz}',
            'Accept-Ranges':'bytes','Content-Length':str(ln),'Cache-Control':'no-cache'})
    def gen2():
        with open(fp,'rb') as f:
            while True:
                d=f.read(65536)
                if not d: break
                yield d
    return Response(gen2(),mimetype=ct,headers={'Accept-Ranges':'bytes','Content-Length':str(fsz),'Cache-Control':'no-cache'})

@app.route('/api/song/<sid>')
def api_song(sid):
    with library_lock: s=music_library.get(sid)
    if not s: abort(404)
    return jsonify(_strip(s))

@app.route('/api/search')
def api_search():
    q=request.args.get('q','').strip().lower()
    if not q: return jsonify({'songs':[],'albums':[],'artists':[]})
    with library_lock:
        songs=[_strip(s) for s in music_library.values()
               if q in s.get('title','').lower() or q in s.get('artist','').lower() or q in s.get('album','').lower()]
        albs=[{'id':a['id'],'name':a['name'],'artist':a['artist'],'year':a['year'],
               'song_count':len(a['songs']),'cover_song_id':a.get('cover_song_id')}
              for a in albums.values() if q in a['name'].lower() or q in a['artist'].lower()]
        arts=[{'name':n,'album_count':len(a['albums']),'cover_song_id':a.get('cover_song_id')}
              for n,a in artists.items() if q in n.lower()]
    return jsonify({'songs':songs[:50],'albums':albs[:20],'artists':arts[:20]})

# ── 歌單 CRUD ──
@app.route('/api/playlists')
def api_pls(): return jsonify(list(playlists.values()))

@app.route('/api/playlists',methods=['POST'])
def api_pl_create():
    d=request.get_json(); name=(d or {}).get('name','').strip()
    if not name: return jsonify({'error':'name required'}),400
    pid=hashlib.md5(f"{name}{time.time()}".encode()).hexdigest()[:12]
    playlists[pid]={'id':pid,'name':name,'songs':[],'created':int(time.time())}
    save_playlists(); return jsonify(playlists[pid])

@app.route('/api/playlists/<pid>')
def api_pl_get(pid):
    if pid not in playlists: abort(404)
    pl=playlists[pid]
    with library_lock:
        details=[_strip(music_library[s]) for s in pl['songs'] if s in music_library]
    return jsonify({**pl,'song_details':details})

@app.route('/api/playlists/<pid>',methods=['PUT'])
def api_pl_update(pid):
    if pid not in playlists: abort(404)
    d=request.get_json()
    if 'name' in d: playlists[pid]['name']=d['name']
    if 'songs' in d: playlists[pid]['songs']=d['songs'][:400]
    save_playlists(); return jsonify(playlists[pid])

@app.route('/api/playlists/<pid>',methods=['DELETE'])
def api_pl_delete(pid):
    if pid not in playlists: abort(404)
    del playlists[pid]; save_playlists(); return jsonify({'status':'deleted'})

@app.route('/api/playlists/<pid>/songs',methods=['POST'])
def api_pl_add(pid):
    if pid not in playlists: abort(404)
    d=request.get_json(); s=d.get('song_id','')
    if not s: return jsonify({'error':'song_id required'}),400
    pl=playlists[pid]
    if len(pl['songs'])>=400: return jsonify({'error':'playlist full'}),400
    if s not in pl['songs']: pl['songs'].append(s)
    save_playlists(); return jsonify(pl)

@app.route('/api/playlists/<pid>/songs',methods=['DELETE'])
def api_pl_remove_batch(pid):
    if pid not in playlists: abort(404)
    d=request.get_json(); ids=d.get('song_ids',[])
    pl=playlists[pid]
    pl['songs']=[s for s in pl['songs'] if s not in ids]
    save_playlists(); return jsonify(pl)

@app.route('/api/playlists/<pid>/songs/<sid>',methods=['DELETE'])
def api_pl_remove(pid,sid):
    if pid not in playlists: abort(404)
    pl=playlists[pid]
    if sid in pl['songs']: pl['songs'].remove(sid)
    save_playlists(); return jsonify(pl)

# ══════════════════════════════════════════════
# 前端 HTML
# ══════════════════════════════════════════════
INDEX_HTML = r"""
<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#0f0f13">
<title>聆聽</title>
<style>
:root{
  --bg:#0f0f13;--s1:#1a1a22;--s2:#22222e;--s3:#2a2a38;
  --acc:#a78bfa;--acc2:#7c3aed;--glow:rgba(167,139,250,.18);
  --t1:#e8e6f0;--t2:#9896a8;--t3:#5c5a6e;
  --bdr:rgba(167,139,250,.13);--red:#f87171;
  --ph:88px;--sw:220px;--tbh:56px;
}
*{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent}
html,body{height:100%;overflow:hidden}
body{font-family:'Segoe UI',system-ui,-apple-system,sans-serif;background:var(--bg);color:var(--t1);display:flex;flex-direction:column}
button{font-family:inherit}

/* ── Layout ── */
#app{display:flex;flex:1;overflow:hidden;min-height:0}
#sidebar{width:var(--sw);background:var(--s1);border-right:1px solid var(--bdr);display:flex;flex-direction:column;flex-shrink:0;overflow:hidden}
#logo{padding:20px 16px 14px;font-size:17px;font-weight:700;color:var(--acc);display:flex;align-items:center;gap:8px;flex-shrink:0}
#snav{flex:1;overflow-y:auto;padding:4px 8px 16px}
.ns{margin-bottom:14px}
.nst{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:var(--t3);padding:8px 8px 4px;display:block}
.ni{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:8px;cursor:pointer;font-size:13.5px;font-weight:500;color:var(--t2);transition:all .15s;white-space:nowrap;overflow:hidden}
.ni:hover{background:var(--s2);color:var(--t1)}.ni.active{background:var(--glow);color:var(--acc)}
.pli{display:flex;align-items:center;justify-content:space-between;padding:6px 10px;border-radius:8px;cursor:pointer;font-size:13px;color:var(--t2);transition:all .15s}
.pli:hover{background:var(--s2);color:var(--t1)}.pli.active{background:var(--glow);color:var(--acc)}
.pli-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.xbtn{background:none;border:none;color:var(--t3);cursor:pointer;padding:2px 6px;border-radius:4px;font-size:14px;flex-shrink:0;opacity:0}
.pli:hover .xbtn{opacity:1}.xbtn:hover{color:var(--red)}
.npl-btn{display:flex;align-items:center;gap:6px;padding:7px 10px;border-radius:8px;cursor:pointer;font-size:13px;color:var(--t3);transition:all .15s}
.npl-btn:hover{background:var(--s2);color:var(--acc)}

/* ── Main ── */
#main{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
#topbar{display:flex;align-items:center;gap:10px;padding:12px 20px;border-bottom:1px solid var(--bdr);background:var(--s1);flex-shrink:0}
#sb{flex:1;max-width:400px;background:var(--s2);border:1px solid var(--bdr);border-radius:24px;padding:9px 16px;color:var(--t1);font-size:13.5px;outline:none;transition:border-color .2s}
#sb:focus{border-color:var(--acc)}
#sb::placeholder{color:var(--t3)}
#scanbtn{background:var(--s2);border:1px solid var(--bdr);color:var(--t2);padding:8px 14px;border-radius:8px;cursor:pointer;font-size:13px;transition:all .15s;white-space:nowrap;flex-shrink:0}
#scanbtn:hover{border-color:var(--acc);color:var(--acc)}
#scanst{font-size:12px;color:var(--t3);white-space:nowrap;flex-shrink:0}
#content{flex:1;overflow-y:auto;padding:20px 24px;scroll-behavior:smooth}

/* ── Views ── */
.view{display:none}.view.active{display:block}
.pg{font-size:22px;font-weight:700;margin-bottom:20px}
.back-btn{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:var(--t2);cursor:pointer;margin-bottom:16px;padding:4px 0;transition:color .15s}
.back-btn:hover{color:var(--acc)}

/* ── Song list ── */
.sl{display:flex;flex-direction:column;gap:1px}
.sr{display:grid;grid-template-columns:32px 44px 1fr 1fr 80px 56px 28px;align-items:center;gap:10px;padding:7px 10px;border-radius:8px;cursor:pointer;transition:background .12s}
.sr:hover{background:var(--s2)}.sr.playing{background:var(--glow)}
.sr.playing .s-title{color:var(--acc)}
.s-num{color:var(--t3);font-size:12px;text-align:center;font-variant-numeric:tabular-nums}
.s-thumb{width:40px;height:40px;border-radius:6px;background:var(--s3);flex-shrink:0;position:relative;overflow:hidden}
.s-thumb img{width:100%;height:100%;object-fit:cover;display:block}
.s-thumb-ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--t3);font-size:15px;background:var(--s3)}
.s-title{font-size:13.5px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.s-artist{font-size:13px;color:var(--t2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.s-album{font-size:13px;color:var(--t3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.s-dur{font-size:12px;color:var(--t3);text-align:right;font-variant-numeric:tabular-nums}
.s-act{position:relative}
/* 改為三點選單按鈕 */
.dot-btn{background:none;border:none;color:var(--t3);cursor:pointer;padding:4px 6px;border-radius:6px;font-size:18px;opacity:0;transition:all .15s;line-height:1}
.sr:hover .dot-btn{opacity:1}.dot-btn:hover{color:var(--acc);background:var(--s3)}
.sl-hdr{display:grid;grid-template-columns:32px 44px 1fr 1fr 80px 56px 28px;gap:10px;padding:4px 10px 8px;font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.5px;color:var(--t3);border-bottom:1px solid var(--bdr);margin-bottom:4px}
/* 播放中動畫 */
.pi{display:inline-flex;align-items:flex-end;gap:2px;height:14px}
.pi span{display:inline-block;width:3px;background:var(--acc);border-radius:1px}
.pi.on span:nth-child(1){animation:bnc .8s ease infinite}
.pi.on span:nth-child(2){animation:bnc .8s .15s ease infinite}
.pi.on span:nth-child(3){animation:bnc .8s .3s ease infinite}
@keyframes bnc{0%,100%{height:4px}50%{height:14px}}

/* ── Album cards ── */
.ag{display:grid;grid-template-columns:repeat(auto-fill,minmax(148px,1fr));gap:14px}
.ac{cursor:pointer;border-radius:12px;background:var(--s1);border:1px solid var(--bdr);transition:all .2s;padding:10px;display:flex;flex-direction:column}
.ac:hover{border-color:var(--acc);background:var(--s2);transform:translateY(-2px);box-shadow:0 6px 20px rgba(0,0,0,.35)}
/* 封面 固定 1:1 比例，overflow:hidden 防止破圖 */
.ac-cover{width:100%;aspect-ratio:1/1;border-radius:8px;overflow:hidden;background:var(--s3);margin-bottom:8px;position:relative;flex-shrink:0}
.ac-cover img{width:100%;height:100%;object-fit:cover;display:block;position:absolute;inset:0}
.ac-cover-ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:34px;color:var(--t3)}
.po{position:absolute;inset:0;background:rgba(0,0,0,.4);display:flex;align-items:center;justify-content:center;opacity:0;transition:opacity .2s;border-radius:8px}
.ac:hover .po{opacity:1}
/* 文字區 固定高度，不讓它撐大卡片 */
.ac-texts{flex:1;min-height:0;overflow:hidden}
.ac-name{font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--t1)}
.ac-sub{font-size:12px;color:var(--t2);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ac-year{font-size:11px;color:var(--t3);margin-top:2px}

/* ── Artist cards ── */
.artg{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:12px}
.artc{cursor:pointer;padding:14px 10px;border-radius:12px;background:var(--s1);border:1px solid var(--bdr);text-align:center;transition:all .2s;display:flex;flex-direction:column;align-items:center}
.artc:hover{border-color:var(--acc);transform:translateY(-2px)}
/* 藝人頭像：固定大小，不讓它變形 */
.art-av{width:72px;height:72px;border-radius:50%;background:var(--s3);margin:0 auto 10px;overflow:hidden;position:relative;flex-shrink:0}
.art-av img{width:100%;height:100%;object-fit:cover;display:block}
.art-av-ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:28px;color:var(--acc);background:var(--s3)}
.art-name{font-size:13px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--t1);width:100%}
.art-sub{font-size:11px;color:var(--t3);margin-top:3px}

/* ── Album detail ── */
.adh{display:flex;gap:24px;margin-bottom:28px;align-items:flex-end}
.adc-wrap{width:180px;height:180px;border-radius:12px;overflow:hidden;background:var(--s3);flex-shrink:0;box-shadow:0 8px 32px rgba(0,0,0,.5);position:relative}
.adc-wrap img{width:100%;height:100%;object-fit:cover;display:block}
.adc-ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:56px;color:var(--t3)}
.ad-label{font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:var(--acc);margin-bottom:6px}
.ad-title{font-size:26px;font-weight:800;line-height:1.2;margin-bottom:6px;word-break:break-word}
.ad-meta{font-size:13px;color:var(--t3);margin-bottom:16px}

/* ── Artist detail ── */
.art-hero{position:relative;height:200px;background:var(--s2);border-radius:14px;margin-bottom:24px;overflow:hidden;display:flex;align-items:flex-end;padding:20px;flex-shrink:0}
.art-hero img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;object-position:top;opacity:.55}
.art-hero-overlay{position:absolute;inset:0;background:linear-gradient(to top,rgba(0,0,0,.85) 35%,transparent 70%)}
.art-hero-info{position:relative;z-index:1}
.art-hero-name{font-size:28px;font-weight:800;text-shadow:0 2px 8px rgba(0,0,0,.5)}
.art-hero-sub{font-size:13px;color:var(--t2);margin-top:4px}

/* ── Playlist ── */
.pl-hdr{display:flex;align-items:center;gap:14px;margin-bottom:20px}
.pl-icon{width:72px;height:72px;background:linear-gradient(135deg,var(--acc2),#4c1d95);border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:30px;flex-shrink:0}
.pl-title{font-size:22px;font-weight:700}
.pl-meta{font-size:13px;color:var(--t3);margin-top:4px}

/* ── Buttons ── */
.play-all{background:var(--acc);border:none;color:#fff;padding:10px 22px;border-radius:24px;cursor:pointer;font-size:14px;font-weight:600;display:inline-flex;align-items:center;gap:7px;transition:all .15s}
.play-all:hover{background:var(--acc2);transform:scale(1.02)}
.btnp{background:var(--acc);border:none;color:#fff;padding:8px 18px;border-radius:8px;cursor:pointer;font-size:13.5px;font-weight:600;transition:all .15s}
.btnp:hover{background:var(--acc2)}
.btns{background:var(--s3);border:1px solid var(--bdr);color:var(--t2);padding:8px 18px;border-radius:8px;cursor:pointer;font-size:13.5px;transition:all .15s}
.btns:hover{color:var(--t1)}
.icon-btn{background:none;border:none;color:var(--t2);cursor:pointer;padding:6px;border-radius:50%;transition:all .15s;display:flex;align-items:center;justify-content:center}
.icon-btn:hover{color:var(--t1);background:var(--s2)}.icon-btn.active{color:var(--acc)}

/* ── Desktop Player ── */
#player{height:var(--ph);background:var(--s1);border-top:1px solid var(--bdr);display:flex;align-items:center;padding:0 20px;gap:16px;flex-shrink:0;position:relative}
#p-left{display:flex;align-items:center;gap:12px;width:260px;flex-shrink:0;overflow:hidden}
.p-cover{width:52px;height:52px;border-radius:8px;overflow:hidden;background:var(--s3);flex-shrink:0;position:relative}
.p-cover img{width:100%;height:100%;object-fit:cover;display:block}
.p-cover-ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--t3);font-size:18px;background:var(--s3)}
#p-info{overflow:hidden;flex:1}
#p-title{font-size:13.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#p-artist{font-size:12px;color:var(--t2);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#p-quality{font-size:10px;color:var(--acc);margin-top:2px;font-weight:600;letter-spacing:.5px}
#p-center{flex:1;display:flex;flex-direction:column;align-items:center;gap:6px}
#p-ctrl{display:flex;align-items:center;gap:6px}
#play-btn{width:40px;height:40px;border-radius:50%;background:var(--acc);border:none;color:#fff;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s;box-shadow:0 2px 12px rgba(167,139,250,.3)}
#play-btn:hover{background:var(--acc2);transform:scale(1.05)}
#p-prog{width:100%;max-width:480px;display:flex;align-items:center;gap:10px}
#tc,#tt{font-size:11px;color:var(--t3);font-variant-numeric:tabular-nums;flex-shrink:0;width:32px}
#tt{text-align:right}
.prog-wrap{flex:1;height:4px;background:var(--s3);border-radius:2px;cursor:pointer;position:relative;touch-action:none}
.prog-fill{height:100%;background:var(--acc);border-radius:2px;pointer-events:none}
.prog-thumb{position:absolute;top:50%;transform:translate(-50%,-50%);width:13px;height:13px;background:#fff;border-radius:50%;pointer-events:none;opacity:0;transition:opacity .15s;box-shadow:0 1px 4px rgba(0,0,0,.4)}
.prog-wrap:hover .prog-thumb,.prog-wrap.drag .prog-thumb{opacity:1}
#p-right{display:flex;align-items:center;gap:6px;width:200px;justify-content:flex-end}
#vol-wrap{display:flex;align-items:center;gap:6px}
#vol-bar{width:76px;height:4px;background:var(--s3);border-radius:2px;cursor:pointer;position:relative;touch-action:none}
#vol-fill{height:100%;background:var(--acc);border-radius:2px;pointer-events:none}
#eq-btn{background:none;border:none;color:var(--t2);cursor:pointer;padding:6px 8px;border-radius:6px;font-size:12px;font-weight:700;transition:all .15s}
#eq-btn:hover,#eq-btn.active{color:var(--acc)}

/* ── EQ Panel (桌面版，不影響手機) ── */
#eq-panel{display:none;position:fixed;bottom:calc(var(--ph) + 10px);right:20px;background:var(--s2);border:1px solid var(--bdr);border-radius:16px;padding:18px;z-index:250;box-shadow:0 8px 32px rgba(0,0,0,.5);width:380px;max-width:95vw}
#eq-panel.open{display:block}
.eq-t{font-size:14px;font-weight:700;margin-bottom:12px;display:flex;align-items:center;justify-content:space-between}
.eq-pres{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:12px}
.eq-pb{background:var(--s3);border:1px solid var(--bdr);color:var(--t2);padding:4px 9px;border-radius:20px;cursor:pointer;font-size:11px;transition:all .15s}
.eq-pb:hover,.eq-pb.active{background:var(--glow);border-color:var(--acc);color:var(--acc)}
/* EQ 滑桿容器：固定高度，不破版 */
.eq-bands-wrap{display:flex;gap:6px;align-items:flex-end;justify-content:space-between;height:100px;margin-bottom:6px;overflow:hidden}
.eq-band{display:flex;flex-direction:column;align-items:center;gap:3px;flex:1}
.eq-band input[type=range]{writing-mode:vertical-lr;direction:rtl;width:20px;height:72px;cursor:pointer;accent-color:var(--acc);flex-shrink:0}
.eq-lbl{font-size:9px;color:var(--t3);text-align:center;white-space:nowrap}
.eq-val{font-size:9px;color:var(--acc);font-variant-numeric:tabular-nums;text-align:center}

/* ── Queue Sheet ── */
#qs-overlay{display:none;position:fixed;inset:0;z-index:400;background:rgba(0,0,0,.55);backdrop-filter:blur(6px)}
#qs-overlay.open{display:flex;align-items:flex-end}
#qs-panel{background:var(--s1);border-radius:20px 20px 0 0;width:100%;max-height:80dvh;display:flex;flex-direction:column}
.qs-hdr{display:flex;align-items:center;justify-content:space-between;padding:14px 18px 10px;border-bottom:1px solid var(--bdr);flex-shrink:0;gap:8px}
.qs-hdr-title{font-size:15px;font-weight:700}
.qs-tabs{display:flex;gap:4px}
.qs-tab{background:none;border:none;color:var(--t3);cursor:pointer;padding:5px 12px;border-radius:20px;font-size:13px;transition:all .15s}
.qs-tab.active{background:var(--glow);color:var(--acc)}
.qs-hdr-acts{display:flex;align-items:center;gap:4px}
#qs-body{flex:1;overflow-y:auto}
.qs-top{display:flex;align-items:center;justify-content:space-between;padding:10px 18px 6px;flex-shrink:0}
.qs-info-txt{font-size:12px;color:var(--t3)}
.qs-acts{display:flex;gap:6px}
.qs-act-btn{background:var(--s2);border:1px solid var(--bdr);color:var(--t2);padding:5px 12px;border-radius:20px;cursor:pointer;font-size:12px;transition:all .15s}
.qs-act-btn:hover{border-color:var(--acc);color:var(--acc)}
/* Queue row */
.qr{display:flex;align-items:center;gap:12px;padding:8px 18px;cursor:pointer;transition:background .12s;user-select:none}
.qr:hover{background:var(--s2)}.qr.playing{background:var(--glow)}
.qr.playing .qr-title{color:var(--acc)}
.qr-thumb{width:40px;height:40px;border-radius:6px;overflow:hidden;background:var(--s3);flex-shrink:0;position:relative}
.qr-thumb img{width:100%;height:100%;object-fit:cover;display:block}
.qr-thumb-ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--t3);font-size:13px}
.qr-info{flex:1;min-width:0}
.qr-title{font-size:13.5px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.qr-artist{font-size:12px;color:var(--t2);margin-top:2px}
.qr-dur{font-size:12px;color:var(--t3);font-variant-numeric:tabular-nums;flex-shrink:0}
.qr-drag{color:var(--t3);flex-shrink:0;cursor:grab;padding:4px 6px;font-size:16px}
.qr-drag:active{cursor:grabbing}
/* Song info tab */
#qs-info-body{padding:18px}
.qi-top{display:flex;align-items:flex-start;gap:14px;margin-bottom:18px}
.qi-cover{width:80px;height:80px;border-radius:8px;overflow:hidden;background:var(--s3);flex-shrink:0;position:relative}
.qi-cover img{width:100%;height:100%;object-fit:cover;display:block}
.qi-cover-ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:var(--t3);font-size:28px}
.qi-title{font-size:16px;font-weight:700;margin-bottom:3px;word-break:break-word}
.qi-artist{font-size:13px;color:var(--t2);margin-bottom:3px}
.qi-qual{font-size:11px;color:var(--acc);font-weight:600;letter-spacing:.5px}
.qi-row{display:flex;justify-content:space-between;align-items:center;padding:9px 0;border-bottom:1px solid var(--bdr);font-size:13px}
.qi-row:last-child{border:none}
.qi-key{color:var(--t3)}
.qi-val{color:var(--t1);word-break:break-all;text-align:right;max-width:60%}
.qi-acts{margin-top:14px;display:flex;flex-direction:column;gap:6px}
.qi-act{display:flex;align-items:center;gap:10px;padding:11px 14px;background:var(--s2);border:1px solid var(--bdr);border-radius:10px;cursor:pointer;font-size:13.5px;color:var(--t2);transition:all .15s;text-align:left;width:100%;font-family:inherit}
.qi-act:hover{border-color:var(--acc);color:var(--acc)}

/* ── Modals ── */
.mo{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:500;align-items:center;justify-content:center;backdrop-filter:blur(4px)}
.mo.open{display:flex}
.mw{background:var(--s2);border:1px solid var(--bdr);border-radius:16px;padding:26px;min-width:300px;max-width:90vw;box-shadow:0 16px 48px rgba(0,0,0,.5)}
.mw-t{font-size:16px;font-weight:700;margin-bottom:14px}
.mi{width:100%;background:var(--s3);border:1px solid var(--bdr);border-radius:8px;padding:10px 14px;color:var(--t1);font-size:14px;outline:none;margin-bottom:14px;font-family:inherit}
.mi:focus{border-color:var(--acc)}
.ma{display:flex;gap:10px;justify-content:flex-end}
.mo-item{padding:12px 0;border-bottom:1px solid var(--bdr);cursor:pointer;font-size:14px;color:var(--t2);transition:color .15s;display:flex;align-items:center;gap:10px}
.mo-item:hover{color:var(--acc)}.mo-item:last-child{border:none}

/* ── Song Action Menu (三點選單) ── */
#song-menu{display:none;position:fixed;background:var(--s2);border:1px solid var(--bdr);border-radius:14px;padding:6px;z-index:600;box-shadow:0 8px 24px rgba(0,0,0,.5);min-width:220px;max-width:280px}
#song-menu.open{display:block}
.sm-header{padding:10px 14px 8px;border-bottom:1px solid var(--bdr);margin-bottom:4px}
.sm-title{font-size:13.5px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sm-artist{font-size:12px;color:var(--t2);margin-top:2px}
.sm-item{padding:10px 14px;cursor:pointer;font-size:13.5px;color:var(--t2);border-radius:8px;transition:all .15s;display:flex;align-items:center;gap:8px}
.sm-item:hover{background:var(--s3);color:var(--acc)}
/* 快速加入播放清單提示 */
#sm-quick-pl{font-size:11px;color:var(--acc);padding:4px 14px 8px;display:none}

/* ── Misc ── */
.empty{text-align:center;padding:60px 20px;color:var(--t3)}
.empty-i{font-size:44px;margin-bottom:12px}
.loading{text-align:center;padding:40px;color:var(--t3);font-size:14px}
#scanbar{height:2px;background:var(--acc);position:fixed;top:0;left:0;z-index:1000;width:0;transition:width .3s}
#toast{position:fixed;bottom:calc(var(--ph)+18px);left:50%;transform:translateX(-50%) translateY(20px);background:var(--s2);border:1px solid var(--bdr);color:var(--t1);padding:10px 20px;border-radius:24px;font-size:13px;opacity:0;pointer-events:none;transition:all .3s;z-index:1000;white-space:nowrap;max-width:85vw;overflow:hidden;text-overflow:ellipsis}
#toast.on{opacity:1;transform:translateX(-50%) translateY(0)}

/* ════ 手機版 (≤768px) ════ */
#mtab,#np-sheet{display:none}

@media(max-width:768px){
  :root{--ph:64px}
  #sidebar{display:none}
  #app{flex-direction:column}
  #main{width:100%}
  #topbar{padding:9px 12px;gap:8px}
  #sb{max-width:none;font-size:14px;padding:8px 14px}
  #scanbtn{padding:8px;font-size:0;width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;flex-shrink:0}
  #scanbtn::before{content:'🔄';font-size:15px}
  #scanst{display:none}
  #content{padding:12px 12px calc(var(--ph) + var(--tbh) + 12px + env(safe-area-inset-bottom,0px))}
  .pg{font-size:19px;margin-bottom:14px}

  /* 歌曲列表手機版 */
  .sl-hdr{display:none}
  .s-thumb{width:44px;height:44px}
  .sr{display:grid;grid-template-columns:44px 1fr 28px;grid-template-rows:auto auto;column-gap:12px;row-gap:0;padding:7px 4px;align-items:center}
  .sr>:nth-child(1){display:none}
  .sr>:nth-child(2){grid-column:1;grid-row:1/3;align-self:center}
  .sr .s-title{grid-column:2;grid-row:1;align-self:end;font-size:14px;padding-bottom:1px}
  .sr .s-artist{grid-column:2;grid-row:2;align-self:start;font-size:12.5px}
  .sr .s-album{display:none}.sr .s-dur{display:none}
  .sr .s-act{grid-column:3;grid-row:1/3;align-self:center}
  .dot-btn{opacity:1;font-size:16px;padding:5px}

  /* Album/Artist grid 手機版 */
  .ag{grid-template-columns:repeat(2,1fr);gap:10px}
  .artg{grid-template-columns:repeat(3,1fr);gap:8px}
  .art-av{width:52px;height:52px}
  .art-av-ph{font-size:20px}
  .artc{padding:12px 6px}
  .art-name{font-size:11px}.art-sub{font-size:10px}

  /* Album detail 手機版 */
  .adh{flex-direction:column;align-items:center;text-align:center;gap:14px;margin-bottom:20px}
  .adc-wrap{width:150px;height:150px}
  .ad-title{font-size:20px}
  .art-hero{height:150px}
  .art-hero-name{font-size:22px}

  /* Mini player */
  #player{position:fixed;bottom:0;left:0;right:0;height:var(--ph);padding:0 10px;gap:10px;z-index:160;cursor:pointer}
  #p-left{width:auto;flex:1;min-width:0;gap:10px}
  .p-cover{width:42px;height:42px}
  #p-info{flex:1;cursor:default}
  #p-title{font-size:13px}
  #p-artist{font-size:11px}.#p-quality{font-size:9px}
  #p-center{display:none}
  #p-right{width:auto;gap:4px}
  #vol-wrap,#eq-btn{display:none}
  .mb-ctrls{display:flex;align-items:center;gap:4px}
  #mb-play{width:36px;height:36px;border-radius:50%;background:var(--acc);border:none;color:#fff;display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0}
  #mb-next{width:36px;height:36px;border-radius:50%;background:none;border:none;color:var(--t2);display:flex;align-items:center;justify-content:center;cursor:pointer;flex-shrink:0}
  #mp-line{position:absolute;top:0;left:0;height:2px;background:var(--acc);width:0;transition:width .1s linear}

  /* Tab bar */
  #mtab{display:flex;position:fixed;bottom:var(--ph);left:0;right:0;height:var(--tbh);background:var(--s1);border-top:1px solid var(--bdr);z-index:150;padding-bottom:env(safe-area-inset-bottom,0)}
  .ti{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;color:var(--t3);cursor:pointer;font-size:10px;font-weight:500;transition:color .15s}
  .ti.active{color:var(--acc)}

  /* ═══ Now Playing Sheet 完全重寫 ═══
     所有元素固定高度，封面 flex:1 自動適應，不超出螢幕 */
  #np-sheet{
    display:flex;flex-direction:column;
    position:fixed;inset:0;
    background:var(--np-bg,#130a22);
    z-index:500;
    transform:translateY(100%);
    transition:transform .32s cubic-bezier(.32,.72,0,1);
    /* 關鍵：overflow hidden 防止任何子元素撐出去 */
    overflow:hidden;
  }
  #np-sheet.open{transform:translateY(0)}

  /* NP header：固定高度 54px */
  #np-hdr{
    display:flex;align-items:center;justify-content:space-between;
    padding:env(safe-area-inset-top,14px) 16px 0;
    height:calc(54px + env(safe-area-inset-top,0px));
    flex-shrink:0;
  }
  .np-close{background:none;border:none;color:var(--t2);cursor:pointer;padding:8px;line-height:1}
  .np-lbl{font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:var(--t3)}
  .np-menu-btn{background:none;border:none;color:var(--t2);cursor:pointer;padding:8px;font-size:20px;line-height:1}

  /* NP 封面：flex:1 佔剩餘空間，min-height:0 防止溢出 */
  #np-cover-wrap{
    flex:1;min-height:0;
    display:flex;align-items:center;justify-content:center;
    padding:8px 20px;
  }
  #np-ci-box{
    /* 封面尺寸：取 vw 和可用高度的較小值 */
    width:min(80vw,calc(100dvh - 380px));
    height:min(80vw,calc(100dvh - 380px));
    min-width:120px;min-height:120px;
    border-radius:16px;overflow:hidden;background:var(--s3);
    box-shadow:0 20px 60px rgba(0,0,0,.5);
    position:relative;flex-shrink:0;
  }
  #np-ci-box img{width:100%;height:100%;object-fit:cover;display:block}
  #np-ci-ph{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:60px;color:var(--t3)}

  /* EQ 抽屜：max-height 動畫，不影響封面大小 */
  #np-eq-drawer{
    flex-shrink:0;overflow:hidden;
    max-height:0;transition:max-height .3s ease;
    background:rgba(0,0,0,.3);margin:0 16px;border-radius:12px;
  }
  #np-eq-drawer.open{max-height:140px}
  #np-eq-inner{padding:10px 12px}
  /* EQ 在 NP 內的滑桿：固定高度 */
  #np-eq-inner .eq-bands-wrap{height:80px;margin-bottom:4px}
  #np-eq-inner .eq-band input[type=range]{height:60px}
  #np-eq-inner .eq-pres{margin-bottom:8px}

  /* NP 歌曲資訊：固定高度約 76px */
  #np-meta{
    flex-shrink:0;padding:10px 20px 0;
    text-align:center;position:relative;
  }
  .np-title{
    font-size:20px;font-weight:700;
    overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
    margin-bottom:3px;padding:0 36px;  /* 為心形按鈕留空間 */
    color:var(--t1);
  }
  .np-artist{font-size:14px;color:var(--t2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .np-qual{font-size:11px;color:var(--acc);font-weight:600;letter-spacing:.5px;margin-top:3px}
  .np-heart{position:absolute;right:12px;top:10px;background:none;border:none;color:var(--t2);cursor:pointer;font-size:22px;padding:4px;line-height:1}

  /* NP 進度條：固定高度 ~44px */
  #np-prog{flex-shrink:0;padding:12px 20px 4px}
  #np-pw{width:100%;height:5px;background:var(--s3);border-radius:3px;cursor:pointer;position:relative;touch-action:none}
  #np-pf{height:100%;background:var(--acc);border-radius:3px;pointer-events:none}
  #np-pth{position:absolute;top:50%;transform:translate(-50%,-50%);width:14px;height:14px;background:#fff;border-radius:50%;pointer-events:none;box-shadow:0 1px 4px rgba(0,0,0,.4)}
  #np-times{display:flex;justify-content:space-between;font-size:11px;color:var(--t3);margin-top:5px;font-variant-numeric:tabular-nums}

  /* NP 控制按鈕：固定高度 ~76px */
  #np-ctrl{display:flex;align-items:center;justify-content:space-evenly;padding:8px 16px 10px;flex-shrink:0}
  .np-btn{background:none;border:none;color:var(--t2);cursor:pointer;padding:8px;display:flex;align-items:center;justify-content:center;transition:color .15s}
  .np-btn.active{color:var(--acc)}
  #np-play-btn{width:58px;height:58px;border-radius:50%;background:var(--acc);color:#fff;border:none;cursor:pointer;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 20px rgba(167,139,250,.35)}

  /* NP 待播清單拉板：固定在底部，點 handle 展開 */
  #np-queue-section{flex-shrink:0;display:flex;flex-direction:column;background:rgba(0,0,0,.25);border-radius:16px 16px 0 0}
  #np-queue-handle{padding:8px 0 0;cursor:pointer;text-align:center;flex-shrink:0}
  #np-queue-bar{width:36px;height:4px;background:rgba(255,255,255,.2);border-radius:2px;display:inline-block}
  .npq-hdr{display:flex;align-items:center;justify-content:space-between;padding:6px 16px 4px;flex-shrink:0}
  .npq-title{font-size:13px;font-weight:700;color:var(--t2)}
  .npq-save{background:rgba(167,139,250,.15);border:1px solid var(--acc);color:var(--acc);padding:4px 11px;border-radius:14px;cursor:pointer;font-size:12px;font-weight:600}
  #np-queue-list{overflow-y:auto;height:130px;transition:height .3s}
  #np-queue-list.expanded{height:42dvh}

  /* Modal 手機版底部彈出 */
  .mo{align-items:flex-end}
  .mo .mw{width:100%;border-radius:20px 20px 0 0;padding:22px 18px calc(22px + env(safe-area-inset-bottom,0));max-height:75dvh;overflow-y:auto;min-width:unset}
  #toast{bottom:calc(var(--ph) + var(--tbh) + 12px)}
  #eq-panel{display:none!important}
  /* Song menu 手機版 */
  #song-menu{position:fixed;bottom:0;left:0;right:0;border-radius:20px 20px 0 0;max-width:100%;min-width:unset;padding-bottom:calc(8px + env(safe-area-inset-bottom,0))}

  /* Mine view */
  .mine-item{background:var(--s1);border:1px solid var(--bdr);border-radius:12px;padding:14px;margin-bottom:8px;display:flex;align-items:center;justify-content:space-between;cursor:pointer;font-size:14px;transition:border-color .15s}
  .mine-item:hover{border-color:var(--acc)}
  .mine-item .xbtn{opacity:1}
}

@media(max-width:360px){
  .artg{grid-template-columns:repeat(2,1fr)}
  .np-title{font-size:17px}
  #np-ctrl{gap:10px}
}
</style>
</head>
<body>
<div id="scanbar"></div>

<div id="app">
<!-- Sidebar (桌面) -->
<div id="sidebar">
  <div id="logo"><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="4"/><path d="M12 8V4m0 16v-4M8 12H4m16 0h-4"/></svg>聆聽</div>
  <div id="snav">
    <div class="ns">
      <div class="ni active" data-v="home" onclick="nav('home')"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>首頁</div>
      <div class="ni" data-v="songs" onclick="nav('songs')"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>所有歌曲</div>
      <div class="ni" data-v="albums" onclick="nav('albums')"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/></svg>專輯</div>
      <div class="ni" data-v="artists" onclick="nav('artists')"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>藝人</div>
    </div>
    <div class="ns">
      <span class="nst">我的歌單</span>
      <div id="pl-nav"></div>
      <div class="npl-btn" onclick="openNewPl()"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>新建歌單</div>
    </div>
  </div>
</div>

<!-- Main -->
<div id="main">
  <div id="topbar">
    <input id="sb" type="text" placeholder="搜尋歌曲、專輯、藝人…" oninput="onSrch(this.value)">
    <span id="scanst"></span>
    <button id="scanbtn" onclick="doScan()">🔄 重新掃描</button>
  </div>
  <div id="content">
    <div class="view active" id="v-home"><div class="pg">首頁</div><div id="home-ct"></div></div>
    <div class="view" id="v-songs"><div class="pg">所有歌曲</div><div class="sl-hdr"><span>#</span><span></span><span>標題</span><span>藝人</span><span>專輯</span><span>時長</span><span></span></div><div class="sl" id="songs-sl"></div></div>
    <div class="view" id="v-albums"><div class="pg">專輯</div><div class="ag" id="alb-grid"></div></div>
    <div class="view" id="v-albumd"><div class="back-btn" onclick="nav('albums')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>返回專輯</div><div id="albumd-ct"></div></div>
    <div class="view" id="v-artists"><div class="pg">藝人</div><div class="artg" id="art-grid"></div></div>
    <div class="view" id="v-artd"><div class="back-btn" onclick="nav('artists')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>返回藝人</div><div id="artd-ct"></div></div>
    <div class="view" id="v-pld"><div class="back-btn" onclick="nav('songs')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"/></svg>返回</div><div id="pld-ct"></div></div>
    <div class="view" id="v-search"><div class="pg">搜尋結果</div><div id="srch-ct"></div></div>
    <div class="view" id="v-mine">
      <div class="pg">我的歌單</div>
      <div id="mine-list"></div>
      <div class="npl-btn" onclick="openNewPl()" style="border:1px dashed var(--bdr);border-radius:12px;padding:14px;justify-content:center;color:var(--acc);font-weight:600"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>新建歌單</div>
    </div>
  </div>
</div>
</div>

<!-- Mobile Tab Bar -->
<div id="mtab">
  <div class="ti active" data-t="home" onclick="mnav('home')"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/><polyline points="9 22 9 12 15 12 15 22"/></svg>首頁</div>
  <div class="ti" data-t="songs" onclick="mnav('songs')"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>歌曲</div>
  <div class="ti" data-t="albums" onclick="mnav('albums')"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><circle cx="12" cy="12" r="3"/></svg>專輯</div>
  <div class="ti" data-t="artists" onclick="mnav('artists')"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>藝人</div>
  <div class="ti" data-t="mine" onclick="mnav('mine')"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>我的</div>
</div>

<!-- Desktop Player -->
<div id="player" onclick="openNpM(event)">
  <div id="mp-line"></div>
  <div id="p-left">
    <div class="p-cover"><img id="p-img" style="display:none" alt=""><div class="p-cover-ph" id="p-ph">♪</div></div>
    <div id="p-info">
      <div id="p-title">未播放</div>
      <div id="p-artist">—</div>
      <div id="p-quality"></div>
    </div>
  </div>
  <div id="p-center">
    <div id="p-ctrl">
      <button class="icon-btn" id="shuf-btn" onclick="toggleShuf()"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 3 21 3 21 8"/><line x1="4" y1="20" x2="21" y2="3"/><polyline points="21 16 21 21 16 21"/><line x1="15" y1="15" x2="21" y2="21"/><line x1="4" y1="4" x2="9" y2="9"/></svg></button>
      <button class="icon-btn" onclick="prevSong()"><svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><polygon points="19 20 9 12 19 4 19 20"/><line x1="5" y1="19" x2="5" y2="5" stroke="currentColor" stroke-width="2"/></svg></button>
      <button id="play-btn" onclick="togglePlay()">
        <svg id="d-pi" width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>
        <svg id="d-pai" width="18" height="18" viewBox="0 0 24 24" fill="currentColor" style="display:none"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
      </button>
      <button class="icon-btn" onclick="nextSong()"><svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 4 15 12 5 20 5 4"/><line x1="19" y1="5" x2="19" y2="19" stroke="currentColor" stroke-width="2"/></svg></button>
      <button class="icon-btn" id="rep-btn" onclick="cycleRep()"><svg id="rep-icon" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" opacity=".35"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg></button>
      <button class="icon-btn" onclick="openQueue()"><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="8" y1="6" x2="21" y2="6"/><line x1="8" y1="12" x2="21" y2="12"/><line x1="8" y1="18" x2="21" y2="18"/><line x1="3" y1="6" x2="3.01" y2="6"/><line x1="3" y1="12" x2="3.01" y2="12"/><line x1="3" y1="18" x2="3.01" y2="18"/></svg></button>
    </div>
    <div id="p-prog">
      <span id="tc">0:00</span>
      <div class="prog-wrap" id="d-pw"><div class="prog-fill" id="d-pf" style="width:0%"></div><div class="prog-thumb" id="d-pth" style="left:0%"></div></div>
      <span id="tt">0:00</span>
    </div>
  </div>
  <div id="p-right">
    <button id="eq-btn" onclick="toggleEQ()">EQ</button>
    <div id="vol-wrap">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="color:var(--t3)"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/></svg>
      <div id="vol-bar"><div id="vol-fill" style="width:80%"></div></div>
    </div>
    <div class="mb-ctrls">
      <button id="mb-play" onclick="event.stopPropagation();togglePlay()">
        <svg id="mb-pi" width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>
        <svg id="mb-pai" width="16" height="16" viewBox="0 0 24 24" fill="currentColor" style="display:none"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
      </button>
      <button id="mb-next" onclick="event.stopPropagation();nextSong()"><svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 4 15 12 5 20 5 4"/><line x1="19" y1="5" x2="19" y2="19" stroke="currentColor" stroke-width="2"/></svg></button>
    </div>
  </div>
</div>

<!-- EQ Panel (桌面) -->
<div id="eq-panel">
  <div class="eq-t"><span>等化器</span><span onclick="toggleEQ()" style="cursor:pointer;color:var(--t3);font-size:18px">×</span></div>
  <div class="eq-pres" id="eq-pres-d"></div>
  <div class="eq-bands-wrap" id="eq-bands-d"></div>
</div>

<!-- Queue Sheet -->
<div id="qs-overlay"><div id="qs-panel">
  <div class="qs-hdr">
    <span class="qs-hdr-title">播放佇列</span>
    <div class="qs-tabs"><button class="qs-tab active" id="qst-q" onclick="qsTab('q')">待播</button><button class="qs-tab" id="qst-i" onclick="qsTab('i')">歌曲資訊</button></div>
    <div class="qs-hdr-acts">
      <button class="icon-btn" onclick="openQsEQ()" title="EQ"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg></button>
      <button class="icon-btn" onclick="closeQueue()" style="font-size:20px;color:var(--t2)">×</button>
    </div>
  </div>
  <div id="qs-body">
    <div id="qs-q-body">
      <div class="qs-top"><span class="qs-info-txt" id="qs-info-txt"></span><div class="qs-acts"><button class="qs-act-btn" onclick="saveQueueAsPl()">💾 存為歌單</button><button class="qs-act-btn" onclick="clearQueue()">清空</button></div></div>
      <div id="qs-list"></div>
    </div>
    <div id="qs-info-body" style="display:none"></div>
  </div>
</div></div>

<!-- Now Playing Sheet (手機) -->
<div id="np-sheet">
  <div id="np-hdr">
    <button class="np-close" onclick="closeNp()"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg></button>
    <span class="np-lbl">正在播放</span>
    <button class="np-menu-btn" onclick="openNpMenu()">⋯</button>
  </div>
  <div id="np-cover-wrap">
    <div id="np-ci-box">
      <img id="np-ci" style="display:none" alt="">
      <div id="np-ci-ph">♪</div>
    </div>
  </div>
  <!-- EQ drawer（放在封面下方，用 max-height 動畫，不影響其他元素） -->
  <div id="np-eq-drawer"><div id="np-eq-inner">
    <div class="eq-pres" id="eq-pres-m"></div>
    <div class="eq-bands-wrap" id="eq-bands-m"></div>
  </div></div>
  <div id="np-meta">
    <div class="np-title" id="np-title">未播放</div>
    <div class="np-artist" id="np-artist">—</div>
    <div class="np-qual" id="np-qual"></div>
    <button class="np-heart" onclick="curSongId&&openSongMenu(curSongId,event)">♡</button>
  </div>
  <div id="np-prog">
    <div id="np-pw"><div id="np-pf" style="width:0%"></div><div id="np-pth" style="left:0%"></div></div>
    <div id="np-times"><span id="np-tc">0:00</span><span id="np-tt">0:00</span></div>
  </div>
  <div id="np-ctrl">
    <button class="np-btn" id="np-shuf" onclick="toggleShuf()"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="16 3 21 3 21 8"/><line x1="4" y1="20" x2="21" y2="3"/><polyline points="21 16 21 21 16 21"/><line x1="15" y1="15" x2="21" y2="21"/><line x1="4" y1="4" x2="9" y2="9"/></svg></button>
    <button class="np-btn" onclick="prevSong()"><svg width="28" height="28" viewBox="0 0 24 24" fill="currentColor"><polygon points="19 20 9 12 19 4 19 20"/><line x1="5" y1="19" x2="5" y2="5" stroke="currentColor" stroke-width="2"/></svg></button>
    <button id="np-play-btn" onclick="togglePlay()">
      <svg id="np-pi" width="26" height="26" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>
      <svg id="np-pai" width="26" height="26" viewBox="0 0 24 24" fill="currentColor" style="display:none"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>
    </button>
    <button class="np-btn" onclick="nextSong()"><svg width="28" height="28" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 4 15 12 5 20 5 4"/><line x1="19" y1="5" x2="19" y2="19" stroke="currentColor" stroke-width="2"/></svg></button>
    <button class="np-btn" id="np-rep" onclick="cycleRep()"><svg id="np-rep-icon" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" opacity=".35"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg></button>
  </div>
  <!-- 待播清單拉板 -->
  <div id="np-queue-section">
    <div id="np-queue-handle" onclick="toggleNpQ()"><div id="np-queue-bar"></div></div>
    <div class="npq-hdr"><span class="npq-title">待播清單</span><button class="npq-save" onclick="saveQueueAsPl()">💾 存為歌單</button></div>
    <div id="np-queue-list"></div>
  </div>
</div>

<!-- NP Context Menu -->
<div class="mo" id="mo-npmenu"><div class="mw"><div id="npm-body"></div><div class="ma" style="margin-top:12px"><button class="btns" onclick="closeMo('mo-npmenu')">關閉</button></div></div></div>
<!-- New Playlist -->
<div class="mo" id="mo-newpl"><div class="mw"><div class="mw-t">新建歌單</div><input class="mi" id="newpl-name" type="text" placeholder="歌單名稱" onkeydown="if(event.key==='Enter')createPl()"><div class="ma"><button class="btns" onclick="closeMo('mo-newpl')">取消</button><button class="btnp" onclick="createPl()">建立</button></div></div></div>
<!-- Save Queue -->
<div class="mo" id="mo-saveq"><div class="mw"><div class="mw-t">存為歌單</div><input class="mi" id="saveq-name" type="text" placeholder="歌單名稱" onkeydown="if(event.key==='Enter')doSaveQ()"><div class="ma"><button class="btns" onclick="closeMo('mo-saveq')">取消</button><button class="btnp" onclick="doSaveQ()">儲存</button></div></div></div>
<!-- Song Action Menu (三點選單) -->
<div id="song-menu">
  <div class="sm-header">
    <div class="sm-title" id="sm-title"></div>
    <div class="sm-artist" id="sm-artist"></div>
  </div>
  <div class="sm-item" onclick="smPlay()">▶ 立即播放</div>
  <div class="sm-item" onclick="smAddNext()">⏭ 下一首播放</div>
  <div class="sm-item" onclick="smAddQueue()">➕ 加入待播清單</div>
  <div class="sm-item" onclick="smAddPl()">
    ♡ 加入播放清單
    <div id="sm-quick-pl"></div>
  </div>
  <div class="sm-item" onclick="smGoAlbum()">💿 前往專輯</div>
  <div class="sm-item" onclick="smGoArtist()">🎤 前往藝人</div>
</div>

<!-- Add to Playlist (選擇歌單) -->
<div class="mo" id="mo-addpl">
  <div class="mw">
    <div class="mw-t">加入歌單</div>
    <div id="addpl-body" style="max-height:220px;overflow-y:auto;margin-bottom:14px"></div>
    <div class="ma"><button class="btns" onclick="closeMo('mo-addpl')">取消</button></div>
  </div>
</div>

<div id="toast"></div>
<audio id="aud" preload="auto"></audio>

<script>
// ════ 全域狀態 ════
let songs=[],albList=[],artList=[],pls={};
const QUEUES=new Map(); let curQId=null,qIdx=-1;
let curSongId=null,curPlId=null;
let shuf=false,rep=0;
let smSongId=null,lastPlId=null; // 上次加入的歌單
let srchTmo=null,scanPoll=null,npQExpanded=false;
let aCtx=null,srcNode=null,eqFilters=[];
const aud=document.getElementById('aud');
// 用靜音 AudioBuffer 填充，避免歌曲切換時的短暫中斷
let silentBuf=null;

const EQ_BANDS=[60,170,310,600,1000,3000,6000,12000,14000,16000];
const EQ_LBLS=['60','170','310','600','1K','3K','6K','12K','14K','16K'];
const EQ_PRE={flat:[0,0,0,0,0,0,0,0,0,0],bass:[6,5,4,2,0,0,0,0,0,0],treble:[0,0,0,0,2,3,4,5,6,6],pop:[1,2,4,4,2,0,0,1,2,3],rock:[5,4,3,1,-1,0,1,3,4,5],jazz:[3,2,1,2,3,3,2,1,1,2],classical:[4,3,2,0,-1,0,2,3,4,4],vocal:[-2,-1,0,2,4,4,2,0,-1,-2]};
const EQ_PNAMES={flat:'平衡',bass:'重低音',treble:'高音增強',pop:'流行',rock:'搖滾',jazz:'爵士',classical:'古典',vocal:'人聲'};

function getQueue(){ return curQId?(QUEUES.get(curQId)||[]):[] }
function setQueue(id,ids){ QUEUES.set(id,ids); curQId=id; }

// ════ Init ════
async function init(){
  buildEQ();
  setupAudio();
  setupDrag('d-pw','d-pf','d-pth', p=>{ if(aud.duration) aud.currentTime=p*aud.duration; });
  setupDrag('np-pw','np-pf','np-pth', p=>{ if(aud.duration) aud.currentTime=p*aud.duration; });
  setupVolDrag();
  await Promise.all([loadLib(),loadPls()]);
  startScanPoll();
}

function setupAudio(){
  aud.addEventListener('timeupdate',onTime);
  aud.addEventListener('ended',onEnd);
  aud.addEventListener('play',onPS);
  aud.addEventListener('pause',onPS);
  aud.addEventListener('error',()=>{ toast('播放錯誤，跳下一首'); setTimeout(nextSong,1500); });
  aud.volume=0.8; setVolUI(0.8);
}

function initACtx(){
  if(aCtx) return;
  try{
    aCtx=new(window.AudioContext||window.webkitAudioContext)();
    srcNode=aCtx.createMediaElementSource(aud);
    const gain=aCtx.createGain(); gain.gain.value=1;
    eqFilters=EQ_BANDS.map((f,i)=>{
      const n=aCtx.createBiquadFilter();
      n.type=i===0?'lowshelf':i===EQ_BANDS.length-1?'highshelf':'peaking';
      n.frequency.value=f; n.gain.value=0; n.Q.value=1; return n;
    });
    let n=srcNode;
    eqFilters.forEach(f=>{n.connect(f);n=f});
    n.connect(gain); gain.connect(aCtx.destination);
  }catch(e){console.warn(e)}
}

// ════ Drag ════
function setupDrag(wrapId,fillId,thumbId,cb){
  const wrap=document.getElementById(wrapId); if(!wrap) return;
  let drag=false;
  const calc=e=>{ const r=wrap.getBoundingClientRect(); return Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)); };
  const go=e=>{ drag=true; wrap.classList.add('drag'); cb(calc(e)); e.preventDefault(); };
  const mv=e=>{ if(drag){ cb(calc(e)); e.preventDefault(); } };
  const up=()=>{ drag=false; wrap.classList.remove('drag'); };
  wrap.addEventListener('mousedown',go);
  window.addEventListener('mousemove',mv);
  window.addEventListener('mouseup',up);
  wrap.addEventListener('touchstart',e=>{ drag=true; wrap.classList.add('drag'); cb(calc(e.touches[0])); },{passive:true});
  window.addEventListener('touchmove',e=>{ if(drag){ cb(calc(e.touches[0])); e.preventDefault(); } },{passive:false});
  window.addEventListener('touchend',up);
}
function setupVolDrag(){
  const bar=document.getElementById('vol-bar'); if(!bar) return;
  let drag=false;
  const calc=e=>{ const r=bar.getBoundingClientRect(); return Math.max(0,Math.min(1,(e.clientX-r.left)/r.width)); };
  bar.addEventListener('mousedown',e=>{drag=true; setVol(calc(e));});
  window.addEventListener('mousemove',e=>{if(drag)setVol(calc(e));});
  window.addEventListener('mouseup',()=>{drag=false;});
}

// ════ Library ════
async function loadLib(){
  try{
    const [sr,ar,aar]=await Promise.all([fetch('/api/library'),fetch('/api/albums'),fetch('/api/artists')]);
    songs=await sr.json(); albList=await ar.json(); artList=await aar.json();
    renderSongs(songs,'songs-sl');
    renderAlbs(albList,'alb-grid');
    renderArts(artList,'art-grid');
    renderHome();
    upScanSt(`${songs.length} 首歌曲`);
  }catch(e){console.error(e)}
}
async function loadPls(){
  try{ const r=await fetch('/api/playlists'); const ps=await r.json(); pls={}; ps.forEach(p=>pls[p.id]=p); renderPlNav(); }catch(e){}
}

// ════ Helpers ════
const esc=s=>s?String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'):'';
const fmt=s=>{ if(!s||isNaN(s))return'0:00'; s=Math.floor(s); return`${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`; };
function setCoverEl(imgId,phId,sid){
  const img=document.getElementById(imgId),ph=document.getElementById(phId);
  if(!img||!ph) return;
  if(sid){ img.src=`/api/song/${sid}/cover`; img.style.display='block'; ph.style.display='none'; img.onerror=()=>{img.style.display='none';ph.style.display='flex'}; }
  else{ img.style.display='none'; ph.style.display='flex'; }
}

// ════ Song List Render ════
// 用 data-* 屬性 + 事件委派，完全避免 onclick 屬性含陣列
const _listCtx={};
function renderSongs(list,cid){
  const el=document.getElementById(cid); if(!el) return;
  if(!list.length){el.innerHTML='<div class="empty"><div class="empty-i">🎵</div><div>沒有歌曲</div></div>';return}
  _listCtx[cid]={ids:list.map(s=>s.id)};
  el.innerHTML=list.map((s,i)=>`
  <div class="sr${s.id===curSongId?' playing':''}" data-sid="${s.id}" data-cid="${cid}" data-idx="${i}">
    <div class="s-num">${s.id===curSongId?`<div class="pi${!aud.paused?' on':''}"><span></span><span></span><span></span></div>`:i+1}</div>
    <div class="s-thumb">${s.has_cover?`<img src="/api/song/${s.id}/cover" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><div class="s-thumb-ph" style="display:none">♪</div>`:`<div class="s-thumb-ph">♪</div>`}</div>
    <div class="s-title">${esc(s.title)}</div>
    <div class="s-artist">${esc(s.artist)}</div>
    <div class="s-album">${esc(s.album)}</div>
    <div class="s-dur">${fmt(s.duration)}</div>
    <div class="s-act"><button class="dot-btn" data-sid="${s.id}" title="選項">⋯</button></div>
  </div>`).join('');
  el.onclick=e=>{
    const dot=e.target.closest('.dot-btn');
    if(dot){openSongMenu(dot.dataset.sid,e);return;}
    const row=e.target.closest('.sr');
    if(!row) return;
    const sid=row.dataset.sid,idx=+row.dataset.idx,cid2=row.dataset.cid;
    const qid=cid2+'_q';
    setQueue(qid,_listCtx[cid2]?.ids||[sid]);
    qIdx=idx; loadAndPlay(sid);
  };
  // 長按
  let ltTmo=null;
  el.addEventListener('touchstart',e=>{
    const row=e.target.closest('.sr'); if(!row) return;
    ltTmo=setTimeout(()=>openSongMenu(row.dataset.sid,e.touches[0]),500);
  },{passive:true});
  el.addEventListener('touchend',()=>clearTimeout(ltTmo));
  el.addEventListener('touchmove',()=>clearTimeout(ltTmo));
}

// ════ Album/Artist Render ════
function renderAlbs(list,cid){
  const el=document.getElementById(cid); if(!el) return;
  el.innerHTML=list.map(a=>`
  <div class="ac" onclick="openAlb('${a.id}')">
    <div class="ac-cover">
      ${a.cover_song_id?`<img src="/api/song/${a.cover_song_id}/cover" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><div class="ac-cover-ph" style="display:none">💿</div>`:`<div class="ac-cover-ph">💿</div>`}
      <div class="po"><svg width="34" height="34" viewBox="0 0 24 24" fill="white"><circle cx="12" cy="12" r="12" fill="rgba(0,0,0,.3)"/><polygon points="10 8 16 12 10 16"/></svg></div>
    </div>
    <div class="ac-texts">
      <div class="ac-name">${esc(a.name)}</div>
      <div class="ac-sub">${esc(a.artist)}</div>
      ${a.year?`<div class="ac-year">${esc(a.year)}</div>`:''}
    </div>
  </div>`).join('');
}
function renderArts(list,cid){
  const el=document.getElementById(cid); if(!el) return;
  el.innerHTML=list.map(a=>`
  <div class="artc" data-artname="${esc(a.name)}" onclick="openArt(this.dataset.artname)">
    <div class="art-av">
      ${a.cover_song_id?`<img src="/api/song/${a.cover_song_id}/cover" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><div class="art-av-ph" style="display:none">🎤</div>`:`<div class="art-av-ph">🎤</div>`}
    </div>
    <div class="art-name">${esc(a.name)}</div>
    <div class="art-sub">${a.album_count} 張專輯</div>
  </div>`).join('');
}

// ════ Home ════
function renderHome(){
  const el=document.getElementById('home-ct'); if(!el||!songs.length) return;
  const rec=songs.slice(0,12);
  const rnd=[...songs].sort(()=>Math.random()-.5).slice(0,12);
  function miniCards(list){
    return list.map((s,i)=>`<div class="ac" data-home-list="${list.map(x=>x.id).join(',')}" data-home-idx="${i}" onclick="homePlay(this)">
      <div class="ac-cover">${s.has_cover?`<img src="/api/song/${s.id}/cover" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><div class="ac-cover-ph" style="display:none">🎵</div>`:`<div class="ac-cover-ph">🎵</div>`}</div>
      <div class="ac-texts"><div class="ac-name">${esc(s.title)}</div><div class="ac-sub">${esc(s.artist)}</div></div>
    </div>`).join('');
  }
  el.innerHTML=`
  <section style="margin-bottom:26px">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px"><div style="font-size:16px;font-weight:700">繼續聆聽</div><span style="font-size:12px;color:var(--acc);cursor:pointer" onclick="nav('songs')">查看全部</span></div>
    <div class="ag" style="grid-template-columns:repeat(auto-fill,minmax(130px,1fr))">${miniCards(rec)}</div>
  </section>
  <section style="margin-bottom:26px">
    <div style="font-size:16px;font-weight:700;margin-bottom:12px">為你推薦</div>
    <div class="ag" style="grid-template-columns:repeat(auto-fill,minmax(130px,1fr))">${miniCards(rnd)}</div>
  </section>
  <section style="margin-bottom:26px">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px"><div style="font-size:16px;font-weight:700">專輯</div><span style="font-size:12px;color:var(--acc);cursor:pointer" onclick="nav('albums')">查看全部</span></div>
    <div class="ag" style="grid-template-columns:repeat(auto-fill,minmax(130px,1fr))">${albList.slice(0,12).map(a=>`<div class="ac" onclick="openAlb('${a.id}')"><div class="ac-cover">${a.cover_song_id?`<img src="/api/song/${a.cover_song_id}/cover" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><div class="ac-cover-ph" style="display:none">💿</div>`:`<div class="ac-cover-ph">💿</div>`}</div><div class="ac-texts"><div class="ac-name">${esc(a.name)}</div><div class="ac-sub">${esc(a.artist)}</div></div></div>`).join('')}</div>
  </section>`;
}
function homePlay(el){
  const idsStr=el.dataset.homeList; const idx=+el.dataset.homeIdx;
  if(!idsStr) return;
  const ids=idsStr.split(',');
  const qid='home_'+Date.now();
  setQueue(qid,ids); qIdx=idx; loadAndPlay(ids[idx]);
}

// ════ Navigation ════
function nav(v){
  document.querySelectorAll('.view').forEach(x=>x.classList.remove('active'));
  const el=document.getElementById('v-'+v); if(el) el.classList.add('active');
  document.querySelectorAll('.ni').forEach(x=>x.classList.toggle('active',x.dataset.v===v));
  document.querySelectorAll('.ti').forEach(x=>x.classList.toggle('active',x.dataset.t===v));
  if(v!=='pld') curPlId=null;
  document.querySelectorAll('.pli').forEach(x=>x.classList.remove('active'));
  if(v==='home') renderHome();
  document.getElementById('content').scrollTop=0;
}
function mnav(v){ nav(v); if(v==='mine') renderMineList(); }

// ════ Detail Pages ════
async function openAlb(id){
  nav('albumd');
  const el=document.getElementById('albumd-ct'); el.innerHTML='<div class="loading">載入中…</div>';
  try{
    const a=await(await fetch(`/api/albums/${id}`)).json();
    el.innerHTML=`
    <div class="adh">
      <div class="adc-wrap">${a.cover_song_id?`<img src="/api/song/${a.cover_song_id}/cover" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><div class="adc-ph" style="display:none">💿</div>`:`<div class="adc-ph">💿</div>`}</div>
      <div style="flex:1;min-width:0">
        <div class="ad-label">專輯</div>
        <div class="ad-title">${esc(a.name)}</div>
        <div class="ac-sub" style="font-size:14px;margin-bottom:4px">${esc(a.artist)}</div>
        <div class="ad-meta">${a.year?esc(a.year)+' · ':''}${a.songs.length} 首歌曲</div>
        <button class="play-all" id="alb-play-all"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>播放全部</button>
      </div>
    </div>
    <div class="sl-hdr"><span>#</span><span></span><span>標題</span><span>藝人</span><span>專輯</span><span>時長</span><span></span></div>
    <div class="sl" id="albumd-sl"></div>`;
    renderSongs(a.songs,'albumd-sl');
    const btn=document.getElementById('alb-play-all');
    if(btn){ const ids=a.songs.map(s=>s.id); btn.onclick=()=>{ const qid='alb_'+id; setQueue(qid,ids); qIdx=0; loadAndPlay(ids[0]); }; }
  }catch(e){el.innerHTML='<div class="empty">載入失敗</div>'}
}

async function openArt(name){
  nav('artd');
  const el=document.getElementById('artd-ct'); el.innerHTML='<div class="loading">載入中…</div>';
  try{
    const a=await(await fetch(`/api/artists/${encodeURIComponent(name)}`)).json();
    el.innerHTML=`
    <div class="art-hero">
      ${a.cover_song_id?`<img src="/api/song/${a.cover_song_id}/cover" alt="" onerror="this.remove()">`:''}
      <div class="art-hero-overlay"></div>
      <div class="art-hero-info"><div class="art-hero-name">${esc(a.name)}</div><div class="art-hero-sub">${a.albums.length} 張專輯</div></div>
    </div>
    <div class="ag" id="artd-alb"></div>`;
    document.getElementById('artd-alb').innerHTML=a.albums.map(al=>`
    <div class="ac" onclick="openAlb('${al.id}')">
      <div class="ac-cover">${al.cover_song_id?`<img src="/api/song/${al.cover_song_id}/cover" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><div class="ac-cover-ph" style="display:none">💿</div>`:`<div class="ac-cover-ph">💿</div>`}
        <div class="po"><svg width="34" height="34" viewBox="0 0 24 24" fill="white"><circle cx="12" cy="12" r="12" fill="rgba(0,0,0,.3)"/><polygon points="10 8 16 12 10 16"/></svg></div>
      </div>
      <div class="ac-texts"><div class="ac-name">${esc(al.name)}</div>${al.year?`<div class="ac-year">${esc(al.year)}</div>`:''}<div class="ac-sub" style="font-size:11px">${al.song_count} 首</div></div>
    </div>`).join('');
  }catch(e){el.innerHTML='<div class="empty">載入失敗</div>'}
}

async function openPl(pid){
  curPlId=pid; nav('pld');
  document.querySelectorAll('.pli').forEach(x=>x.classList.toggle('active',x.getAttribute('onclick')?.includes(pid)));
  const el=document.getElementById('pld-ct'); el.innerHTML='<div class="loading">載入中…</div>';
  try{
    const pl=await(await fetch(`/api/playlists/${pid}`)).json();
    const sgs=pl.song_details||[];
    el.innerHTML=`
    <div class="pl-hdr">
      <div class="pl-icon">🎵</div>
      <div><div class="pl-title">${esc(pl.name)}</div><div class="pl-meta">${sgs.length}/400 首</div>
      ${sgs.length?`<button class="play-all" style="margin-top:10px" id="pl-play-all"><svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"/></svg>播放全部</button>`:''}
      </div>
    </div>
    <div class="sl-hdr"><span>#</span><span></span><span>標題</span><span>藝人</span><span>專輯</span><span>時長</span><span></span></div>
    <div class="sl" id="pld-sl"></div>`;
    renderSongs(sgs,'pld-sl');
    const btn=document.getElementById('pl-play-all');
    if(btn){ const ids=sgs.map(s=>s.id); btn.onclick=()=>{ const qid='pl_'+pid; setQueue(qid,ids); qIdx=0; loadAndPlay(ids[0]); }; }
    // 覆寫 dot-btn 為移除按鈕
    sgs.forEach(s=>{
      const sl=document.getElementById('pld-sl');
      const row=sl?.querySelector(`[data-sid="${s.id}"]`);
      if(row){ const db=row.querySelector('.dot-btn'); if(db){ db.textContent='−'; db.dataset.plid=pid; } }
    });
    const slEl=document.getElementById('pld-sl');
    if(slEl){
      const origClick=slEl.onclick;
      slEl.onclick=e=>{
        const db=e.target.closest('.dot-btn');
        if(db&&db.dataset.plid){ e.stopPropagation(); rmFromPl(db.dataset.plid,db.dataset.sid); return; }
        origClick&&origClick(e);
      };
    }
  }catch(e){el.innerHTML='<div class="empty">載入失敗</div>'}
}

function renderPlNav(){
  document.getElementById('pl-nav').innerHTML=Object.values(pls).map(p=>`
  <div class="pli${curPlId===p.id?' active':''}" onclick="openPl('${p.id}')">
    <span class="pli-name">🎵 ${esc(p.name)}</span>
    <button class="xbtn" onclick="event.stopPropagation();delPl('${p.id}')">×</button>
  </div>`).join('');
  renderMineList();
}
function renderMineList(){
  const el=document.getElementById('mine-list'); if(!el) return;
  const ps=Object.values(pls);
  el.innerHTML=ps.length?ps.map(p=>`<div class="mine-item" onclick="openPl('${p.id}')">
    <span>🎵 ${esc(p.name)} <span style="color:var(--t3);font-size:11px">(${p.songs.length}/400)</span></span>
    <button class="xbtn" onclick="event.stopPropagation();delPl('${p.id}')">×</button>
  </div>`).join(''):'<div class="empty"><div class="empty-i">🎵</div><div>還沒有歌單</div></div>';
}

// ════ Playback ════
function loadAndPlay(id){
  initACtx();
  if(aCtx&&aCtx.state==='suspended') aCtx.resume();
  if(curSongId!==id){
    curSongId=id;
    aud.src=`/api/song/${id}/stream`;
    upPlayerUI(id); upRows();
    // 更新 NP sheet 背景色（根據封面主色，用固定漸層替代）
  }
  aud.play().catch(e=>console.warn(e));
  renderQBody(); renderNpQ();
}

function upPlayerUI(id){
  const s=songs.find(x=>x.id===id); if(!s) return;
  // 歌名允許捲動（text-overflow: ellipsis 但不截斷關鍵字）
  ['p-title','np-title'].forEach(i=>{ const el=document.getElementById(i); if(el) el.textContent=s.title; });
  ['p-artist','np-artist'].forEach(i=>{ const el=document.getElementById(i); if(el) el.textContent=s.artist; });
  ['p-quality','np-qual'].forEach(i=>{ const el=document.getElementById(i); if(el) el.textContent=s.quality||''; });
  setCoverEl('p-img','p-ph', s.has_cover?id:null);
  setCoverEl('np-ci','np-ci-ph', s.has_cover?id:null);
  document.title=`${s.title} — ${s.artist}`;
}

function upRows(){
  document.querySelectorAll('.sr').forEach(row=>{
    const sid=row.dataset.sid; const cur=sid===curSongId;
    row.classList.toggle('playing',cur);
    const num=row.querySelector('.s-num');
    if(num){
      if(cur) num.innerHTML=`<div class="pi${!aud.paused?' on':''}"><span></span><span></span><span></span></div>`;
      else num.textContent=Array.from(row.parentElement.children).indexOf(row)+1;
    }
    const t=row.querySelector('.s-title'); if(t) t.style.color=cur?'var(--acc)':'';
  });
}

function togglePlay(){
  if(!curSongId&&songs.length){ const qid='all'; setQueue(qid,songs.map(s=>s.id)); qIdx=0; loadAndPlay(songs[0].id); return; }
  if(aud.paused){ if(aCtx&&aCtx.state==='suspended')aCtx.resume(); aud.play(); } else aud.pause();
}
function prevSong(){
  const q=getQueue(); if(!q.length) return;
  if(aud.currentTime>3){ aud.currentTime=0; return; }
  qIdx=qIdx>0?qIdx-1:q.length-1; loadAndPlay(q[qIdx]);
}
function nextSong(){
  const q=getQueue(); if(!q.length) return;
  if(shuf){ let i; do{i=Math.floor(Math.random()*q.length)}while(i===qIdx&&q.length>1); qIdx=i; }
  else qIdx=(qIdx+1)%q.length;
  loadAndPlay(q[qIdx]);
}

// 歌曲結束：預先載入下一首以減少中斷
function onEnd(){
  const q=getQueue();
  if(rep===2){ aud.currentTime=0; aud.play(); }
  else if(rep===1||qIdx<q.length-1) nextSong();
}

function onPS(){
  const p=!aud.paused;
  [['d-pi','d-pai'],['mb-pi','mb-pai'],['np-pi','np-pai']].forEach(([pi,pai])=>{
    const a=document.getElementById(pi),b=document.getElementById(pai);
    if(a)a.style.display=p?'none':'block'; if(b)b.style.display=p?'block':'none';
  });
  document.querySelectorAll('.pi').forEach(x=>x.classList.toggle('on',p));
}
function onTime(){
  if(!aud.duration) return;
  const pct=(aud.currentTime/aud.duration)*100;
  const cs=fmt(aud.currentTime),ct=fmt(aud.duration);
  [['d-pf','d-pth','tc','tt'],['np-pf','np-pth','np-tc','np-tt']].forEach(([pf,pth,tc,tt])=>{
    const f=document.getElementById(pf),th=document.getElementById(pth),c=document.getElementById(tc),t=document.getElementById(tt);
    if(f)f.style.width=pct+'%'; if(th)th.style.left=pct+'%';
    if(c)c.textContent=cs; if(t)t.textContent=ct;
  });
  const ml=document.getElementById('mp-line'); if(ml) ml.style.width=pct+'%';
}

// ════ Shuffle / Repeat ════
function toggleShuf(){
  shuf=!shuf;
  ['shuf-btn','np-shuf'].forEach(id=>{const el=document.getElementById(id);if(el)el.classList.toggle('active',shuf)});
  toast(shuf?'隨機播放 開':'隨機播放 關');
}
// 循環三態圖示：不循環(淡)、循環全部(亮)、單曲循環(紫色+1)
const REP_CFG=[
  {opacity:.35,stroke:'currentColor',label:'不循環',extra:''},
  {opacity:1,stroke:'currentColor',label:'循環全部',extra:''},
  {opacity:1,stroke:'var(--acc)',label:'單曲循環',extra:'<text x="12" y="13.5" font-size="7" fill="var(--acc)" text-anchor="middle" dominant-baseline="middle" stroke="none" font-weight="bold">1</text>'},
];
function _repIconSVG(cfg,sz){
  return `<svg width="${sz}" height="${sz}" viewBox="0 0 24 24" fill="none" stroke="${cfg.stroke}" stroke-width="2" opacity="${cfg.opacity}"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/>${cfg.extra}</svg>`;
}
function cycleRep(){
  rep=(rep+1)%3; const cfg=REP_CFG[rep];
  const ri=document.getElementById('rep-icon'),nri=document.getElementById('np-rep-icon');
  if(ri)ri.outerHTML=`<svg id="rep-icon" ${_repIconSVG(cfg,18).slice(4)}`;
  if(nri)nri.outerHTML=`<svg id="np-rep-icon" ${_repIconSVG(cfg,22).slice(4)}`;
  ['rep-btn','np-rep'].forEach(id=>{const el=document.getElementById(id);if(el)el.classList.toggle('active',rep>0)});
  toast(cfg.label);
}
function setVol(v){ aud.volume=v; setVolUI(v); }
function setVolUI(v){ const f=document.getElementById('vol-fill');if(f)f.style.width=(v*100)+'%'; }

// ════ Queue ════
function openQueue(){ document.getElementById('qs-overlay').classList.add('open'); renderQBody(); }
function closeQueue(){ document.getElementById('qs-overlay').classList.remove('open'); }
function qsTab(t){
  document.getElementById('qs-q-body').style.display=t==='q'?'block':'none';
  document.getElementById('qs-info-body').style.display=t==='i'?'block':'none';
  document.getElementById('qst-q').classList.toggle('active',t==='q');
  document.getElementById('qst-i').classList.toggle('active',t==='i');
  if(t==='i') renderQsInfo();
}
function renderQBody(){
  const el=document.getElementById('qs-list'); if(!el) return;
  const q=getQueue();
  const info=document.getElementById('qs-info-txt');if(info)info.textContent=`${q.length} 首歌曲`;
  const qs=q.map(id=>songs.find(s=>s.id===id)).filter(Boolean);
  el.innerHTML=qs.map((s,i)=>`
  <div class="qr${s.id===curSongId?' playing':''}" data-qidx="${i}">
    <div class="qr-thumb">${s.has_cover?`<img src="/api/song/${s.id}/cover" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><div class="qr-thumb-ph" style="display:none">♪</div>`:`<div class="qr-thumb-ph">♪</div>`}</div>
    <div class="qr-info"><div class="qr-title">${esc(s.title)}</div><div class="qr-artist">${esc(s.artist)}</div></div>
    <div class="qr-dur">${fmt(s.duration)}</div>
    <span class="qr-drag" title="拖動排序">☰</span>
    <button class="icon-btn" style="color:var(--t3);font-size:16px" data-qidx="${i}" onclick="rmFromQ(+this.dataset.qidx)">×</button>
  </div>`).join('');
  el.onclick=e=>{
    const row=e.target.closest('.qr');
    if(!row) return;
    if(e.target.tagName==='BUTTON') return;
    const idx=+row.dataset.qidx; const q2=getQueue();
    qIdx=idx; loadAndPlay(q2[idx]); renderQBody(); renderNpQ();
  };
  // 拖動排序（觸控 + 滑鼠）
  setupQueueDrag(el);
}
// 待播清單拖動排序
function setupQueueDrag(container){
  let dragEl=null,dragIdx=-1,placeholder=null;
  function getDragRow(e){ return e.target.closest('.qr-drag')?e.target.closest('.qr'):null; }
  function startDrag(row,clientY){
    dragEl=row; dragIdx=+row.dataset.qidx;
    row.style.opacity='.4';
    placeholder=document.createElement('div');
    placeholder.style.cssText=`height:${row.offsetHeight}px;background:var(--glow);border-radius:8px;margin:2px 18px`;
    row.parentElement.insertBefore(placeholder,row.nextSibling);
  }
  function moveDrag(clientY){
    if(!dragEl) return;
    const rows=[...container.querySelectorAll('.qr:not([style*="opacity"])')];
    for(const r of rows){
      const rect=r.getBoundingClientRect();
      if(clientY>rect.top&&clientY<rect.bottom){
        r.parentElement.insertBefore(placeholder,clientY<rect.top+rect.height/2?r:r.nextSibling);
        break;
      }
    }
  }
  function endDrag(){
    if(!dragEl) return;
    const newIdx=[...container.children].filter(c=>c!==dragEl).indexOf(placeholder);
    dragEl.style.opacity='';
    placeholder.remove();
    if(newIdx>=0&&newIdx!==dragIdx){
      const q=getQueue(); const [removed]=q.splice(dragIdx,1);
      const insertAt=newIdx>dragIdx?newIdx-1:newIdx;
      q.splice(insertAt,0,removed);
      if(curQId) QUEUES.set(curQId,q);
      if(dragIdx===qIdx) qIdx=insertAt;
      renderQBody(); renderNpQ();
    }
    dragEl=null; dragIdx=-1; placeholder=null;
  }
  container.addEventListener('mousedown',e=>{ const r=getDragRow(e); if(r)startDrag(r,e.clientY); });
  window.addEventListener('mousemove',e=>moveDrag(e.clientY));
  window.addEventListener('mouseup',endDrag);
  container.addEventListener('touchstart',e=>{ const r=getDragRow(e); if(r)startDrag(r,e.touches[0].clientY); },{passive:true});
  window.addEventListener('touchmove',e=>{ if(dragEl)moveDrag(e.touches[0].clientY); },{passive:true});
  window.addEventListener('touchend',endDrag);
}

function renderNpQ(){
  const el=document.getElementById('np-queue-list'); if(!el) return;
  const q=getQueue();
  const qs=q.map(id=>songs.find(s=>s.id===id)).filter(Boolean);
  el.innerHTML=qs.map((s,i)=>`
  <div class="qr${s.id===curSongId?' playing':''}" data-qidx="${i}">
    <div class="qr-thumb">${s.has_cover?`<img src="/api/song/${s.id}/cover" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><div class="qr-thumb-ph" style="display:none">♪</div>`:`<div class="qr-thumb-ph">♪</div>`}</div>
    <div class="qr-info"><div class="qr-title">${esc(s.title)}</div><div class="qr-artist">${esc(s.artist)}</div></div>
    <div class="qr-dur">${fmt(s.duration)}</div>
  </div>`).join('');
  el.onclick=e=>{
    const row=e.target.closest('.qr'); if(!row) return;
    const idx=+row.dataset.qidx; const q2=getQueue();
    qIdx=idx; loadAndPlay(q2[idx]); renderQBody(); renderNpQ();
  };
}
function rmFromQ(i){
  const q=getQueue(); q.splice(i,1);
  if(curQId)QUEUES.set(curQId,q);
  if(qIdx>i)qIdx--;
  renderQBody(); renderNpQ();
}
function clearQueue(){ if(curQId)QUEUES.set(curQId,[]); renderQBody(); renderNpQ(); toast('已清空待播清單'); }

function renderQsInfo(){
  const el=document.getElementById('qs-info-body'); if(!el) return;
  const s=songs.find(x=>x.id===curSongId);
  if(!s){el.innerHTML='<div class="empty">未選擇歌曲</div>';return}
  el.innerHTML=`<div style="padding:18px">
  <div class="qi-top">
    <div class="qi-cover">${s.has_cover?`<img src="/api/song/${s.id}/cover" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><div class="qi-cover-ph" style="display:none">♪</div>`:`<div class="qi-cover-ph">♪</div>`}</div>
    <div><div class="qi-title">${esc(s.title)}</div><div class="qi-artist">${esc(s.artist)}</div><div class="qi-qual">${esc(s.quality||'')}</div></div>
  </div>
  ${[['專輯',s.album],['年份',s.year],['時長',fmt(s.duration)],['類型',s.genre]].filter(r=>r[1]).map(r=>`<div class="qi-row"><span class="qi-key">${r[0]}</span><span class="qi-val">${esc(String(r[1]))}</span></div>`).join('')}
  <div class="qi-acts">
    <button class="qi-act" data-sid="${s.id}" onclick="closeQueue();openAlbBySong(this.dataset.sid)">💿 前往專輯</button>
    <button class="qi-act" data-art="${esc(s.artist)}" onclick="closeQueue();openArt(this.dataset.art)">🎤 前往藝人</button>
    <button class="qi-act" data-sid="${s.id}" onclick="closeQueue();openSongMenu(this.dataset.sid,event)">＋ 加入歌單</button>
  </div>
  </div>`;
}
function openAlbBySong(sid){
  const s=songs.find(x=>x.id===sid); if(!s) return;
  const a=albList.find(x=>x.name===s.album&&x.artist===s.album_artist)||albList.find(x=>x.name===s.album);
  if(a) openAlb(a.id);
}
function openQsEQ(){ closeQueue(); toggleEQ(); }
function saveQueueAsPl(){
  const q=getQueue();
  if(!q.length){toast('待播清單是空的');return}
  document.getElementById('saveq-name').value=''; openMo('mo-saveq');
  setTimeout(()=>document.getElementById('saveq-name').focus(),120);
}
async function doSaveQ(){
  const name=document.getElementById('saveq-name').value.trim(); if(!name) return;
  const q=getQueue();
  const r=await fetch('/api/playlists',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  const pl=await r.json();
  for(const sid of q.slice(0,400))
    await fetch(`/api/playlists/${pl.id}/songs`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({song_id:sid})});
  await loadPls(); closeMo('mo-saveq'); toast(`已儲存歌單「${name}」(${Math.min(q.length,400)} 首)`);
}

// ════ NP Sheet ════
function openNpM(e){ if(window.innerWidth>768||!curSongId) return; openNp(); }
function openNp(){ document.getElementById('np-sheet').classList.add('open'); }
function closeNp(){ document.getElementById('np-sheet').classList.remove('open'); }
function toggleNpQ(){
  npQExpanded=!npQExpanded;
  const el=document.getElementById('np-queue-list');
  if(el)el.classList.toggle('expanded',npQExpanded);
}
function openNpMenu(){
  const s=songs.find(x=>x.id===curSongId); if(!s) return;
  document.getElementById('npm-body').innerHTML=`
  <div style="font-weight:700;font-size:15px;margin-bottom:3px">${esc(s.title)}</div>
  <div style="color:var(--t2);font-size:13px;margin-bottom:14px">${esc(s.artist)}</div>
  ${[
    ['🎛 等化器',`closeMo('mo-npmenu');const d=document.getElementById('np-eq-drawer');d.classList.toggle('open')`],
    ['📋 待播清單',`closeMo('mo-npmenu');closeNp();openQueue()`],
    ['💿 前往專輯',`closeMo('mo-npmenu');closeNp();openAlbBySong('${s.id}')`],
    ['🎤 前往藝人',`closeMo('mo-npmenu');closeNp();openArt(${JSON.stringify(s.artist)})`],
    ['♡ 加入歌單',`closeMo('mo-npmenu');openSongMenu('${s.id}',event)`],
    ['💾 存為歌單',`closeMo('mo-npmenu');saveQueueAsPl()`],
  ].map(([l,a])=>`<div class="mo-item" onclick="${a}">${l}</div>`).join('')}`;
  openMo('mo-npmenu');
}

// ════ Song Action Menu（三點選單）════
function openSongMenu(sid,e){
  smSongId=sid;
  const s=songs.find(x=>x.id===sid);
  document.getElementById('sm-title').textContent=s?s.title:'';
  document.getElementById('sm-artist').textContent=s?s.artist:'';
  // 顯示上次加入的歌單快捷
  const qpl=document.getElementById('sm-quick-pl');
  if(lastPlId&&pls[lastPlId]){ qpl.textContent=`（${pls[lastPlId].name}）`; qpl.style.display='inline'; }
  else qpl.style.display='none';
  const menu=document.getElementById('song-menu');
  menu.classList.add('open');
  if(window.innerWidth>768&&e){
    menu.style.position='fixed';
    menu.style.left=Math.min(e.clientX,window.innerWidth-230)+'px';
    menu.style.top=Math.min(e.clientY,window.innerHeight-280)+'px';
    menu.style.bottom='';
  }else{
    menu.style.left='0'; menu.style.top=''; menu.style.bottom='0';
  }
}
function closeSongMenu(){ document.getElementById('song-menu').classList.remove('open'); smSongId=null; }
function smPlay(){
  if(!smSongId) return; closeSongMenu();
  const idx=songs.findIndex(s=>s.id===smSongId);
  const qid='all_'+Date.now(); setQueue(qid,songs.map(s=>s.id)); qIdx=idx<0?0:idx; loadAndPlay(smSongId);
}
function smAddNext(){
  if(!smSongId) return; closeSongMenu();
  const q=getQueue(); q.splice(qIdx+1,0,smSongId);
  if(curQId)QUEUES.set(curQId,q); else{const qid='q_'+Date.now();setQueue(qid,[smSongId]);}
  renderQBody(); renderNpQ(); toast('已加到下一首播放');
}
function smAddQueue(){
  if(!smSongId) return; closeSongMenu();
  const q=getQueue(); if(!q.includes(smSongId)){q.push(smSongId);}
  if(curQId)QUEUES.set(curQId,q); else{const qid='q_'+Date.now();setQueue(qid,[smSongId]);}
  renderQBody(); renderNpQ(); toast('已加入待播清單');
}
function smAddPl(){
  // 如果有上次加入的歌單，直接加入並提示「加入其他播放清單」
  if(lastPlId&&pls[lastPlId]){
    addToPlById(lastPlId,smSongId,'auto');
    closeSongMenu();
  }else{
    closeSongMenu(); openAddPlModal(smSongId);
  }
}
function smGoAlbum(){ if(!smSongId)return; closeSongMenu(); openAlbBySong(smSongId); }
function smGoArtist(){ if(!smSongId)return; closeSongMenu(); const s=songs.find(x=>x.id===smSongId); if(s)openArt(s.artist); }

// ════ Playlists ════
function openNewPl(){ document.getElementById('newpl-name').value=''; openMo('mo-newpl'); setTimeout(()=>document.getElementById('newpl-name').focus(),100); }
async function createPl(){
  const name=document.getElementById('newpl-name').value.trim(); if(!name) return;
  const r=await fetch('/api/playlists',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  const pl=await r.json(); pls[pl.id]=pl; renderPlNav(); closeMo('mo-newpl'); toast(`歌單「${name}」已建立`);
}
async function delPl(pid){
  if(!confirm('確定要刪除這個歌單嗎？')) return;
  await fetch(`/api/playlists/${pid}`,{method:'DELETE'}); delete pls[pid]; renderPlNav();
  if(curPlId===pid) nav('songs'); toast('歌單已刪除');
}
function openAddPlModal(sid){
  smSongId=sid;
  const ps=Object.values(pls);
  document.getElementById('addpl-body').innerHTML=ps.length?ps.map(p=>`<div class="mo-item" data-pid="${p.id}" onclick="addToPlById(this.dataset.pid,'${sid}','modal')">🎵 ${esc(p.name)} <span style="color:var(--t3);font-size:11px">(${p.songs.length}/400)</span></div>`).join(''):'<div style="color:var(--t3);font-size:13px;text-align:center;padding:12px">請先建立歌單</div>';
  openMo('mo-addpl');
}
async function addToPlById(pid,sid,mode){
  const r=await fetch(`/api/playlists/${pid}/songs`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({song_id:sid})});
  const pl=await r.json();
  if(pl.error){ toast('歌單已滿（最多400首）'); return; }
  pls[pid]=pl; lastPlId=pid;
  if(mode==='modal'){
    closeMo('mo-addpl');
    // 顯示 toast 含「加入其他播放清單」選項
    showToastWithAction(`已加入「${pl.name}」`,'加入其他',()=>openAddPlModal(sid));
  }else if(mode==='auto'){
    showToastWithAction(`已加入「${pl.name}」`,'換別的',()=>openAddPlModal(sid));
  }
}
async function rmFromPl(pid,sid){
  await fetch(`/api/playlists/${pid}/songs/${sid}`,{method:'DELETE'});
  toast('已移除'); openPl(pid);
}

// ════ EQ ════
function buildEQ(){
  const preH=Object.entries(EQ_PNAMES).map(([k,l])=>`<button class="eq-pb${k==='flat'?' active':''}" data-p="${k}" onclick="applyPreset('${k}')">${l}</button>`).join('');
  const bandH=EQ_BANDS.map((_,i)=>`<div class="eq-band"><div class="eq-val" id="ev-SFXSFX-${i}">0</div><input type="range" min="-12" max="12" value="0" step=".5" id="es-SFXSFX-${i}" oninput="setEQB(${i},this.value)"><div class="eq-lbl">${EQ_LBLS[i]}</div></div>`).join('');
  ['d','m'].forEach(sfx=>{
    const p=document.getElementById(`eq-pres-${sfx}`); if(p)p.innerHTML=preH;
    const b=document.getElementById(`eq-bands-${sfx}`); if(b)b.innerHTML=bandH.replace(/SFXSFX/g,sfx);
  });
}
function setEQB(i,v){
  const g=parseFloat(v); const vs=(g>=0?'+':'')+g.toFixed(1);
  if(eqFilters[i])eqFilters[i].gain.value=g;
  ['d','m'].forEach(sfx=>{
    const ve=document.getElementById(`ev-${sfx}-${i}`); if(ve)ve.textContent=vs;
    const se=document.getElementById(`es-${sfx}-${i}`); if(se)se.value=g;
  });
  document.querySelectorAll('.eq-pb').forEach(b=>b.classList.remove('active'));
}
function applyPreset(name){
  (EQ_PRE[name]||EQ_PRE.flat).forEach((g,i)=>{
    const vs=(g>=0?'+':'')+g.toFixed(1);
    if(eqFilters[i])eqFilters[i].gain.value=g;
    ['d','m'].forEach(sfx=>{
      const ve=document.getElementById(`ev-${sfx}-${i}`); if(ve)ve.textContent=vs;
      const se=document.getElementById(`es-${sfx}-${i}`); if(se)se.value=g;
    });
  });
  document.querySelectorAll(`.eq-pb[data-p="${name}"]`).forEach(b=>b.classList.add('active'));
  document.querySelectorAll(`.eq-pb:not([data-p="${name}"])`).forEach(b=>b.classList.remove('active'));
}
function toggleEQ(){
  const p=document.getElementById('eq-panel'); p.classList.toggle('open');
  document.getElementById('eq-btn').classList.toggle('active',p.classList.contains('open'));
}

// ════ Search ════
function onSrch(v){ clearTimeout(srchTmo); if(!v.trim()){nav('songs');return} srchTmo=setTimeout(()=>doSrch(v.trim()),300); }
async function doSrch(q){
  nav('search'); const el=document.getElementById('srch-ct'); el.innerHTML='<div class="loading">搜尋中…</div>';
  try{
    const d=await(await fetch(`/api/search?q=${encodeURIComponent(q)}`)).json();
    let h='';
    if(d.songs.length){ h+=`<div style="font-size:14px;font-weight:700;color:var(--t2);margin-bottom:8px">歌曲 (${d.songs.length})</div><div class="sl-hdr"><span>#</span><span></span><span>標題</span><span>藝人</span><span>專輯</span><span>時長</span><span></span></div><div class="sl" id="srch-sl" style="margin-bottom:24px"></div>`; }
    if(d.albums.length){ h+=`<div style="font-size:14px;font-weight:700;color:var(--t2);margin-bottom:8px">專輯</div><div class="ag" style="margin-bottom:24px">${d.albums.map(a=>`<div class="ac" onclick="openAlb('${a.id}')"><div class="ac-cover">${a.cover_song_id?`<img src="/api/song/${a.cover_song_id}/cover" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><div class="ac-cover-ph" style="display:none">💿</div>`:`<div class="ac-cover-ph">💿</div>`}</div><div class="ac-texts"><div class="ac-name">${esc(a.name)}</div><div class="ac-sub">${esc(a.artist)}</div></div></div>`).join('')}</div>`; }
    if(d.artists.length){ h+=`<div style="font-size:14px;font-weight:700;color:var(--t2);margin-bottom:8px">藝人</div><div class="artg">${d.artists.map(a=>`<div class="artc" data-artname="${esc(a.name)}" onclick="openArt(this.dataset.artname)"><div class="art-av">${a.cover_song_id?`<img src="/api/song/${a.cover_song_id}/cover" alt="" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'"><div class="art-av-ph" style="display:none">🎤</div>`:`<div class="art-av-ph">🎤</div>`}</div><div class="art-name">${esc(a.name)}</div></div>`).join('')}</div>`; }
    el.innerHTML=h||`<div class="empty"><div class="empty-i">🔍</div><div>找不到「${esc(q)}」</div></div>`;
    if(d.songs.length&&document.getElementById('srch-sl')) renderSongs(d.songs,'srch-sl');
  }catch(e){el.innerHTML='<div class="empty">搜尋失敗</div>'}
}

// ════ Scan ════
async function doScan(){ await fetch('/api/scan',{method:'POST'}); upScanSt('掃描中…'); startScanPoll(); }
function startScanPoll(){
  if(scanPoll)clearInterval(scanPoll);
  scanPoll=setInterval(async()=>{
    try{
      const st=await(await fetch('/api/scan/status')).json();
      if(st.scanning){ const p=st.total>0?Math.round(st.progress/st.total*100):0; upScanSt(`掃描中… ${st.progress}/${st.total}`); document.getElementById('scanbar').style.width=p+'%'; }
      else if(st.done){ document.getElementById('scanbar').style.width='100%'; setTimeout(()=>document.getElementById('scanbar').style.width='0',600); clearInterval(scanPoll); await loadLib(); }
    }catch(e){}
  },800);
}
function upScanSt(t){ document.getElementById('scanst').textContent=t; }

// ════ Utils ════
function openMo(id){ document.getElementById(id).classList.add('open'); }
function closeMo(id){ document.getElementById(id).classList.remove('open'); }
let ttmo,toastActCb=null;
function toast(msg){ showToastWithAction(msg,null,null); }
function showToastWithAction(msg,actionLabel,cb){
  const el=document.getElementById('toast');
  el.innerHTML=actionLabel?`${esc(msg)} <span style="color:var(--acc);text-decoration:underline;cursor:pointer;margin-left:8px" onclick="_toastAct()">${esc(actionLabel)}</span>`:esc(msg);
  el.classList.add('on'); toastActCb=cb;
  clearTimeout(ttmo); ttmo=setTimeout(()=>{el.classList.remove('on');toastActCb=null;},3500);
}
window._toastAct=function(){ document.getElementById('toast').classList.remove('on'); if(toastActCb)toastActCb(); };

// 關閉歌曲選單 (點外部)
document.addEventListener('click',e=>{
  const menu=document.getElementById('song-menu');
  if(menu.classList.contains('open')&&!menu.contains(e.target)) closeSongMenu();
  const ep=document.getElementById('eq-panel'),eb=document.getElementById('eq-btn');
  if(ep&&ep.classList.contains('open')&&!ep.contains(e.target)&&e.target!==eb){ep.classList.remove('open');eb.classList.remove('active');}
});
document.querySelectorAll('.mo').forEach(o=>o.addEventListener('click',e=>{if(e.target===o)o.classList.remove('open')}));
document.getElementById('qs-overlay').addEventListener('click',e=>{if(e.target===document.getElementById('qs-overlay'))closeQueue()});
document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT') return;
  if(e.code==='Space'){e.preventDefault();togglePlay()}
  else if(e.code==='ArrowRight')aud.currentTime+=10;
  else if(e.code==='ArrowLeft')aud.currentTime-=10;
  else if(e.code==='ArrowUp'){aud.volume=Math.min(1,aud.volume+.05);setVolUI(aud.volume)}
  else if(e.code==='ArrowDown'){aud.volume=Math.max(0,aud.volume-.05);setVolUI(aud.volume)}
});

init();
</script>
</body>
</html>

"""



# ══════════════════════════════════════════════
if __name__ == '__main__':
    load_playlists()
    print(f"Starting music server v3, scanning {MUSIC_DIR}…")
    print(f"Playlists: {PLAYLISTS_F}")
    threading.Thread(target=scan_library, daemon=True).start()
    app.run(host='0.0.0.0', port=1979, debug=False, threaded=True)
