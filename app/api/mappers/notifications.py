# app/api/mappers/notifications.py
"""Notification display values for the mobile app."""

from __future__ import annotations

from app.models.values import NotificationCategory

# Stored category code -> the label the mobile app uses (its mock categories).
CATEGORY_LABELS: dict[str, str] = {
    NotificationCategory.MAINTENANCE: "Maintenance",
    NotificationCategory.UPDATES: "Updates",
    NotificationCategory.INCIDENTS: "Incidents",
    NotificationCategory.APPOINTMENT: "Appointment",
    NotificationCategory.TRAINING: "Training",
    NotificationCategory.OTHERS: "Others",
}


def category_label(code: str) -> str:
    return CATEGORY_LABELS.get(code, "Others")
