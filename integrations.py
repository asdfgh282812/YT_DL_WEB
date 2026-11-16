"""Server-to-server bridge: TallyCheck/BeeCount Cloud grants bonus daily limit bought with 好好幣."""
import asyncio
import hmac
import os
import time
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, ValidationError, field_validator

from user_store import StoreError

MAX_GRANT_DAYS = 30
MAX_SUPPORTERS = 5000


class HhGrant(BaseModel):
    requestId: str
    subject: str = Field(min_length=1, max_length=255)
    units: int = Field(ge=1, le=100)
    expiresAt: str

    @field_validator('requestId')
    @classmethod
    def valid_uuid(cls, value):
        uuid.UUID(value)
        return value


class Supporter(BaseModel):
    subject: str = Field(min_length=1, max_length=255)
    units: int = Field(ge=1, le=100)
    until: str


class HhSupporters(BaseModel):
    supporters: list[Supporter] = Field(max_length=MAX_SUPPORTERS)


def bridge_secret():
    return os.environ.get('YTDL_BRIDGE_SECRET', '').strip()


def require_bridge(request: Request):
    secret = bridge_secret()
    if not secret:
        raise HTTPException(404, 'Not Found')
    header = request.headers.get('authorization', '')
    supplied = header[7:] if header.startswith('Bearer ') else ''
    if not hmac.compare_digest(supplied.encode(), secret.encode()):
        raise HTTPException(401, '橋接密鑰錯誤')


def parse_instant(value):
    instant = datetime.fromisoformat(value)
    return (instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc)).timestamp()


def install_bridge_routes(app, auth, broadcast):
    @app.post('/api/integrations/hh/grants')
    async def hh_grant(request: Request):
        require_bridge(request)
        try:
            grant = HhGrant.model_validate(await request.json())
            expires = datetime.fromisoformat(grant.expiresAt)
        except (ValidationError, ValueError, TypeError):
            raise HTTPException(422, '請求內容格式錯誤')
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        expires_at, now = expires.timestamp(), time.time()
        if expires_at <= now or expires_at > now + MAX_GRANT_DAYS * 86400:
            raise HTTPException(422, f'expiresAt 必須在現在之後、{MAX_GRANT_DAYS} 天內')
        try:
            duplicate, active = await asyncio.to_thread(auth.store.add_grant, grant.requestId, grant.subject, grant.units, expires_at)
        except StoreError as exc:
            raise HTTPException(409, str(exc))
        if not duplicate:
            await broadcast()
        return {'granted': True, 'duplicate': duplicate, 'activeUnits': active}

    @app.put('/api/integrations/hh/supporters')
    async def hh_supporters(request: Request):
        """Cloud's periodic full snapshot of active supporters (400 plan); absent subjects lose the bonus."""
        require_bridge(request)
        try:
            payload = HhSupporters.model_validate(await request.json())
            rows = {item.subject: (item.subject, item.units, parse_instant(item.until)) for item in payload.supporters}
        except (ValidationError, ValueError, TypeError):
            raise HTTPException(422, '請求內容格式錯誤')
        now = time.time()
        count = await asyncio.to_thread(auth.store.replace_supporters, [row for row in rows.values() if row[2] > now])
        await broadcast()
        return {'replaced': True, 'count': count}
