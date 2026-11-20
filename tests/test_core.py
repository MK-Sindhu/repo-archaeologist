from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from archaeologist.config import LLMConfig
from archaeologist.contracts import LLM, Toolbox
from archaeologist.llm import FALLBACK_BETA, ClaudeLLM, LLMError, LLMRefusal
from archaeologist.refs import extract_refs, parse_ref, same_ref
from archaeologist.testing import FakeLLM, FakeToolbox, HashEmbedder


def test_extract_refs_squash_subject_and_closing_keyword():
    assert extract_refs("Cap backoff at 30s (#12)\n\nFixes #7. See #9.") == ["pr:12", "issue:7", "gh:9"]


def test_extract_refs_urls_respect_repo():
    text = "see https://github.com/acme/toy/pull/3 and https://github.com/other/x/issues/4"
    assert extract_refs(text, owner="acme", repo="toy") == ["pr:3"]


def test_extract_refs_commit_sha():
    assert extract_refs("reverts 3f2a9c1d and deadbeefcafe") == ["commit:3f2a9c1d"]


def test_same_ref():
    assert same_ref("commit:3f2a9c1", "commit:3f2a9c1d00aa")
    assert same_ref("gh:12", "pr:12")
    assert not same_ref("pr:12", "issue:12")
    with pytest.raises(ValueError):
        parse_ref("nonsense")


def test_fakes_satisfy_protocols():
    assert isinstance(FakeLLM(), LLM)
    assert isinstance(FakeToolbox(), Toolbox)
    e = HashEmbedder().embed(["retry backoff", "unrelated"])
    assert e.shape == (2, 256)


class _Out(BaseModel):
    x: int


def _fake_client(stop_reason="end_turn", text='{"x": 1}'):
    captured = {}

    def create(**kw):
        captured.update(kw)
        return SimpleNamespace(stop_reason=stop_reason,
                               content=[SimpleNamespace(type="text", text=text)],
                               usage=SimpleNamespace(input_tokens=10, output_tokens=5))

    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create))), captured


def test_claude_llm_structured_sends_effort_and_fallbacks():
    client, captured = _fake_client()
    llm = ClaudeLLM(LLMConfig(model="claude-opus-5-5", use_fallbacks=True), client=client)
    out = llm.structured(system="s", prompt="p", schema=_Out, effort="low", role="grade")
    assert out.x == 1
    assert captured["output_config"]["effort"] == "low"
    fmt = captured["output_config"]["format"]
    assert fmt["type"] == "json_schema" and fmt["schema"]["properties"]["x"]["type"] == "integer"
    assert captured["betas"] == [FALLBACK_BETA] and captured["fallbacks"] == "default"
    assert llm.usage_snapshot() == {"calls": 1, "input_tokens": 10, "output_tokens": 5}


def test_claude_llm_refusal_raises():
    client, _ = _fake_client(stop_reason="refusal", text="I can't help with that.")
    with pytest.raises(LLMRefusal):
        ClaudeLLM(LLMConfig(use_fallbacks=False), client=client).structured(system="s", prompt="p", schema=_Out)


def _sdk_client(stop_reason: str, text: str):
    """A real Anthropic client whose HTTP layer answers every request with one message."""
    import anthropic
    import httpx2

    def handler(request):
        body = {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 16000},
        }
        return httpx2.Response(200, json=body)

    transport = httpx2.MockTransport(handler)
    return anthropic.Anthropic(api_key="x", max_retries=0,
                               http_client=httpx2.Client(transport=transport))


@pytest.mark.parametrize(
    "stop_reason, text, error",
    [
        ("max_tokens", '{"x": 1, "note": "The cap was added because', LLMError),
        ("refusal", "I can't help with that.", LLMRefusal),
        ("end_turn", '{"y": 2}', LLMError),
    ],
)
def test_claude_llm_structured_failures_are_llm_errors_and_counted(stop_reason, text, error):
    """Truncated, refused or invalid replies raise LLMError (not a raw ValidationError) and
    their tokens still count toward usage."""
    llm = ClaudeLLM(LLMConfig(use_fallbacks=False), client=_sdk_client(stop_reason, text))
    with pytest.raises(error) as info:
        llm.structured(system="s", prompt="p", schema=_Out)
    if stop_reason == "max_tokens":
        assert "max_tokens" in str(info.value)
    assert llm.usage_snapshot() == {"calls": 1, "input_tokens": 1000, "output_tokens": 16000}


def test_claude_llm_structured_through_real_sdk():
    llm = ClaudeLLM(LLMConfig(use_fallbacks=False), client=_sdk_client("end_turn", '{"x": 7}'))
    assert llm.structured(system="s", prompt="p", schema=_Out).x == 7


def test_toy_repo_fixture(toy_repo_shas):
    path, shas = toy_repo_shas
    assert len(shas) == 4 and (path / "src/pkg/retry.py").exists()
