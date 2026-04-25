import asyncio
import json
import logging
import os
import shutil
import subprocess
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect, Request, BackgroundTasks
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

def yt_dlp_hook(d):
    # This hook is called synchronously by yt-dlp, we need to carefully update state
    # and we can't await in it directly. We'll queue updates if needed, or rely on 
    # a separate loop to broadcast.
    # To keep it simple, we update the task object directly. The broadcast loop handles sending.
    
    # We pass the task_id via the info_dict if possible, or just parse filename.
    # A cleaner way is to keep a mapping of filename to task_id or pass a custom logger.
    # We will handle updates directly in the download function using closures.
    pass

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
        
        ydl_opts = {
            'outtmpl': outtmpl,
            'logger': MyLogger(),
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True
        }

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
            format_str = f"{task.format_id}+bestaudio/best" if task.format_id else 'bestvideo+bestaudio/best'
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
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'nocheckcertificate': True
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
            
        title = info.get('title', 'Unknown Title')
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

@app.post("/api/update-ytdlp")
async def update_ytdlp(authorization: Optional[str] = Header(None)):
    verify_token(authorization)
    try:
        process = subprocess.Popen(["pip", "install", "--upgrade", "yt-dlp"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        if process.returncode != 0:
             raise Exception(stderr.decode())
        return {"success": True, "output": stdout.decode()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Mount frontend
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
