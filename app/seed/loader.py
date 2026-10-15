# app/seed/loader.py
"""Read the seed files, validate each one, then check references between files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from app.db.migrate import PROJECT_ROOT
from app.seed.schemas import (
    InventoryTemplate,
    MasterSeed,
    OverridesSeed,
    PromptsSeed,
    ScenariosSeed,
    StoresSeed,
)

DEFAULT_DATA_DIR = PROJECT_ROOT / "seed" / "data"


class SeedDataError(ValueError):
    """A seed file is missing, malformed or inconsistent with another file."""


@dataclass(frozen=True)
class SeedData:
    master: MasterSeed
    stores: StoresSeed
    template: InventoryTemplate
    overrides: OverridesSeed
    scenarios: ScenariosSeed
    prompts: PromptsSeed


def _load[M: BaseModel](path: Path, model: type[M]) -> M:
    if not path.is_file():
        raise SeedDataError(f"{path.name}: file not found in {path.parent}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SeedDataError(f"{path.name}: invalid YAML: {exc}") from exc
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise SeedDataError(f"{path.name}: {exc}") from exc


def device_names(template: InventoryTemplate) -> set[str]:
    """Every device name the template produces (names do not depend on the store)."""
    return {
        device.name.format(n=n, h=10 + n, site="", net="")
        for device in template.devices
        for n in range(1, device.count + 1)
    }


def _cross_check(data: SeedData) -> None:
    errors: list[str] = []
    sites = {s.site_code for s in data.stores.stores}
    element_types = {t.code for t in data.master.network_element_types}
    device_types = {t.code for t in data.master.device_types}
    keys = {e.key for e in data.template.network_elements}
    key_types = {e.key: e.type for e in data.template.network_elements}
    names = device_names(data.template)

    for element in data.template.network_elements:
        if element.type not in element_types:
            errors.append(f"inventory_template.yaml: {element.key}: unknown type {element.type!r}")
    for device in data.template.devices:
        if device.type not in device_types:
            errors.append(f"inventory_template.yaml: unknown device type {device.type!r}")

    for site, override in data.overrides.stores.items():
        if site not in sites:
            errors.append(f"store_overrides.yaml: unknown site_code {site!r}")
        for name in override.devices:
            if name not in names:
                errors.append(f"store_overrides.yaml: {site}: unknown device {name!r}")

    for site, scenario in data.scenarios.stores.items():
        if site not in sites:
            errors.append(f"event_scenarios.yaml: unknown site_code {site!r}")
        refs = [o.element for o in scenario.outages]
        refs += [d.element for d in scenario.duplicates]
        refs += [late.element for late in scenario.late_events]
        for ref in refs:
            if ref not in keys:
                errors.append(f"event_scenarios.yaml: {site}: unknown element key {ref!r}")
        outage_keys = {o.element for o in scenario.outages}
        for duplicate in scenario.duplicates:
            if duplicate.element not in outage_keys:
                errors.append(
                    f"event_scenarios.yaml: {site}: duplicate of {duplicate.element!r} "
                    "needs an outage for that element"
                )
        for edge in scenario.unmatched:
            if edge.serial in {e.serial for e in data.template.network_elements}:
                errors.append(f"event_scenarios.yaml: {site}: {edge.serial} is in the template")

    gateway_refs = {e.source_gateway for e in data.template.network_elements if e.source_gateway}
    for ref in gateway_refs:
        if key_types.get(ref) != "gateway":
            errors.append(f"inventory_template.yaml: source_gateway {ref!r} is not a gateway")

    if errors:
        raise SeedDataError("\n".join(errors))


def load_seed_data(data_dir: Path = DEFAULT_DATA_DIR) -> SeedData:
    data = SeedData(
        master=_load(data_dir / "master.yaml", MasterSeed),
        stores=_load(data_dir / "stores.yaml", StoresSeed),
        template=_load(data_dir / "inventory_template.yaml", InventoryTemplate),
        overrides=_load(data_dir / "store_overrides.yaml", OverridesSeed),
        scenarios=_load(data_dir / "event_scenarios.yaml", ScenariosSeed),
        prompts=_load(data_dir / "prompts.yaml", PromptsSeed),
    )
    _cross_check(data)
    return data
