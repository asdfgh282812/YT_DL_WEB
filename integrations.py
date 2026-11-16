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


def bridge_secret():
    return os.environ.get('YTDL_BRIDGE_SECRET', '').strip()


def install_bridge_routes(app, auth, broadcast):
    @app.post('/api/integrations/hh/grants')
    async def hh_grant(request: Request):
        secret = bridge_secret()
        if not secret:
            raise HTTPException(404, 'Not Found')
        header = request.headers.get('authorization', '')
        supplied = header[7:] if header.startswith('Bearer ') else ''
        if not hmac.compare_digest(supplied.encode(), secret.encode()):
            raise HTTPException(401, '橋接密鑰錯誤')
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
