"""Durable accounts, daily reservations and private encrypted website cookies."""
import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet, InvalidToken
from cookie_scopes import group_cookies

ADMIN_ID = 'bootstrap-admin'


class StoreError(ValueError):
    pass


class QuotaExceeded(StoreError):
    pass


def hash_password(password):
    salt = secrets.token_bytes(16)
    value = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return 'scrypt$' + salt.hex() + '$' + value.hex()


def password_matches(password, encoded):
    try:
        kind, salt, expected = encoded.split('$')
        if kind != 'scrypt':
            return False
        value = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32)
        return hmac.compare_digest(value.hex(), expected)
    except (ValueError, TypeError, AttributeError):
        return False


class UserStore:
    def __init__(self, path, secret):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.cipher = Fernet(base64.urlsafe_b64encode(hashlib.sha256(('website-cookies:' + secret).encode()).digest()))
        self.timezone = ZoneInfo(os.environ.get('APP_TIMEZONE', 'Asia/Taipei'))
        self.bootstrap_usable = os.environ.get('AUTH_MODE', 'both') != 'sso'
        with self.connection() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','user')),
                    password_hash TEXT, oidc_sub TEXT UNIQUE, disabled INTEGER NOT NULL DEFAULT 0,
                    daily_limit INTEGER CHECK(daily_limit >= 0), created_at REAL NOT NULL,
                    auth_version INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY CHECK(id=1), daily_limit INTEGER NOT NULL);
                INSERT OR IGNORE INTO policy VALUES (1, 5);
                CREATE TABLE IF NOT EXISTS downloads (
                    task_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                    day TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('reserved','completed','released'))
                );
                CREATE INDEX IF NOT EXISTS quota_usage ON downloads(user_id, state, day);
                CREATE TABLE IF NOT EXISTS site_accounts (
                    user_id TEXT NOT NULL REFERENCES users(id), site_key TEXT NOT NULL,
                    name TEXT NOT NULL, origin TEXT NOT NULL, cookies BLOB NOT NULL,
                    user_agent TEXT NOT NULL, updated_at REAL NOT NULL,
                    PRIMARY KEY(user_id, site_key)
                );
                CREATE TABLE IF NOT EXISTS login_sessions (
                    session_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    auth_version INTEGER NOT NULL, method TEXT NOT NULL, subject TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS login_session_user ON login_sessions(user_id);
            ''')
            if 'auth_version' not in {row['name'] for row in db.execute('PRAGMA table_info(users)')}:
                db.execute('ALTER TABLE users ADD COLUMN auth_version INTEGER NOT NULL DEFAULT 0')
            db.execute('INSERT OR IGNORE INTO users (id,username,name,role,created_at) VALUES (?,?,?,?,?)',
                       (ADMIN_ID, 'admin', '系統管理者', 'admin', time.time()))
        self.dummy_hash = hash_password(secrets.token_urlsafe(24))

    @contextmanager
    def connection(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def today(self):
        return datetime.now(self.timezone).date().isoformat()

    @staticmethod
    def public_user(row):
        if row is None:
            return None
        result = {key: row[key] for key in ('id', 'username', 'name', 'role', 'disabled', 'daily_limit', 'oidc_sub', 'auth_version')}
        binding = result['oidc_sub']
        if binding and binding.startswith('['):
            result['oidc_issuer'], result['oidc_sub'] = json.loads(binding)
        return result

    def get_user(self, user_id):
        with self.connection() as db:
            return self.public_user(db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone())

    def user_for_session(self, session_user):
        user = self.get_user(session_user.get('id', ''))
        if not user or user['disabled'] or session_user.get('auth_version', -1) != user['auth_version']:
            return None
        user.update(method=session_user.get('method', 'password'), sub=session_user.get('sub', user['id']))
        return user

    def authenticate(self, username, password):
        with self.connection() as db:
            row = db.execute('SELECT * FROM users WHERE username=? COLLATE NOCASE', (username,)).fetchone()
        valid = password_matches(password, row['password_hash'] if row and row['password_hash'] else self.dummy_hash)
        return self.public_user(row) if row and valid and not row['disabled'] else None

    @staticmethod
    def session_hash(sid):
        return hashlib.sha256(sid.encode()).hexdigest()

    def save_login_session(self, sid, user, expires_at):
        with self.connection(write=True) as db:
            db.execute('DELETE FROM login_sessions WHERE expires_at <= ?', (time.time(),))
            db.execute('INSERT INTO login_sessions VALUES (?,?,?,?,?,?)',
                       (self.session_hash(sid), user['id'], user['auth_version'],
                        user.get('method', 'password'), user.get('sub', user['id']), expires_at))

    def login_session_user(self, sid):
        if not isinstance(sid, str) or not sid or len(sid) > 256:
            return None
        with self.connection() as db:
            row = db.execute('''SELECT u.*, s.method AS session_method, s.subject AS session_subject
                FROM login_sessions s JOIN users u ON u.id=s.user_id
                WHERE s.session_hash=? AND s.expires_at>? AND s.auth_version=u.auth_version AND u.disabled=0''',
                (self.session_hash(sid), time.time())).fetchone()
        user = self.public_user(row)
        if user:
            user.update(method=row['session_method'], sub=row['session_subject'])
        return user

    def delete_login_session(self, sid):
        if isinstance(sid, str) and sid:
            with self.connection(write=True) as db:
                db.execute('DELETE FROM login_sessions WHERE session_hash=?', (self.session_hash(sid),))

    def revoke_login_sessions(self, user_id):
        with self.connection(write=True) as db:
            db.execute('DELETE FROM login_sessions WHERE user_id=?', (user_id,))

    def create_user(self, username, name, password, role='user', daily_limit=None):
        user_id = str(uuid.uuid4())
        encoded = hash_password(password)
        try:
            with self.connection(write=True) as db:
                db.execute('INSERT INTO users (id,username,name,role,password_hash,daily_limit,created_at) VALUES (?,?,?,?,?,?,?)',
                           (user_id, username, name, role, encoded, daily_limit, time.time()))
        except sqlite3.IntegrityError:
            raise StoreError('此帳號名稱已存在')
        return self.get_user(user_id)

    def oidc_user(self, sub, name, is_admin=False, issuer=''):
        binding = json.dumps([issuer, sub], separators=(',', ':')) if issuer else sub
        with self.connection(write=True) as db:
            row = db.execute('SELECT * FROM users WHERE oidc_sub=?', (binding,)).fetchone()
            if not row:
                user_id = str(uuid.uuid4())
                username = 'sso-' + hashlib.sha256(binding.encode()).hexdigest()[:16]
                db.execute('INSERT INTO users (id,username,name,role,oidc_sub,created_at) VALUES (?,?,?,?,?,?)',
                           (user_id, username, name, 'admin' if is_admin else 'user', binding, time.time()))
                row = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
            if row['disabled']:
                raise StoreError('此帳號已停用')
            if is_admin and row['role'] != 'admin':
                db.execute("UPDATE users SET role='admin' WHERE id=?", (row['id'],))
                row = db.execute('SELECT * FROM users WHERE id=?', (row['id'],)).fetchone()
            user = self.public_user(row)
        user.update(method='sso', sub=sub)
        return user

    def update_user(self, user_id, changes):
        changes = dict(changes)
        if 'password' in changes:
            password = changes.pop('password')
            if password:
                changes['password_hash'] = hash_password(password)
        with self.connection(write=True) as db:
            row = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
            if not row:
                raise StoreError('找不到使用者')
            if user_id == ADMIN_ID and (changes.get('role', 'admin') != 'admin' or changes.get('disabled')):
                raise StoreError('系統管理者不可降權或停用')
            if row['role'] == 'admin' and not row['disabled'] and (changes.get('role', 'admin') != 'admin' or changes.get('disabled')):
                count = db.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0 AND (id != ? OR ?=1)",
                                   (ADMIN_ID, int(self.bootstrap_usable))).fetchone()[0]
                if count <= 1:
                    raise StoreError('至少需要保留一位啟用中的管理者')
            allowed = ('name', 'role', 'daily_limit', 'disabled', 'password_hash')
            fields = [key for key in changes if key in allowed]
            if fields:
                db.execute('UPDATE users SET ' + ','.join(key + '=?' for key in fields) + ' WHERE id=?',
                           [changes[key] for key in fields] + [user_id])
                if 'password_hash' in changes or ('disabled' in changes and bool(changes['disabled']) != bool(row['disabled'])):
                    db.execute('UPDATE users SET auth_version=auth_version+1 WHERE id=?', (user_id,))
        return self.get_user(user_id)

    def policy(self):
        with self.connection() as db:
            return {'default_daily_limit': db.execute('SELECT daily_limit FROM policy WHERE id=1').fetchone()[0], 'timezone': str(self.timezone)}

    def set_policy(self, daily_limit):
        with self.connection(write=True) as db:
            db.execute('UPDATE policy SET daily_limit=? WHERE id=1', (daily_limit,))
        return self.policy()

    def quota(self, user_id, db=None):
        if db is None:
            with self.connection() as connection:
                return self.quota(user_id, connection)
        row = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
        if not row:
            raise StoreError('找不到使用者')
        default = db.execute('SELECT daily_limit FROM policy WHERE id=1').fetchone()[0]
        limit = None if row['role'] == 'admin' else row['daily_limit'] if row['daily_limit'] is not None else default
        counts = db.execute("SELECT state,COUNT(*) AS n FROM downloads WHERE user_id=? AND (state='reserved' OR (state='completed' AND day=?)) GROUP BY state", (user_id, self.today())).fetchall()
        counts = {item['state']: item['n'] for item in counts}
        used, reserved = counts.get('completed', 0), counts.get('reserved', 0)
        return {'date': self.today(), 'timezone': str(self.timezone), 'limit': limit, 'used': used,
                'reserved': reserved, 'remaining': None if limit is None else max(0, limit - used - reserved),
                'inherits_default': row['daily_limit'] is None}

    def reserve(self, user_id, task_ids):
        with self.connection(write=True) as db:
            row = db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
            if not row or row['disabled']:
                raise StoreError('此帳號無法加入下載')
            quota = self.quota(user_id, db)
            if quota['remaining'] is not None and len(task_ids) > quota['remaining']:
                raise QuotaExceeded(f"每日上限 {quota['limit']} 部，今日已完成 {quota['used']} 部、預留 {quota['reserved']} 部，還可加入 {quota['remaining']} 部")
            for task_id in task_ids:
                previous = db.execute('SELECT user_id,state FROM downloads WHERE task_id=?', (task_id,)).fetchone()
                if previous and (previous['user_id'] != user_id or previous['state'] != 'released'):
                    raise StoreError('任務已計入額度，不可重複加入')
                db.execute("INSERT INTO downloads VALUES (?,?,?,'reserved') ON CONFLICT(task_id) DO UPDATE SET day=excluded.day,state='reserved'", (task_id, user_id, self.today()))

    def settle(self, task_id, success):
        with self.connection(write=True) as db:
            db.execute("UPDATE downloads SET state=?,day=? WHERE task_id=? AND state='reserved'",
                       ('completed' if success else 'released', self.today(), task_id))

    def release_orphaned(self):
        with self.connection(write=True) as db:
            db.execute("UPDATE downloads SET state='released' WHERE state='reserved'")

    def list_users(self):
        with self.connection() as db:
            users = [self.public_user(row) for row in db.execute('SELECT * FROM users ORDER BY created_at')]
            for user in users:
                user['quota'] = self.quota(user['id'], db)
        return users

    def save_site(self, user_id, site_key, name, origin, cookies, user_agent=''):
        payload = self.cipher.encrypt(json.dumps(cookies).encode())
        with self.connection(write=True) as db:
            db.execute('INSERT INTO site_accounts VALUES (?,?,?,?,?,?,?) ON CONFLICT(user_id,site_key) DO UPDATE SET name=excluded.name,origin=excluded.origin,cookies=excluded.cookies,user_agent=excluded.user_agent,updated_at=excluded.updated_at',
                       (user_id, site_key, name, origin, payload, user_agent, time.time()))

    def _migrate_cookie_sites(self, db, user_id):
        legacy = db.execute('SELECT * FROM site_accounts WHERE user_id=? AND site_key=?', (user_id, '*')).fetchone()
        if not legacy:
            return
        try:
            groups = group_cookies(json.loads(self.cipher.decrypt(legacy['cookies'])))
        except (InvalidToken, ValueError, KeyError, TypeError):
            return  # Keep unreadable legacy data available for explicit removal/re-import.
        for key, site in groups.items():
            payload = self.cipher.encrypt(json.dumps(site['cookies']).encode())
            db.execute('''INSERT INTO site_accounts VALUES (?,?,?,?,?,?,?)
                ON CONFLICT(user_id,site_key) DO UPDATE SET name=excluded.name, origin=excluded.origin,
                cookies=excluded.cookies, user_agent=excluded.user_agent, updated_at=excluded.updated_at
                WHERE excluded.updated_at > site_accounts.updated_at''',
                (user_id, key, site['name'], '', payload, legacy['user_agent'], legacy['updated_at']))
        db.execute('DELETE FROM site_accounts WHERE user_id=? AND site_key=?', (user_id, '*'))

    def migrate_cookie_sites(self, user_id):
        with self.connection() as db:
            exists = db.execute('SELECT 1 FROM site_accounts WHERE user_id=? AND site_key=?', (user_id, '*')).fetchone()
        if exists:
            with self.connection(write=True) as db:
                self._migrate_cookie_sites(db, user_id)

    def import_cookie_sites(self, user_id, groups):
        with self.connection(write=True) as db:
            self._migrate_cookie_sites(db, user_id)
            updated_at = time.time()
            for key, site in groups.items():
                payload = self.cipher.encrypt(json.dumps(site['cookies']).encode())
                db.execute('''INSERT INTO site_accounts VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(user_id,site_key) DO UPDATE SET name=excluded.name, origin=excluded.origin,
                    cookies=excluded.cookies, user_agent=excluded.user_agent, updated_at=excluded.updated_at''',
                    (user_id, key, site['name'], '', payload, '', updated_at))

    def site(self, user_id, site_key):
        if site_key != '*':
            self.migrate_cookie_sites(user_id)
        with self.connection() as db:
            row = db.execute('SELECT * FROM site_accounts WHERE user_id=? AND site_key=?', (user_id, site_key)).fetchone()
        if not row:
            return None
        data = dict(row)
        try:
            data['cookies'] = json.loads(self.cipher.decrypt(data['cookies']))
        except InvalidToken:
            raise StoreError('Cookies 資料無法解密，請重新匯入 cookies.txt 或檢查 SESSION_SECRET')
        return data

    def list_sites(self, user_id):
        self.migrate_cookie_sites(user_id)
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT site_key,name,origin,updated_at FROM site_accounts WHERE user_id=? ORDER BY name', (user_id,))]

    def delete_site(self, user_id, site_key):
        if site_key != '*':
            self.migrate_cookie_sites(user_id)
        with self.connection(write=True) as db:
            db.execute('DELETE FROM site_accounts WHERE user_id=? AND site_key=?', (user_id, site_key))

    def delete_all_sites(self, user_id):
        with self.connection(write=True) as db:
            db.execute('DELETE FROM site_accounts WHERE user_id=?', (user_id,))
