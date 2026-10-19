# scripts/request_log_tail.py
"""Show the most recent api_request_log rows.

uv run python -m scripts.request_log_tail            last 20 requests
uv run python -m scripts.request_log_tail -n 50      last 50
uv run python -m scripts.request_log_tail --site 01234
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.db.session import create_engine_from_settings, create_session_factory
from app.models import ApiRequestLog


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show recent API requests.")
    parser.add_argument("-n", type=int, default=20, help="number of rows (default 20)")
    parser.add_argument("--site", help="only requests for this site code")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    query = select(ApiRequestLog).order_by(ApiRequestLog.id.desc()).limit(args.n)
    if args.site:
        query = query.where(ApiRequestLog.site_code == args.site)

    engine = create_engine_from_settings()
    try:
        async with create_session_factory(engine)() as session:
            rows = list((await session.scalars(query)).all())
    finally:
        await engine.dispose()

    print(f"{'time (UTC)':<19}  {'app':<12} {'site':<5}  {'status':>6}  {'ms':>5}  route / error")
    for row in reversed(rows):
        when = row.occurred_at.strftime("%Y-%m-%d %H:%M:%S")
        target = f"{row.method} {row.route or row.path}"
        error = f"  [{row.error_code}]" if row.error_code else ""
        print(
            f"{when}  {row.client_app:<12} {row.site_code or '-':<5}  "
            f"{row.status_code:>6}  {row.duration_ms:>5}  {target}{error}"
        )
    print(f"{len(rows)} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
