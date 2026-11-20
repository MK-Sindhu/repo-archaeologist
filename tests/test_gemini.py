import json

import httpx
import pytest
from typer.testing import CliRunner

from archaeologist import cli
from archaeologist.config import LLMConfig, llm_provider
from archaeologist.contracts import LLM
from archaeologist.gemini import GeminiLLM, gemini_config
from archaeologist.graph.schemas import Grades
from archaeologist.llm import ClaudeLLM, LLMAuthError, LLMError, LLMRefusal

CFG = LLMConfig(model="gemini-3.5-flash", grader_model="gemini-3.5-flash-lite", use_fallbacks=False)
GRADES = {"grades": [{"id": "commit:abc", "score": 3, "reason": "states why"}], "follow": ["pr:12"]}


def ok(text, finish="STOP", usage=None, thought=None):
    parts = ([{"text": thought, "thought": True}] if thought else []) + [{"text": text}]
    return httpx.Response(200, json={
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
        "usageMetadata": usage or {"promptTokenCount": 100, "candidatesTokenCount": 20,
                                   "thoughtsTokenCount": 30},
    })


def error(status, message, details=(), state="RESOURCE_EXHAUSTED"):
    return httpx.Response(status, json={"error": {"code": status, "message": message,
                                                  "status": state, "details": list(details)}})


def make(responses, **kw):
    """GeminiLLM over a MockTransport answering with `responses` in order; records requests."""
    seen, sleeps = [], []
    queue = list(responses)

    def handler(request):
        seen.append(request)
        return queue.pop(0)

    kw.setdefault("fallbacks", [])
    llm = GeminiLLM(CFG, api_key="k", transport=httpx.MockTransport(handler),
                    sleep=sleeps.append, **kw)
    return llm, seen, sleeps


@pytest.fixture(autouse=True)
def _fresh_quota_state():
    from archaeologist import gemini

    gemini._exhausted.clear()
    yield
    gemini._exhausted.clear()


def body(request):
    return json.loads(request.content)


def test_structured_request_shape_and_usage():
    llm, seen, _ = make([ok(json.dumps(GRADES), thought="hmm")])
    out = llm.structured(system="sys", prompt="p", schema=Grades, effort="low", role="grade")
    assert out.grades[0].score == 3 and out.follow == ["pr:12"]
    req = seen[0]
    assert req.url.path.endswith("/models/gemini-3.5-flash-lite:generateContent")  # grader model
    assert req.headers["x-goog-api-key"] == "k" and "key=" not in str(req.url)
    gen = body(req)["generationConfig"]
    assert gen["responseMimeType"] == "application/json"
    assert gen["responseJsonSchema"]["properties"]["grades"]["type"] == "array"
    assert gen["thinkingConfig"] == {"thinkingLevel": "low"}
    assert body(req)["systemInstruction"]["parts"][0]["text"] == "sys"
    assert llm.usage_snapshot() == {"calls": 1, "input_tokens": 100, "output_tokens": 50}
    assert isinstance(llm, LLM)


def test_main_model_and_text():
    llm, seen, _ = make([ok("plain answer")])
    assert llm.text(system="s", prompt="p", effort="high") == "plain answer"
    assert "/models/gemini-3.5-flash:generateContent" in seen[0].url.path
    assert body(seen[0])["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "high"
    assert "responseJsonSchema" not in body(seen[0])["generationConfig"]


def test_retries_503_then_succeeds():
    llm, seen, sleeps = make([error(503, "high demand", state="UNAVAILABLE"),
                              ok(json.dumps(GRADES))])
    assert llm.structured(system="s", prompt="p", schema=Grades).grades
    assert len(seen) == 2 and len(sleeps) == 1


def test_429_waits_server_retry_delay():
    retry = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}
    llm, _, sleeps = make([error(429, "per-minute quota", [retry]), ok(json.dumps(GRADES))])
    llm.structured(system="s", prompt="p", schema=Grades)
    assert 7 <= sleeps[0] <= 8


def test_daily_quota_fails_fast():
    quota = {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
        {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]}
    llm, seen, sleeps = make([error(429, "quota exceeded", [quota])])
    with pytest.raises(LLMError, match=r"daily limit is used up .*gemini-3.5-flash"):
        llm.structured(system="s", prompt="p", schema=Grades)
    assert len(seen) == 1 and sleeps == []


DAILY = {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": [
    {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaValue": "20"}]}


def test_daily_quota_falls_back_to_the_next_model(caplog):
    llm, seen, sleeps = make([error(429, "quota", [DAILY]), ok(json.dumps(GRADES)),
                              ok(json.dumps(GRADES))],
                             fallbacks=["gemini-2.5-flash", "gemini-3.5-flash-lite"])
    with caplog.at_level("WARNING", logger="archaeologist.gemini"):
        assert llm.structured(system="s", prompt="p", schema=Grades).grades
    assert "/models/gemini-3.5-flash:" in seen[0].url.path
    assert "/models/gemini-2.5-flash:" in seen[1].url.path
    assert "thinkingConfig" not in body(seen[1])["generationConfig"]  # 2.x: no thinkingLevel
    assert "using gemini-2.5-flash" in caplog.text and sleeps == []
    # the exhausted model is skipped from now on, also by other instances
    llm.structured(system="s", prompt="p", schema=Grades)
    assert "/models/gemini-2.5-flash:" in seen[2].url.path and len(seen) == 3


def test_every_model_exhausted_says_when_it_resets():
    llm, seen, _ = make([error(429, "quota", [DAILY])] * 2, fallbacks=["gemini-2.5-flash"])
    with pytest.raises(LLMError, match=r"every configured model .*midnight Pacific"):
        llm.text(system="s", prompt="p")
    assert len(seen) == 2


def test_exhaustion_expires_at_the_reset(monkeypatch):
    from archaeologist import gemini

    gemini._exhausted["gemini-3.5-flash"] = 100.0
    monkeypatch.setattr(gemini.time, "time", lambda: 50.0)
    assert gemini._is_exhausted("gemini-3.5-flash")
    monkeypatch.setattr(gemini.time, "time", lambda: 101.0)
    assert not gemini._is_exhausted("gemini-3.5-flash")


def test_next_reset_is_midnight_pacific():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from archaeologist.gemini import _next_reset

    noon = datetime(2026, 11, 21, 12, 0, tzinfo=ZoneInfo("America/Los_Angeles")).timestamp()
    reset = datetime.fromtimestamp(_next_reset(noon), ZoneInfo("America/Los_Angeles"))
    assert (reset.day, reset.hour, reset.minute) == (22, 0, 0)


def test_fallbacks_from_environment(monkeypatch):
    monkeypatch.setenv("ARCH_GEMINI_FALLBACKS", "a-model, b-model")
    assert GeminiLLM(CFG, api_key="k").fallbacks == ["a-model", "b-model"]
    monkeypatch.setenv("ARCH_GEMINI_FALLBACKS", "")
    assert GeminiLLM(CFG, api_key="k").fallbacks == []
    monkeypatch.delenv("ARCH_GEMINI_FALLBACKS")
    assert GeminiLLM(CFG, api_key="k").fallbacks[0] == "gemini-3-flash-preview"


def test_gives_up_after_max_retries():
    llm, seen, _ = make([error(503, "busy", state="UNAVAILABLE")] * 3, max_retries=2)
    with pytest.raises(LLMError, match="503"):
        llm.structured(system="s", prompt="p", schema=Grades)
    assert len(seen) == 3


@pytest.mark.parametrize("response, exc, match", [
    (ok('{"grades": [', finish="MAX_TOKENS"), LLMError, "max_tokens"),
    (ok("{}", finish="SAFETY"), LLMRefusal, "SAFETY"),
    (httpx.Response(200, json={"promptFeedback": {"blockReason": "PROHIBITED_CONTENT"},
                               "usageMetadata": {"promptTokenCount": 9}}), LLMRefusal, "blocked"),
    (ok('{"grades": "not a list"}'), LLMError, "does not match Grades"),
])
def test_bad_replies_raise_and_count_usage(response, exc, match):
    llm, _, _ = make([response])
    with pytest.raises(exc, match=match):
        llm.structured(system="s", prompt="p", schema=Grades)
    assert llm.usage_snapshot()["calls"] == 1


def test_invalid_key_is_auth_error():
    info = {"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}
    llm, _, _ = make([error(400, "API key not valid.", [info], state="INVALID_ARGUMENT")])
    with pytest.raises(LLMAuthError, match="GEMINI_API_KEY"):
        llm.structured(system="s", prompt="p", schema=Grades)


def test_missing_key_is_auth_error():
    llm = GeminiLLM(CFG, api_key="")
    with pytest.raises(LLMAuthError, match="aistudio"):
        llm.text(system="s", prompt="p")


def test_drops_thinking_level_when_model_rejects_it():
    llm, seen, _ = make([
        error(400, "Thinking level is not supported for this model.", state="INVALID_ARGUMENT"),
        ok(json.dumps(GRADES)), ok(json.dumps(GRADES)),
    ])
    llm.structured(system="s", prompt="p", schema=Grades)
    llm.structured(system="s", prompt="p", schema=Grades)
    assert "thinkingConfig" in body(seen[0])["generationConfig"]
    assert "thinkingConfig" not in body(seen[1])["generationConfig"]
    assert "thinkingConfig" not in body(seen[2])["generationConfig"]  # remembered per model


def test_gemini_2_models_get_no_thinking_level():
    llm, seen, _ = make([ok("x")])
    llm.config = LLMConfig(model="gemini-2.5-flash", grader_model="gemini-2.5-flash")
    llm.text(system="s", prompt="p")
    assert "thinkingConfig" not in body(seen[0])["generationConfig"]


def test_pacing_spaces_requests():
    llm, _, sleeps = make([ok("a"), ok("b")], rpm=30)
    llm.text(system="s", prompt="p")
    llm.text(system="s", prompt="p")
    assert len(sleeps) == 1 and 1.5 < sleeps[0] <= 2.0


@pytest.mark.parametrize("env, expected", [
    ({}, "anthropic"),
    ({"GOOGLE_GEMINI_KEY": "g"}, "gemini"),
    ({"GEMINI_API_KEY": "g", "ANTHROPIC_API_KEY": "a"}, "anthropic"),
    ({"GEMINI_API_KEY": "g", "ANTHROPIC_API_KEY": "a", "ARCH_PROVIDER": "gemini"}, "gemini"),
    ({"ANTHROPIC_API_KEY": "a", "ARCH_MODEL": "gemini-3.5-flash"}, "gemini"),
    ({"GEMINI_API_KEY": "g", "ARCH_MODEL": "claude-haiku-5-5"}, "anthropic"),
])
def test_provider_selection(monkeypatch, env, expected):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert llm_provider() == expected


def test_bad_provider_rejected(monkeypatch):
    monkeypatch.setenv("ARCH_PROVIDER", "openai")
    with pytest.raises(ValueError, match="ARCH_PROVIDER"):
        llm_provider()


def test_gemini_config_defaults_and_overrides(monkeypatch):
    cfg = gemini_config()
    assert (cfg.model, cfg.grader_model) == ("gemini-3.5-flash", "gemini-3.5-flash-lite")
    monkeypatch.setenv("ARCH_MODEL", "gemini-3.1-pro-preview")
    cfg = gemini_config()
    assert cfg.model == cfg.grader_model == "gemini-3.1-pro-preview"


def test_make_llm_picks_backend(monkeypatch):
    monkeypatch.setenv("GOOGLE_GEMINI_KEY", "g")
    assert isinstance(cli.make_llm(), GeminiLLM)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    assert isinstance(cli.make_llm(), ClaudeLLM)


def test_cli_ask_gemini_without_key_is_friendly(toy_repo, monkeypatch):
    monkeypatch.setenv("ARCH_PROVIDER", "gemini")
    monkeypatch.setenv("ARCH_EMBEDDER", "none")
    result = CliRunner().invoke(cli.app, ["ask", str(toy_repo), "why cap backoff?", "--offline"])
    assert result.exit_code == 2
    assert "GEMINI_API_KEY" in result.stderr and "Traceback" not in result.output


@pytest.mark.live
def test_live_gemini_structured(monkeypatch):
    import os

    from archaeologist.config import gemini_api_key, load_env_file

    monkeypatch.delenv("ARCH_NO_DOTENV")
    before = set(os.environ)
    load_env_file()
    key = gemini_api_key()
    for k in set(os.environ) - before:  # don't leak .env values into other tests
        del os.environ[k]
    if not key:
        pytest.skip("no Gemini key")
    llm = GeminiLLM(gemini_config(), api_key=key)
    out = llm.structured(
        system="Grade evidence relevance 0-3 and list refs worth following.",
        prompt="Question: why is backoff capped at 30s?\nEvidence: [commit:abc] Cap backoff at "
               "30s to avoid huge sleeps (#12).",
        schema=Grades, effort="low", role="grade",
    )
    assert out.grades and out.grades[0].id == "commit:abc"
