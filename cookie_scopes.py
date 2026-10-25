"""Cookie website grouping, independent of storage and HTTP routes."""
from urllib.parse import urlsplit

from publicsuffix2 import get_sld

from media import http_url


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



def group_cookies(cookies):
    groups = {}
    for cookie in cookies:
        domain = cookie['domain'].lstrip('.').rstrip('.').lower()
        if not domain or any(char in domain for char in '/\\@?#'):
            raise ValueError('Cookie 網域格式不正確，請重新匯出檔案')
        host = f'[{domain}]' if ':' in domain and not domain.startswith('[') else domain
        site = site_for_url('https://' + host)
        if site['key'] not in groups:
            groups[site['key']] = {**site, 'cookies': []}
        groups[site['key']]['cookies'].append(cookie)
    return groups


def import_cookie_groups(cookies, scope='all', website=''):
    groups = group_cookies(cookies)
    if scope == 'site':
        website = website.strip()
        if not website:
            raise ValueError('請輸入要匯入的網站網址或網域')
        site = site_for_url(website if '://' in website else 'https://' + website)
        if site['key'] not in groups:
            raise ValueError('檔案中沒有此網站的 cookies，請在該網站重新匯出，或改選「檔案內所有網站」')
        return {site['key']: groups[site['key']]}
    return groups
