# app/api/router.py
"""Collects the business routers under one versioned prefix.

From Phase 5 onwards each domain adds its router here, for example:

    from app.api.routes import network
    api_router.include_router(network.router)

Health routes are NOT under this prefix; they stay at the root (/health).
"""

from __future__ import annotations

from fastapi import APIRouter

API_PREFIX = "/api/v1"

api_router = APIRouter()
