# tests/test_llm_eval.py
"""The scorecard cases and runner (scripted fake LLM, no network, no database)."""

from __future__ import annotations

from app.llm.fake import FakeProvider
from app.worker.evaluate import active_prompts, case_input, load_cases, run_case
from app.worker.rules import Task


def classification(**values) -> dict:
    answer = {
        "decision": "action",
        "store_request_quote": "",
        "title": "Title",
        "detail": "Detail for the store.",
        "severity": "warning",
        "expected_response": "acknowledge",
        "due_time_quote": "",
        "due_by_local": "",
        "rationale": "r",
    }
    answer.update(values)
    return answer


def by_name():
    return {case.name: case for case in load_cases()}


def test_cases_load_and_build_contexts() -> None:
    cases = load_cases()
    assert len(cases) == 18
    for case in cases:
        data = case_input(case)
        assert data.context["task"] == case.task.value
        if case.task == Task.ASSESS_IMPACT:
            assert data.context["allowed_severities"]
            assert "trigger" not in data.context
        else:
            assert data.context["trigger"]["text"] == case.trigger
            assert (data.context["open_action"] is None) == (case.open_action is None)


def test_active_prompts_are_version_2() -> None:
    prompts = active_prompts()
    assert {w: p.version for w, p in prompts.items()} == {
        "impact_assessment": 2,
        "update_classification": 2,
    }


async def test_grounded_action_with_due_time_passes() -> None:
    answer = classification(
        store_request_quote="Store must provide access to the comms room for the engineer",
        title="Provide comms room access at 1 PM",
        due_time_quote="arrives at 1 PM today",
        due_by_local="2026-11-04T13:00",
    )
    result = await run_case(FakeProvider([answer]), active_prompts(),
                            by_name()["access-request-with-time"])  # fmt: skip
    assert (result.ok, result.got, result.adjustments) == (True, "action acknowledge due 13:00", [])


async def test_guard_turns_a_described_problem_into_notify() -> None:
    answer = classification(store_request_quote="trying to reach the store to check the device")
    result = await run_case(FakeProvider([answer]), active_prompts(),
                            by_name()["trying-to-reach-store"])  # fmt: skip
    assert result.ok and result.got == "notify"
    assert result.adjustments[0].startswith("request barrier:")


async def test_failing_case_and_leaked_text() -> None:
    wrong = await run_case(FakeProvider([classification(decision="none", title="",
                                                         detail="")]),
                           active_prompts(), by_name()["access-request-with-time"])  # fmt: skip
    assert not wrong.ok and wrong.got == "none"
    leaked = await run_case(
        FakeProvider([classification(decision="notify", detail="Ticket VND-448812 raised.")]),
        active_prompts(),
        by_name()["internal-ids-never-shown"],
    )
    assert not leaked.ok and leaked.got == "rejected by guards"


async def test_impact_case() -> None:
    answer = {"affected": ["store_network", "payments"], "severity": "critical",
              "title": "Store network down", "detail": "Card payments are unavailable.",
              "rationale": "r"}  # fmt: skip
    result = await run_case(FakeProvider([answer]), active_prompts(),
                            by_name()["impact-store-outage"])  # fmt: skip
    assert (result.ok, result.got) == (True, "critical")
