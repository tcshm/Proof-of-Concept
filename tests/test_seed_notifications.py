# tests/test_seed_notifications.py
"""Seed: appointment notices (itsm step) and portal announcements (notifications step)."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Notification, NotificationTarget
from app.queries.notifications import live_notifications, unread_count
from app.seed import ids
from app.seed.itsm import build_itsm
from app.seed.loader import DEFAULT_DATA_DIR, SeedDataError, load_seed_data
from app.seed.notifications import load_announcements
from app.seed.runner import STEP_ORDER, STEPS

ANCHOR = datetime(2026, 10, 13, 3, 30, tzinfo=UTC)  # 09:00 IST
CHENNAI = ids.store_id("01234")


def test_appointment_notices_are_built_with_the_appointments() -> None:
    rows = build_itsm(load_seed_data(), ANCHOR)
    assert len(rows.notifications) == len(rows.appointments) == 6
    titles = {n["title"]: n for n in rows.notifications}
    isp = titles["New Appointment: ISP Technician at 01:00 PM"]  # mock n-004
    assert isp["publish_at"] == ANCHOR - timedelta(hours=1)
    assert isp["system_event"] == "appointment_scheduled" and isp["source"] == "system"
    assert "New Appointment: Third Party Technician at 02:00 PM" in titles  # AP-004, 2 PM
    for notice, target, appointment in zip(
        rows.notifications, rows.notification_targets, rows.appointments, strict=True
    ):
        assert notice["appointment_id"] == appointment["id"]
        assert notice["incident_id"] == appointment["incident_id"]
        assert target["store_id"] == appointment["store_id"]
        assert notice["publish_at"] < appointment["scheduled_start"]
        assert notice["expires_at"] == appointment["scheduled_end"] + timedelta(hours=24)


def test_announcement_file() -> None:
    seeds = load_announcements()
    keys = [n.key for n in seeds.notifications]
    assert {"n-001", "n-002", "n-008", "n-009", "n-010", "n-011"} <= set(keys)
    assert len(keys) == 10
    n009 = next(n for n in seeds.notifications if n.key == "n-009")
    assert n009.sites == ["01234"] and not n009.all_stores


SITES = '\n    sites: ["01234"]'  # n-009 only (not read_by_sites)


def _broken_copy(tmp_path: Path, old: str, new: str) -> Path:
    target = tmp_path / "data"
    shutil.copytree(DEFAULT_DATA_DIR, target)
    path = target / "notifications.yaml"
    text = path.read_text(encoding="utf-8")
    assert old in text
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return target


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (SITES, SITES + "\n    all_stores: true", "exactly one of"),
        ('category: "training"', 'category: "news"', "notifications.yaml"),
        ('key: "n-002"', 'key: "n-001"', "duplicate keys"),
    ],
)
def test_broken_announcement_file(tmp_path: Path, old: str, new: str, message: str) -> None:
    with pytest.raises(SeedDataError, match=message):
        load_announcements(_broken_copy(tmp_path, old, new))


# --- database -------------------------------------------------------------------------
async def _seed(session: AsyncSession, anchor: datetime = ANCHOR) -> None:
    data = load_seed_data()
    for name in STEP_ORDER:
        await STEPS[name](session, data, anchor)


async def _counts(session: AsyncSession) -> dict[tuple[str, str], int]:
    rows = await session.execute(
        select(Notification.source, Notification.status, func.count()).group_by(
            Notification.source, Notification.status
        )
    )
    return {(source, status): count for source, status, count in rows}


async def test_seeded_notifications(db_session: AsyncSession) -> None:
    await _seed(db_session)
    assert await _counts(db_session) == {
        ("system", "published"): 6,
        ("seed", "published"): 9,
        ("seed", "withdrawn"): 1,
    }
    assert await db_session.scalar(select(func.count()).select_from(NotificationTarget)) == 90

    chennai = await live_notifications(db_session, CHENNAI, ANCHOR)
    assert [n.title for n in chennai] == [
        "Network maintenance activity scheduled tonight at 1 AM - No action needed from stores",
        "Deployment for curbside application planned on 6th Dec - No action needed from stores",
        "New Appointment: ISP Technician at 01:00 PM",
        "New Appointment: Third Party Technician at 02:00 PM",
        "New Appointment: Site Visit - Field Tech Engineer at 12:00 PM",
        "POS software update for India stores this Sunday",
        "Store Manager Message: Team Meeting at 3PM",
        "Training Module Available: Network Basics",
        "Software update tonight for mobile devices",
        "Policy Update: New IT Security Guidelines",
    ]
    assert await unread_count(db_session, CHENNAI, ANCHOR) == 7

    berlin = await live_notifications(db_session, ids.store_id("02112"), ANCHOR)
    assert len(berlin) == 5
    bengaluru = await live_notifications(db_session, ids.store_id("01456"), ANCHOR)
    assert len(bengaluru) == 7  # 5 for all stores + India rollout + Airtel visit

    # The scheduled maintenance notice goes live 6 hours after the anchor.
    later = await live_notifications(db_session, CHENNAI, ANCHOR + timedelta(hours=6))
    assert "Wi-Fi controller upgrade tomorrow at 2 AM - No action needed from stores" in [
        n.title for n in later
    ]


async def test_seed_re_run_replaces_notifications(db_session: AsyncSession) -> None:
    await _seed(db_session)
    first = await _counts(db_session)
    await _seed(db_session, ANCHOR + timedelta(hours=1))
    assert await _counts(db_session) == first
    assert await db_session.scalar(select(func.count()).select_from(NotificationTarget)) == 90
