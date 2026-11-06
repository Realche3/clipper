"""Shared Claude helper. Every LLM call in the project goes through `ask_json`.

Usage:
    class Picks(BaseModel):
        clips: list[ClipPick]

    picks = ask_json(SYSTEM, prompt, Picks, model=config.CLAUDE_MODEL_RANK, task="rank")

Callers must catch `LLMError` and fall back to a non-LLM result (see CLAUDE.md).
Every successful call is logged with its token usage and an estimated cost, and appended to
workspace/claude_usage.jsonl (see `clipper usage`).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime
from typing import TypeVar

import anthropic
from pydantic import BaseModel

from clipper import config

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# Server-side refusal fallback: if the model declines, the API re-runs on a fallback model.
# Only some models accept it (Haiku doesn't), so it's sent per model.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
MAX_ATTEMPTS = 2  # our retry on top of the SDK's own retries for 429/5xx/network

# USD per million tokens (input, output). Estimates only: cache discounts and Haiku's
# >100K-token prompt tier are ignored. Update when prices change.
PRICES = {
    "claude-fable-5-1": (10.00, 50.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}

_client: anthropic.Anthropic | None = None
_usage_lock = threading.Lock()  # sounds refresh calls Claude from several threads


class LLMError(Exception):
    """Claude couldn't produce a valid answer. Callers should fall back."""


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        # api_key=None lets the SDK resolve ANTHROPIC_API_KEY / `ant auth login` itself.
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None, timeout=120.0)
    return _client


def ask_json(
    system: str,
    prompt: str,
    schema: type[T],
    *,
    model: str | None = None,
    task: str = "other",
    max_tokens: int = 16000,
    effort: str | None = None,
    client: anthropic.Anthropic | None = None,
) -> T:
    """Ask Claude for a JSON answer matching `schema`; return the validated model.

    `model` defaults to CLAUDE_MODEL; `task` labels the call in the usage log.
    Raises LLMError on refusal, truncation, invalid output after retries, or API failure.
    """
    client = client or get_client()
    model = model or config.CLAUDE_MODEL
    extra = {"betas": [FALLBACK_BETA], "fallbacks": "default"} if model in FALLBACK_MODELS else {}
    last_error = "unknown error"

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.beta.messages.parse(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
                output_config={"effort": effort or config.CLAUDE_EFFORT},
                **extra,
            )
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
            raise LLMError(f"Claude auth failed, check ANTHROPIC_API_KEY: {e}") from e
        except anthropic.BadRequestError as e:
            raise LLMError(f"Claude rejected the request: {e.message}") from e
        except (anthropic.RateLimitError, anthropic.APIConnectionError) as e:
            last_error = f"{type(e).__name__}: {e}"
            log.warning("Claude call failed (attempt %d/%d): %s", attempt, MAX_ATTEMPTS, last_error)
            continue
        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                last_error = f"server error {e.status_code}"
                log.warning("Claude %s (attempt %d/%d)", last_error, attempt, MAX_ATTEMPTS)
                continue
            raise LLMError(f"Claude API error {e.status_code}: {e.message}") from e

        _record_usage(task, model, response)  # billed even if the answer turns out unusable
        if response.stop_reason == "refusal":
            raise LLMError("Claude declined the request (refusal)")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"Claude output was cut off at max_tokens={max_tokens}")

        parsed = response.parsed_output
        if parsed is not None:
            return parsed
        last_error = "response did not match the schema"
        log.warning("Claude %s (attempt %d/%d)", last_error, attempt, MAX_ATTEMPTS)

    raise LLMError(f"Claude failed after {MAX_ATTEMPTS} attempts: {last_error}")


# ---------------------------------------------------------------- usage / cost


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


def _record_usage(task: str, model: str, response) -> None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    served_by = getattr(response, "model", None) or model  # differs after a refusal fallback
    tokens_in = (
        (getattr(usage, "input_tokens", 0) or 0)
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
        + (getattr(usage, "cache_read_input_tokens", 0) or 0)
    )
    tokens_out = getattr(usage, "output_tokens", 0) or 0  # includes thinking tokens
    cost = estimate_cost(served_by, tokens_in, tokens_out)
    log.info(
        "claude %s: %s in=%d out=%d ~$%s",
        task,
        served_by,
        tokens_in,
        tokens_out,
        f"{cost:.4f}" if cost is not None else "?",
    )
    entry = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "task": task,
        "model": served_by,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "cost_usd": round(cost, 6) if cost is not None else None,
    }
    try:
        config.WORKSPACE.mkdir(parents=True, exist_ok=True)
        with _usage_lock, open(usage_file(), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:  # never fail a pipeline step because the log can't be written
        log.warning("could not write Claude usage log: %s", e)


def usage_file():
    return config.WORKSPACE / "claude_usage.jsonl"


def spend_since(since: datetime, rows: list[dict] | None = None) -> float:
    rows = load_usage() if rows is None else rows
    return sum(r.get("cost_usd") or 0.0 for r in rows if datetime.fromisoformat(r["at"]) >= since)


def load_usage() -> list[dict]:
    path = usage_file()
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows
