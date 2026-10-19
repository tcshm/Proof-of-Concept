# app/api/router.py
"""Collects the business routers under one versioned prefix.

Each domain adds its router here. Health routes are NOT under this prefix;
they stay at the root (/health).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.routes import store

API_PREFIX = "/api/v1"

api_router = APIRouter()
api_router.include_router(store.router)
