import asyncio
import json
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import main
from authentication import AUTH_TOKEN
from media import describe_media, download_options

HEADERS = {'Authorization': 'Bearer ' + AUTH_TOKEN}


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('Condition was not reached')


def add(client, name):
    response = client.post('/api/download', headers=HEADERS, json={'url': 'https://example.com/' + name, 'title': name})
    assert response.status_code == 200, response.text
    return response.json()['task_id']


def test_unknown_height_video_and_audio_only():
    parsed = describe_media({'formats': [
        {'format_id': 'hls', 'ext': 'mp4', 'vcodec': None, 'acodec': None},
        {'format_id': 'audio', 'ext': 'm4a', 'vcodec': 'none', 'acodec': 'aac'},
        {'format_id': 'drm', 'height': 1080, 'has_drm': True},
    ]})
    assert parsed['has_video']
    assert [f['id'] for f in parsed['video_formats']] == ['hls']
    assert parsed['video_formats'][0]['resolution'] == '自動辨識畫質'
    audio = describe_media({'formats': [{'format_id': 'a', 'vcodec': 'none', 'ext': 'm4a'}]})
    assert not audio['has_video'] and audio['warning']


def test_foreign_playlist_ids_are_not_turned_into_youtube():
    result = describe_media({'extractor': 'OtherSite', 'entries': [
        {'id': 'abc', 'title': 'Unavailable ID'},
        {'url': 'https://other.example/1', 'title': 'Valid'},
    ]})
    assert result['entries'] == [{'url': 'https://other.example/1', 'title': 'Valid'}]


def test_two_downloads_and_live_limit_decrease(monkeypatch):
    gates = {name: threading.Event() for name in ('one', 'two', 'three')}
    started = []
    lock = threading.Lock()
    def download(task):
        with lock:
            started.append(task.title)
        assert gates[task.title].wait(10)
        task.state = main.TaskState.COMPLETED
    monkeypatch.setattr(main, 'perform_download', download)
    try:
        with TestClient(main.app) as client:
            for name in gates:
                add(client, name)
            wait_for(lambda: len(started) == 2)
            assert main.active_tasks == 2 and set(started) == {'one', 'two'}
            assert client.post('/api/config', headers=HEADERS, json={'max_concurrent_downloads': 1}).status_code == 200
            gates['one'].set()
            wait_for(lambda: main.active_tasks == 1)
            assert set(started) == {'one', 'two'}
            gates['two'].set()
            wait_for(lambda: len(started) == 3)
            assert main.active_tasks == 1
            gates['three'].set()
            wait_for(lambda: main.active_tasks == 0)
            assert main.task_queue._unfinished_tasks == 0
    finally:
        for gate in gates.values():
            gate.set()


def test_live_limit_increase_and_failure_releases_slot(monkeypatch):
    gate = threading.Event()
    started = []
    def download(task):
        started.append(task.title)
        if task.title == 'fail':
            raise RuntimeError('simulated download failure')
        assert gate.wait(10)
        task.state = main.TaskState.COMPLETED
    monkeypatch.setattr(main, 'perform_download', download)
    main.config_data['max_concurrent_downloads'] = 1
    try:
        with TestClient(main.app) as client:
            one = add(client, 'one')
            failed = add(client, 'fail')
            add(client, 'two')
            wait_for(lambda: started == ['one'])
            client.post('/api/config', headers=HEADERS, json={'max_concurrent_downloads': 2})
            wait_for(lambda: len(started) == 3)
            assert main.active_tasks == 2 and main.tasks[failed].state == 'ERROR'
            assert client.delete('/api/task/' + one, headers=HEADERS).status_code == 409
            assert client.post('/api/task/' + one + '/retry', headers=HEADERS).status_code == 409
            gate.set()
            wait_for(lambda: main.active_tasks == 0)
            assert client.post('/api/task/' + failed + '/retry', headers=HEADERS).status_code == 200
            wait_for(lambda: started.count('fail') == 2)
            wait_for(lambda: main.active_tasks == 0)
    finally:
        gate.set()


@pytest.mark.parametrize('payload', [{'max_concurrent_downloads': 0}, {'max_concurrent_downloads': 11}, {'concurrent_fragments': 17}])
def test_settings_ranges(payload):
    with TestClient(main.app) as client:
        assert client.post('/api/config', headers=HEADERS, json=payload).status_code == 422


def test_auth_session_websocket_file_and_logout(tmp_path):
    output = tmp_path / 'done.mp4'
    output.write_bytes(b'video')
    with TestClient(main.app) as client:
        assert client.get('/api/version').status_code == 401
        assert client.post('/api/auth/login', json={'token': 'wrong'}).status_code == 401
        assert client.post('/api/auth/login', json={'token': AUTH_TOKEN}).status_code == 200
        assert client.get('/api/version').status_code == 200
        assert 'HttpOnly' in str(client.cookies) or client.cookies.get('media_downloader_session')
        with client.websocket_connect('/ws', headers={'origin': 'http://testserver'}) as ws:
            ws.send_json({})
            assert ws.receive_json()['type'] == 'state_update'
        task = main.DownloadTask('done', 'https://example.com', '', False, '測試')
        task.state, task.file_path = 'COMPLETED', str(output)
        main.tasks['done'] = task
        assert client.get('/api/download/done').content == b'video'
        old_cookie = client.cookies.get('media_downloader_session')
        assert client.post('/api/auth/logout').status_code == 200
        assert client.get('/api/download/done').status_code == 401
        client.cookies.set('media_downloader_session', old_cookie)
        assert client.get('/api/version').status_code == 401


def test_cross_origin_and_url_header_validation():
    with TestClient(main.app) as client:
        assert client.post('/api/auth/login', headers={'Origin': 'https://evil.example'}, json={'token': AUTH_TOKEN}).status_code == 403
        assert client.post('/api/parse', headers=HEADERS, json={'url': 'file:///etc/passwd'}).status_code == 422
        assert client.post('/api/parse', headers=HEADERS, json={'url': 'https://example.com', 'user_agent': 'bad\r\nheader'}).status_code == 422


@pytest.mark.parametrize('message', [{'token': 42}, ['invalid-message']])
def test_invalid_websocket_auth_is_rejected(message):
    with TestClient(main.app) as client:
        with pytest.raises(WebSocketDisconnect) as closed:
            with client.websocket_connect('/ws') as ws:
                ws.send_json(message)
                ws.receive_json()
        assert closed.value.code == 1008


def test_update_cannot_interrupt_downloads(monkeypatch):
    with TestClient(main.app) as client:
        main.active_tasks = 1
        try:
            assert client.post('/api/update-ytdlp', headers=HEADERS).status_code == 409
        finally:
            main.active_tasks = 0
        monkeypatch.setattr(main, 'update_in_progress', True)
        assert client.post('/api/download', headers=HEADERS, json={'url': 'https://example.com/media.mp4'}).status_code == 409
        assert client.get('/api/version', headers=HEADERS).status_code == 200


def test_parse_does_not_block_other_requests(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def parse(req, user_id=None):
        entered.set()
        assert release.wait(10)
        return {'title': 'test'}
    monkeypatch.setattr(main, 'extract_media', parse)
    with TestClient(main.app) as client, ThreadPoolExecutor() as pool:
        pending = pool.submit(client.post, '/api/parse', headers=HEADERS, json={'url': 'https://example.com'})
        try:
            assert entered.wait(3)
            before = time.monotonic()
            assert client.get('/api/version', headers=HEADERS).status_code == 200
            assert time.monotonic() - before < 1
        finally:
            release.set()
        assert pending.result().status_code == 200


@pytest.mark.parametrize('source,output,audio', [
    ('stream.m3u8', 'mp4', False), ('stream.m3u8', 'mkv', False),
    ('stream.m3u8', 'original', False), ('stream.mpd', 'mp4', False),
    ('source.mp4', 'mp4', False), ('audio.m3u8', 'mp3', True),
    ('stream.m3u8', 'm4a', True),
])
def test_actual_media_download(media_server, source, output, audio):
    base, folder = media_server
    result = main.extract_media(main.ParseRequest(url=base + '/' + source))
    assert result['has_video'] != (source == 'audio.m3u8')
    task = main.DownloadTask(str(uuid.uuid4()), base + '/' + source, '', audio, 'fixture', output_format=output)
    main.perform_download(task)
    assert task.state == 'COMPLETED', task.error_msg
    streams = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type', '-of', 'json', task.file_path]))['streams']
    kinds = {s['codec_type'] for s in streams}
    assert kinds == ({'audio'} if audio else {'audio', 'video'})
    if output != 'original':
        assert Path(task.file_path).suffix == '.' + output


def test_explicit_format_with_muxed_audio(media_server):
    base, _ = media_server
    parsed = main.extract_media(main.ParseRequest(url=base + '/stream.m3u8'))
    task = main.DownloadTask(str(uuid.uuid4()), base + '/stream.m3u8', parsed['video_formats'][0]['id'], False, 'explicit')
    main.perform_download(task)
    assert task.state == 'COMPLETED', task.error_msg


def test_headers_passed_to_manifest_and_fragments(media_server):
    base, _ = media_server
    req = main.ParseRequest(url=base + '/protected/stream.m3u8', referer='https://player.example/watch', user_agent='DownloaderTest/1.0')
    assert main.extract_media(req)['has_video']
    task = main.DownloadTask(str(uuid.uuid4()), req.url, '', False, 'protected', source_options=req)
    main.perform_download(task)
    assert task.state == 'COMPLETED', task.error_msg


def test_audio_manifest_cannot_complete_as_video(media_server):
    base, _ = media_server
    task = main.DownloadTask(str(uuid.uuid4()), base + '/audio.m3u8', '', False, 'audio as video')
    main.perform_download(task)
    assert task.state == 'ERROR' and not task.file_path


def test_partial_file_not_completed(monkeypatch):
    from contextlib import contextmanager
    @contextmanager
    def partial(*args, **kwargs):
        class Fake:
            def extract_info(self, *args, **kwargs):
                return {'title': 'partial'}
        yield Fake()
    monkeypatch.setattr(main, 'youtube_dl', partial)
    task = main.DownloadTask(str(uuid.uuid4()), 'https://example.com', '', False, 'partial')
    folder = Path(main.TEMP_DIR) / task.task_id
    folder.mkdir()
    (folder / 'media.mp4.part').write_bytes(b'partial')
    main.perform_download(task)
    assert task.state == 'ERROR' and not task.file_path
