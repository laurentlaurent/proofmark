"""A thin client for OpenAI-compatible Chat Completions APIs.

Works with Gemini's OpenAI endpoint, Groq, Ollama, OpenRouter, LM Studio and
OpenAI itself. It handles the failure modes that matter on free tiers: rate
limits (429, with Retry-After), transient 5xx errors, providers that reject
JSON mode, and models that wrap JSON in prose or code fences.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

import httpx

from .config import LLMSettings

THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
RETRY_IN_RE = re.compile(r"retry in ([\d.]+)\s*s", re.I)


class LLMError(RuntimeError):
    """A model call failed. The message is written for the person using the app."""


def parse_json_object(text: str) -> dict:
    """Parse the first JSON object in a model answer (fences and prose tolerated)."""
    if not text or not text.strip():
        raise ValueError("the answer was empty")
    cleaned = THINK_RE.sub("", text).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object was found") from None
        try:
            data = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON ({exc.msg} at character {exc.pos})") from None
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object, got a list or a value")
    return data


def user_content(text: str, images: list[str] | None = None) -> str | list[dict]:
    """Plain text, or text plus images in the OpenAI 'content parts' format."""
    if not images:
        return text
    parts: list[dict] = [{"type": "text", "text": text}]
    parts.extend({"type": "image_url", "image_url": {"url": url}} for url in images)
    return parts


def _api_message(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:300].strip() or resp.reason_phrase
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        error = data.get("error", data)
        if isinstance(error, dict):
            return str(error.get("message") or error)[:300]
        return str(error)[:300]
    return str(data)[:300]


class ChatClient:
    MAX_RETRIES = 3

    def __init__(self, settings: LLMSettings, *, transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.settings = settings
        self._http = httpx.Client(timeout=httpx.Timeout(settings.timeout, connect=10.0), transport=transport)
        self._sleep = sleep
        self._json_mode = True
        self.calls = 0
        self.usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def __enter__(self) -> "ChatClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _retry_delay(self, resp: httpx.Response, attempt: int) -> float:
        header = resp.headers.get("retry-after", "")
        try:
            return min(float(header), 30.0)
        except ValueError:
            pass
        match = RETRY_IN_RE.search(resp.text or "")
        if match:
            return min(float(match.group(1)) + 0.5, 40.0)
        return min(2.0 ** (attempt + 1), 20.0)

    def chat(self, messages: list[dict], *, json_mode: bool = False, temperature: float = 0.3) -> str:
        s = self.settings
        url = f"{s.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if s.api_key:
            headers["Authorization"] = f"Bearer {s.api_key}"
        payload: dict[str, Any] = {"model": s.model, "messages": messages, "temperature": temperature}
        if json_mode and self._json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_error = ""
        for attempt in range(self.MAX_RETRIES + 1):
            try:
                resp = self._http.post(url, headers=headers, json=payload)
            except httpx.TimeoutException:
                raise LLMError(f"The model did not answer within {s.timeout:.0f} seconds. Try again, "
                               "or pick a faster model.") from None
            except httpx.HTTPError as exc:
                hint = " Is Ollama running? Start it with `ollama serve`." if s.provider == "ollama" else ""
                raise LLMError(f"Could not reach {s.base_url} ({exc.__class__.__name__}).{hint}") from None
            self.calls += 1

            if resp.status_code == 429 or resp.status_code >= 500:
                last_error = _api_message(resp)
                if attempt < self.MAX_RETRIES:
                    self._sleep(self._retry_delay(resp, attempt))
                    continue
                if resp.status_code == 429:
                    raise LLMError("The provider's rate limit was reached. Free tiers allow only a few requests "
                                   f"per minute: wait a minute and try again. ({last_error})")
                raise LLMError(f"The model API failed with {resp.status_code}: {last_error}")

            if (resp.status_code in (400, 422) and "response_format" in payload
                    and re.search(r"response_format|json_object|json mode", resp.text or "", re.I)):
                payload.pop("response_format")
                self._json_mode = False
                continue
            if resp.status_code in (401, 403):
                raise LLMError(f"{s.label} rejected the API key ({resp.status_code}). Check the key and try again.")
            if resp.status_code == 404:
                raise LLMError(f"Model '{s.model}' was not found at {s.base_url}. Check the model name "
                               f"in Model settings. ({_api_message(resp)})")
            if resp.status_code >= 400:
                raise LLMError(f"The model API returned {resp.status_code}: {_api_message(resp)}")
            return self._content(resp)
        raise LLMError(last_error or "The model API kept failing. Try again in a minute.")

    def _content(self, resp: httpx.Response) -> str:
        try:
            data = resp.json()
        except ValueError:
            raise LLMError("The model API answered with something that is not JSON.") from None
        try:
            message = data["choices"][0]["message"]
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"Unexpected answer from the model API: {str(data)[:200]}") from None
        usage = data.get("usage") or {}
        for key in self.usage:
            if isinstance(usage.get(key), int):
                self.usage[key] += usage[key]
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        content = THINK_RE.sub("", content or "").strip()
        if not content:
            raise LLMError("The model returned an empty answer. Try again, or pick another model.")
        return content

    def chat_json(self, messages: list[dict], *, temperature: float = 0.1) -> dict:
        """Ask for JSON; if the answer does not parse, ask once more with the error."""
        text = self.chat(messages, json_mode=True, temperature=temperature)
        try:
            return parse_json_object(text)
        except ValueError as first_error:
            retry = messages + [
                {"role": "assistant", "content": text[:4000]},
                {"role": "user", "content": f"That was not valid JSON: {first_error}. "
                                            "Reply again with only the JSON object: no prose, no code fences."},
            ]
            second = self.chat(retry, json_mode=True, temperature=0.0)
            try:
                return parse_json_object(second)
            except ValueError as exc:
                raise LLMError(f"The model did not return valid JSON twice in a row ({exc}). "
                               "Try again or pick a stronger model.") from None
