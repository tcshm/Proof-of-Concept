# app/processing/aruba.py
"""Aruba payload extractors.

This is the ONLY module that knows the Aruba payload format. Each supported
envelope event_type has one extractor that turns the nested payload into a
flat ExtractedEvent whose fields map onto network_events columns. Payload
fields without a column are kept in ExtractedEvent.details, so nothing is lost.

When the real Aruba format is confirmed (field names, timestamps in seconds or
milliseconds, event type names), only this module changes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

DEVICE_STATUS = "aruba.device.status"
GATEWAY_UPLINK = "aruba.gateway.uplink"
WAN_HEALTH = "aruba.gateway.wan_health"
CLIENT_EVENT = "aruba.client.event"

# What an event is about: the reporting element itself, or the router / WAN link
# behind one of the gateway's uplinks.
Target = Literal["element", "router", "wan_link"]

_STATUS_WORDS = {"UP": "online", "DOWN": "offline"}

# Payload keys that are copied into typed columns (everything else goes to details).
_TOP_LEVEL_KEYS = {"id", "nid", "alert_type", "severity", "description", "details"}
_IDENTITY_KEYS = {"serial", "hostname", "macaddr", "device_type"}

# Client fields: payload details key -> (network_events column, kind)
_CLIENT_FIELDS: dict[str, tuple[str, str]] = {
    "client_mac": ("client_mac", "mac"),
    "client_ip": ("client_ip", "text"),
    "client_hostname": ("client_hostname", "text"),
    "username": ("username", "text"),
    "user_role": ("user_role", "text"),
    "vlan": ("vlan", "text"),
    "bssid": ("bssid", "text"),
    "ssid": ("ssid", "text"),
    "band": ("band", "text"),
    "channel": ("channel", "text"),
    "rssi": ("rssi", "int"),
    "snr": ("snr", "int"),
    "retry_rate": ("retry_rate", "int"),
    "reason": ("reason", "text"),
    "dhcp_latency_ms": ("dhcp_latency_ms", "int"),
    "roaming_latency_ms": ("roaming_latency_ms", "int"),
}

# Millisecond epoch timestamps are larger than this; second timestamps are not.
_MILLISECONDS_THRESHOLD = 100_000_000_000


class ArubaPayloadError(ValueError):
    """The payload is missing a required field or has an unexpected value."""


class UnsupportedEventType(ArubaPayloadError):
    """No extractor exists for this envelope event_type."""


@dataclass(frozen=True)
class ExtractedEvent:
    target: Target
    vendor_event_uuid: str | None
    vendor_event_number: str | None
    vendor_event_type: str
    level: str | None
    description: str | None
    aruba_device_type: str | None
    hostname: str | None
    device_serial: str | None
    device_mac: str | None
    reported_status: str | None
    uplink_name: str | None
    client: dict[str, Any] = field(default_factory=dict)
    details: dict[str, Any] = field(default_factory=dict)


# --- helpers -------------------------------------------------------------------
def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ArubaPayloadError(f"expected a number, got {value!r}") from exc


def _mac(value: Any) -> str | None:
    text = _text(value)
    return None if text is None else text.lower().replace("-", ":")


def parse_timestamp(value: Any) -> datetime | None:
    """Epoch seconds or milliseconds -> aware UTC datetime."""
    number = _int(value)
    if number is None:
        return None
    if number > _MILLISECONDS_THRESHOLD:
        return datetime.fromtimestamp(number / 1000, UTC)
    return datetime.fromtimestamp(number, UTC)


def _status(value: Any, field_name: str) -> str:
    word = _text(value)
    if word is None:
        raise ArubaPayloadError(f"details.{field_name} is missing")
    status = _STATUS_WORDS.get(word.upper())
    if status is None:
        raise ArubaPayloadError(f"details.{field_name} must be UP or DOWN, got {word!r}")
    return status


def _details_of(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ArubaPayloadError("payload must be a JSON object")
    details = payload.get("details")
    if not isinstance(details, dict):
        raise ArubaPayloadError("payload.details must be a JSON object")
    return details


def _leftovers(payload: dict[str, Any], details: dict[str, Any], used: set[str]) -> dict[str, Any]:
    """Everything not copied into a column, plus the Aruba timestamp as ISO text."""
    extra: dict[str, Any] = {k: v for k, v in payload.items() if k not in _TOP_LEVEL_KEYS}
    if "timestamp" in extra:
        parsed = parse_timestamp(extra.pop("timestamp"))
        extra["aruba_timestamp"] = parsed.isoformat() if parsed else None
    extra.update({k: v for k, v in details.items() if k not in used and v is not None})
    return extra


def _common(
    payload: dict[str, Any], details: dict[str, Any], target: Target, used: set[str], **fields: Any
) -> ExtractedEvent:
    return ExtractedEvent(
        target=target,
        vendor_event_uuid=_text(payload.get("id")),
        vendor_event_number=_text(payload.get("nid")),
        vendor_event_type=_text(payload.get("alert_type")) or "unknown",
        level=_text(payload.get("severity")),
        description=_text(payload.get("description")),
        aruba_device_type=_text(details.get("device_type")),
        hostname=_text(details.get("hostname")),
        device_serial=_text(details.get("serial")),
        device_mac=_mac(details.get("macaddr")),
        details=_leftovers(payload, details, used | _IDENTITY_KEYS),
        **fields,
    )


def _require_identity(details: dict[str, Any]) -> None:
    if _text(details.get("serial")) is None and _text(details.get("macaddr")) is None:
        raise ArubaPayloadError("details.serial and details.macaddr are both missing")


# --- extractors ----------------------------------------------------------------
def extract_device_status(payload: Any) -> ExtractedEvent:
    details = _details_of(payload)
    _require_identity(details)
    return _common(
        payload,
        details,
        "element",
        {"status"},
        reported_status=_status(details.get("status"), "status"),
        uplink_name=None,
    )


def _extract_uplink(payload: Any, target: Target, status_field: str) -> ExtractedEvent:
    details = _details_of(payload)
    _require_identity(details)
    uplink = _text(details.get("uplink"))
    if uplink is None:
        raise ArubaPayloadError("details.uplink is missing")
    return _common(
        payload,
        details,
        target,
        {"uplink", status_field},
        reported_status=_status(details.get(status_field), status_field),
        uplink_name=uplink,
    )


def extract_gateway_uplink(payload: Any) -> ExtractedEvent:
    """Uplink link state: the status of the ROUTER on that uplink."""
    return _extract_uplink(payload, "router", "link_status")


def extract_wan_health(payload: Any) -> ExtractedEvent:
    """WAN health: the status of the WAN LINK on that uplink."""
    return _extract_uplink(payload, "wan_link", "wan_status")


def extract_client_event(payload: Any) -> ExtractedEvent:
    """A client event reported by an access point. Carries no element status."""
    details = _details_of(payload)
    _require_identity(details)
    client: dict[str, Any] = {}
    for key, (column, kind) in _CLIENT_FIELDS.items():
        value = details.get(key)
        if kind == "int":
            client[column] = _int(value)
        elif kind == "mac":
            client[column] = _mac(value)
        else:
            client[column] = _text(value)
    return _common(
        payload,
        details,
        "element",
        set(_CLIENT_FIELDS),
        reported_status=None,
        uplink_name=None,
        client=client,
    )


EXTRACTORS: dict[str, Callable[[Any], ExtractedEvent]] = {
    DEVICE_STATUS: extract_device_status,
    GATEWAY_UPLINK: extract_gateway_uplink,
    WAN_HEALTH: extract_wan_health,
    CLIENT_EVENT: extract_client_event,
}


def extract(event_type: str, payload: Any) -> ExtractedEvent:
    extractor = EXTRACTORS.get(event_type)
    if extractor is None:
        raise UnsupportedEventType(f"unsupported event type {event_type!r}")
    return extractor(payload)
