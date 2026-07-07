#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NAS 系統 - 單檔案版本 (2024 最新版)
Python 3.11+ | FastAPI 0.109+ | Pydantic V2
"""

import os
import json
import shutil
import asyncio
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, List, Dict
import psutil

from fastapi import FastAPI, Depends, HTTPException, File, UploadFile, Form, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthCredentials
from pydantic import BaseModel, Field
from passlib.context import CryptContext
from jose import JWTError, jwt
import uvicorn
import aiofiles

# ==================== 配置 ====================
SECRET_KEY = "nas-2024-secret-key"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
MAX_UPLOAD_SIZE = 500 * 1024 * 1024

STORAGE_PATHS = {
    "internal": "/storage/emulated/0/sdcard/ftp",
    "sdcard": "/storage/466F-CCB7/ftp-sdcard"
}

for path in STORAGE_PATHS.values():
    Path(path).mkdir(parents=True, exist_ok=True)

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# ==================== 資料庫 ====================
class FileDB:
    def __init__(self):
        self.home = Path.home()
        self.db_dir = self.home / ".nas_db"
        self.db_dir.mkdir(exist_ok=True)
        self.users_file = self.db_dir / "users.json"
        self.trash_file = self.db_dir / "trash.json"
        self.shares_file = self.db_dir / "shares.json"
        self.init_db()
    
    def init_db(self):
        if not self.users_file.exists():
            users = {
                "admin": {"password_hash": pwd_context.hash("admin123"), "role": "admin"},
                "user": {"password_hash": pwd_context.hash("user123"), "role": "user"}
            }
            self.save_json(self.users_file, users)
        
        if not self.trash_file.exists():
            self.save_json(self.trash_file, {})
        
        if not self.shares_file.exists():
            self.save_json(self.shares_file, {})
    
    @staticmethod
    def save_json(filepath, data):
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    
    @staticmethod
    def load_json(filepath):
        if not filepath.exists():
            return {}
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    
    def get_users(self):
        return self.load_json(self.users_file)
    
    def get_trash(self):
        return self.load_json(self.trash_file)
    
    def save_trash(self, trash):
        self.save_json(self.trash_file, trash)

db = FileDB()

# ==================== Pydantic 模型 (V2) ====================
class LoginRequest(BaseModel):
    username: str = Field(..., min_length=3)
    password: str = Field(..., min_length=6)

# ==================== 認證 ====================
def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta or timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def authenticate_user(username: str, password: str):
    users = db.get_users()
    user = users.get(username)
    if not user or not verify_password(password, user["password_hash"]):
        return None
    return user

# ==================== 依賴注入 ====================
security = HTTPBearer()

async def verify_token(credentials: HTTPAuthCredentials = Depends(security)) -> str:
    try:
        payload = jwt.decode(credentials.credentials, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if not username:
            raise HTTPException(status_code=401, detail="無效 Token")
        return username
    except JWTError:
        raise HTTPException(status_code=401, detail="Token 已過期")

# ==================== 儲存管理 ====================
class StorageManager:
    @staticmethod
    async def get_storage_info() -> Dict[str, Dict]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, StorageManager._sync_get_storage_info)
    
    @staticmethod
    def _sync_get_storage_info() -> Dict[str, Dict]:
        info = {}
        for name, path in STORAGE_PATHS.items():
            try:
                stat = shutil.disk_usage(path)
                info[name] = {
                    "path": path,
                    "total": stat.total,
                    "used": stat.used,
                    "free": stat.free,
                    "percent": round(stat.used / stat.total * 100, 2) if stat.total > 0 else 0
                }
            except Exception as e:
                info[name] = {"error": str(e)}
        return info
    
    @staticmethod
    def list_files(storage: str, path: str = "") -> List[Dict]:
        base_path = STORAGE_PATHS.get(storage)
        if not base_path:
            return []
        
        full_path = Path(base_path) / path
        if not full_path.exists():
            return []

        files = []
        try:
            for item in full_path.iterdir():
                try:
                    stat = item.stat()
                    files.append({
                        "name": item.name,
                        "path": str(item.relative_to(base_path)),
                        "type": "folder" if item.is_dir() else "file",
                        "size": stat.st_size,
                        "extension": item.suffix.lower() if item.is_file() else "",
                        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    })
                except Exception:
                    continue
        except Exception:
            pass

        return sorted(files, key=lambda x: (x["type"] != "folder", x["name"].lower()))
    
    @staticmethod
    def get_file(storage: str, path: str) -> Optional[Path]:
        base_path = STORAGE_PATHS.get(storage)
        if not base_path:
            return None
        
        full_path = Path(base_path) / path
        return full_path if full_path.exists() and full_path.is_file() else None
    
    @staticmethod
    def delete_file(storage: str, path: str) -> bool:
        base_path = STORAGE_PATHS.get(storage)
        if not base_path:
            return False
        
        full_path = Path(base_path) / path
        if not full_path.exists():
            return False

        try:
            trash = db.get_trash()
            trash_id = f"{datetime.now().timestamp()}_{path.replace('/', '_')}"
            trash_path = db.db_dir / "trash" / trash_id
            trash_path.parent.mkdir(exist_ok=True)
            
            shutil.move(str(full_path), str(trash_path))
            
            trash[trash_id] = {
                "original_path": path,
                "storage": storage,
                "deleted_at": datetime.now().isoformat(),
                "size": os.path.getsize(trash_path) if trash_path.is_file() else 0
            }
            db.save_trash(trash)
            return True
        except Exception:
            return False
    
    @staticmethod
    def create_folder(storage: str, path: str, folder_name: str) -> bool:
        base_path = STORAGE_PATHS.get(storage)
        if not base_path:
            return False
        
        try:
            (Path(base_path) / path / folder_name).mkdir(parents=True, exist_ok=True)
            return True
        except Exception:
            return False
    
    @staticmethod
    def rename_file(storage: str, path: str, new_name: str) -> bool:
        base_path = STORAGE_PATHS.get(storage)
        if not base_path:
            return False
        
        file_path = Path(base_path) / path
        try:
            file_path.rename(file_path.parent / new_name)
            return True
        except Exception:
            return False
    
    @staticmethod
    def move_file(storage: str, source: str, destination: str) -> bool:
        base_path = STORAGE_PATHS.get(storage)
        if not base_path:
            return False
        
        try:
            src = Path(base_path) / source
            dst = Path(base_path) / destination
            if dst.is_dir():
                shutil.move(str(src), str(dst / src.name))
            else:
                shutil.move(str(src), str(dst))
            return True
        except Exception:
            return False
    
    @staticmethod
    def copy_file(storage: str, source: str, destination: str) -> bool:
        base_path = STORAGE_PATHS.get(storage)
        if not base_path:
            return False
        
        try:
            src = Path(base_path) / source
            dst = Path(base_path) / destination
            if src.is_dir():
                shutil.copytree(str(src), str(dst))
            else:
                shutil.copy2(str(src), str(dst))
            return True
        except Exception:
            return False
    
    @staticmethod
    def search_files(storage: str, query: str, limit: int = 100) -> List[Dict]:
        base_path = STORAGE_PATHS.get(storage)
        if not base_path:
            return []
        
        results = []
        query_lower = query.lower()
        
        try:
            for root, dirs, files in os.walk(base_path):
                if len(results) >= limit:
                    break
                for file in files:
                    if len(results) >= limit:
                        break
                    if query_lower in file.lower():
                        full_path = Path(root) / file
                        results.append({
                            "name": file,
                            "path": str(full_path.relative_to(base_path)),
                            "size": full_path.stat().st_size,
                            "modified": datetime.fromtimestamp(full_path.stat().st_mtime).isoformat()
                        })
        except Exception:
            pass
        
        return results
    
    @staticmethod
    def get_trash() -> List[Dict]:
        trash = db.get_trash()
        return [
            {"id": trash_id, **metadata}
            for trash_id, metadata in trash.items()
            if (db.db_dir / "trash" / trash_id).exists()
        ]
    
    @staticmethod
    def restore_from_trash(trash_id: str) -> bool:
        trash = db.get_trash()
        if trash_id not in trash:
            return False
        
        try:
            trash_path = db.db_dir / "trash" / trash_id
            metadata = trash[trash_id]
            base_path = STORAGE_PATHS.get(metadata["storage"])
            
            if not base_path:
                return False
            
            restore_path = Path(base_path) / metadata["original_path"]
            restore_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(trash_path), str(restore_path))
            
            del trash[trash_id]
            db.save_trash(trash)
            return True
        except Exception:
            return False
    
    @staticmethod
    def empty_trash() -> bool:
        try:
            trash_dir = db.db_dir / "trash"
            if trash_dir.exists():
                shutil.rmtree(trash_dir)
                trash_dir.mkdir(exist_ok=True)
            db.save_trash({})
            return True
        except Exception:
            return False

# ==================== FastAPI 應用 ====================
app = FastAPI(title="NAS API v2", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ==================== API 端點 ====================

@app.get("/api/health")
async def health():
    return {"status": "ok"}

@app.post("/api/login")
async def login(request: LoginRequest):
    user = authenticate_user(request.username, request.password)
    if not user:
        raise HTTPException(status_code=401, detail="認証失敗")
    
    token = create_access_token(
        data={"sub": request.username},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    
    return {
        "token": token,
        "tokenType": "bearer",
        "user": {
            "username": request.username,
            "role": user.get("role", "user")
        }
    }

@app.post("/api/register")
async def register(request: LoginRequest):
    users = db.get_users()
    if request.username in users:
        raise HTTPException(status_code=400, detail="用戶已存在")
    
    users[request.username] = {
        "password_hash": get_password_hash(request.password),
        "role": "user"
    }
    db.save_json(db.users_file, users)
    return {"message": "註冊成功"}

@app.get("/api/storage/info")
async def get_storage_info(username: str = Depends(verify_token)):
    info = await StorageManager.get_storage_info()
    return [{"name": k, **v} for k, v in info.items()]

@app.get("/api/files/{storage}")
async def list_files(
    storage: str,
    path: str = Query(""),
    username: str = Depends(verify_token)
):
    if storage not in STORAGE_PATHS:
        raise HTTPException(status_code=404, detail="儲存不存在")
    
    return StorageManager.list_files(storage, path)

@app.post("/api/files/upload/{storage}")
async def upload_file(
    storage: str,
    file: UploadFile = File(...),
    path: str = Form(""),
    username: str = Depends(verify_token)
):
    if storage not in STORAGE_PATHS:
        raise HTTPException(status_code=404, detail="儲存不存在")
    
    try:
        base_path = Path(STORAGE_PATHS[storage]) / path
        base_path.mkdir(parents=True, exist_ok=True)
        
        file_path = base_path / file.filename
        content = await file.read()
        
        if len(content) > MAX_UPLOAD_SIZE:
            raise HTTPException(status_code=413, detail="檔案過大")
        
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(content)
        
        return {"message": "上傳成功", "filename": file.filename, "size": len(content)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/files/download/{storage}")
async def download_file(
    storage: str,
    path: str = Query(...),
    username: str = Depends(verify_token)
):
    if storage not in STORAGE_PATHS:
        raise HTTPException(status_code=404, detail="儲存不存在")
    
    file_path = StorageManager.get_file(storage, path)
    if not file_path:
        raise HTTPException(status_code=404, detail="檔案不存在")
    
    return FileResponse(file_path, filename=Path(path).name)

@app.post("/api/files/delete/{storage}")
async def delete_file(
    storage: str,
    path: str = Form(...),
    username: str = Depends(verify_token)
):
    if storage not in STORAGE_PATHS:
        raise HTTPException(status_code=404, detail="儲存不存在")
    
    return {"success": StorageManager.delete_file(storage, path)}

@app.post("/api/files/mkdir/{storage}")
async def create_folder(
    storage: str,
    path: str = Form(""),
    folder_name: str = Form(...),
    username: str = Depends(verify_token)
):
    if storage not in STORAGE_PATHS:
        raise HTTPException(status_code=404, detail="儲存不存在")
    
    return {"success": StorageManager.create_folder(storage, path, folder_name)}

@app.post("/api/files/rename/{storage}")
async def rename_file(
    storage: str,
    path: str = Form(...),
    new_name: str = Form(...),
    username: str = Depends(verify_token)
):
    if storage not in STORAGE_PATHS:
        raise HTTPException(status_code=404, detail="儲存不存在")
    
    return {"success": StorageManager.rename_file(storage, path, new_name)}

@app.post("/api/files/move/{storage}")
async def move_file(
    storage: str,
    source: str = Form(...),
    destination: str = Form(...),
    username: str = Depends(verify_token)
):
    if storage not in STORAGE_PATHS:
        raise HTTPException(status_code=404, detail="儲存不存在")
    
    return {"success": StorageManager.move_file(storage, source, destination)}

@app.post("/api/files/copy/{storage}")
async def copy_file(
    storage: str,
    source: str = Form(...),
    destination: str = Form(...),
    username: str = Depends(verify_token)
):
    if storage not in STORAGE_PATHS:
        raise HTTPException(status_code=404, detail="儲存不存在")
    
    return {"success": StorageManager.copy_file(storage, source, destination)}

@app.get("/api/files/search/{storage}")
async def search_files(
    storage: str,
    query: str = Query(...),
    limit: int = Query(100),
    username: str = Depends(verify_token)
):
    if storage not in STORAGE_PATHS:
        raise HTTPException(status_code=404, detail="儲存不存在")
    
    return StorageManager.search_files(storage, query, limit)

@app.get("/api/trash")
async def get_trash(username: str = Depends(verify_token)):
    return StorageManager.get_trash()

@app.post("/api/trash/restore")
async def restore_trash(
    trash_id: str = Form(...),
    username: str = Depends(verify_token)
):
    return {"success": StorageManager.restore_from_trash(trash_id)}

@app.post("/api/trash/empty")
async def empty_trash(username: str = Depends(verify_token)):
    return {"success": StorageManager.empty_trash()}

@app.get("/api/system/info")
async def system_info(username: str = Depends(verify_token)):
    return {
        "cpu_percent": psutil.cpu_percent(interval=1),
        "memory_percent": psutil.virtual_memory().percent,
        "memory_total": psutil.virtual_memory().total,
        "memory_used": psutil.virtual_memory().used,
    }

if __name__ == "__main__":
    print("""
╔════════════════════════════════════════╗
║     NAS API v2.0                       ║
║     192.168.213.117:8000               ║
╚════════════════════════════════════════╝
    """)
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="info"
    )
