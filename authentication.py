"""Shared token/SSO authentication for HTTP, files and WebSockets."""
import hmac
import asyncio
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from authlib.integrations.starlette_client import OAuth
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware
from user_store import UserStore, ADMIN_ID

AUTH_TOKEN = os.environ.get('AUTH_TOKEN', 'your_secure_password_here')


class TokenLogin(BaseModel):
    token: str = Field(default='', max_length=4096)
    username: str = Field(default='', max_length=64)
    password: str = Field(default='', max_length=256)


class Authentication:
    def __init__(self, config_dir):
        self.authority = os.environ.get('OIDC_AUTHORITY', '').rstrip('/')
        self.client_id = os.environ.get('OIDC_CLIENT_ID', '')
        self.client_secret = os.environ.get('OIDC_CLIENT_SECRET', '')
        self.oidc_enabled = bool(self.authority and self.client_id and self.client_secret)
        self.token_enabled = os.environ.get('AUTH_MODE', 'both') != 'sso'
        self.base_url = os.environ.get('PUBLIC_BASE_URL', '').strip().rstrip('/')
        if self.base_url and '://' not in self.base_url:
            self.base_url = 'https://' + self.base_url
        if self.base_url:
            public = urlsplit(self.base_url)
            if public.scheme not in ('http', 'https') or not public.hostname or public.path or public.query or public.fragment or public.username or public.password:
                raise RuntimeError('PUBLIC_BASE_URL 請設定完整網站來源，例如 https://download.example.com')
        self.session_max_age = int(os.environ.get('SESSION_MAX_AGE', '604800'))
        if not 3600 <= self.session_max_age <= 2592000:
            raise RuntimeError('SESSION_MAX_AGE 需介於 3600 與 2592000 秒')
        self.allowed_subjects = set(filter(None, (s.strip() for s in os.environ.get('OIDC_ALLOWED_SUBJECTS', '').split(','))))
        if not self.token_enabled and not self.oidc_enabled:
            raise RuntimeError('AUTH_MODE=sso 時必須設定 OIDC_AUTHORITY、OIDC_CLIENT_ID、OIDC_CLIENT_SECRET')
        secret = os.environ.get('SESSION_SECRET', '')
        if not secret:
            path = Path(config_dir) / '.session-secret'
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                secret = path.read_text().strip()
            else:
                secret = secrets.token_urlsafe(48)
                with os.fdopen(fd, 'w') as stream:
                    stream.write(secret)
        if len(secret) < 32:
            raise RuntimeError('SESSION_SECRET 需至少 32 個字元')
        self.secret = secret
        self.store = UserStore(Path(config_dir) / 'users.sqlite3', secret)
        self.login_failures = {}
        self.admin_subjects = set(filter(None, (s.strip() for s in os.environ.get('OIDC_ADMIN_SUBJECTS', '').split(','))))
        self.oauth = OAuth()
        if self.oidc_enabled:
            kwargs = {
                'scope': os.environ.get('OIDC_SCOPE', 'openid profile email'),
                'token_endpoint_auth_method': os.environ.get('OIDC_TOKEN_AUTH_METHOD', 'client_secret_post'),
            }
            if 'openid' not in kwargs['scope'].split():
                raise RuntimeError('OIDC_SCOPE 必須包含 openid')
            if os.environ.get('OIDC_USE_PKCE', 'false').lower() == 'true':
                kwargs['code_challenge_method'] = 'S256'
            self.oauth.register('sso', client_id=self.client_id, client_secret=self.client_secret,
                                server_metadata_url=self.authority + '/.well-known/openid-configuration',
                                client_kwargs=kwargs)

    def token_valid(self, token):
        return self.token_enabled and isinstance(token, str) and bool(token) and hmac.compare_digest(token.encode(), AUTH_TOKEN.encode())

    def session_user(self, connection):
        sid = connection.scope.get('session', {}).get('sid')
        user = self.store.login_session_user(sid)
        if not user:
            return None
        if user['method'] == 'sso':
            if not self.oidc_enabled or (self.allowed_subjects and user['sub'] not in self.allowed_subjects):
                return None
        elif not self.token_enabled:
            return None
        return user

    def current_user(self, connection, token=None):
        user = self.session_user(connection)
        if user:
            return user
        if self.token_valid(token):
            user = self.store.get_user(ADMIN_ID)
            user.update(method='token', sub='token')
            return user
        return None

    @staticmethod
    def require_admin(request):
        if request.state.user['role'] != 'admin':
            raise HTTPException(403, '此功能僅限管理者使用')

    def revoke_user(self, user_id):
        self.store.revoke_login_sessions(user_id)

    def create_session(self, request, user):
        if 'id' not in user and user.get('method') == 'token':
            user = {**self.store.get_user(ADMIN_ID), **user}
        self.store.delete_login_session(request.session.get('sid'))
        sid = secrets.token_urlsafe(32)
        self.store.save_login_session(sid, user, time.time() + self.session_max_age)
        request.session.clear()
        request.session['sid'] = sid

    def origin_allowed(self, connection):
        origin = connection.headers.get('origin')
        if not origin:
            return True  # CLI clients authenticate using a bearer token.
        expected = self.base_url or str(connection.base_url).rstrip('/')
        actual_parts, expected_parts = urlsplit(origin), urlsplit(expected)
        scheme = {'ws': 'http', 'wss': 'https'}.get(expected_parts.scheme, expected_parts.scheme)
        try:
            return (actual_parts.scheme, actual_parts.hostname, actual_parts.port or (443 if actual_parts.scheme == 'https' else 80)) == (
                scheme, expected_parts.hostname, expected_parts.port or (443 if scheme == 'https' else 80)
            )
        except ValueError:
            return False

    def install(self, app):
        @app.middleware('http')
        async def authenticate(request: Request, call_next):
            path = request.url.path
            if path.startswith('/api/'):
                if request.method not in ('GET', 'HEAD', 'OPTIONS') and not self.origin_allowed(request):
                    return JSONResponse({'detail': '不接受跨來源操作'}, status_code=403)
                public = path in ('/api/auth/config', '/api/auth/login', '/api/auth/logout', '/api/auth/me') or (
                    request.method == 'GET' and path == '/api/v1/auth/sso/callback'
                )
                authorization = request.headers.get('authorization', '')
                bearer = authorization[7:] if authorization.startswith('Bearer ') else ''
                legacy_file_token = request.query_params.get('token') if request.method == 'GET' and path.startswith('/api/download/') else None
                user = self.current_user(request, bearer or legacy_file_token)
                request.state.user = user
                if not public and not user:
                    return JSONResponse({'detail': '請先登入系統'}, status_code=401)
                admin_path = path.startswith('/api/admin/') or path in ('/api/config', '/api/update-ytdlp')
                if admin_path and (not user or user['role'] != 'admin'):
                    return JSONResponse({'detail': '此功能僅限管理者使用'}, status_code=403)
            response = await call_next(request)
            if path.startswith(('/api/', '/auth/')):
                response.headers['Cache-Control'] = 'no-store'
                response.headers['Referrer-Policy'] = 'no-referrer'
            return response

        app.add_middleware(SessionMiddleware, secret_key=self.secret, session_cookie='media_downloader_session',
                           max_age=self.session_max_age, same_site='lax',
                           https_only=os.environ.get('SESSION_HTTPS_ONLY', 'true' if self.base_url.startswith('https:') else 'false').lower() == 'true')

        @app.get('/api/auth/config')
        async def auth_config():
            return {'sso_enabled': self.oidc_enabled, 'token_enabled': self.token_enabled, 'password_enabled': self.token_enabled,
                    'sso_name': os.environ.get('OIDC_DISPLAY_NAME', 'Synology SSO')}

        @app.get('/api/auth/me')
        async def me(request: Request):
            user = self.session_user(request)
            if not user:
                raise HTTPException(401, '請先登入系統')
            return {'user': user, 'quota': self.store.quota(user['id'])}

        @app.post('/api/auth/login')
        async def token_login(req: TokenLogin, request: Request):
            key = (request.client.host if request.client else '', req.username.casefold())
            now = time.time()
            failures = [stamp for stamp in self.login_failures.get(key, []) if now - stamp < 300]
            if len(failures) >= 10:
                raise HTTPException(429, '登入失敗次數過多，請五分鐘後再試')
            user = None
            if req.token and self.token_valid(req.token):
                user = self.store.get_user(ADMIN_ID)
                user.update(method='token', sub='token')
            elif self.token_enabled and req.username and req.password:
                user = await asyncio.to_thread(self.store.authenticate, req.username.strip(), req.password)
                if user:
                    user.update(method='password', sub=user['id'])
            if not user:
                if len(self.login_failures) > 10000:
                    self.login_failures.clear()
                self.login_failures[key] = failures + [now]
                raise HTTPException(401, 'Token 無效或已停用' if req.token else '帳號、密碼錯誤或帳號已停用')
            self.login_failures.pop(key, None)
            self.create_session(request, user)
            return {'success': True, 'user': user, 'quota': self.store.quota(user['id'])}

        @app.post('/api/auth/logout')
        async def logout(request: Request):
            self.store.delete_login_session(request.session.get('sid'))
            request.session.clear()
            return {'success': True}

        @app.get('/auth/sso/login')
        async def sso_login(request: Request):
            if not self.oidc_enabled:
                raise HTTPException(404, 'SSO 尚未設定')
            redirect_uri = os.environ.get('OIDC_REDIRECT_URI') or (self.base_url + '/auth/sso/callback' if self.base_url else str(request.url_for('sso_callback')))
            # Use query mode for Synology; state and nonce are managed by Authlib.
            self.store.delete_login_session(request.session.get('sid'))
            request.session.clear()
            try:
                return await self.oauth.sso.authorize_redirect(request, redirect_uri, response_mode='query')
            except Exception:
                return RedirectResponse('/?auth_error=sso_unavailable', status_code=303)

        @app.get('/api/v1/auth/sso/callback', name='sso_callback_api_v1', include_in_schema=False)
        @app.get('/auth/sso/callback', name='sso_callback')
        async def sso_callback(request: Request):
            if not self.oidc_enabled:
                raise HTTPException(404, 'SSO 尚未設定')
            try:
                token = await self.oauth.sso.authorize_access_token(request)
                user = token.get('userinfo')
                if not token.get('id_token') or not user or not user.get('sub'):
                    raise ValueError('Missing validated ID token')
                if self.allowed_subjects and user['sub'] not in self.allowed_subjects:
                    request.session.clear()
                    return RedirectResponse('/?auth_error=access_denied', status_code=303)
                account = self.store.oidc_user(user['sub'], user.get('name') or user.get('preferred_username') or user['sub'], user['sub'] in self.admin_subjects, user.get('iss') or self.authority)
                self.create_session(request, account)
            except Exception:
                request.session.clear()
                return RedirectResponse('/?auth_error=sso_failed', status_code=303)
            return RedirectResponse('/', status_code=303)
