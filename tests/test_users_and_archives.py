import io
import sqlite3
import threading
import uuid
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import main
from authentication import AUTH_TOKEN
from media import SourceOptions
from user_store import ADMIN_ID, QuotaExceeded, StoreError, UserStore

HEADERS = {'Authorization': 'Bearer ' + AUTH_TOKEN}
PASSWORD = 'a-long-test-password'


def user(name='alice', **kwargs):
    return main.auth.store.create_user(name, name.capitalize(), PASSWORD, **kwargs)


def login(client, name='alice'):
    response = client.post('/api/auth/login', json={'username': name, 'password': PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def test_admin_create_accounts_and_web_policy():
    with TestClient(main.app) as client:
        response = client.post('/api/admin/users', headers=HEADERS, json={'username': 'alice', 'name': 'Alice', 'password': PASSWORD})
        assert response.status_code == 200, response.text
        alice = response.json()['user']
        assert 'password_hash' not in alice and alice['daily_limit'] is None
        assert client.post('/api/admin/users', headers=HEADERS, json={'username': 'ALICE', 'name': 'Other', 'password': PASSWORD}).status_code == 409
        assert client.put('/api/admin/policy', headers=HEADERS, json={'default_daily_limit': 7}).json()['default_daily_limit'] == 7
        assert main.auth.store.quota(alice['id'])['limit'] == 7
        changed = client.patch('/api/admin/users/' + alice['id'], headers=HEADERS, json={'daily_limit': 2})
        assert changed.status_code == 200
        assert main.auth.store.quota(alice['id'])['limit'] == 2
        assert client.patch('/api/admin/users/' + alice['id'], headers=HEADERS, json={'daily_limit': None}).status_code == 200
        assert main.auth.store.quota(alice['id'])['limit'] == 7
        assert client.patch('/api/admin/users/' + ADMIN_ID, headers=HEADERS, json={'role': 'user'}).status_code == 400
        assert client.patch('/api/admin/users/' + ADMIN_ID, headers=HEADERS, json={'disabled': True}).status_code == 400
        identity = login(client)
        assert identity['user']['role'] == 'user' and identity['quota']['limit'] == 7
        for path, method in [('/api/admin/users', 'get'), ('/api/config', 'post'), ('/api/update-ytdlp', 'post')]:
            response = client.post(path, json={}) if method == 'post' else client.get(path)
            assert response.status_code == 403


def test_password_hashes_disable_reset_and_roles(tmp_path):
    alice = user()
    store = main.auth.store
    assert store.authenticate('ALICE', PASSWORD)['id'] == alice['id']
    assert store.authenticate('alice', 'incorrect') is None
    with sqlite3.connect(store.path) as db:
        stored = db.execute('SELECT password_hash FROM users WHERE id=?', (alice['id'],)).fetchone()[0]
    assert PASSWORD not in stored and stored.startswith('scrypt$')
    with TestClient(main.app) as client:
        login(client)
        assert client.get('/api/auth/me').status_code == 200
        store.update_user(alice['id'], {'disabled': True})
        assert client.get('/api/auth/me').status_code == 401
        store.update_user(alice['id'], {'disabled': False, 'role': 'admin'})
        assert client.get('/api/auth/me').status_code == 401
        login(client)
        assert client.get('/api/auth/me').json()['user']['role'] == 'admin'
        store.update_user(alice['id'], {'password': 'replacement-password'})
        assert client.get('/api/auth/me').status_code == 401
        assert store.authenticate('alice', PASSWORD) is None
        assert store.authenticate('alice', 'replacement-password')


def test_sso_subjects_are_bound_to_issuer_and_explicit_admins_can_recover():
    store = main.auth.store
    first = store.oidc_user('same-sub', 'First', issuer='https://one.example')
    other = store.oidc_user('same-sub', 'Other', issuer='https://two.example')
    assert first['id'] != other['id'] and first['role'] == 'user'
    promoted = store.oidc_user('same-sub', 'First', is_admin=True, issuer='https://one.example')
    assert promoted['id'] == first['id'] and promoted['role'] == 'admin'
    assert promoted['oidc_sub'] == 'same-sub' and promoted['oidc_issuer'] == 'https://one.example'


def test_default_five_override_zero_and_unlimited_admin():
    alice, bob, admin = user(), user('bob', daily_limit=2), user('otheradmin', role='admin', daily_limit=0)
    store = main.auth.store
    assert store.quota(alice['id'])['limit'] == 5
    assert store.quota(bob['id'])['limit'] == 2
    assert store.quota(admin['id'])['limit'] is None
    store.set_policy(0)
    assert store.quota(alice['id'])['remaining'] == 0
    assert store.quota(bob['id'])['remaining'] == 2
    store.reserve(admin['id'], [str(uuid.uuid4()) for _ in range(100)])
    assert store.quota(admin['id'])['remaining'] is None


def test_atomic_quota_reservations_and_failure_refund():
    alice = user()
    store = main.auth.store
    barrier = threading.Barrier(10)
    def attempt(_):
        task_id = str(uuid.uuid4())
        barrier.wait()
        try:
            store.reserve(alice['id'], [task_id])
            return task_id
        except QuotaExceeded:
            return None
    with ThreadPoolExecutor(max_workers=10) as pool:
        accepted = [value for value in pool.map(attempt, range(10)) if value]
    assert len(accepted) == 5
    assert store.quota(alice['id'])['remaining'] == 0
    store.settle(accepted[0], False)
    assert store.quota(alice['id'])['remaining'] == 1
    store.reserve(alice['id'], [accepted[0]])  # A failed retry reserves again.
    store.settle(accepted[0], True)
    assert store.quota(alice['id'])['used'] == 1
    with pytest.raises(StoreError):
        store.reserve(alice['id'], [accepted[0]])
    same_db = UserStore(store.path, main.auth.secret)
    assert same_db.quota(alice['id'])['used'] == 1
    same_db.release_orphaned()
    assert same_db.quota(alice['id'])['used'] == 1 and same_db.quota(alice['id'])['remaining'] == 4


def test_batch_reservation_is_all_or_nothing():
    alice = user(daily_limit=1)
    store = main.auth.store
    with pytest.raises(QuotaExceeded):
        store.reserve(alice['id'], ['one', 'two'])
    assert store.quota(alice['id'])['reserved'] == 0
    store.reserve(alice['id'], ['one'])
    assert store.quota(alice['id'])['reserved'] == 1


def test_midnight_resets_completed_but_keeps_pending(monkeypatch):
    alice = user()
    store = main.auth.store
    day = ['2026-10-25']
    monkeypatch.setattr(store, 'today', lambda: day[0])
    store.reserve(alice['id'], ['yesterday', 'overnight'])
    store.settle('yesterday', True)
    assert store.quota(alice['id'])['used'] == 1
    day[0] = '2026-10-26'
    quota = store.quota(alice['id'])
    assert quota['used'] == 0 and quota['reserved'] == 1 and quota['remaining'] == 4
    store.reserve(alice['id'], ['today1', 'today2', 'today3', 'today4'])
    store.settle('overnight', True)
    assert store.quota(alice['id'])['used'] == 1 and store.quota(alice['id'])['remaining'] == 0


def test_daily_boundary_uses_taipei(monkeypatch):
    from datetime import datetime, timezone
    import user_store
    real_datetime = datetime
    class FixedDate:
        @classmethod
        def now(cls, zone):
            return real_datetime(2026, 10, 25, 16, 0, tzinfo=timezone.utc).astimezone(zone)
    monkeypatch.setattr(user_store, 'datetime', FixedDate)
    assert main.auth.store.today() == '2026-10-26'


def task(tmp_path, title, owner_id, name=None, state='COMPLETED'):
    task_id = str(uuid.uuid4())
    record = main.DownloadTask(task_id, 'https://example.com/video', '', False, title, owner_id=owner_id)
    output = tmp_path / (name or (task_id + '.mp4'))
    output.write_bytes(('content:' + task_id).encode())
    record.file_path, record.state = str(output), state
    main.tasks[task_id] = record
    return record


def test_owned_tasks_zip_unique_names_and_no_quota_charge(tmp_path):
    alice, bob = user(), user('bob')
    with TestClient(main.app) as client:
        one = task(tmp_path, '中文影片', alice['id'])
        two = task(tmp_path, '中文影片', alice['id'])
        odd = task(tmp_path, '../../CON\\bad:name', alice['id'])
        incomplete = task(tmp_path, 'not completed', alice['id'], state='ERROR')
        other = task(tmp_path, 'Private Bob', bob['id'])
        login(client)
        for path, method in [('/api/download/' + other.task_id, 'get'), ('/api/task/' + other.task_id, 'delete'), ('/api/task/' + other.task_id + '/retry', 'post')]:
            assert getattr(client, method)(path).status_code == 404
        assert client.get('/api/download/' + one.task_id).content == Path(one.file_path).read_bytes()
        before = main.auth.store.quota(alice['id'])
        response = client.get('/api/downloads/archive')
        assert response.status_code == 200 and response.headers['content-type'] == 'application/zip'
        assert int(response.headers['content-length']) == len(response.content)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            assert len(archive.namelist()) == 3
            assert '中文影片.mp4' in archive.namelist() and '中文影片 (2).mp4' in archive.namelist()
            assert all('/' not in name and '\\' not in name for name in archive.namelist())
            assert set(archive.read(name) for name in archive.namelist()) == {Path(record.file_path).read_bytes() for record in (one, two, odd)}
        assert all(getattr(record, 'archive_readers', 0) == 0 for record in (one, two, odd))
        assert main.auth.store.quota(alice['id']) == before
        assert client.delete('/api/task/' + one.task_id).status_code == 200
        assert main.auth.store.quota(alice['id']) == before


def test_websocket_is_private_and_quotas_are_per_user(tmp_path):
    alice, bob = user(), user('bob')
    with TestClient(main.app) as client:
        mine = task(tmp_path, 'Mine', alice['id'])
        task(tmp_path, 'Other', bob['id'])
        login(client)
        with client.websocket_connect('/ws', headers={'Origin': 'http://testserver'}) as ws:
            ws.send_json({})
            state = ws.receive_json()['data']
            assert [record['task_id'] for record in state['tasks']] == [mine.task_id]
            assert state['user']['id'] == alice['id']
            assert state['quota']['limit'] == 5
            assert 'cookie_profile' not in state['config']
            main.auth.store.update_user(alice['id'], {'disabled': True})
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 1008


def test_queued_delete_refunds_and_completed_delete_does_not(tmp_path):
    alice = user()
    with TestClient(main.app) as client:
        queued = task(tmp_path, 'queued', alice['id'], state='QUEUED')
        done = task(tmp_path, 'done', alice['id'])
        main.auth.store.reserve(alice['id'], [queued.task_id, done.task_id])
        main.auth.store.settle(done.task_id, True)
        login(client)
        assert client.delete('/api/task/' + queued.task_id).status_code == 200
        assert main.auth.store.quota(alice['id'])['reserved'] == 0
        assert client.delete('/api/task/' + done.task_id).status_code == 200
        assert main.auth.store.quota(alice['id'])['used'] == 1


def test_zip_empty_and_missing_file_are_clear_errors(tmp_path):
    user()
    with TestClient(main.app) as client:
        login(client)
        assert client.get('/api/downloads/archive').status_code == 400
        record = task(tmp_path, 'gone', main.auth.store.authenticate('alice', PASSWORD)['id'])
        Path(record.file_path).unlink()
        assert client.get('/api/downloads/archive').status_code == 409


def test_normal_users_never_use_shared_or_other_users_cookies(monkeypatch):
    from contextlib import contextmanager
    alice, bob = user(), user('bob')
    secret_cookie = {'name': 'private', 'value': 'secret', 'domain': '.youtube.com', 'path': '/', 'expires': -1}
    main.auth.store.save_site(bob['id'], 'youtube', 'YouTube', 'https://youtube.com', [secret_cookie])
    captured = {}
    @contextmanager
    def capture(config, cookie_file, options, cookies=None):
        captured['cookies'] = cookies
        yield None
    monkeypatch.setattr(main, 'youtube_dl', capture)
    with main.private_media(alice['id'], 'https://www.youtube.com/watch?v=1', {}):
        pass
    assert captured['cookies'] == []
    with main.private_media(bob['id'], 'https://www.youtube.com/watch?v=1', {}):
        pass
    assert captured['cookies'][0]['value'] == 'secret'
    assert b'secret' not in Path(main.auth.store.path).read_bytes()
    with TestClient(main.app) as client:
        login(client)
        assert client.get('/api/sites').json()['sites'] == []


def test_api_batch_reserves_five_and_sixth_is_rejected(monkeypatch):
    import time
    alice = user()
    gate = threading.Event()
    def download(record):
        assert gate.wait(10)
        record.state = 'COMPLETED'
    monkeypatch.setattr(main, 'perform_download', download)
    try:
        with TestClient(main.app) as client:
            login(client)
            items = [{'url': f'https://example.com/{index}', 'title': str(index)} for index in range(6)]
            too_many = client.post('/api/download/batch', json={'items': items})
            assert too_many.status_code == 429 and not main.tasks
            accepted = client.post('/api/download/batch', json={'items': items[:5]})
            assert accepted.status_code == 200 and len(main.tasks) == 5
            assert main.auth.store.quota(alice['id'])['reserved'] == 5
            assert client.post('/api/download', json={'url': 'https://example.com/six'}).status_code == 429
            gate.set()
            deadline = time.monotonic() + 5
            while main.active_tasks or any(record.state == 'QUEUED' for record in main.tasks.values()):
                assert time.monotonic() < deadline
                time.sleep(.02)
            quota = main.auth.store.quota(alice['id'])
            assert quota['used'] == 5 and quota['reserved'] == 0 and quota['remaining'] == 0
    finally:
        gate.set()


def test_failed_worker_refunds_and_retry_counts_once(monkeypatch):
    import time
    alice = user(daily_limit=1)
    attempts = {}
    def download(record):
        attempts[record.task_id] = attempts.get(record.task_id, 0) + 1
        if attempts[record.task_id] == 1:
            raise RuntimeError('test failure')
        record.state = 'COMPLETED'
    monkeypatch.setattr(main, 'perform_download', download)
    with TestClient(main.app) as client:
        login(client)
        result = client.post('/api/download', json={'url': 'https://example.com/video'})
        assert result.status_code == 200
        task_id = result.json()['task_id']
        deadline = time.monotonic() + 5
        while main.tasks[task_id].state != 'ERROR' or main.active_tasks:
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert main.auth.store.quota(alice['id'])['remaining'] == 1
        assert client.post('/api/task/' + task_id + '/retry').status_code == 200
        while main.tasks[task_id].state != 'COMPLETED' or main.active_tasks:
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert main.auth.store.quota(alice['id'])['used'] == 1
        assert main.auth.store.quota(alice['id'])['reserved'] == 0


def test_password_reset_api_revokes_current_session():
    with TestClient(main.app) as client:
        client.post('/api/auth/login', json={'token': AUTH_TOKEN})
        response = client.patch('/api/admin/users/' + ADMIN_ID, json={'password': 'new-administrator-password'})
        assert response.status_code == 200 and response.json()['reauth_required']
        assert client.get('/api/auth/me').status_code == 401
        assert client.post('/api/auth/login', json={'username': 'admin', 'password': 'new-administrator-password'}).status_code == 200


def test_sso_only_cannot_remove_last_usable_admin(monkeypatch, tmp_path):
    monkeypatch.setenv('AUTH_MODE', 'sso')
    store = UserStore(tmp_path / 'sso-only.sqlite3', main.auth.secret)
    admin = store.oidc_user('administrator', 'SSO admin', is_admin=True, issuer='https://sso.example')
    with pytest.raises(StoreError):
        store.update_user(admin['id'], {'role': 'user'})


def test_imported_host_only_cookie_is_used_on_its_exact_host(monkeypatch):
    from contextlib import contextmanager
    alice = user()
    cookie = {'name': 'host-session', 'value': 'only-this-host', 'domain': 'video.example.com', 'path': '/', 'expires': -1}
    main.auth.store.save_site(alice['id'], '*', 'import', '', [cookie])
    captured = {}
    @contextmanager
    def capture(config, cookie_file, options, cookies=None):
        captured['cookies'] = cookies
        yield None
    monkeypatch.setattr(main, 'youtube_dl', capture)
    with main.private_media(alice['id'], 'https://video.example.com/watch', {}):
        pass
    assert captured['cookies'][0]['name'] == 'host-session'


def test_zip_connection_failure_releases_even_before_body_starts():
    import asyncio
    from archives import ArchiveResponse
    from starlette.requests import ClientDisconnect
    released = []
    async def content():
        yield b'zip-data'
    async def disconnected_send(message):
        raise OSError('client disconnected before headers')
    async def receive():
        return {'type': 'http.disconnect'}
    response = ArchiveResponse(content(), on_close=lambda: released.append(True))
    with pytest.raises(ClientDisconnect):
        asyncio.run(response({'type': 'http', 'method': 'GET', 'asgi': {'spec_version': '2.4'}}, receive, disconnected_send))
    assert released == [True]
