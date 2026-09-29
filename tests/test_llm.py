import json

import httpx
import pytest

from proofmark.config import LLMSettings
from proofmark.llm import ChatClient, LLMError, parse_json_object

SETTINGS = LLMSettings(provider="groq", base_url="https://llm.test/v1", model="m", api_key="k", vision=False)


def reply(content, status=200, headers=None):
    body = {"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
    return httpx.Response(status, json=body, headers=headers or {})


def client_for(handler, sleeps=None):
    record = sleeps if sleeps is not None else []
    return ChatClient(SETTINGS, transport=httpx.MockTransport(handler), sleep=record.append)


def test_parse_json_object_tolerates_fences_and_prose():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('Here you go: {"a": {"b": 2}} Hope it helps!') == {"a": {"b": 2}}
    assert parse_json_object('<think>hmm</think>{"a": 3}') == {"a": 3}
    with pytest.raises(ValueError):
        parse_json_object("[1, 2]")


def test_sends_bearer_key_and_json_mode():
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return reply('{"ok": true}')

    with client_for(handler) as client:
        assert client.chat_json([{"role": "user", "content": "hi"}]) == {"ok": True}
        assert client.usage == {"prompt_tokens": 10, "completion_tokens": 5}
    assert seen["auth"] == "Bearer k"
    assert seen["body"]["response_format"] == {"type": "json_object"}


def test_falls_back_when_json_mode_is_not_supported():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, json={"error": {"message": "response_format is not supported"}})
        return reply('{"ok": 1}')

    with client_for(handler) as client:
        assert client.chat_json([{"role": "user", "content": "hi"}]) == {"ok": 1}
        client.chat_json([{"role": "user", "content": "again"}])
    assert "response_format" not in bodies[-1]  # remembered for later calls
    assert len(bodies) == 3


def test_retries_rate_limits_using_retry_after():
    calls = []
    sleeps = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": {"message": "slow down"}}, headers={"Retry-After": "3"})
        return reply("done")

    with client_for(handler, sleeps) as client:
        assert client.chat([{"role": "user", "content": "hi"}]) == "done"
    assert sleeps == [3.0]


def test_gives_up_after_repeated_rate_limits():
    sleeps = []
    with client_for(lambda r: httpx.Response(429, json={"error": "limit"}), sleeps) as client:
        with pytest.raises(LLMError, match="rate limit"):
            client.chat([{"role": "user", "content": "hi"}])
    assert len(sleeps) == ChatClient.MAX_RETRIES


def test_repairs_invalid_json_once():
    answers = iter(["not json at all", '{"fixed": true}'])
    with client_for(lambda r: reply(next(answers))) as client:
        assert client.chat_json([{"role": "user", "content": "hi"}]) == {"fixed": True}


def test_clear_errors_for_bad_keys_and_models():
    with client_for(lambda r: httpx.Response(401, json={"error": "bad key"})) as client:
        with pytest.raises(LLMError, match="rejected the API key"):
            client.chat([{"role": "user", "content": "hi"}])
    with client_for(lambda r: httpx.Response(404, json={"error": "no model"})) as client:
        with pytest.raises(LLMError, match="Model 'm' was not found"):
            client.chat([{"role": "user", "content": "hi"}])


def test_strips_reasoning_and_rejects_empty_answers():
    with client_for(lambda r: reply("<think>plan</think>\n# Title")) as client:
        assert client.chat([{"role": "user", "content": "hi"}]) == "# Title"
    with client_for(lambda r: reply("   ")) as client:
        with pytest.raises(LLMError, match="empty answer"):
            client.chat([{"role": "user", "content": "hi"}])
