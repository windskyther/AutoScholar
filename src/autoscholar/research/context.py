"""Server-owned date context shared by planning, retrieval and report generation."""

import calendar
import re
from datetime import UTC, date, datetime


def today() -> date:
    return datetime.now(UTC).date()


def research_context(objective: str, as_of: date | None = None) -> dict[str, str]:
    reference = as_of or today()
    context = {
        "as_of_date": reference.isoformat(),
        "date_policy": (
            "Use as_of_date, not model training knowledge, to judge past and future. "
            "Preserve the user's topic and requested period. Verify publication dates "
            "before presenting a source as a finding from that period; undated or older "
            "sources may provide background only. Missing evidence is not proof that "
            "the requested period is in the future. Answer in the user's language."
        ),
    }
    matches = set(re.findall(r"(?<!\d)(20\d{2})\s*年\s*(1[0-2]|0?[1-9])\s*月", objective))
    if len(matches) == 1:
        year, month = (int(part) for part in next(iter(matches)))
        context.update(
            requested_start=date(year, month, 1).isoformat(),
            requested_end=date(year, month, calendar.monthrange(year, month)[1]).isoformat(),
        )
    return context
