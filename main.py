import asyncio
import json
import logging
import os
import shutil
import subprocess
import uuid
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, BackgroundTasks, UploadFile, File, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, field_validator, model_validator
from authentication import Authentication
from media import Settings, SourceOptions, http_url, youtube_dl, describe_media, download_options, validate_cookie_file, probe_unknown_stream
import yt_dlp
from user_store import ADMIN_ID, QuotaExceeded, StoreError
from accounts import install_account_routes
from site_cookies import install_site_routes, site_for_url, needs_login, domain_matches
from archives import install_archive_routes

# --- Configuration ---
TEMP_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "temp"))
CONFIG_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "config"))
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
COOKIE_FILE = os.path.join(CONFIG_DIR, "cookies.txt")

os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(CONFIG_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app):
    global task_queue, scheduler_wake, download_executor, active_tasks
    task_queue = asyncio.Queue()
    scheduler_wake = asyncio.Event()
    download_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="download")
    active_tasks = 0
    auth.store.release_orphaned()
    backgrounds = [asyncio.create_task(fn()) for fn in (download_worker, periodic_broadcaster, cleanup_task)]
    try:
        yield
    finally:
        for job in backgrounds:
            job.cancel()
        await asyncio.gather(*backgrounds, return_exceptions=True)
        if running_downloads:
            await asyncio.gather(*running_downloads, return_exceptions=True)
        download_executor.shutdown(wait=True)

app = FastAPI(lifespan=lifespan)

auth = Authentication(CONFIG_DIR)
auth.install(app)

# --- State Management ---
class TaskState:
    QUEUED = "QUEUED"
    DOWNLOADING = "DOWNLOADING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    ERROR = "ERROR"

class DownloadTask:
    def __init__(self, task_id: str, url: str, format_id: str, is_audio_only: bool, title: str, output_format="mp4", source_options=None, owner_id=ADMIN_ID):
        self.owner_id = owner_id
        self.login_required = False
        self.output_format = output_format
        self.source_options = source_options or SourceOptions()
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
            "output_format": self.output_format,
            "login_required": self.login_required,
            "error_msg": self.error_msg
        }

tasks: Dict[str, DownloadTask] = {}
task_queue = asyncio.Queue()
active_tasks = 0
scheduler_wake = asyncio.Event()
running_downloads = set()
download_executor = None
active_websockets: List[WebSocket] = []
update_in_progress = False

# --- Config Management ---
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return Settings(**json.load(f)).model_dump()
        except Exception as e:
            logger.error(f"Error loading config: {e}")
    return Settings().model_dump()

def save_config(config):
    try:
        temporary = CONFIG_FILE + ".tmp"
        with open(temporary, "w") as f:
            json.dump(config, f, indent=2)
        os.replace(temporary, CONFIG_FILE)
    except Exception as e:
        logger.error(f"Error saving config: {e}")
        raise HTTPException(500, "設定無法寫入磁碟")

config_data = load_config()

# --- WebSocket ---
def user_config(user):
    if user['role'] == 'admin':
        return dict(config_data)
    return {key: config_data[key] for key in ('max_concurrent_downloads', 'concurrent_fragments')}

async def broadcast_state():
    for ws in list(active_websockets):
        try:
            user = auth.current_user(ws, getattr(ws.state, "legacy_token", ""))
            if not user:
                await ws.close(code=1008)
                active_websockets.remove(ws)
                continue
            own = [task for task in list(tasks.values()) if task.owner_id == user['id']]
            state = {
                "tasks": [task.to_dict() for task in own], "config": user_config(user), "user": user,
                "quota": auth.store.quota(user['id']),
                "active_downloads": sum(task.state in (TaskState.DOWNLOADING, TaskState.PROCESSING) for task in own),
                "queued_downloads": sum(task.state == TaskState.QUEUED for task in own),
            }
            await asyncio.wait_for(ws.send_text(json.dumps({"type": "state_update", "data": state})), timeout=2)
        except Exception:
            if ws in active_websockets:
                active_websockets.remove(ws)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    if not auth.origin_allowed(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        data = json.loads(await asyncio.wait_for(websocket.receive_text(), timeout=10))
        if not isinstance(data, dict):
            await websocket.close(code=1008)
            return
        if not auth.session_user(websocket):
            if not auth.token_valid(data.get("token", "")):
                await websocket.close(code=1008)
                return
            websocket.state.legacy_token = data["token"]
        active_websockets.append(websocket)
        await broadcast_state()
        while True:
            await websocket.receive_text()
    except (WebSocketDisconnect, asyncio.TimeoutError, ValueError):
        pass
    finally:
        if websocket in active_websockets:
            active_websockets.remove(websocket)

# --- Task Worker ---
async def run_download(task):
    global active_tasks
    try:
        await asyncio.get_running_loop().run_in_executor(download_executor, perform_download, task)
    except Exception as exc:
        task.state = TaskState.ERROR
        task.error_msg = str(exc)
    finally:
        try:
            auth.store.settle(task.task_id, task.state == TaskState.COMPLETED)
        except Exception:
            logger.exception("Failed to settle quota for %s", task.task_id)
        active_tasks -= 1
        task.last_accessed = datetime.now()
        task_queue.task_done()
        scheduler_wake.set()
        await broadcast_state()

async def download_worker():
    """Dispatch jobs without awaiting each download; re-read the limit on every dispatch."""
    global active_tasks
    while True:
        task_id = await task_queue.get()
        while active_tasks >= config_data["max_concurrent_downloads"]:
            scheduler_wake.clear()
            await scheduler_wake.wait()
        task = tasks.get(task_id)
        if not task or task.state != TaskState.QUEUED:
            task_queue.task_done()
            continue
        owner = auth.store.get_user(task.owner_id)
        if not owner or owner['disabled']:
            task.state = TaskState.ERROR
            task.error_msg = '帳號已停用，下載已停止並歸還預留額度'
            auth.store.settle(task_id, False)
            task_queue.task_done()
            continue
        active_tasks += 1
        task.state = TaskState.DOWNLOADING
        job = asyncio.create_task(run_download(task))
        running_downloads.add(job)
        job.add_done_callback(running_downloads.discard)
        await broadcast_state()

def perform_download(task: DownloadTask):
    try:
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise RuntimeError("缺少 FFmpeg/ffprobe，請先安裝以處理串流、合併音軌與驗證影片")
        folder = Path(TEMP_DIR) / task.task_id
        folder.mkdir(exist_ok=True)
        finished_paths = []
        options = download_options(task.format_id, task.is_audio_only, task.output_format)
        options.update({
            "outtmpl": str(folder / "media.%(ext)s"), "noplaylist": True,
            "http_headers": task.source_options.headers(),
            "post_hooks": [finished_paths.append],
        })
        def progress_hook(data):
            if data["status"] == "downloading":
                task.state = TaskState.DOWNLOADING
                total = data.get("total_bytes") or data.get("total_bytes_estimate")
                if total:
                    task.progress = min(99.9, round(data.get("downloaded_bytes", 0) / total * 100, 2))
                speed = data.get("speed")
                task.speed = f"{speed / 1024 / 1024:.2f} MB/s" if speed else ""
                eta = data.get("eta")
                task.eta = f"{eta}s" if eta is not None else ""
            elif data["status"] == "finished":
                task.state = TaskState.PROCESSING
                task.speed = ""
                task.eta = "合併與驗證中"
        options["progress_hooks"] = [progress_hook]
        with private_media(task.owner_id, task.url, options) as ydl:
            info = ydl.extract_info(task.url, download=True)
        # post_hooks run only after all postprocessors and moves have succeeded.
        if not finished_paths:
            raise RuntimeError("下載沒有產生完成檔案，請確認來源不是空的播放清單")
        output = Path(finished_paths[-1]).resolve()
        if output.parent != folder.resolve() or not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError("找不到已完成的輸出檔案")
        probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(output)], capture_output=True, text=True, timeout=30)
        if probe.returncode:
            raise RuntimeError("輸出媒體驗證失敗，下載可能不完整")
        stream_types = {stream.get("codec_type") for stream in json.loads(probe.stdout).get("streams", [])}
        required = "audio" if task.is_audio_only else "video"
        if required not in stream_types:
            raise RuntimeError("來源只提供音軌，請改用含影片的 m3u8 主播放清單" if required == "video" else "來源沒有可用音軌")
        task.file_path = str(output)
        task.progress = 100.0
        task.speed = task.eta = ""
        task.state = TaskState.COMPLETED
        if task.title in ("", "未命名媒體"):
            task.title = info.get("title") or task.title
    except Exception as exc:
        logger.error("Download failed for %s: %s", task.task_id, exc)
        task.state = TaskState.ERROR
        task.error_msg = str(exc)
        task.login_required = needs_login(task.error_msg)
        task.speed = task.eta = ""

# Periodic broadcast task
async def periodic_broadcaster():
    while True:
        await broadcast_state()
        await asyncio.sleep(1) # Broadcast every second

# Background Cleanup Task
async def cleanup_task():
    while True:
        cutoff = datetime.now() - timedelta(hours=2)
        for task_id, task in list(tasks.items()):
            if task.state in (TaskState.COMPLETED, TaskState.ERROR) and task.last_accessed < cutoff and not getattr(task, 'archive_readers', 0):
                shutil.rmtree(Path(TEMP_DIR) / task_id, ignore_errors=True)
                tasks.pop(task_id, None)
        # Never remove files belonging to queued or running tasks.
        live_ids = set(tasks)
        for path in Path(TEMP_DIR).iterdir():
            if path.name in live_ids or datetime.fromtimestamp(path.stat().st_mtime) >= cutoff:
                continue
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.is_file() and not any(path.name.startswith(task_id) for task_id in live_ids):
                path.unlink(missing_ok=True)
        await asyncio.sleep(600)

class ParseRequest(SourceOptions):
    url: str
    _valid_url = field_validator("url")(http_url)

def private_media(user_id, url, options):
    site = site_for_url(url)
    specific = auth.store.site(user_id, site['key'])
    imported = auth.store.site(user_id, '*')
    hosts = (urlsplit(url).hostname, *site['domains'])
    if imported and not any(domain_matches(host, cookie['domain'].lstrip('.')) for host in hosts for cookie in imported['cookies']):
        imported = None
    candidates = [record for record in (specific, imported) if record]
    record = max(candidates, key=lambda record: record['updated_at']) if candidates else None
    owner = auth.store.get_user(user_id)
    cookies = record['cookies'] if record else None if owner and user_id == ADMIN_ID else []
    options = dict(options)
    if record and record['user_agent']:
        options['http_headers'] = {"User-Agent": record['user_agent'], **options.get('http_headers', {})}
    return youtube_dl(dict(config_data), COOKIE_FILE, options, cookies=cookies)

def extract_media(req, user_id=ADMIN_ID):
    options = {"extract_flat": "in_playlist", "http_headers": req.headers()}
    with private_media(user_id, req.url, options) as ydl:
        info = ydl.extract_info(req.url, download=False)
        if info:
            probe_unknown_stream(info, ydl)
    if not info:
        raise ValueError("找不到可下載的媒體")
    return describe_media(info)

@app.post("/api/parse")
async def parse_url(req: ParseRequest, request: Request):
    try:
        return await asyncio.to_thread(extract_media, req, request.state.user['id'])
    except Exception as exc:
        site = site_for_url(req.url)['name']
        required = needs_login(str(exc))
        message = f'來源拒絕存取，可先登入 {site} 後重試，並確認影片能在該網站播放。' if required else str(exc)
        return JSONResponse(status_code=400, content={"detail": message, "login_required": required, "site": site})

def own_task(task_id, user_id):
    task = tasks.get(task_id)
    if not task or task.owner_id != user_id:
        raise HTTPException(404, "找不到任務")
    return task

def reserve_quota(user_id, task_ids):
    try:
        auth.store.reserve(user_id, task_ids)
    except QuotaExceeded as exc:
        raise HTTPException(429, str(exc))
    except StoreError as exc:
        raise HTTPException(403, str(exc))

class DownloadRequest(ParseRequest):
    url: str
    format_id: str = ""
    is_audio_only: bool = False
    title: str = Field(default="未命名媒體", max_length=500)
    output_format: str = "mp4"

    @model_validator(mode="after")
    def match_output(self):
        if self.is_audio_only and self.output_format in ("mp4", "mkv", "original"):
            self.output_format = "mp3"  # Compatibility with older API clients.
        elif not self.is_audio_only and self.output_format in ("mp3", "m4a"):
            raise ValueError("影片模式請選 MP4、MKV 或原始容器")
        return self

    @field_validator("output_format")
    @classmethod
    def valid_output(cls, value):
        if value not in ("mp4", "mkv", "original", "mp3", "m4a"):
            raise ValueError("不支援此輸出格式")
        return value

    @field_validator("format_id")
    @classmethod
    def valid_format(cls, value):
        if value and value != "best" and not all(c.isalnum() or c in "-_." for c in value):
            raise ValueError("請選擇解析結果中的單一格式")
        return value

class BatchDownloadItem(ParseRequest):
    url: str
    title: str

class BatchDownloadRequest(SourceOptions):
    items: list[BatchDownloadItem] = Field(min_length=1, max_length=200)
    is_audio_only: bool = False
    output_format: str = "mp4"
    _valid_output = field_validator("output_format")(DownloadRequest.valid_output.__func__)
    _match_output = model_validator(mode="after")(DownloadRequest.match_output)

@app.post("/api/download")
async def add_download(req: DownloadRequest, request: Request):
    if update_in_progress:
        raise HTTPException(409, "系統更新中，請完成後再加入下載")
    task_id = str(uuid.uuid4())
    reserve_quota(request.state.user['id'], [task_id])
    task = DownloadTask(
        task_id=task_id,
        url=req.url,
        format_id=req.format_id,
        is_audio_only=req.is_audio_only,
        title=req.title,
        output_format=req.output_format,
        source_options=SourceOptions(referer=req.referer, user_agent=req.user_agent),
        owner_id=request.state.user['id']
    )
    
    tasks[task_id] = task
    await task_queue.put(task_id)
    await broadcast_state()
    
    return {"task_id": task_id, "quota": auth.store.quota(request.state.user['id'])}

@app.post("/api/download/batch")
async def add_batch_download(req: BatchDownloadRequest, request: Request):
    if update_in_progress:
        raise HTTPException(409, "系統更新中，請完成後再加入下載")
    
    task_ids = [str(uuid.uuid4()) for _ in req.items]
    reserve_quota(request.state.user['id'], task_ids)
    count = 0
    for item, task_id in zip(req.items, task_ids):
        task = DownloadTask(
            task_id=task_id,
            url=item.url,
            format_id="", # Empty format id for audio_only which is default for batch
            is_audio_only=req.is_audio_only,
            title=item.title,
            output_format=req.output_format,
            source_options=SourceOptions(referer=req.referer, user_agent=req.user_agent),
            owner_id=request.state.user['id']
        )
        tasks[task_id] = task
        await task_queue.put(task_id)
        count += 1
        
    if count > 0:
        await broadcast_state()
        
    return {"added_count": count}

@app.get("/api/download/{task_id}")
async def get_download_file(task_id: str, request: Request):
        
    task = own_task(task_id, request.state.user['id'])
    if task.state != TaskState.COMPLETED or not task.file_path or not Path(task.file_path).is_file():
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
async def delete_task(task_id: str, background_tasks: BackgroundTasks, request: Request):
    task = own_task(task_id, request.state.user["id"])
    if task:
        if getattr(task, 'archive_readers', 0):
            raise HTTPException(409, "此檔案正在打包下載，請完成後再刪除")
        if task.state in (TaskState.DOWNLOADING, TaskState.PROCESSING):
            raise HTTPException(409, "下載仍在進行，完成後才能刪除")
        background_tasks.add_task(shutil.rmtree, str(Path(TEMP_DIR) / task_id), True)
        if task.file_path and os.path.exists(task.file_path):
            def remove_file(path):
                try:
                    os.remove(path)
                except Exception as e:
                    logger.error(f"Failed to remove {path}: {e}")
            background_tasks.add_task(remove_file, task.file_path)
        if task.state == TaskState.QUEUED:
            auth.store.settle(task_id, False)
        del tasks[task_id]
        await broadcast_state()
    return {"success": True}

@app.post("/api/task/{task_id}/retry")
async def retry_task(task_id: str, request: Request):
    if update_in_progress:
        raise HTTPException(409, "系統更新中，請完成後再重試")
    task = own_task(task_id, request.state.user['id'])
    if task.state != TaskState.ERROR:
        raise HTTPException(409, "僅能重試失敗任務")
    reserve_quota(request.state.user['id'], [task_id])
    task.state = TaskState.QUEUED
    task.login_required = False
    task.error_msg = task.speed = task.eta = ""
    task.progress = 0
    task.last_accessed = datetime.now()
    await task_queue.put(task_id)
    await broadcast_state()
    return {"success": True}

@app.post("/api/config")
async def update_config(req: Settings):
    updated = req.model_dump()
    save_config(updated)
    config_data.clear()
    config_data.update(updated)
    scheduler_wake.set()
    await broadcast_state()
    return {"success": True, "config": config_data}

@app.get("/api/version")
async def get_version():
    import yt_dlp
    # 取得目前載入記憶體中的 yt-dlp 版本
    version = getattr(yt_dlp.version, '__version__', '未知版本')
    return {"version": version}

@app.get("/api/cookie-status")
async def get_cookie_status(request: Request):
    own = auth.store.site(request.state.user['id'], '*')
    admin = request.state.user['id'] == ADMIN_ID
    return {"exists": bool(own) or (admin and os.path.isfile(COOKIE_FILE)),
            "source": config_data["cookie_source"] if admin else 'personal',
            "browser": config_data["cookie_browser"] if admin else '',
            "ffmpeg_available": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
            "sso_enabled": auth.oidc_enabled}

@app.post("/api/cookies/test")
async def test_cookies(request: Request):
    user = request.state.user
    def check():
        own = auth.store.site(user['id'], '*')
        if own:
            return {"success": True, "count": len(own['cookies']), "message": f"已讀取個人匯入檔案的 {len(own['cookies'])} 筆 cookies；實際登入狀態須以網站下載驗證"}
        if user['id'] != ADMIN_ID:
            raise ValueError("尚未匯入個人 cookies，請從「Cookies 設定」依教學匯入 cookies.txt")
        if config_data["cookie_source"] == "none":
            return {"success": True, "count": 0, "message": "已停用伺服器 cookies；個人匯入的 cookies 仍會自動套用"}
        if config_data["cookie_source"] == "file" and not os.path.isfile(COOKIE_FILE):
            raise ValueError("尚未上傳 cookies 檔案")
        with youtube_dl(dict(config_data), COOKIE_FILE) as ydl:
            count = len(ydl.cookiejar)
            if not count:
                raise ValueError("來源未提供任何 cookie，請先在選定的瀏覽器登入")
            return {"success": True, "count": count, "message": f"成功讀取 {count} 筆 cookie；實際登入狀態仍須以目標網站下載驗證"}
    try:
        return await asyncio.to_thread(check)
    except Exception as exc:
        raise HTTPException(400, str(exc))

@app.delete("/api/cookies")
async def delete_cookies(request: Request):
    auth.store.delete_site(request.state.user['id'], '*')
    if request.state.user['id'] == ADMIN_ID:
        Path(COOKIE_FILE).unlink(missing_ok=True)
    return {"success": True, "message": "已刪除個人匯入的 cookies"}

@app.post("/api/upload-cookie")
async def upload_cookie(request: Request, file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".txt"):
        raise HTTPException(400, "請上傳 Netscape 格式的 .txt 檔案")
    content = await file.read(2 * 1024 * 1024 + 1)
    if len(content) > 2 * 1024 * 1024:
        raise HTTPException(400, "Cookie 檔案上限為 2 MB")
    import tempfile
    from http.cookiejar import LoadError
    from yt_dlp.cookies import YoutubeDLCookieJar
    fd, path = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".txt")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
        count = validate_cookie_file(path)
        jar = YoutubeDLCookieJar(path)
        jar.load(ignore_discard=True, ignore_expires=True)
        cookies = [{"name": cookie.name, "value": cookie.value, "domain": cookie.domain, "path": cookie.path,
                    "expires": cookie.expires if cookie.expires else -1, "secure": cookie.secure,
                    "httpOnly": cookie.has_nonstandard_attr('HttpOnly'), "sameSite": "Lax"} for cookie in jar]
        auth.store.save_site(request.state.user['id'], '*', '個人匯入檔案', '', cookies)
        return {"success": True, "message": f"已儲存 {count} 筆個人 cookies，之後解析與下載自動套用"}
    except LoadError:
        raise HTTPException(400, "無效的 Cookie 檔案，請使用 Netscape cookies.txt 格式")
    except OSError:
        raise HTTPException(500, "無法儲存 cookies，請檢查儲存空間")
    except Exception:
        raise HTTPException(400, "無效的 Cookie 檔案，請使用 Netscape cookies.txt 格式")
    finally:
        Path(path).unlink(missing_ok=True)

@app.post("/api/update-ytdlp")
async def update_ytdlp(background_tasks: BackgroundTasks):
    global update_in_progress
    if update_in_progress or active_tasks or any(task.state == TaskState.QUEUED for task in tasks.values()):
        raise HTTPException(409, "請等下載任務完成後再更新")
    update_in_progress = True
    restart_pending = False
    process = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp[default]",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
        if process.returncode:
            raise RuntimeError(stderr.decode(errors="replace"))
        output = stdout.decode(errors="replace")
        if "Successfully installed" in output:
            async def restart_server():
                await asyncio.sleep(2)
                logger.info("yt-dlp updated, restarting server...")
                os._exit(0)
            background_tasks.add_task(restart_server)
            restart_pending = True
            return {"success": True, "output": output, "new_version": "更新成功，正在重新啟動", "require_restart": True}
        version = getattr(yt_dlp.version, "__version__", "未知版本")
        return {"success": True, "output": output, "new_version": f"已是最新版本 ({version})", "require_restart": False}
    except asyncio.TimeoutError:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        raise HTTPException(504, "更新逾時，請稍後重試")
    except Exception as exc:
        raise HTTPException(500, str(exc))
    finally:
        if not restart_pending:
            update_in_progress = False

install_account_routes(app, auth, broadcast_state)
install_site_routes(app, auth, lambda: COOKIE_FILE)
install_archive_routes(app, lambda: tasks)

# Mount frontend
app.mount("/", StaticFiles(directory=str(Path(__file__).parent / "frontend"), html=True), name="frontend")
