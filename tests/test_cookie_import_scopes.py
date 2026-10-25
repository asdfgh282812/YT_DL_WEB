from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

import main
from authentication import AUTH_TOKEN
from cookie_scopes import import_cookie_groups
from user_store import ADMIN_ID

HEADERS = {'Authorization': 'Bearer ' + AUTH_TOKEN}


def cookie(domain, value):
    return {'name': 'session', 'value': value, 'domain': domain, 'path': '/', 'expires': 2147483647}


def upload(client, entries, **data):
    content = '# Netscape HTTP Cookie File\n' + ''.join(
        f"{domain}\t{'TRUE' if domain.startswith('.') else 'FALSE'}\t/\tFALSE\t2147483647\tsession\t{value}\n"
        for domain, value in entries
    )
    return client.post('/api/upload-cookie', headers=HEADERS, data=data,
                       files={'file': ('cookies.txt', content)})


def value(key, user_id=ADMIN_ID):
    return main.auth.store.site(user_id, key)['cookies'][0]['value']


def test_all_sites_import_then_single_site_update_preserves_other_sites():
    with TestClient(main.app) as client:
        result = upload(client, [('.youtube.com', 'yt-first'), ('.bilibili.com', 'bili-first')])
        assert result.status_code == 200
        assert result.json()['site_count'] == 2 and result.json()['count'] == 2
        assert {site['site_key'] for site in result.json()['sites']} == {'youtube', 'bilibili'}
        result = upload(client, [('.youtube.com', 'yt-second')], scope='site', website='https://youtu.be/watch')
        assert result.status_code == 200 and result.json()['site_count'] == 1
        assert value('youtube') == 'yt-second'
        assert value('bilibili') == 'bili-first'
        assert client.post('/api/cookies/test', headers=HEADERS).json()['site_count'] == 2
        assert client.get('/api/cookie-status', headers=HEADERS).json()['exists']
        with main.private_media(ADMIN_ID, 'https://www.bilibili.com/video/example', {}) as ydl:
            assert [entry.value for entry in ydl.cookiejar] == ['bili-first']
        assert client.delete('/api/sites/youtube', headers=HEADERS).status_code == 200
        assert main.auth.store.site(ADMIN_ID, 'youtube') is None
        assert value('bilibili') == 'bili-first'


def test_selected_site_filters_out_other_sites_in_a_mixed_file():
    with TestClient(main.app) as client:
        upload(client, [('.bilibili.com', 'keep-bili')])
        response = upload(client, [('.youtube.com', 'new-youtube'), ('.bilibili.com', 'ignore-bili')],
                          scope='site', website='youtube.com')
        assert response.status_code == 200 and response.json()['count'] == 1
        assert value('youtube') == 'new-youtube' and value('bilibili') == 'keep-bili'


def test_all_sites_only_updates_sites_present_in_file():
    with TestClient(main.app) as client:
        upload(client, [('.youtube.com', 'keep-youtube'), ('.bilibili.com', 'old-bili')])
        response = upload(client, [('.bilibili.com', 'new-bili'), ('.vimeo.com', 'new-vimeo')], scope='all')
        assert response.status_code == 200
        assert value('youtube') == 'keep-youtube'
        assert value('bilibili') == 'new-bili' and value('vimeo') == 'new-vimeo'


def test_invalid_or_missing_target_does_not_modify_any_saved_site():
    with TestClient(main.app) as client:
        upload(client, [('.youtube.com', 'keep')])
        before = main.auth.store.list_sites(ADMIN_ID)
        for data, status in (({'scope': 'site', 'website': 'vimeo.com'}, 400),
                             ({'scope': 'site', 'website': ''}, 400),
                             ({'scope': 'site', 'website': 'file:///etc/passwd'}, 400),
                             ({'scope': 'site', 'website': 'youtube.com.evil.example'}, 400),
                             ({'scope': 'invalid'}, 422)):
            response = upload(client, [('.youtube.com', 'wrong')], **data)
            assert response.status_code == status
            assert main.auth.store.list_sites(ADMIN_ID) == before
            assert value('youtube') == 'keep'


def test_legacy_import_is_split_without_resurrection_after_site_removal():
    main.auth.store.save_site(ADMIN_ID, '*', '個人匯入檔案', '',
                             [cookie('.youtube.com', 'legacy-yt'), cookie('.bilibili.com', 'legacy-bili')])
    with TestClient(main.app) as client:
        response = upload(client, [('.youtube.com', 'updated-yt')])
        assert response.status_code == 200
        assert main.auth.store.site(ADMIN_ID, '*') is None
        assert value('bilibili') == 'legacy-bili' and value('youtube') == 'updated-yt'
        assert client.delete('/api/sites/bilibili', headers=HEADERS).status_code == 200
        assert main.auth.store.site(ADMIN_ID, 'bilibili') is None
        assert value('youtube') == 'updated-yt'


def test_legacy_migration_keeps_newer_site_specific_data():
    main.auth.store.save_site(ADMIN_ID, '*', 'old', '',
                             [cookie('.youtube.com', 'old-yt'), cookie('.vimeo.com', 'old-vimeo')])
    main.auth.store.save_site(ADMIN_ID, 'youtube', 'YouTube', '', [cookie('.youtube.com', 'new-yt')])
    listed = main.auth.store.list_sites(ADMIN_ID)
    assert {site['site_key'] for site in listed} == {'youtube', 'vimeo'}
    assert value('youtube') == 'new-yt' and value('vimeo') == 'old-vimeo'


def test_concurrent_imports_and_other_user_data_do_not_overwrite_each_other():
    alice = main.auth.store.create_user('alice', 'Alice', 'cookie-scope-user-password')
    main.auth.store.import_cookie_sites(alice['id'], import_cookie_groups([cookie('.youtube.com', 'alice-yt')]))
    domains = ['youtube.com', 'bilibili.com', 'vimeo.com', 'example.com']
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda domain: main.auth.store.import_cookie_sites(ADMIN_ID,
                      import_cookie_groups([cookie('.' + domain, 'admin-' + domain)])), domains))
    assert len(main.auth.store.list_sites(ADMIN_ID)) == 4
    assert value('youtube', alice['id']) == 'alice-yt'
    with TestClient(main.app) as client:
        assert client.delete('/api/cookies', headers=HEADERS).status_code == 200
        assert main.auth.store.list_sites(ADMIN_ID) == []
        assert value('youtube', alice['id']) == 'alice-yt'
    data = Path(main.auth.store.path).read_bytes()
    assert b'alice-yt' not in data and b'admin-youtube.com' not in data


def test_subdomains_and_provider_aliases_share_a_site_without_changing_cookie_domains():
    groups = import_cookie_groups([cookie('www.youtube.com', 'yt'), cookie('.googlevideo.com', 'media'),
                                   cookie('player.example.co.uk', 'uk-player'), cookie('.example.co.uk', 'uk-root')])
    assert set(groups) == {'youtube', 'example.co.uk'}
    assert {item['domain'] for item in groups['youtube']['cookies']} == {'www.youtube.com', '.googlevideo.com'}


def test_unknown_suffix_parent_and_host_cookie_scopes_both_apply():
    main.auth.store.import_cookie_sites(ADMIN_ID, import_cookie_groups([
        cookie('.media.unknownsuffix', 'parent-session'), cookie('video.media.unknownsuffix', 'host-session')]))
    with main.private_media(ADMIN_ID, 'https://video.media.unknownsuffix/watch', {}) as ydl:
        assert {entry.value for entry in ydl.cookiejar} == {'parent-session', 'host-session'}
