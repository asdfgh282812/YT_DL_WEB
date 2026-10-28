import functools
import json
import os
import shutil
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Tests always configure their own token and never depend on a production secret.
os.environ['AUTH_TOKEN'] = 'tests-only-administrator-token'
import main
from media import Settings
from user_store import UserStore


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    folder = tmp_path / 'downloads'
    folder.mkdir()
    monkeypatch.setattr(main, 'TEMP_DIR', str(folder))
    monkeypatch.setattr(main, 'CONFIG_FILE', str(tmp_path / 'config.json'))
    monkeypatch.setattr(main, 'CONFIG_DIR', str(tmp_path))
    monkeypatch.setattr(main, 'COOKIE_FILE', str(tmp_path / 'cookies.txt'))
    monkeypatch.setattr(main, 'tasks', {})
    monkeypatch.setattr(main, 'update_in_progress', False)
    monkeypatch.setattr(main, 'config_data', Settings(cookie_source='none').model_dump())
    monkeypatch.setattr(main.auth, 'store', UserStore(tmp_path / 'users.sqlite3', main.auth.secret))
    main.auth.login_failures.clear()
    yield


@pytest.fixture(scope='session')
def media_server(tmp_path_factory):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg is required for integration tests')
    folder = tmp_path_factory.mktemp('streams')
    source = folder / 'source.mp4'
    def run(*args):
        subprocess.run(['ffmpeg', '-v', 'error', '-y', *map(str, args)], check=True, capture_output=True)
    run('-f', 'lavfi', '-i', 'testsrc=size=160x90:rate=10', '-f', 'lavfi', '-i', 'sine=frequency=440',
        '-t', '2', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', source)
    run('-i', source, '-c', 'copy', '-f', 'hls', '-hls_time', '1', '-hls_list_size', '0', folder / 'stream.m3u8')
    run('-i', source, '-vn', '-c:a', 'aac', '-f', 'hls', '-hls_list_size', '0', folder / 'audio.m3u8')
    run('-i', source, '-c', 'copy', '-f', 'dash', folder / 'stream.mpd')
    protected = folder / 'protected'
    shutil.copytree(folder, protected)

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            if self.path.startswith('/protected/') and (
                self.headers.get('Referer') != 'https://player.example/watch' or self.headers.get('User-Agent') != 'DownloaderTest/1.0'
            ):
                self.send_error(403)
                return
            super().do_GET()

    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(folder)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}', folder
    server.shutdown()
    server.server_close()
    thread.join()
