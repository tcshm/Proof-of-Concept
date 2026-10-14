# app/models/values.py
"""Allowed values for text columns guarded by CHECK constraints.

Use these instead of string literals in services, schemas and seeds so a typo
fails in Python before it reaches the database. Keep them in sync with
migrations/sql (tests/test_values_match_db.py checks this).
"""

from __future__ import annotations

from enum import IntEnum, StrEnum


# --- Master data -------------------------------------------------------------
class StoreStatus(StrEnum):
    PLANNED = "planned"
    OPEN = "open"
    TEMPORARILY_CLOSED = "temporarily_closed"
    CLOSED = "closed"


class Criticality(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# --- Inventory ---------------------------------------------------------------
class DeviceStatus(StrEnum):
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class ElementStatus(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class ConnectionType(StrEnum):
    WIRED = "wired"
    WIRELESS = "wireless"


class RawProcessingStatus(StrEnum):
    PENDING = "pending"
    PROCESSED = "processed"
    DUPLICATE = "duplicate"
    UNMATCHED = "unmatched"
    ERROR = "error"


class MatchedBy(StrEnum):
    SERIAL = "serial"
    DEVICE_MAC = "device_mac"
    GATEWAY_UPLINK = "gateway_uplink"
    NONE = "none"


# --- ITSM --------------------------------------------------------------------
class IncidentState(IntEnum):
    NEW = 1
    IN_PROGRESS = 2
    ON_HOLD = 3
    RESOLVED = 6
    CLOSED = 7
    CANCELED = 8


ACTIVE_INCIDENT_STATES: tuple[int, ...] = (
    IncidentState.NEW,
    IncidentState.IN_PROGRESS,
    IncidentState.ON_HOLD,
)


class IncidentCategory(StrEnum):
    NETWORK = "network"
    HARDWARE = "hardware"
    SOFTWARE = "software"
    INQUIRY = "inquiry"


class ContactType(StrEnum):
    MONITORING = "monitoring"
    PHONE = "phone"
    EMAIL = "email"
    SELF_SERVICE = "self_service"
    WALK_IN = "walk_in"


class HoldReason(StrEnum):
    AWAITING_CALLER = "awaiting_caller"
    AWAITING_VENDOR = "awaiting_vendor"
    AWAITING_SITE_VISIT = "awaiting_site_visit"
    AWAITING_CHANGE = "awaiting_change"
    AWAITING_PROBLEM = "awaiting_problem"


class JournalElement(StrEnum):
    COMMENTS = "comments"
    WORK_NOTES = "work_notes"
    SYSTEM = "system"


class ActorType(StrEnum):
    """Used by incident_journal.author_type and audit_log.actor_type."""

    IT_STAFF = "it_staff"
    STORE_USER = "store_user"
    SYSTEM = "system"
    AGENT = "agent"


class JournalSource(StrEnum):
    SERVICENOW = "servicenow"
    ADMIN_PORTAL = "admin_portal"
    MOBILE_APP = "mobile_app"
    AGENT = "agent"
    SEED = "seed"


class JournalProcessStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"


class AppointmentType(StrEnum):
    FIELD_TECH = "field_tech"
    ISP_TECHNICIAN = "isp_technician"
    THIRD_PARTY_VENDOR = "third_party_vendor"
    USER_SUPPORT = "user_support"


class AppointmentStatus(StrEnum):
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class AppointmentSource(StrEnum):
    SERVICENOW = "servicenow"
    ADMIN_PORTAL = "admin_portal"
    AGENT = "agent"
    SEED = "seed"


# --- AI workflow -------------------------------------------------------------
class AiWorkflow(StrEnum):
    IMPACT_ASSESSMENT = "impact_assessment"
    UPDATE_CLASSIFICATION = "update_classification"


class AiRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


class AiOutcome(StrEnum):
    ALERT_CREATED = "alert_created"
    ACTION_CREATED = "action_created"
    NOTIFICATION_CREATED = "notification_created"
    NO_ACTION = "no_action"
    INVALID_OUTPUT = "invalid_output"


# --- Store communication -----------------------------------------------------
class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertSource(StrEnum):
    LLM = "llm"
    SYSTEM = "system"
    ADMIN_PORTAL = "admin_portal"
    SEED = "seed"


class AlertStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


class AlertCloseReason(StrEnum):
    INCIDENT_RESOLVED = "incident_resolved"
    INCIDENT_CANCELED = "incident_canceled"
    MANUAL = "manual"


class ActionSource(StrEnum):
    LLM = "llm"
    ADMIN_PORTAL = "admin_portal"
    SEED = "seed"


class ExpectedResponse(StrEnum):
    ACKNOWLEDGE = "acknowledge"
    YES_NO = "yes_no"
    FREE_TEXT = "free_text"


class ActionStatus(StrEnum):
    OPEN = "open"
    RESPONDED = "responded"
    CLOSED = "closed"


class ActionCloseReason(StrEnum):
    COMPLETED = "completed"
    INCIDENT_CLOSED = "incident_closed"
    EXPIRED = "expired"
    MANUAL = "manual"


class ResponseSource(StrEnum):
    MOBILE_APP = "mobile_app"
    ADMIN_PORTAL = "admin_portal"
    SEED = "seed"


class NotificationCategory(StrEnum):
    MAINTENANCE = "maintenance"
    UPDATES = "updates"
    INCIDENTS = "incidents"
    APPOINTMENT = "appointment"
    TRAINING = "training"
    OTHERS = "others"


class NotificationSource(StrEnum):
    ADMIN_PORTAL = "admin_portal"
    SYSTEM = "system"
    SEED = "seed"


class SystemEvent(StrEnum):
    INCIDENT_UPDATE = "incident_update"
    ACTION_REQUIRED = "action_required"
    APPOINTMENT_SCHEDULED = "appointment_scheduled"
    APPOINTMENT_RESCHEDULED = "appointment_rescheduled"
    APPOINTMENT_CANCELLED = "appointment_cancelled"


class NotificationStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    WITHDRAWN = "withdrawn"


# --- Audit and logs ----------------------------------------------------------
class AuditSource(StrEnum):
    ADMIN_PORTAL = "admin_portal"
    MOBILE_APP = "mobile_app"
    WORKER = "worker"
    PROCESSOR = "processor"
    AGENT = "agent"
    SEED = "seed"


class AuditAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    STATUS_CHANGE = "status_change"
    PUBLISH = "publish"
    WITHDRAW = "withdraw"
    RESPOND = "respond"
    DEACTIVATE = "deactivate"


class ClientApp(StrEnum):
    MOBILE_APP = "mobile_app"
    ADMIN_PORTAL = "admin_portal"
    OTHER = "other"


class ProcessingRunMode(StrEnum):
    REBUILD = "rebuild"
    INCREMENTAL = "incremental"


class ProcessingRunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
