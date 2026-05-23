import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
import sys          # <--- 新增這行
import importlib    # <--- 新增這行
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, Request, BackgroundTasks, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp

# --- Configuration ---
AUTH_TOKEN = os.environ.get("AUTH_TOKEN", "your_secure_password_here")
TEMP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "temp"))
CONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "config"))
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
COOKIE_FILE = os.path.join(CONFIG_DIR, "cookies.txt")

os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- State Management ---
class TaskState:
    QUEUED = "QUEUED"
    DOWNLOADING = "DOWNLOADING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"

class DownloadTask:
    def __init__(self, task_id: str, url: str, format_id: str, is_audio_only: bool, title: str):
        self.task_id = task_id
        self.url = url
        self.format_id = format_id
        self.is_audio_only = is_audio_only
        self.title = title
        self.state = TaskState.QUEUED
        self.progress = 0.0
        self.speed = ""
        self.eta = ""
        self.file_path = ""
        self.error_msg = ""
        self.created_at = datetime.now()
        self.last_accessed = datetime.now()

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "url": self.url,
            "title": self.title,
            "state": self.state,
            "progress": self.progress,
            "speed": self.speed,
            "eta": self.eta,
            "is_audio_only": self.is_audio_only,
            "error_msg": self.error_msg
        }

tasks: Dict[str, DownloadTask] = {}
task_queue = asyncio.Queue()
active_tasks = 0
active_websockets: List[WebSocket] = []

# --- Config Management ---
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading config: {e}")
    return {"max_concurrent_downloads": 2}

def save_config(config):
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving config: {e}")

config_data = load_config()

# --- Auth Dependency ---
def verify_token(authorization: Optional[str] = Header(None)):
    if not authorization or authorization != f"Bearer {AUTH_TOKEN}":
        raise HTTPException(status_code=401, detail="Unauthorized")

# --- WebSocket ---
async def broadcast_state():
    state = {
        "tasks": [task.to_dict() for task in tasks.values()],
        "config": config_data
    }
    message = json.dumps({"type": "state_update", "data": state})
    for ws in active_websockets:
        try:
            await ws.send_text(message)
        except Exception:
            pass

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    
    # Simple auth for WS
    auth_msg = await websocket.receive_text()
    try:
        auth_data = json.loads(auth_msg)
        if auth_data.get("token") != AUTH_TOKEN:
            await websocket.close(code=1008)
            return
    except Exception:
        await websocket.close(code=1008)
        return

    active_websockets.append(websocket)
    try:
        await broadcast_state()
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_websockets.remove(websocket)

# --- Task Worker ---
class MyLogger(object):
    def debug(self, msg):
        pass
    def warning(self, msg):
        pass
    def error(self, msg):
        logger.error(msg)

def get_common_ydl_opts() -> dict:
    opts = {
        'logger': MyLogger(),
        'quiet': True,
        'no_warnings': True,
        'nocheckcertificate': True, 'legacyserverconnect': True, 'no_check_certificates': True,
        'extractor_args': {'youtube': ['player_client=ios,tv,web']}
    }
    if os.path.exists(COOKIE_FILE):
        opts['cookiefile'] = COOKIE_FILE
    return opts


async def download_worker():
    global active_tasks
    while True:
        try:
            # Wait for a task
            task_id = await task_queue.get()
            task = tasks.get(task_id)
            if not task:
                task_queue.task_done()
                continue

            # Check if we can start
            while active_tasks >= config_data.get("max_concurrent_downloads", 2):
                await asyncio.sleep(1)

            active_tasks += 1
            task.state = TaskState.DOWNLOADING
            await broadcast_state()

            # Run download in a thread to not block event loop
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, perform_download, task)

            active_tasks -= 1
            task_queue.task_done()
            await broadcast_state()
            
        except Exception as e:
            logger.error(f"Worker error: {e}")
            await asyncio.sleep(1)

def perform_download(task: DownloadTask):
    try:
        outtmpl = os.path.join(TEMP_DIR, f"{task.task_id}.%(ext)s")
        
        ydl_opts = get_common_ydl_opts()
        ydl_opts.update({
            'outtmpl': outtmpl,
        })

        if task.is_audio_only:
            ydl_opts.update({
                'format': task.format_id if task.format_id else 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
            })
            final_ext = "mp3"
        else:
            # Video: format_id + bestaudio, convert to mp4
            format_str = f"{task.format_id}+bestaudio/bestvideo+bestaudio/best/best" if task.format_id else 'bestvideo+bestaudio/best/best'
            ydl_opts.update({
                'format': format_str,
                'merge_output_format': 'mp4',
            })
            final_ext = "mp4"

        # Progress hook
        def progress_hook(d):
            if d['status'] == 'downloading':
                task.state = TaskState.DOWNLOADING
                
                # Calculate progress
                downloaded = d.get('downloaded_bytes', 0)
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                if total > 0:
                    task.progress = round((downloaded / total) * 100, 2)
                
                # Speed
                speed = d.get('speed', 0)
                if speed:
                    task.speed = f"{speed / 1024 / 1024:.2f} MB/s"
                
                # ETA
                eta = d.get('eta', 0)
                if eta is not None:
                    task.eta = f"{eta}s"
                    
            elif d['status'] == 'finished':
                task.state = TaskState.PROCESSING
                task.progress = 100.0
                task.speed = ""
                task.eta = "Processing..."
                
            # Broadcast state (since we are in a thread, use threadsafe queue or just let background loop do it)
            # We'll rely on the periodic broadcaster to pick up these changes

        ydl_opts['progress_hooks'] = [progress_hook]

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([task.url])

        # Find the output file
        expected_file = os.path.join(TEMP_DIR, f"{task.task_id}.{final_ext}")
        
        # Sometime yt-dlp might output .mkv temporarily before merge, but since we set merge_output_format
        # it should end up as .mp4. For audio it extracts to .mp3.
        # Let's verify file exists
        if os.path.exists(expected_file):
            task.file_path = expected_file
            task.state = TaskState.COMPLETED
        else:
            # Try to find any file starting with task_id
            found = False
            for f in os.listdir(TEMP_DIR):
                if f.startswith(task.task_id):
                    task.file_path = os.path.join(TEMP_DIR, f)
                    task.state = TaskState.COMPLETED
                    found = True
                    break
            if not found:
                raise Exception("Output file not found after download.")

    except Exception as e:
        logger.error(f"Download failed for {task.task_id}: {e}")
        task.state = TaskState.ERROR
        task.error_msg = str(e)

# Periodic broadcast task
async def periodic_broadcaster():
    while True:
        await broadcast_state()
        await asyncio.sleep(1) # Broadcast every second

# Background Cleanup Task
async def cleanup_task():
    while True:
        try:
            now = datetime.now()
            cutoff_time = now - timedelta(hours=2)
            
            # Clean tasks memory
            tasks_to_delete = []
            for task_id, task in tasks.items():
                if task.last_accessed < cutoff_time:
                    tasks_to_delete.append(task_id)
            
            for task_id in tasks_to_delete:
                del tasks[task_id]
                
            # Clean files in temp dir
            for filename in os.listdir(TEMP_DIR):
                filepath = os.path.join(TEMP_DIR, filename)
                if os.path.isfile(filepath):
                    file_mtime = datetime.fromtimestamp(os.path.getmtime(filepath))
                    if file_mtime < cutoff_time:
                        try:
                            os.remove(filepath)
                            logger.info(f"Cleaned up old file: {filepath}")
                        except Exception as e:
                            logger.error(f"Error removing {filepath}: {e}")
                            
            if tasks_to_delete:
                await broadcast_state()
                
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
            
        await asyncio.sleep(600) # Run every 10 mins

# --- Startup Events ---
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(download_worker())
    asyncio.create_task(periodic_broadcaster())
    asyncio.create_task(cleanup_task())

# --- API Endpoints ---
class ParseRequest(BaseModel):
    url: str

@app.post("/api/parse")
async def parse_url(req: ParseRequest, authorization: Optional[str] = Header(None)):
    verify_token(authorization)
    try:
        ydl_opts = get_common_ydl_opts()
        ydl_opts.update({
            # 在最後面補上 bestaudio，讓純音樂也能順利過關
            'format': 'bestvideo+bestaudio/best/bestaudio/best',
            'extract_flat': 'in_playlist'
        })
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
            
        title = info.get('title', 'Unknown Title')
        
        # Handle playlist
        if 'entries' in info:
            entries = []
            for entry in info['entries']:
                if entry is None:
                    continue
                entry_title = entry.get('title', '')
                if not entry_title or entry_title in ['[Deleted video]', '[Private video]']:
                    continue
                entry_url = entry.get('url') or entry.get('id')
                if not entry_url:
                    continue
                    
                # Fix url if it is just an id
                if not entry_url.startswith('http'):
                    entry_url = f"https://www.youtube.com/watch?v={entry_url}"
                    
                entries.append({
                    "title": entry_title,
                    "url": entry_url
                })
                
            return {
                "is_playlist": True,
                "title": title,
                "item_count": len(entries),
                "entries": entries
            }
        
        # Handle single video
        formats = info.get('formats', [])
        
        # Filter formats
        video_formats = []
        for f in formats:
            # We want formats that have video. yt-dlp usually separates them.
            if f.get('vcodec') != 'none':
                height = f.get('height')
                ext = f.get('ext')
                format_id = f.get('format_id')
                format_note = f.get('format_note', '')
                if height:
                    video_formats.append({
                        "id": format_id,
                        "resolution": f"{height}p",
                        "note": format_note,
                        "ext": ext
                    })
        
        # Deduplicate and sort by height
        unique_videos = {}
        for f in video_formats:
            res = f['resolution']
            if res not in unique_videos:
                unique_videos[res] = f
            else:
                # prefer mp4
                if f['ext'] == 'mp4' and unique_videos[res]['ext'] != 'mp4':
                    unique_videos[res] = f
                    
        sorted_videos = sorted(unique_videos.values(), key=lambda x: int(x['resolution'].replace('p', '')), reverse=True)

        return {
            "is_playlist": False,
            "title": title,
            "video_formats": sorted_videos
        }
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

class DownloadRequest(BaseModel):
    url: str
    format_id: str
    is_audio_only: bool
    title: str

class BatchDownloadItem(BaseModel):
    url: str
    title: str

class BatchDownloadRequest(BaseModel):
    items: list[BatchDownloadItem]
    is_audio_only: bool

@app.post("/api/download")
async def add_download(req: DownloadRequest, authorization: Optional[str] = Header(None)):
    verify_token(authorization)
    task_id = str(uuid.uuid4())
    
    task = DownloadTask(
        task_id=task_id,
        url=req.url,
        format_id=req.format_id,
        is_audio_only=req.is_audio_only,
        title=req.title
    )
    
    tasks[task_id] = task
    await task_queue.put(task_id)
    await broadcast_state()
    
    return {"task_id": task_id}

@app.post("/api/download/batch")
async def add_batch_download(req: BatchDownloadRequest, authorization: Optional[str] = Header(None)):
    verify_token(authorization)
    
    count = 0
    for item in req.items:
        task_id = str(uuid.uuid4())
        task = DownloadTask(
            task_id=task_id,
            url=item.url,
            format_id="", # Empty format id for audio_only which is default for batch
            is_audio_only=req.is_audio_only,
            title=item.title
        )
        tasks[task_id] = task
        await task_queue.put(task_id)
        count += 1
        
    if count > 0:
        await broadcast_state()
        
    return {"added_count": count}

@app.get("/api/download/{task_id}")
async def get_download_file(task_id: str, token: str):
    if token != AUTH_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    task = tasks.get(task_id)
    if not task or task.state != TaskState.COMPLETED or not task.file_path:
        raise HTTPException(status_code=404, detail="File not ready or found")
        
    task.last_accessed = datetime.now()
    
    filename = os.path.basename(task.file_path)
    # Sanitize title for download filename
    safe_title = "".join([c for c in task.title if c.isalpha() or c.isdigit() or c==' ']).rstrip()
    ext = filename.split('.')[-1]
    download_name = f"{safe_title}.{ext}" if safe_title else filename
    
    return FileResponse(
        path=task.file_path, 
        filename=download_name,
        media_type='application/octet-stream'
    )

@app.delete("/api/task/{task_id}")
async def delete_task(task_id: str, background_tasks: BackgroundTasks, authorization: Optional[str] = Header(None)):
    verify_token(authorization)
    task = tasks.get(task_id)
    if task:
        if task.file_path and os.path.exists(task.file_path):
            def remove_file(path):
                try:
                    os.remove(path)
                except Exception as e:
                    logger.error(f"Failed to remove {path}: {e}")
            background_tasks.add_task(remove_file, task.file_path)
        del tasks[task_id]
        await broadcast_state()
    return {"success": True}

class ConfigRequest(BaseModel):
    max_concurrent_downloads: int

@app.post("/api/config")
async def update_config(req: ConfigRequest, authorization: Optional[str] = Header(None)):
    verify_token(authorization)
    global config_data
    config_data["max_concurrent_downloads"] = req.max_concurrent_downloads
    save_config(config_data)
    await broadcast_state()
    return {"success": True}

@app.get("/api/version")
async def get_version(authorization: Optional[str] = Header(None)):
    verify_token(authorization)
    import yt_dlp
    # 取得目前載入記憶體中的 yt-dlp 版本
    version = getattr(yt_dlp.version, '__version__', '未知版本')
    return {"version": version}

@app.get("/api/cookie-status")
async def get_cookie_status(authorization: Optional[str] = Header(None)):
    verify_token(authorization)
    exists = os.path.exists(COOKIE_FILE)
    return {"exists": exists}

@app.post("/api/upload-cookie")
async def upload_cookie(
    file: UploadFile = File(...),
    authorization: Optional[str] = Header(None)
):
    verify_token(authorization)
    if not file.filename.endswith(".txt"):
        raise HTTPException(status_code=400, detail="Only .txt files are allowed")

    try:
        content = await file.read()
        with open(COOKIE_FILE, "wb") as f:
            f.write(content)
        return {"success": True, "message": "Cookie 檔案上傳成功"}
    except Exception as e:
        logger.error(f"Failed to upload cookie: {e}")
        raise HTTPException(status_code=500, detail="儲存 Cookie 檔案時發生錯誤")

@app.post("/api/update-ytdlp")
async def update_ytdlp(background_tasks: BackgroundTasks, authorization: Optional[str] = Header(None)):
    verify_token(authorization)
    try:
        # 使用 sys.executable 確保使用目前環境的 pip
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
             raise Exception(stderr.decode())

        # 將終端機輸出的位元組解碼為字串
        output_str = stdout.decode()

        # 【核心邏輯】：判斷是否有真正下載並安裝新版本
        # pip 如果有安裝新版本，輸出文字會包含 "Successfully installed"
        if "Successfully installed" in output_str:
            # 定義重啟伺服器的背景任務
            async def restart_server():
                await asyncio.sleep(2)  # 等待 2 秒，讓 HTTP 成功回應可以確實傳送到前端
                logger.info("yt-dlp updated, restarting server...")
                os._exit(0)    # 強制結束 Python 行程，觸發 Docker 的 restart 機制

            # 將重啟任務加入背景執行
            background_tasks.add_task(restart_server)

            return {
                "success": True, 
                "output": output_str, 
                "new_version": "更新成功！即將重啟系統以套用新版...",
                "require_restart": True   # <--- 新增這行：告訴前端需要重啟
            }
        else:
            # 如果沒有 "Successfully installed"，通常代表 "Requirement already satisfied"
            # 代表目前已經是最新版，不需要重啟系統
            import yt_dlp
            current_version = getattr(yt_dlp.version, '__version__', '未知版本')
            return {
                "success": True, 
                "output": output_str, 
                "new_version": f"已是最新版本 ({current_version})，系統無需重啟",
                "require_restart": False  # <--- 新增這行：告訴前端不需要重啟
            }
            
    except Exception as e:
        logger.error(f"Failed to update yt-dlp: {e}")
        raise HTTPException(status_code=500, detail=str(e))
# Mount frontend
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
