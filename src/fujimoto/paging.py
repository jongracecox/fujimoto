"""How far PageUp/PageDown move the highlight in the TUI's lists.

The `page_step` global config key holds it as text: `50%` moves by that share
of the rows that fit in the viewport, and a bare number such as `8` moves by
that many rows. A whole page made it easy to jump past the row you wanted, so
the default is half a page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

DEFAULT_PAGE_STEP = "50%"


@dataclass(frozen=True)
class PageStep:
    """`kind="percent"`: `amount`% of the viewport. `kind="rows"`: `amount` rows."""

    kind: Literal["percent", "rows"]
    amount: int

    def __str__(self) -> str:
        return f"{self.amount}%" if self.kind == "percent" else str(self.amount)


def parse_page_step(value: object) -> PageStep:
    """Parse `50%` or `8` (or the int 8, as YAML reads it); ValueError if invalid."""
    if isinstance(value, bool):
        raise ValueError("page_step must be a percentage like 50% or a row count")
    if isinstance(value, int):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        raise ValueError("page_step must be a percentage like 50% or a row count")
    percent = text.endswith("%")
    digits = text[:-1].strip() if percent else text
    if not digits.isdigit():
        raise ValueError(
            f"page_step must be a percentage like 50% or a row count, got {value!r}"
        )
    amount = int(digits)
    if percent and not 1 <= amount <= 100:
        raise ValueError(f"page_step percentage must be 1%-100%, got {text}")
    if not percent and amount < 1:
        raise ValueError(f"page_step row count must be at least 1, got {text}")
    return PageStep("percent" if percent else "rows", amount)


DEFAULT = parse_page_step(DEFAULT_PAGE_STEP)
