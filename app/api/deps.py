# app/api/deps.py
"""Shared FastAPI dependencies.

The engine and session factory are created once per process in the lifespan
and stored on app.state; these dependencies hand them to each request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request, always closed afterwards.

    This dependency never commits. A service that writes calls commit() itself,
    so the success response is only sent after the data is saved. On error the
    transaction is rolled back; read-only requests simply close the session.
    """
    factory = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
