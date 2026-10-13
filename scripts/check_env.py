# scripts/check_env.py
"""Verify the local environment is ready for development.

uv run python -m scripts.check_env
"""

from __future__ import annotations

import asyncio
import sys
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings

MIN_PG_VERSION_NUM = 130000
results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    results.append((ok, label))
    print(f"[{'OK' if ok else 'FAIL'}] {label}")


def check_python() -> None:
    v = sys.version_info
    check(v >= (3, 12), f"Python {v.major}.{v.minor}.{v.micro} (need >= 3.12)")


def check_timezones() -> None:
    try:
        ZoneInfo("Europe/London")
        ZoneInfo("Asia/Kolkata")
        check(True, "IANA timezone database available (tzdata)")
    except Exception as exc:
        check(False, f"IANA timezone database missing: {exc}")


async def check_database(url: URL) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            version_num = int(await conn.scalar(text("SHOW server_version_num")))
            version = await conn.scalar(text("SHOW server_version"))
            encoding = await conn.scalar(text("SHOW server_encoding"))
            tz = await conn.scalar(text("SHOW TimeZone"))
            user = await conn.scalar(text("SELECT current_user"))
            is_super = await conn.scalar(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            )
            await conn.scalar(text("SELECT gen_random_uuid()"))

        check(True, f"{url.database}: connected as {user}")
        check(
            version_num >= MIN_PG_VERSION_NUM,
            f"{url.database}: PostgreSQL {version} (need >= 13)",
        )
        check(encoding == "UTF8", f"{url.database}: encoding {encoding}")
        check(tz == "UTC", f"{url.database}: session timezone {tz}")
        check(not is_super, f"{url.database}: app role is not a superuser")
    except Exception as exc:
        check(False, f"{url.database}: cannot connect ({exc.__class__.__name__}: {exc})")
    finally:
        await engine.dispose()


async def main() -> int:
    check_python()
    check_timezones()

    settings = get_settings()
    main_url = make_url(settings.database_url)
    test_url = main_url.set(database=f"{main_url.database}_test")

    await check_database(main_url)
    await check_database(test_url)

    failed = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
