"""Website matching and private, user-owned cookie records."""
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Request
from publicsuffix2 import get_sld

from media import http_url
from user_store import ADMIN_ID


PROVIDERS = (
    {'key': 'youtube', 'name': 'YouTube', 'domains': ('youtube.com', 'youtu.be', 'googlevideo.com')},
    {'key': 'bilibili', 'name': 'Bilibili', 'domains': ('bilibili.com', 'b23.tv')},
    {'key': 'instagram', 'name': 'Instagram', 'domains': ('instagram.com',)},
    {'key': 'facebook', 'name': 'Facebook', 'domains': ('facebook.com', 'fb.watch')},
    {'key': 'x', 'name': 'X', 'domains': ('x.com', 'twitter.com', 't.co')},
    {'key': 'tiktok', 'name': 'TikTok', 'domains': ('tiktok.com',)},
    {'key': 'vimeo', 'name': 'Vimeo', 'domains': ('vimeo.com',)},
    {'key': 'twitch', 'name': 'Twitch', 'domains': ('twitch.tv',)},
)


def domain_matches(host, domain):
    return host == domain or host.endswith('.' + domain)


def site_for_url(url):
    host = urlsplit(http_url(url)).hostname.encode('idna').decode().lower()
    for provider in PROVIDERS:
        if any(domain_matches(host, domain) for domain in provider['domains']):
            return dict(provider)
    root = get_sld(host, strict=True) or host
    return {'key': root, 'name': root, 'domains': (root,)}


def needs_login(message):
    lowered = message.lower()
    return any(word in lowered for word in ('sign in', 'log in', 'login', 'cookies', '403', '401', 'age-restricted', 'members-only', 'private video', '登入'))


def install_site_routes(app, auth, legacy_cookie_file=None):
    @app.get('/api/sites')
    async def sites(request: Request):
        return {'sites': auth.store.list_sites(request.state.user['id'])}

    @app.delete('/api/sites/{site_key}')
    async def forget(site_key: str, request: Request):
        auth.store.delete_site(request.state.user['id'], site_key)
        if site_key == '*' and request.state.user['id'] == ADMIN_ID and legacy_cookie_file:
            Path(legacy_cookie_file()).unlink(missing_ok=True)
        return {'success': True}
