"""The single place where the program talks to Claude.

Every call:
* returns a Pydantic-validated object (structured outputs), never free text;
* uses adaptive thinking with the effort level from config.yaml;
* marks the static start of the system prompt for prompt caching;
* is checked against the budget before it is made and logged with its cost afterwards;
* turns every stop reason and API error into one of a few clear exceptions.

API shapes follow the Anthropic Python SDK documentation (checked 2026-11-14,
SDK 2.8): `client.beta.messages.parse(..., output_format=Model)`.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, cast

import anthropic
from anthropic import AsyncAnthropic
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session, sessionmaker

from papertrader.config import BudgetConfig, Effort
from papertrader.db.models import ApiUsage
from papertrader.db.types import utcnow
from papertrader.llm.governor import BudgetExceededError, CostGovernor
from papertrader.llm.pricing import Usage, cost_usd, estimate_cost

log = logging.getLogger(__name__)

# If Claude's safety filter declines a request, let the API retry it once on the model
# Anthropic recommends, inside the same call. Not available for Haiku models.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    """The call did not produce a usable answer. The caller carries on without it."""


class LLMRefusedError(LLMError):
    """Claude declined the request (stop_reason == "refusal")."""


class LLMUnavailableError(LLMError):
    """Rate limit, server error or network problem that outlasted the retries."""


class LLMConfigError(LLMError):
    """Bad API key or permissions. Nothing will work until this is fixed."""


@dataclass(frozen=True)
class SystemPrompt:
    """`shared` is identical for every agent on the same model, so it is cached once and
    read from the cache by the others. `specific` is the agent's own instructions."""

    shared: str
    specific: str

    def blocks(self) -> list[dict[str, Any]]:
        return [
            {"type": "text", "text": self.shared, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": self.specific},
        ]

    def __len__(self) -> int:
        return len(self.shared) + len(self.specific)


@dataclass(frozen=True)
class LLMResult[T: BaseModel]:
    output: T
    model: str  # the model that actually answered
    usage: Usage
    cost_usd: float
    request_id: str | None


def supports_fallback(model: str) -> bool:
    return not model.startswith("claude-haiku")


class ClaudeClient:
    def __init__(
        self,
        client: AsyncAnthropic,
        session_factory: sessionmaker[Session],
        budget: BudgetConfig,
        governor: CostGovernor,
    ) -> None:
        self._client = client
        self._sessions = session_factory
        self._budget = budget
        self.governor = governor

    async def structured[T: BaseModel](
        self,
        *,
        purpose: str,
        model: str,
        effort: Effort,
        max_tokens: int,
        system: SystemPrompt,
        user: str,
        output: type[T],
        cycle_id: int | None = None,
    ) -> LLMResult[T]:
        """One request, one validated `output` object. Raises LLMError subclasses or
        BudgetExceededError; never returns something unvalidated."""
        estimate = estimate_cost(len(system) + len(user), model, effort, max_tokens, self._budget)
        self.governor.reserve(estimate)  # may raise BudgetExceededError: nothing was sent

        started = time.monotonic()
        actual = 0.0
        record: dict[str, Any] = {"purpose": purpose, "model": model, "effort": effort,
                                  "cycle_id": cycle_id}  # fmt: skip
        try:
            extra: dict[str, Any] = {}
            if supports_fallback(model):
                extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"}
            # The raw response is read first: token counts and the stop reason must be
            # recorded even when the answer itself turns out to be unusable.
            raw = await self._client.beta.messages.with_raw_response.parse(
                model=model,
                max_tokens=max_tokens,
                thinking={"type": "adaptive"},
                output_config={"effort": effort},
                system=system.blocks(),  # type: ignore[arg-type]
                messages=[{"role": "user", "content": user}],
                output_format=output,
                **extra,
            )
            payload = cast(dict[str, Any], await raw.json())
        except anthropic.AuthenticationError as exc:
            record["error"] = "authentication failed"
            raise LLMConfigError("Claude rejected the API key (ANTHROPIC_API_KEY)") from exc
        except anthropic.PermissionDeniedError as exc:
            record["error"] = "permission denied"
            raise LLMConfigError(f"the API key may not use {model}: {exc.message}") from exc
        except anthropic.RateLimitError as exc:
            record["error"] = "rate limited"
            raise LLMUnavailableError("rate limited by the Claude API after retries") from exc
        except anthropic.APIStatusError as exc:
            record["error"] = f"HTTP {exc.status_code}: {exc.message}"[:500]
            if exc.status_code >= 500:
                raise LLMUnavailableError(
                    f"Claude API error {exc.status_code} after retries"
                ) from exc
            raise LLMError(
                f"Claude API rejected the request ({exc.status_code}): {exc.message}"
            ) from exc
        except anthropic.APIConnectionError as exc:  # includes timeouts
            record["error"] = type(exc).__name__
            raise LLMUnavailableError("could not reach the Claude API") from exc
        else:
            counts = payload.get("usage") or {}
            usage = Usage(
                input_tokens=counts.get("input_tokens") or 0,
                output_tokens=counts.get("output_tokens") or 0,
                cache_creation_input_tokens=counts.get("cache_creation_input_tokens") or 0,
                cache_read_input_tokens=counts.get("cache_read_input_tokens") or 0,
            )
            answered_by = str(payload.get("model") or model)
            served = answered_by if answered_by in self._budget.prices else model
            if answered_by != model:
                detail = {"purpose": purpose, "asked": model, "served": answered_by}
                log.warning("answered by a fallback model", extra=detail)
            actual = cost_usd(usage, served, self._budget)
            stop_reason = payload.get("stop_reason")
            record.update(
                model=served, request_id=raw.request_id, stop_reason=stop_reason,
                input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                cache_creation_input_tokens=usage.cache_creation_input_tokens,
                cache_read_input_tokens=usage.cache_read_input_tokens, cost_usd=actual,
            )  # fmt: skip

            # Always look at the stop reason before touching the content.
            if stop_reason == "refusal":
                category = (payload.get("stop_details") or {}).get("category")
                record["error"] = f"refused ({category})"
                raise LLMRefusedError(f"{purpose}: Claude declined the request ({category})")
            if stop_reason == "max_tokens":
                record["error"] = "cut off at max_tokens"
                raise LLMError(
                    f"{purpose}: answer was cut off; raise max_tokens above {max_tokens}"
                )
            if stop_reason != "end_turn":
                record["error"] = f"unexpected stop_reason {stop_reason}"
                raise LLMError(f"{purpose}: no usable answer (stop_reason={stop_reason})")
            try:
                parsed = (await raw.parse()).parsed_output
            except ValidationError as exc:
                record["error"] = "output did not match the schema"
                raise LLMError(f"{purpose}: answer did not match the expected structure") from exc
            if parsed is None:
                record["error"] = "no structured output"
                raise LLMError(f"{purpose}: the answer contained no structured output")
            return LLMResult(parsed, served, usage, actual, raw.request_id)
        finally:
            record["duration_ms"] = int((time.monotonic() - started) * 1000)
            self._log_usage(record)
            self.governor.settle(estimate, actual)

    def _log_usage(self, record: dict[str, Any]) -> None:
        """One row per call, including failed ones, so spend is never under-counted."""
        try:
            with self._sessions() as session:
                session.add(ApiUsage(ts=utcnow(), **record))
                session.commit()
        except Exception:  # never let bookkeeping hide the real result or error
            log.exception("could not record API usage", extra={"purpose": record.get("purpose")})


def make_anthropic(api_key: str) -> AsyncAnthropic:
    # The SDK retries 408/409/429/5xx and connection errors with exponential backoff.
    return AsyncAnthropic(api_key=api_key, max_retries=4)


__all__ = [
    "BudgetExceededError",
    "ClaudeClient",
    "LLMConfigError",
    "LLMError",
    "LLMRefusedError",
    "LLMResult",
    "LLMUnavailableError",
    "SystemPrompt",
    "make_anthropic",
]
