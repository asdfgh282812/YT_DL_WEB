import asyncio
import os
import subprocess
import sys
import threading
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import authentication
import main
from media import youtube_dl


def test_unconfigured_token_has_no_public_default():
    environment = {key: value for key, value in os.environ.items() if key != 'AUTH_TOKEN'}
    result = subprocess.run(
        [sys.executable, '-c', 'from authentication import AUTH_TOKEN; assert AUTH_TOKEN == ""'],
        env=environment, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('token', ['', '   ', 'your_secure_password_here'])
def test_empty_and_example_tokens_cannot_authenticate(monkeypatch, token):
    monkeypatch.setattr(authentication, 'AUTH_TOKEN', token)
    with TestClient(main.app) as client:
        assert client.post('/api/auth/login', json={'token': token}).status_code == 401
        assert client.get('/api/version', headers={'Authorization': 'Bearer ' + token}).status_code == 401


def test_password_rate_limit_normalizes_username(monkeypatch):
    monkeypatch.setattr(main.auth.store, 'authenticate', lambda *args: None)
    with TestClient(main.app) as client:
        for index in range(10):
            response = client.post('/api/auth/login', json={
                'username': ' ' * index + 'ALICE', 'password': 'incorrect-password',
            })
            assert response.status_code == 401
        assert client.post('/api/auth/login', json={
            'username': 'alice ', 'password': 'incorrect-password',
        }).status_code == 429


def test_token_rate_limit_ignores_supplied_username():
    with TestClient(main.app) as client:
        for index in range(10):
            assert client.post('/api/auth/login', json={
                'token': 'incorrect-token', 'username': str(index),
            }).status_code == 401
        assert client.post('/api/auth/login', json={
            'token': 'incorrect-token', 'username': 'another-account',
        }).status_code == 429


def test_disabling_token_also_rejects_existing_token_sessions(monkeypatch):
    with TestClient(main.app) as client:
        assert client.post('/api/auth/login', json={'token': authentication.AUTH_TOKEN}).status_code == 200
        monkeypatch.setattr(authentication, 'AUTH_TOKEN', '')
        assert client.get('/api/auth/me').status_code == 401


def test_source_rate_limit_prevents_rotating_account_names(monkeypatch):
    monkeypatch.setattr(main.auth.store, 'authenticate', lambda *args: None)
    with TestClient(main.app) as client:
        for index in range(50):
            assert client.post('/api/auth/login', json={
                'username': f'account-{index}', 'password': 'incorrect-password',
            }).status_code == 401
        assert client.post('/api/auth/login', json={
            'username': 'next-account', 'password': 'incorrect-password',
        }).status_code == 429


def test_login_rate_limit_expires_after_five_minutes(monkeypatch):
    now = [1000]
    monkeypatch.setattr(authentication.time, 'monotonic', lambda: now[0])
    # Exercise the limiter directly to avoid changing the HTTP client's clocks.
    request = SimpleNamespace(client=SimpleNamespace(host='test-client'))
    credentials = authentication.TokenLogin(username='alice', password='incorrect-password')
    for _ in range(10):
        main.auth.reserve_login_attempt(request, credentials)
    with pytest.raises(authentication.HTTPException) as blocked:
        main.auth.reserve_login_attempt(request, credentials)
    assert blocked.value.status_code == 429
    now[0] += 300
    main.auth.reserve_login_attempt(request, credentials)


def test_full_limiter_does_not_clear_active_limits():
    credentials = authentication.TokenLogin(username='alice', password='incorrect-password')
    for index in range(5000):
        main.auth.reserve_login_attempt(SimpleNamespace(client=SimpleNamespace(host=f'client-{index}')), credentials)
    with pytest.raises(authentication.HTTPException) as blocked:
        main.auth.reserve_login_attempt(SimpleNamespace(client=SimpleNamespace(host='new-client')), credentials)
    assert blocked.value.status_code == 429
    existing = SimpleNamespace(client=SimpleNamespace(host='client-0'))
    for _ in range(9):
        main.auth.reserve_login_attempt(existing, credentials)
    with pytest.raises(authentication.HTTPException) as blocked:
        main.auth.reserve_login_attempt(existing, credentials)
    assert blocked.value.status_code == 429


def test_successful_logins_do_not_exhaust_failure_budget(monkeypatch):
    account = main.auth.store.create_user('alice', 'Alice', 'a-valid-long-password')
    monkeypatch.setattr(main.auth.store, 'authenticate', lambda *args: dict(account))
    with TestClient(main.app) as client:
        for _ in range(12):
            assert client.post('/api/auth/login', json={
                'username': 'alice', 'password': 'a-valid-long-password',
            }).status_code == 200


def test_parallel_password_attempts_cannot_bypass_rate_limit(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    calls = 0

    def check_password(*args):
        nonlocal calls
        with lock:
            calls += 1
            if calls == 10:
                entered.set()
        assert release.wait(10)
        return None

    monkeypatch.setattr(main.auth.store, 'authenticate', check_password)

    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://testserver') as client:
            pending = [asyncio.create_task(client.post('/api/auth/login', json={
                'username': 'alice', 'password': 'incorrect-password',
            })) for _ in range(20)]
            try:
                assert await asyncio.to_thread(entered.wait, 5)
            finally:
                release.set()
            responses = await asyncio.gather(*pending)
        assert calls == 10
        assert sorted(response.status_code for response in responses) == [401] * 10 + [429] * 10

    asyncio.run(exercise())


@pytest.mark.parametrize('source', ['personal', 'legacy'])
def test_host_only_cookies_never_reach_subdomains(tmp_path, source):
    cookie = {'name': 'session', 'value': 'private-cookie', 'domain': 'video.example.com',
              'path': '/private', 'secure': True, 'expires': -1}
    path = tmp_path / 'cookies.txt'
    path.write_text('# Netscape HTTP Cookie File\nvideo.example.com\tFALSE\t/private\tTRUE\t0\tsession\tprivate-cookie\n')
    kwargs = {'cookies': [cookie]} if source == 'personal' else {}
    with youtube_dl({'cookie_source': 'file'}, str(path), **kwargs) as ydl:
        assert ydl.cookiejar.get_cookie_header('https://video.example.com/private/watch') == 'session=private-cookie'
        for url in ('https://untrusted.video.example.com/private/watch',
                    'http://video.example.com/private/watch',
                    'https://video.example.com/other/watch'):
            assert not ydl.cookiejar.get_cookie_header(url)


def test_domain_cookies_still_reach_authorized_subdomains(tmp_path):
    cookie = {'name': 'session', 'value': 'domain-cookie', 'domain': '.example.com',
              'path': '/', 'expires': -1}
    with youtube_dl({'cookie_source': 'none'}, str(tmp_path / 'unused.txt'), cookies=[cookie]) as ydl:
        assert ydl.cookiejar.get_cookie_header('https://video.example.com/watch') == 'session=domain-cookie'
        assert not ydl.cookiejar.get_cookie_header('https://example.com.attacker.invalid/watch')


def test_outbound_requests_and_redirects_preserve_host_only_cookies(tmp_path, monkeypatch):
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            observed.append((self.headers['Host'].split(':')[0], self.headers.get('Cookie', '')))
            if self.path == '/start':
                self.send_response(302)
                self.send_header('Location', f'http://untrusted.video.example.com:{self.server.server_port}/end')
                self.send_header('Set-Cookie', 'refreshed=host-only; Path=/')
            else:
                self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()

    original_lookup = socket.getaddrinfo

    def local_lookup(host, *args, **kwargs):
        if host in ('video.example.com', 'untrusted.video.example.com'):
            host = '127.0.0.1'
        return original_lookup(host, *args, **kwargs)

    monkeypatch.setattr(socket, 'getaddrinfo', local_lookup)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cookie = {'name': 'session', 'value': 'private-cookie', 'domain': 'video.example.com',
                  'path': '/', 'expires': -1}
        with youtube_dl({'cookie_source': 'none'}, str(tmp_path / 'unused.txt'), {'proxy': ''}, cookies=[cookie]) as ydl:
            for host, path in [('video.example.com', '/start'), ('video.example.com', '/end'),
                               ('untrusted.video.example.com', '/end')]:
                with ydl.urlopen(f'http://{host}:{server.server_port}{path}') as response:
                    response.read()
        assert observed == [
            ('video.example.com', 'session=private-cookie'),
            ('untrusted.video.example.com', ''),
            ('video.example.com', 'session=private-cookie; refreshed=host-only'),
            ('untrusted.video.example.com', ''),
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
