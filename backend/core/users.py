"""User helpers: the default local user for single-user (AUTH_REQUIRED=false) mode."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.ids import iso
from backend.db.models import User

LOCAL_USER_ID = "usr_local"
LOCAL_USER_EMAIL = "local@jevora.local"


async def ensure_local_user(sessions: async_sessionmaker[AsyncSession]) -> User:
    async with sessions() as session:
        user = await session.get(User, LOCAL_USER_ID)
        if user is None:
            user = User(id=LOCAL_USER_ID, email=LOCAL_USER_EMAIL, name="Local User", is_local=True)
            session.add(user)
            await session.commit()
            await session.refresh(user)
        return user


def user_public(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "avatar_url": user.avatar_url,
        "is_local": user.is_local,
        "created_at": iso(user.created_at),
    }
