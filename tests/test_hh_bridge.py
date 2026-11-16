import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import main
from authentication import AUTH_TOKEN
from user_store import QuotaExceeded

SECRET = 'bridge-secret-for-tests-only-0123456789'
BRIDGE = {'Authorization': 'Bearer ' + SECRET}
PASSWORD = 'a-long-test-password'


@pytest.fixture(autouse=True)
def bridge_secret(monkeypatch):
    monkeypatch.setenv('YTDL_BRIDGE_SECRET', SECRET)


def iso(days=3, seconds=0):
    return (datetime.now(timezone.utc) + timedelta(days=days, seconds=seconds)).isoformat().replace('+00:00', 'Z')


def body(subject='sub-1', units=1, expires=None, request_id=None):
    return {'requestId': request_id or str(uuid.uuid4()), 'subject': subject, 'units': units, 'expiresAt': expires or iso()}


def sso_user(subject='sub-1', issuer='', role='user', **limits):
    user = main.auth.store.oidc_user(subject, 'Momo', issuer=issuer)
    if role != 'user' or limits:
        user = main.auth.store.update_user(user['id'], {'role': role, **limits})
    return user


def post(client, payload, headers=BRIDGE):
    return client.post('/api/integrations/hh/grants', headers=headers, json=payload)


def test_authorization_and_disabled(monkeypatch):
    with TestClient(main.app) as client:
        assert post(client, body(), headers={}).status_code == 401
        assert post(client, body(), headers={'Authorization': 'Bearer wrong'}).status_code == 401
        assert post(client, body(), headers={'Authorization': 'Bearer ' + AUTH_TOKEN}).status_code == 401
        assert post(client, body()).status_code == 200
        monkeypatch.setenv('YTDL_BRIDGE_SECRET', '')
        assert post(client, body()).status_code == 404


def test_idempotent_and_conflicting_request_ids():
    with TestClient(main.app) as client:
        payload = body(units=2)
        first = post(client, payload)
        assert first.json() == {'granted': True, 'duplicate': False, 'activeUnits': 2}
        again = post(client, payload)
        assert again.status_code == 200 and again.json() == {'granted': True, 'duplicate': True, 'activeUnits': 2}
        assert post(client, {**payload, 'units': 3}).status_code == 409
        assert post(client, {**payload, 'subject': 'other'}).status_code == 409
        second = post(client, body(units=1))
        assert second.json()['activeUnits'] == 3


@pytest.mark.parametrize('mutation', [
    {'expiresAt': iso(days=0, seconds=-5)},
    {'expiresAt': iso(days=31)},
    {'expiresAt': 'not-a-date'},
    {'units': 0}, {'units': 101}, {'subject': ''}, {'requestId': 'nope'},
])
def test_invalid_payloads_are_rejected(mutation):
    with TestClient(main.app) as client:
        assert post(client, {**body(), **mutation}).status_code == 422


def test_quota_stacks_bonus_for_bare_and_issuer_bound_subjects():
    store = main.auth.store
    bare = sso_user('sub-1')
    issued = sso_user('sub-2', issuer='https://sso.example')
    local = store.create_user('local', 'Local', PASSWORD)
    with TestClient(main.app) as client:
        post(client, body('sub-1', 2))
        post(client, body('sub-2', 1))
        post(client, body('sub-1', 4, expires=iso(days=1)))
    default = store.policy()['default_daily_limit']
    quota = store.quota(bare['id'])
    assert (quota['base_limit'], quota['bonus'], quota['limit']) == (default, 6, default + 6)
    assert [batch['units'] for batch in quota['bonus_batches']] == [4, 2]
    assert quota['bonus_batches'][0]['expires_at'] < quota['bonus_batches'][1]['expires_at']
    assert store.quota(issued['id'])['bonus'] == 1
    assert store.quota(local['id'])['bonus'] == 0
    assert store.quota(local['id'])['limit'] == default


def test_expired_grants_do_not_count_and_old_rows_are_pruned():
    store = main.auth.store
    user = sso_user('sub-1')
    now = time.time()
    with sqlite3.connect(store.path) as db:
        db.execute('INSERT INTO quota_grants VALUES (?,?,?,?,?)', ('old', 'sub-1', 5, now - 5 * 86400, now - 100))
        db.execute('INSERT INTO quota_grants VALUES (?,?,?,?,?)', ('ancient', 'sub-1', 5, now - 90 * 86400, now - 60 * 86400))
    assert store.quota(user['id'])['bonus'] == 0
    store.add_grant(str(uuid.uuid4()), 'sub-1', 1, now + 3600)
    with sqlite3.connect(store.path) as db:
        ids = {row[0] for row in db.execute('SELECT request_id FROM quota_grants')}
    assert 'ancient' not in ids and 'old' in ids
    assert store.quota(user['id'])['bonus'] == 1


def test_admin_stays_unlimited_and_zero_base_limit_gets_bonus():
    store = main.auth.store
    admin = sso_user('admin-sub', role='admin')
    zero = sso_user('sub-1', daily_limit=0)
    store.add_grant(str(uuid.uuid4()), 'admin-sub', 3, time.time() + 3600)
    store.add_grant(str(uuid.uuid4()), 'sub-1', 2, time.time() + 3600)
    assert store.quota(admin['id'])['limit'] is None and store.quota(admin['id'])['bonus'] == 0
    quota = store.quota(zero['id'])
    assert (quota['base_limit'], quota['limit'], quota['remaining']) == (0, 2, 2)


def test_bonus_lets_reserve_take_one_more_download():
    store = main.auth.store
    user = sso_user('sub-1', daily_limit=1)
    store.reserve(user['id'], ['t1'])
    with pytest.raises(QuotaExceeded):
        store.reserve(user['id'], ['t2'])
    store.add_grant(str(uuid.uuid4()), 'sub-1', 1, time.time() + 3600)
    store.reserve(user['id'], ['t2'])
    assert store.quota(user['id'])['remaining'] == 0


def test_bridge_path_does_not_open_other_endpoints():
    with TestClient(main.app) as client:
        for path in ('/api/integrations/hh/other', '/api/integrations/hh/grants/extra', '/api/admin/users', '/api/auth/me'):
            assert client.post(path, headers=BRIDGE, json={}).status_code in (401, 404, 405), path
        assert client.get('/api/integrations/hh/grants').status_code in (401, 405)
        assert client.post('/api/integrations/hh/other', json={}).status_code == 401
