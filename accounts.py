"""Administrator-controlled local users and global/per-user download limits."""
import asyncio
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from user_store import StoreError


class CreateUser(BaseModel):
    username: str = Field(min_length=3, max_length=64, pattern=r'^[A-Za-z0-9][A-Za-z0-9_.@-]+$')
    name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=10, max_length=256)
    role: Literal['admin', 'user'] = 'user'
    daily_limit: int | None = Field(default=None, ge=0, le=10000)

    @field_validator('username', 'name', mode='before')
    @classmethod
    def strip(cls, value):
        return value.strip() if isinstance(value, str) else value


class UpdateUser(BaseModel):
    name: str = Field(default='', min_length=1, max_length=80)
    role: Literal['admin', 'user'] = 'user'
    disabled: bool = False
    daily_limit: int | None = Field(default=None, ge=0, le=10000)
    password: str | None = Field(default=None, min_length=10, max_length=256)


class Policy(BaseModel):
    default_daily_limit: int = Field(ge=0, le=10000)


def install_account_routes(app, auth, broadcast):
    @app.get('/api/admin/users')
    async def users(request: Request):
        auth.require_admin(request)
        return {'users': auth.store.list_users(), 'policy': auth.store.policy()}

    @app.post('/api/admin/users')
    async def create(req: CreateUser, request: Request):
        auth.require_admin(request)
        try:
            user = await asyncio.to_thread(auth.store.create_user, **req.model_dump())
        except StoreError as exc:
            raise HTTPException(409, str(exc))
        await broadcast()
        return {'user': user}

    @app.patch('/api/admin/users/{user_id}')
    async def update(user_id: str, req: UpdateUser, request: Request):
        auth.require_admin(request)
        try:
            user = await asyncio.to_thread(auth.store.update_user, user_id, req.model_dump(exclude_unset=True))
        except StoreError as exc:
            raise HTTPException(400, str(exc))
        reauthenticate = bool(req.password) or bool(req.disabled)
        if reauthenticate:
            auth.revoke_user(user_id)
        await broadcast()
        return {'user': user, 'reauth_required': reauthenticate and request.state.user['id'] == user_id}

    @app.put('/api/admin/policy')
    async def update_policy(req: Policy, request: Request):
        auth.require_admin(request)
        result = auth.store.set_policy(req.default_daily_limit)
        await broadcast()
        return result
