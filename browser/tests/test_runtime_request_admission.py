"""Runtime admission/accounting without Phase 10 evaluation infrastructure or APIs."""

from typing import Any

import httpx
import pytest

from autoscholar.core.budget import Budget, BudgetedLLM, BudgetLimits, current_budget
from autoscholar.core.journal import current_journal
from autoscholar.llm.errors import LLMRequestRejected, LLMUnavailableError
from autoscholar.llm.models import ChatMessage
from autoscholar.llm.openai_compatible import OpenAICompatibleProvider
from tests.test_llm_provider import configured_settings


@pytest.mark.parametrize("mode", ["early", "late", "unknown"])
async def test_admission_distinguishes_unsent_from_unknown_calls(mode: str) -> None:
    events: list[tuple[str, str, bool]] = []
    sends = 0

    async def journal(call_id: str, kind: str, starting: bool) -> None:
        events.append((call_id, kind, starting))

    def reject(payload: dict[str, Any], reserve: int) -> None:
        assert payload["model"] == "test-model"
        raise LLMRequestRejected(message="Local test policy refused this request")

    def wire(request: httpx.Request) -> httpx.Response:
        nonlocal sends
        if mode == "late":
            raise LLMRequestRejected(message="Local transport rejected before sending")
        sends += 1
        raise httpx.ReadTimeout("Response unknown", request=request)

    budget = Budget(BudgetLimits(), used={"model_calls": 2, "total_tokens": 10})
    budget_token, journal_token = current_budget.set(budget), current_journal.set(journal)
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
            provider = BudgetedLLM(
                OpenAICompatibleProvider(
                    configured_settings(),
                    http_client=client,
                    request_validator=reject if mode == "early" else None,
                )
            )
            with pytest.raises(LLMUnavailableError if mode == "unknown" else LLMRequestRejected):
                await provider.generate([ChatMessage(role="user", content="public test")])
        assert sends == int(mode == "unknown")
        assert budget.used == {"model_calls": 3 if mode == "unknown" else 2, "total_tokens": 10}
        assert [event[1:] for event in events] == {
            "early": [("llm_local_rejected", False)],
            "late": [("llm", True), ("llm_local_rejected", False)],
            "unknown": [("llm", True)],
        }[mode]
        if mode == "late":
            assert events[0][0] == events[1][0] != ""
    finally:
        current_journal.reset(journal_token)
        current_budget.reset(budget_token)
