# tests/test_seed_files.py
"""The committed seed files are valid, and broken files fail with a clear message."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.seed.anchor import parse_offset, resolve_anchor
from app.seed.loader import DEFAULT_DATA_DIR, SeedDataError, load_seed_data


def test_committed_seed_files_are_valid() -> None:
    data = load_seed_data()
    assert len(data.stores.stores) == 10
    assert len(data.master.assignment_groups) == 4
    assert {t.code for t in data.master.network_element_types} == {
        "wan_link",
        "gateway",
        "router",
        "switch",
        "access_point",
    }
    assert {t.code for t in data.master.device_types} == {
        "pos",
        "register",
        "rf_gun",
        "printer",
        "workstation",
    }
    assert len(data.prompts.prompts) == 2


def test_chennai_is_store_01234() -> None:
    store = next(s for s in load_seed_data().stores.stores if s.site_code == "01234")
    assert store.city == "Chennai"
    assert store.timezone == "Asia/Kolkata"


def _broken_copy(tmp_path: Path, file: str, old: str, new: str) -> Path:
    target = tmp_path / "data"
    shutil.copytree(DEFAULT_DATA_DIR, target)
    path = target / file
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not found in {file}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return target


@pytest.mark.parametrize(
    ("file", "old", "new", "message"),
    [
        ("stores.yaml", '"Asia/Kolkata"', '"Asia/Chenai"', "unknown IANA timezone"),
        ("stores.yaml", 'site_code: "01234"', 'site_code: "1234"', "stores.yaml"),
        ("inventory_template.yaml", 'type: "rf_gun"', 'type: "pin_pad"', "unknown device type"),
        ("store_overrides.yaml", '"Register 04"', '"Register 09"', "unknown device"),
        ("event_scenarios.yaml", 'element: "ap004"', 'element: "ap099"', "unknown element key"),
        ("event_scenarios.yaml", 'down_at: "-6h"', 'down_at: "-30h"', "outside the 24-hour"),
    ],
)
def test_broken_file_fails_with_clear_message(
    tmp_path: Path, file: str, old: str, new: str, message: str
) -> None:
    with pytest.raises(SeedDataError, match=message):
        load_seed_data(_broken_copy(tmp_path, file, old, new))


def test_offsets_and_anchor() -> None:
    assert parse_offset("-5h").total_seconds() == -5 * 3600
    assert parse_offset("-2h20m").total_seconds() == -(2 * 3600 + 20 * 60)
    assert parse_offset("-90m").total_seconds() == -90 * 60
    for bad in ("5h", "-", "-2x", "+1h"):
        with pytest.raises(ValueError):
            parse_offset(bad)
    anchor = resolve_anchor("2026-10-13T09:15:42+05:30")
    assert anchor.isoformat() == "2026-10-13T03:45:00+00:00"
    with pytest.raises(ValueError):
        resolve_anchor("2026-10-13T09:00:00")
