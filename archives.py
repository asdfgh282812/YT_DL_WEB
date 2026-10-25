"""Stream completed private downloads as one Zip64 archive without duplicate disk files."""
import re
import unicodedata
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import iterate_in_threadpool
from zipstream import ZIP_STORED, ZipStream


class ArchiveResponse(StreamingResponse):
    def __init__(self, *args, on_close, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_close = on_close

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Also release when the connection fails before the first body chunk.
            self.on_close()


def filename(task):
    title = unicodedata.normalize('NFC', task.title)
    title = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', '_', title).strip(' .')[:120].rstrip(' .')
    title = title or task.task_id
    if title.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        title = '_' + title
    return title + Path(task.file_path).suffix


def install_archive_routes(app, tasks):
    @app.get('/api/downloads/archive')
    async def archive(request: Request):
        selected = [task for task in list(tasks().values()) if task.owner_id == request.state.user['id'] and task.state == 'COMPLETED']
        if not selected:
            raise HTTPException(400, '目前沒有已完成的影片可以打包')
        if any(not task.file_path or not Path(task.file_path).is_file() for task in selected):
            raise HTTPException(409, '部分檔案已不存在，請移除失效任務後再下載')
        stream = ZipStream(compress_type=ZIP_STORED, sized=True)
        used = set()
        for task in selected:
            desired = filename(task)
            current = desired
            index = 2
            while current.casefold() in used:
                path = Path(desired)
                current = f'{path.stem} ({index}){path.suffix}'
                index += 1
            used.add(current.casefold())
            stream.add_path(task.file_path, arcname=current)
        for task in selected:
            task.archive_readers = getattr(task, 'archive_readers', 0) + 1
            task.last_accessed = datetime.now()
        released = False

        def release():
            nonlocal released
            if released:
                return
            released = True
            for task in selected:
                task.archive_readers = max(0, task.archive_readers - 1)
                task.last_accessed = datetime.now()

        async def content():
            try:
                async for chunk in iterate_in_threadpool(iter(stream)):
                    yield chunk
            finally:
                release()

        return ArchiveResponse(content(), media_type='application/zip', on_close=release, headers={
            'Content-Disposition': 'attachment; filename="downloads.zip"',
            'Content-Length': str(len(stream)),
            'Cache-Control': 'no-store',
        })
