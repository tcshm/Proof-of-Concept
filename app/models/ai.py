# app/models/ai.py
"""Group 4: AI workflow (versioned prompts and a record of every LLM call)."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ForeignKey, Integer, Numeric, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAt, UUIDPk


class AiPromptVersion(Base):
    __tablename__ = "ai_prompt_versions"
    __table_args__ = (UniqueConstraint("workflow", "version"),)

    id: Mapped[UUIDPk]
    workflow: Mapped[str]
    version: Mapped[int] = mapped_column(Integer)
    system_prompt: Mapped[str]
    user_prompt_template: Mapped[str]
    output_schema: Mapped[dict[str, Any]] = mapped_column(JSONB)
    model_name: Mapped[str]
    temperature: Mapped[Decimal] = mapped_column(Numeric(3, 2), server_default=text("0"))
    is_active: Mapped[bool] = mapped_column(server_default=text("false"))
    change_notes: Mapped[str | None]
    created_by: Mapped[str]
    created_at: Mapped[CreatedAt]


class AiWorkflowRun(Base):
    __tablename__ = "ai_workflow_runs"

    id: Mapped[UUIDPk]
    workflow: Mapped[str]
    journal_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incident_journal.id"))
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"))
    prompt_version_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ai_prompt_versions.id"))
    model_name: Mapped[str]
    input_context: Mapped[dict[str, Any]] = mapped_column(JSONB)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    outcome: Mapped[str | None]
    status: Mapped[str] = mapped_column(server_default=text("'running'"))
    error_message: Mapped[str | None]
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    completed_at: Mapped[datetime | None]
