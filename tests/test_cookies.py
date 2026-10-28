from pathlib import Path

from fastapi.testclient import TestClient

import main
from authentication import AUTH_TOKEN
from media import youtube_dl
from yt_dlp.cookies import YoutubeDLCookieJar

HEADERS = {'Authorization': 'Bearer ' + AUTH_TOKEN}
COOKIE = '# Netscape HTTP Cookie File\n.example.com\tTRUE\t/\tFALSE\t2147483647\tsession\ttest-value\n'


def test_cookie_upload_validation_snapshot_and_delete():
    with TestClient(main.app) as client:
        bad = client.post('/api/upload-cookie', headers=HEADERS, files={'file': ('bad.txt', 'invalid')})
        assert bad.status_code == 400
        response = client.post('/api/upload-cookie', headers=HEADERS, files={'file': ('cookies.txt', COOKIE)})
        assert response.status_code == 200
        assert main.auth.store.site('bootstrap-admin', 'example.com')['cookies'][0]['value'] == 'test-value'
        assert not Path(main.COOKIE_FILE).exists()  # New personal imports stay encrypted.
        Path(main.COOKIE_FILE).write_text(COOKIE)  # Exercise the legacy snapshot reader separately.
        original = Path(main.COOKIE_FILE).read_bytes()
        main.config_data['cookie_source'] = 'file'
        assert client.post('/api/cookies/test', headers=HEADERS).json()['count'] == 1
        with youtube_dl(main.config_data, main.COOKIE_FILE) as ydl:
            snapshot = ydl.params['cookiefile']
            assert snapshot != main.COOKIE_FILE
            assert len(ydl.cookiejar) == 1
            Path(snapshot).write_text('changed')
        assert not Path(snapshot).exists()
        assert Path(main.COOKIE_FILE).read_bytes() == original
        assert client.delete('/api/cookies', headers=HEADERS).status_code == 200
        assert not Path(main.COOKIE_FILE).exists()
        assert client.post('/api/cookies/test', headers=HEADERS).status_code == 400


def test_browser_selection_passes_profile_without_file(monkeypatch):
    captured = {}
    class Fake:
        def __init__(self, options):
            captured.update(options)
            self.cookiejar = YoutubeDLCookieJar()
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr('media.yt_dlp.YoutubeDL', Fake)
    with youtube_dl({'cookie_source': 'browser', 'cookie_browser': 'firefox', 'cookie_profile': '/browser/profile'}, main.COOKIE_FILE):
        pass
    assert captured['cookiesfrombrowser'] == ('firefox', '/browser/profile', None, None)
    assert 'cookiefile' not in captured
