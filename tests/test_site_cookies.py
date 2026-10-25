import functools
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from authentication import AUTH_TOKEN
from site_cookies import site_for_url
from user_store import ADMIN_ID


HEADERS = {'Authorization': 'Bearer ' + AUTH_TOKEN}
PASSWORD = 'test-cookie-account-password'


def login(client, username):
    response = client.post('/api/auth/login', json={'username': username, 'password': PASSWORD})
    assert response.status_code == 200


@pytest.fixture
def cookie_video(media_server):
    _, folder = media_server

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if 'site_session=authenticated' not in self.headers.get('Cookie', ''):
                self.send_error(403)
                return
            super().do_GET()

    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(folder)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/source.mp4'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_imported_cookies_download_protected_media_and_remain_private(cookie_video):
    alice = main.auth.store.create_user('alice', 'Alice', PASSWORD)
    main.auth.store.create_user('bob', 'Bob', PASSWORD)
    cookie_file = '# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t2147483647\tsite_session\tauthenticated\n'
    with TestClient(main.app) as client:
        login(client, 'alice')
        blocked = client.post('/api/parse', json={'url': cookie_video})
        assert blocked.status_code == 400 and blocked.json()['login_required']
        imported = client.post('/api/upload-cookie', files={'file': ('cookies.txt', cookie_file)})
        assert imported.status_code == 200
        assert not Path(main.COOKIE_FILE).exists()
        assert b'authenticated' not in Path(main.auth.store.path).read_bytes()
        listed = client.get('/api/sites').json()
        assert len(listed['sites']) == 1
        assert 'cookies' not in listed['sites'][0]
        assert 'authenticated' not in str(listed)
        parsed = client.post('/api/parse', json={'url': cookie_video})
        assert parsed.status_code == 200 and parsed.json()['has_video']
        started = client.post('/api/download', json={'url': cookie_video, 'format_id': 'best'})
        assert started.status_code == 200
        task_id = started.json()['task_id']
        deadline = time.monotonic() + 15
        while main.tasks[task_id].state not in ('COMPLETED', 'ERROR'):
            assert time.monotonic() < deadline
            time.sleep(.02)
        assert main.tasks[task_id].state == 'COMPLETED', main.tasks[task_id].error_msg
        assert client.get('/api/download/' + task_id).status_code == 200
        assert main.auth.store.quota(alice['id'])['used'] == 1
        assert client.post('/api/auth/logout').status_code == 200
        login(client, 'bob')
        assert client.get('/api/sites').json()['sites'] == []
        assert not client.get('/api/cookie-status').json()['exists']
        assert client.get('/api/download/' + task_id).status_code == 404
        assert client.post('/api/parse', json={'url': cookie_video}).status_code == 400
        # Removing Bob's import does not remove Alice's record.
        assert client.delete('/api/sites/127.0.0.1').status_code == 200
        assert main.auth.store.site(alice['id'], '127.0.0.1')
        login(client, 'alice')
        assert client.delete('/api/sites/127.0.0.1').status_code == 200
        assert client.post('/api/parse', json={'url': cookie_video}).status_code == 400


def test_existing_site_cookies_remain_available_and_can_be_removed():
    alice = main.auth.store.create_user('alice', 'Alice', PASSWORD)
    cookie = {'name': 'session', 'value': 'old-private-value', 'domain': '.youtube.com', 'path': '/', 'expires': -1}
    main.auth.store.save_site(alice['id'], 'youtube', 'YouTube', '', [cookie])
    with TestClient(main.app) as client:
        login(client, 'alice')
        listed = client.get('/api/sites').json()['sites']
        assert listed[0]['site_key'] == 'youtube' and 'cookies' not in listed[0]
        with main.private_media(alice['id'], 'https://www.youtube.com/watch?v=example', {}) as ydl:
            assert list(ydl.cookiejar)[0].value == 'old-private-value'
        assert client.delete('/api/sites/youtube').status_code == 200
        assert main.auth.store.site(alice['id'], 'youtube') is None


def test_remote_browser_routes_are_removed():
    assert not any(getattr(route, 'path', '').startswith('/api/site-logins') for route in main.app.routes)
    with TestClient(main.app) as client:
        for method, path in (
            ('post', '/api/site-logins'),
            ('get', '/api/site-logins/unused/frame'),
            ('post', '/api/site-logins/unused/input'),
            ('post', '/api/site-logins/unused/complete'),
            ('delete', '/api/site-logins/unused'),
        ):
            # The root static mount responds 405 for unsupported HTTP methods.
            assert getattr(client, method)(path, headers=HEADERS).status_code in (404, 405)
        assert 'interactive_enabled' not in client.get('/api/sites', headers=HEADERS).json()
        Path(main.COOKIE_FILE).write_text('legacy-private-data')
        assert client.delete('/api/sites/*', headers=HEADERS).status_code == 200
        assert not Path(main.COOKIE_FILE).exists()


def test_site_matching_preserves_cookie_scope():
    assert site_for_url('https://youtu.be/example')['key'] == 'youtube'
    assert site_for_url('https://www.youtube.com.evil.example/video')['key'] != 'youtube'
    assert site_for_url('https://video.example.co.uk/watch')['key'] == 'example.co.uk'
