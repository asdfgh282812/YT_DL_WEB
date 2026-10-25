import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from joserfc import jwk, jwt

from authentication import Authentication


@pytest.fixture
def oidc_app(tmp_path, monkeypatch):
    monkeypatch.delenv('OIDC_REDIRECT_URI', raising=False)
    for name, value in {
        'OIDC_AUTHORITY': 'https://sso.example/sso', 'OIDC_CLIENT_ID': 'downloader',
        'OIDC_CLIENT_SECRET': 'test-secret', 'OIDC_USE_PKCE': 'false',
        'AUTH_MODE': 'sso', 'PUBLIC_BASE_URL': 'http://testserver',
        'OIDC_ALLOWED_SUBJECTS': 'owner',
    }.items():
        monkeypatch.setenv(name, value)
    auth = Authentication(tmp_path)
    app = FastAPI()
    auth.install(app)
    @app.get('/api/protected')
    def protected():
        return {'ok': True}
    return app, auth


@pytest.mark.parametrize('corrupt', [None, 'state', 'nonce', 'issuer', 'audience', 'expired', 'signature', 'subject'])
@pytest.mark.parametrize('callback_path', ['/auth/sso/callback', '/api/v1/auth/sso/callback'])
def test_oidc_flow_checks(oidc_app, corrupt, callback_path, monkeypatch):
    if callback_path.startswith('/api/'):
        monkeypatch.setenv('OIDC_REDIRECT_URI', 'http://testserver' + callback_path)
    app, auth = oidc_app
    key = jwk.RSAKey.generate_key(parameters={'kid': 'provider'})
    other_key = jwk.RSAKey.generate_key(parameters={'kid': 'provider'})
    metadata = {
        'issuer': 'https://sso.example/sso', 'authorization_endpoint': 'https://sso.example/authorize',
        'token_endpoint': 'https://sso.example/token', 'jwks_uri': 'https://sso.example/keys',
        'id_token_signing_alg_values_supported': ['RS256'], 'response_types_supported': ['code'],
        'token_endpoint_auth_methods_supported': ['client_secret_post'],
    }
    with respx.mock(assert_all_called=False) as provider, TestClient(app) as client:
        provider.get('https://sso.example/sso/.well-known/openid-configuration').mock(return_value=httpx.Response(200, json=metadata))
        provider.get('https://sso.example/keys').mock(return_value=httpx.Response(200, json={'keys': [key.as_dict(private=False)]}))
        start = client.get('/auth/sso/login', follow_redirects=False)
        assert start.status_code == 302
        params = parse_qs(urlsplit(start.headers['location']).query)
        assert params['response_mode'] == ['query']
        assert params['response_type'] == ['code']
        assert params['redirect_uri'] == ['http://testserver' + callback_path]
        assert 'code_challenge' not in params
        now = int(time.time())
        claims = {
            'iss': metadata['issuer'], 'aud': 'downloader', 'sub': 'owner',
            'iat': now, 'exp': now + 300, 'nonce': params['nonce'][0], 'name': '測試使用者',
        }
        if corrupt == 'nonce': claims['nonce'] = 'wrong-nonce'
        if corrupt == 'issuer': claims['iss'] = 'https://other.example'
        if corrupt == 'audience': claims['aud'] = 'another-client'
        if corrupt == 'expired': claims['exp'] = now - 3600
        if corrupt == 'subject': claims['sub'] = 'not-allowed'
        id_token = jwt.encode({'alg': 'RS256', 'kid': 'provider'}, claims, other_key if corrupt == 'signature' else key)
        def exchange(request):
            form = parse_qs(request.content.decode())
            assert form['client_secret'] == ['test-secret']
            assert form['grant_type'] == ['authorization_code']
            assert form['redirect_uri'] == ['http://testserver' + callback_path]
            return httpx.Response(200, json={'access_token': 'provider-access-token', 'token_type': 'Bearer', 'id_token': id_token})
        token_route = provider.post('https://sso.example/token').mock(side_effect=exchange)
        callback = client.get(callback_path, params={'code': 'authorization-code', 'state': 'wrong' if corrupt == 'state' else params['state'][0]}, follow_redirects=False)
        assert callback.status_code == 303
        if corrupt:
            assert 'auth_error=' in callback.headers['location']
            assert client.get('/api/protected').status_code == 401
            if corrupt == 'state': assert not token_route.called
        else:
            assert callback.headers['location'] == '/'
            assert client.get('/api/protected').status_code == 200
            assert client.get('/api/auth/me').json()['user']['sub'] == 'owner'
            with auth.store.connection() as db:
                expires_at = db.execute('SELECT expires_at FROM login_sessions').fetchone()['expires_at']
            assert expires_at > claims['exp'] + 86400
            # A short-lived ID token authenticates the login, not the app session's lifetime.
            with monkeypatch.context() as future:
                future.setattr('user_store.time.time', lambda: now + 600)
                assert client.get('/api/protected').status_code == 200
            assert 'provider-access-token' not in client.cookies.get('media_downloader_session')
            # The state cannot be replayed, including after logout.
            assert client.post('/api/auth/logout').status_code == 200
            assert client.get('/api/protected').status_code == 401
            replay = client.get(callback_path, params={'code': 'authorization-code', 'state': params['state'][0]}, follow_redirects=False)
            assert 'auth_error=' in replay.headers['location']


def test_sso_only_rejects_shared_token(oidc_app):
    app, auth = oidc_app
    with TestClient(app) as client:
        assert not client.get('/api/auth/config').json()['token_enabled']
        assert client.post('/api/auth/login', json={'token': 'your_secure_password_here'}).status_code == 401
        assert client.get('/api/protected', headers={'Authorization': 'Bearer your_secure_password_here'}).status_code == 401


def test_api_callback_exception_keeps_other_routes_protected(oidc_app):
    app, auth = oidc_app
    with TestClient(app) as client:
        for path in ('/api/protected', '/api/v1/auth/sso/callback/extra', '/api/v1/auth/me'):
            assert client.get(path).status_code == 401
        assert client.post('/api/v1/auth/sso/callback').status_code == 401
        response = client.get('/api/v1/auth/sso/callback', follow_redirects=False)
        assert response.status_code == 303
        assert response.headers['location'] == '/?auth_error=sso_failed'
        assert client.get('/api/protected').status_code == 401
