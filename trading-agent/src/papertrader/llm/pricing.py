"""Turns the token counts of one API call into dollars."""

from __future__ import annotations

from dataclasses import dataclass

from papertrader.config import BudgetConfig


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


def cost_usd(usage: Usage, model: str, budget: BudgetConfig, *, batch: bool = False) -> float:
    """Cost of one call. Thinking tokens are billed as output tokens and are included in
    `output_tokens`. Batch requests cost `batch_discount` of the normal price."""
    price = budget.prices[model]
    total = (
        usage.input_tokens * price.input
        + usage.output_tokens * price.output
        + usage.cache_creation_input_tokens * price.cache_write
        + usage.cache_read_input_tokens * price.cache_read
    ) / 1_000_000
    return total * budget.batch_discount if batch else total


# Output size assumed per effort level when estimating a call before making it. Output
# includes the model's thinking. Real calls measured on 2026-11-14 used 150-1,200 tokens
# at medium (agents) and 600-1,200 at high (judge); these figures are two to four times
# that, so estimates stay on the safe side without being wildly off.
_EXPECTED_OUTPUT = {"low": 1_200, "medium": 2_500, "high": 5_000, "xhigh": 9_000, "max": 16_000}


def estimate_tokens(text: str) -> int:
    """Rough token count. Deliberately on the high side so budgets are not overrun."""
    return len(text) // 3 + 16


def estimate_cost(
    prompt_chars: int, model: str, effort: str, max_tokens: int, budget: BudgetConfig
) -> float:
    usage = Usage(
        input_tokens=prompt_chars // 3 + 16,
        output_tokens=min(_EXPECTED_OUTPUT[effort], max_tokens),
    )
    return cost_usd(usage, model, budget)
