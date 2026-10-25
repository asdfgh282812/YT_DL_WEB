"""Private, user-owned website cookie management routes."""
from pathlib import Path

from fastapi import Request

from cookie_scopes import site_for_url, needs_login, domain_matches
from user_store import ADMIN_ID


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
