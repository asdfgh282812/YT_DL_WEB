"""yt-dlp integration shared by parsing and downloads."""
import os
import json
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from http.cookiejar import Cookie, DefaultCookiePolicy

import yt_dlp
from pydantic import BaseModel, Field, field_validator
from yt_dlp.cookies import YoutubeDLCookieJar

BROWSERS = ('chrome', 'chromium', 'firefox', 'edge', 'safari', 'brave', 'opera', 'vivaldi', 'whale')
AUDIO_EXTENSIONS = {'mp3', 'm4a', 'aac', 'flac', 'wav', 'ogg', 'opus'}


def http_url(value):
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('請提供完整的 http 或 https 網址，不接受帳密內嵌網址')
    return value


class SourceOptions(BaseModel):
    referer: str = Field(default='', max_length=4096)
    user_agent: str = Field(default='', max_length=1024)

    @field_validator('referer', 'user_agent')
    @classmethod
    def valid_header(cls, value):
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('來源資訊不可含換行或控制字元')
        return value

    @field_validator('referer')
    @classmethod
    def valid_referer(cls, value):
        return http_url(value) if value else ''

    def headers(self):
        return {key: val for key, val in {'Referer': self.referer, 'User-Agent': self.user_agent}.items() if val}


class Settings(BaseModel):
    max_concurrent_downloads: int = Field(default=2, ge=1, le=10)
    concurrent_fragments: int = Field(default=4, ge=1, le=16)
    cookie_source: Literal['file', 'browser', 'none'] = 'file'
    cookie_browser: Literal['chrome', 'chromium', 'firefox', 'edge', 'safari', 'brave', 'opera', 'vivaldi', 'whale'] = 'firefox'
    cookie_profile: str = Field(default='', max_length=4096)


def validate_cookie_file(path):
    jar = YoutubeDLCookieJar(str(path))
    jar.load(ignore_discard=True, ignore_expires=True)
    if not len(jar):
        raise ValueError('Cookie 檔案沒有有效紀錄，請匯出 Netscape cookies.txt 格式')
    return len(jar)


@contextmanager
def youtube_dl(config, cookie_file, extra=None, cookies=None):
    """Snapshot uploaded cookies; yt-dlp must never rewrite the shared jar."""
    options = {
        'quiet': True, 'no_warnings': True, 'socket_timeout': 30,
        'retries': 5, 'fragment_retries': 5, 'skip_unavailable_fragments': False,
        'concurrent_fragment_downloads': config.get('concurrent_fragments', 4),
        'cachedir': False,
    }
    runtimes = {name: {} for name in ('node', 'deno') if shutil.which(name)}
    if runtimes:
        options['js_runtimes'] = runtimes
    snapshot = None
    try:
        source = config.get('cookie_source', 'file') if cookies is None else 'none'
        if source == 'file' and os.path.isfile(cookie_file):
            fd, snapshot = tempfile.mkstemp(suffix='.txt')
            os.close(fd)
            shutil.copyfile(cookie_file, snapshot)
            options['cookiefile'] = snapshot
        elif source == 'browser':
            options['cookiesfrombrowser'] = (config.get('cookie_browser', 'firefox'), config.get('cookie_profile') or None, None, None)
        options.update(extra or {})
        # Requests clones cookie jars without their policy. Urllib preserves the
        # strict host-only rule for initial requests and every redirect.
        options['compat_opts'] = {*options.get('compat_opts', ()), 'prefer-legacy-http-handler'}
        with yt_dlp.YoutubeDL(options) as ydl:
            # Python's liberal Netscape policy otherwise sends host-only cookies
            # to subdomains, unlike browsers. Apply to imported and legacy jars.
            ydl.cookiejar.set_policy(DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
            for item in cookies or []:
                expires = item.get('expires', -1)
                domain = item['domain']
                ydl.cookiejar.set_cookie(Cookie(
                    version=0, name=item['name'], value=item['value'], port=None, port_specified=False,
                    domain=domain, domain_specified=domain.startswith('.'), domain_initial_dot=domain.startswith('.'),
                    path=item.get('path', '/'), path_specified=True, secure=item.get('secure', False),
                    expires=int(expires) if expires and expires > 0 else None,
                    discard=not expires or expires <= 0, comment=None, comment_url=None,
                    rest={'HttpOnly': None} if item.get('httpOnly') else {}, rfc2109=False,
                ))
            yield ydl
    finally:
        if snapshot:
            Path(snapshot).unlink(missing_ok=True)


def describe_media(info):
    title = info.get('title') or info.get('id') or '未命名媒體'
    if 'entries' in info:
        entries = []
        for entry in info.get('entries') or []:
            if not entry or entry.get('title') in ('[Deleted video]', '[Private video]'):
                continue
            url = entry.get('webpage_url') or entry.get('url')
            if not url and entry.get('id') and 'youtube' in (entry.get('ie_key') or info.get('extractor') or '').lower():
                url = 'https://www.youtube.com/watch?v=' + entry['id']
            if url and url.startswith(('http://', 'https://')):
                entries.append({'title': entry.get('title') or entry.get('id') or '未命名媒體', 'url': url})
        return {'is_playlist': True, 'title': title, 'entries': entries, 'item_count': len(entries)}

    formats = info.get('formats') or [info]
    videos, audio = [], []
    for fmt in formats:
        if fmt.get('has_drm') or fmt.get('vcodec') == 'images' or fmt.get('ext') == 'mhtml':
            continue
        if fmt.get('vcodec') == 'none' or (fmt.get('ext') in AUDIO_EXTENSIONS and not fmt.get('height')):
            audio.append(fmt)
            continue
        height = fmt.get('height')
        videos.append({
            'id': fmt.get('format_id') or 'best', 'height': height,
            'resolution': f'{height}p' if height else '自動辨識畫質',
            'note': fmt.get('format_note') or '', 'ext': fmt.get('ext') or '未知',
            'protocol': fmt.get('protocol') or '', 'fps': fmt.get('fps'),
            'has_audio': fmt.get('acodec') not in (None, 'none'),
            'vcodec': fmt.get('vcodec'),
        })
    videos.sort(key=lambda f: (f['height'] or 0, f['fps'] or 0, f['ext'] == 'mp4'), reverse=True)
    return {
        'is_playlist': False, 'title': title, 'video_formats': videos,
        'has_video': bool(videos), 'has_audio': bool(audio) or any(v['has_audio'] for v in videos),
        'extractor': info.get('extractor_key') or info.get('extractor') or 'generic',
        'duration': info.get('duration'), 'is_live': bool(info.get('is_live')),
        'warning': '此網址只提供音軌；請改貼包含影片的主播放清單。' if not videos and audio else '',
    }


def probe_unknown_stream(info, ydl):
    """HLS media playlists often omit both CODECS and resolution; inspect their streams."""
    if 'entries' in info or not shutil.which('ffprobe'):
        return
    formats = info.get('formats') or [info]
    for fmt in formats[:8]:
        if fmt.get('vcodec') is not None or fmt.get('height') or not fmt.get('url') or fmt.get('has_drm'):
            continue
        headers = dict(ydl.params.get('http_headers') or {})
        headers.update(info.get('http_headers') or {})
        headers.update(fmt.get('http_headers') or {})
        # Leave authenticated unknown streams selectable. Let yt-dlp apply the
        # browser cookie policy during download rather than copying secrets to ffprobe.
        if ydl.cookiejar.get_cookie_header(fmt['url']):
            continue
        command = ['ffprobe', '-v', 'error', '-rw_timeout', '5000000', '-analyzeduration', '2000000', '-probesize', '1000000']
        if headers:
            command += ['-headers', ''.join(f'{key}: {value}\r\n' for key, value in headers.items())]
        command += ['-show_entries', 'stream=codec_type,codec_name,height,width', '-of', 'json', fmt['url']]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=12)
            if result.returncode:
                continue
            streams = json.loads(result.stdout).get('streams', [])
            video = next((s for s in streams if s.get('codec_type') == 'video'), None)
            audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
            if video:
                fmt.update(vcodec=video.get('codec_name'), height=video.get('height'), width=video.get('width'))
            elif audio:
                fmt['vcodec'] = 'none'
            if audio:
                fmt['acodec'] = audio.get('codec_name')
        except (OSError, subprocess.TimeoutExpired, ValueError):
            pass  # Unknown metadata must not hide a potentially valid video.


def download_options(format_id, audio_only, output_format):
    if audio_only:
        codec = output_format if output_format in ('mp3', 'm4a') else 'mp3'
        return {'format': format_id or 'ba/b', 'postprocessors': [
            {'key': 'FFmpegExtractAudio', 'preferredcodec': codec, 'preferredquality': '192'},
        ]}
    selector = 'bv*+ba/b' if format_id in ('', 'best') else f'{format_id}[acodec=none]+ba/{format_id}'
    options = {'format': selector}
    if output_format in ('mp4', 'mkv'):
        options['merge_output_format'] = output_format
        options['postprocessors'] = [{'key': 'FFmpegVideoRemuxer', 'preferedformat': output_format}]
    return options
