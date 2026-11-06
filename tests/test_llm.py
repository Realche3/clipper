from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

from clipper import llm


class Answer(BaseModel):
    text: str


def _response(parsed=None, stop_reason="end_turn"):
    return SimpleNamespace(
        parsed_output=parsed, stop_reason=stop_reason, model="m", _request_id="req_1"
    )


class FakeClient:
    """Mimics client.beta.messages.parse, returning/raising queued results in order."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.calls.append(kwargs)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _status_error(cls, code):
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls("boom", response=httpx2.Response(code, request=req), body=None)


def test_returns_parsed_model_and_sends_expected_params():
    client = FakeClient(_response(Answer(text="hi")))
    out = llm.ask_json("sys", "prompt", Answer, client=client)
    assert out == Answer(text="hi")
    call = client.calls[0]
    assert call["output_format"] is Answer
    assert call["fallbacks"] == "default"
    assert llm.FALLBACK_BETA in call["betas"]
    assert call["system"] == "sys"


def test_retries_once_when_output_invalid():
    client = FakeClient(_response(None), _response(Answer(text="ok")))
    assert llm.ask_json("s", "p", Answer, client=client).text == "ok"
    assert len(client.calls) == 2


def test_raises_after_all_attempts_invalid():
    client = FakeClient(_response(None), _response(None))
    with pytest.raises(llm.LLMError, match="schema"):
        llm.ask_json("s", "p", Answer, client=client)


def test_refusal_raises_without_retry():
    client = FakeClient(_response(stop_reason="refusal"))
    with pytest.raises(llm.LLMError, match="refusal"):
        llm.ask_json("s", "p", Answer, client=client)
    assert len(client.calls) == 1


def test_max_tokens_raises():
    client = FakeClient(_response(stop_reason="max_tokens"))
    with pytest.raises(llm.LLMError, match="cut off"):
        llm.ask_json("s", "p", Answer, client=client)


def test_rate_limit_is_retried():
    client = FakeClient(_status_error(anthropic.RateLimitError, 429), _response(Answer(text="ok")))
    assert llm.ask_json("s", "p", Answer, client=client).text == "ok"


def test_bad_request_is_not_retried():
    client = FakeClient(_status_error(anthropic.BadRequestError, 400))
    with pytest.raises(llm.LLMError, match="rejected"):
        llm.ask_json("s", "p", Answer, client=client)
    assert len(client.calls) == 1


# ---------------------------------------------------------------- models + usage


@pytest.fixture
def ws(tmp_path, monkeypatch):
    from clipper import config

    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    monkeypatch.setattr(config, "CLAUDE_MODEL", "claude-opus-5-5")
    return tmp_path


def _billed(model="claude-opus-5-5", tokens_in=10_000, tokens_out=2_000, parsed=None):
    r = _response(parsed or Answer(text="ok"))
    r.model = model
    r.usage = SimpleNamespace(
        input_tokens=tokens_in,
        output_tokens=tokens_out,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return r


def test_model_defaults_to_claude_model_with_fallback(ws):
    client = FakeClient(_billed())
    llm.ask_json("s", "p", Answer, client=client)
    call = client.calls[0]
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"


def test_haiku_gets_no_fallback_params(ws):
    client = FakeClient(_billed("claude-haiku-5-5"))
    llm.ask_json("s", "p", Answer, model="claude-haiku-5-5", effort="low", client=client)
    call = client.calls[0]
    assert call["model"] == "claude-haiku-5-5"
    assert "fallbacks" not in call and "betas" not in call
    assert call["output_config"] == {"effort": "low"}


def test_usage_is_logged_with_cost(ws):
    llm.ask_json("s", "p", Answer, task="captions", client=FakeClient(_billed()))
    [row] = llm.load_usage()
    assert row["task"] == "captions" and row["model"] == "claude-opus-5-5"
    assert (row["input_tokens"], row["output_tokens"]) == (10_000, 2_000)
    assert row["cost_usd"] == pytest.approx(10_000 * 4e-6 + 2_000 * 20e-6)  # $0.08


def test_usage_uses_the_model_that_actually_answered(ws):
    llm.ask_json("s", "p", Answer, client=FakeClient(_billed(model="claude-opus-5")))
    assert llm.load_usage()[0]["model"] == "claude-opus-5"


def test_invalid_answers_are_still_billed(ws):
    client = FakeClient(_billed(parsed=None), _billed(parsed=None))
    client.results[0].parsed_output = client.results[1].parsed_output = None
    with pytest.raises(llm.LLMError):
        llm.ask_json("s", "p", Answer, client=client)
    assert len(llm.load_usage()) == 2


def test_estimate_cost_and_spend_since(ws):
    from datetime import datetime, timedelta

    assert llm.estimate_cost("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.60)
    assert llm.estimate_cost("some-future-model", 1, 1) is None
    now = datetime.now()
    rows = [
        {"at": (now - timedelta(days=2)).isoformat(), "cost_usd": 1.0},
        {"at": now.isoformat(), "cost_usd": 0.25},
        {"at": now.isoformat(), "cost_usd": None},
    ]
    assert llm.spend_since(now - timedelta(days=1), rows) == pytest.approx(0.25)
    assert llm.spend_since(now - timedelta(days=7), rows) == pytest.approx(1.25)


def test_cli_usage_summary(ws):
    from typer.testing import CliRunner

    from clipper import cli

    llm.ask_json("s", "p", Answer, task="rank", client=FakeClient(_billed()))
    llm.ask_json("s", "p", Answer, task="rank", client=FakeClient(_billed()))
    out = CliRunner().invoke(cli.app, ["usage"])
    assert out.exit_code == 0, out.output
    assert "rank" in out.output and "2 calls" in out.output and "$0.160" in out.output
