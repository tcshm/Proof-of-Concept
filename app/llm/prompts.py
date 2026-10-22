# app/llm/prompts.py
"""Load the active prompt version of a workflow and turn it into an LlmRequest.

ai_prompt_versions.user_prompt_template uses {name} placeholders. {context_json}
is always available: the call's context as pretty JSON. A placeholder the
template uses but no value is given for is an error, never an empty string.
Literal braces in a template must be doubled: {{ and }}.

The model is chosen by the settings (LLM_PROVIDER / LLM_MODEL), not by the prompt
row: model_name in ai_prompt_versions records the model the prompt was tuned for.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from string import Formatter
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.llm.types import LlmRequest
from app.llm.validation import check_schema
from app.models import AiPromptVersion
from app.models.values import AiWorkflow


class PromptError(RuntimeError):
    """No active prompt, or the template cannot be filled."""


@dataclass(frozen=True)
class PromptVersion:
    id: uuid.UUID
    workflow: str
    version: int
    system_prompt: str
    user_prompt_template: str
    output_schema: dict[str, Any]
    model_name: str
    temperature: float


async def load_active_prompt(session: AsyncSession, workflow: AiWorkflow | str) -> PromptVersion:
    row = await session.scalar(
        select(AiPromptVersion).where(
            AiPromptVersion.workflow == str(AiWorkflow(workflow)),
            AiPromptVersion.is_active.is_(True),
        )
    )
    if row is None:
        raise PromptError(f"no active prompt version for workflow {workflow!s}")
    check_schema(row.output_schema)
    return PromptVersion(
        id=row.id,
        workflow=row.workflow,
        version=row.version,
        system_prompt=row.system_prompt,
        user_prompt_template=row.user_prompt_template,
        output_schema=row.output_schema,
        model_name=row.model_name,
        temperature=float(row.temperature),
    )


def template_fields(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name}


def render_template(template: str, values: dict[str, Any]) -> str:
    missing = sorted(template_fields(template) - set(values))
    if missing:
        raise PromptError(f"no value for template placeholder(s): {', '.join(missing)}")
    try:
        return template.format_map(values)
    except (IndexError, KeyError, ValueError) as exc:
        raise PromptError(f"the prompt template cannot be filled: {exc}") from exc


def context_json(context: dict[str, Any]) -> str:
    return json.dumps(context, indent=2, ensure_ascii=False, default=str)


def build_request(
    prompt: PromptVersion,
    context: dict[str, Any],
    *,
    extra_values: dict[str, Any] | None = None,
    max_output_tokens: int | None = None,
) -> LlmRequest:
    values = {"context_json": context_json(context), **(extra_values or {})}
    return LlmRequest(
        system_prompt=prompt.system_prompt,
        user_prompt=render_template(prompt.user_prompt_template, values),
        output_schema=prompt.output_schema,
        temperature=prompt.temperature,
        max_output_tokens=max_output_tokens,
    )
