import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from authentication import Authentication


PASSWORD = 'persistent-session-test-password'


@pytest.fixture
def service(tmp_path, monkeypatch):
    for key in ('SESSION_SECRET', 'OIDC_AUTHORITY', 'OIDC_CLIENT_ID', 'OIDC_CLIENT_SECRET'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('SESSION_MAX_AGE', '604800')
    monkeypatch.setenv('SESSION_HTTPS_ONLY', 'false')
    monkeypatch.setenv('PUBLIC_BASE_URL', 'http://testserver')
    monkeypatch.setenv('AUTH_MODE', 'both')

    def build():
        auth = Authentication(tmp_path)
        app = FastAPI()
        auth.install(app)
        return app, auth

    return build


def login(client):
    response = client.post('/api/auth/login', json={'username': 'alice', 'password': PASSWORD})
    assert response.status_code == 200
    return response


def test_login_survives_restart_and_logout_revokes_cookie_replay(service):
    app, auth = service()
    auth.store.create_user('alice', 'Alice', PASSWORD)
    with TestClient(app) as first:
        response = login(first)
        cookie = first.cookies.get('media_downloader_session')
        assert 'Max-Age=604800' in response.headers['set-cookie']
        sid = json.loads(base64.b64decode(cookie.split('.')[0]))['sid']
        with auth.store.connection() as db:
            row = db.execute('SELECT * FROM login_sessions').fetchone()
        assert row['session_hash'] != sid and len(row['session_hash']) == 64
        assert sid.encode() not in Path(auth.store.path).read_bytes()
        restarted_app, restarted_auth = service()
        assert restarted_auth.secret == auth.secret
        with TestClient(restarted_app) as restarted:
            restarted.cookies.update({'media_downloader_session': cookie})
            assert restarted.get('/api/auth/me').json()['user']['username'] == 'alice'
            assert restarted.post('/api/auth/logout').status_code == 200
            assert first.get('/api/auth/me').status_code == 401
            restarted.cookies.update({'media_downloader_session': cookie})
            assert restarted.get('/api/auth/me').status_code == 401


def test_server_deadline_is_enforced_even_with_a_valid_cookie(service):
    app, auth = service()
    auth.store.create_user('alice', 'Alice', PASSWORD)
    with TestClient(app) as client:
        login(client)
        with auth.store.connection(write=True) as db:
            db.execute('UPDATE login_sessions SET expires_at=?', (time.time() - 1,))
        assert client.get('/api/auth/me').status_code == 401


def test_revocation_and_password_reset_apply_across_restarts(service):
    app, auth = service()
    alice = auth.store.create_user('alice', 'Alice', PASSWORD)
    with TestClient(app) as client:
        login(client)
        _, second_auth = service()
        second_auth.revoke_user(alice['id'])
        assert client.get('/api/auth/me').status_code == 401
        login(client)
        second_auth.store.update_user(alice['id'], {'disabled': True})
        assert client.get('/api/auth/me').status_code == 401
        second_auth.store.update_user(alice['id'], {'disabled': False})
        assert client.get('/api/auth/me').status_code == 401
        login(client)
        second_auth.store.update_user(alice['id'], {'password': 'new-persistent-password'})
        assert client.get('/api/auth/me').status_code == 401


def test_login_rotates_session_without_revoking_other_devices(service):
    app, auth = service()
    auth.store.create_user('alice', 'Alice', PASSWORD)
    with TestClient(app) as first, TestClient(app) as other:
        login(first)
        old_cookie = first.cookies.get('media_downloader_session')
        login(other)
        login(first)
        assert first.cookies.get('media_downloader_session') != old_cookie
        assert other.get('/api/auth/me').status_code == 200
        first.cookies.clear()
        first.cookies.update({'media_downloader_session': old_cookie})
        assert first.get('/api/auth/me').status_code == 401


def test_public_origin_handles_bare_host_and_checks_scheme_port(service, monkeypatch):
    monkeypatch.setenv('PUBLIC_BASE_URL', 'download.example.com')
    _, auth = service()
    assert auth.base_url == 'https://download.example.com'
    for origin in ('https://download.example.com', 'https://download.example.com:443'):
        assert auth.origin_allowed(SimpleNamespace(headers={'origin': origin}))
    for origin in ('http://download.example.com', 'https://other.example.com', 'https://download.example.com:1234', 'https://download.example.com:invalid'):
        assert not auth.origin_allowed(SimpleNamespace(headers={'origin': origin}))


def test_configured_session_lifetime_updates_cookie_and_database(service, monkeypatch):
    monkeypatch.setenv('SESSION_MAX_AGE', '3600')
    app, auth = service()
    auth.store.create_user('alice', 'Alice', PASSWORD)
    with TestClient(app) as client:
        response = login(client)
        assert 'Max-Age=3600' in response.headers['set-cookie']
        with auth.store.connection() as db:
            expires_at = db.execute('SELECT expires_at FROM login_sessions').fetchone()['expires_at']
        assert 3590 < expires_at - time.time() <= 3600


def test_sso_only_mode_rejects_password_sessions_from_previous_mode(service, monkeypatch):
    app, auth = service()
    auth.store.create_user('alice', 'Alice', PASSWORD)
    with TestClient(app) as client:
        login(client)
        cookie = client.cookies.get('media_downloader_session')
        monkeypatch.setenv('AUTH_MODE', 'sso')
        monkeypatch.setenv('OIDC_AUTHORITY', 'https://sso.example')
        monkeypatch.setenv('OIDC_CLIENT_ID', 'downloader')
        monkeypatch.setenv('OIDC_CLIENT_SECRET', 'test-secret')
        restarted_app, _ = service()
        with TestClient(restarted_app) as restarted:
            restarted.cookies.update({'media_downloader_session': cookie})
            assert restarted.get('/api/auth/me').status_code == 401
