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


# Typical output size per effort level, used only to estimate a call before making it.
# Output includes the model's thinking, which is why the numbers are well above the
# size of the final answer.
_EXPECTED_OUTPUT = {"low": 2_000, "medium": 4_500, "high": 9_000, "xhigh": 14_000, "max": 20_000}


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
