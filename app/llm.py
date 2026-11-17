"""Local LLM client (Ollama). JSON-mode chat with repair retries and VRAM unloading."""
from __future__ import annotations

import json
import logging
import re
import time

import requests

from .config import settings

log = logging.getLogger("ytm.llm")


class LLMError(RuntimeError):
    pass


def _base() -> str:
    return settings.get("llm.base_url", "http://127.0.0.1:11434").rstrip("/")


def model() -> str:
    return settings.get("llm.model", "qwen2.5:7b-instruct")


def _parse(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.M).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError("no JSON object in reply")
        obj = json.loads(m.group(0))
    if not isinstance(obj, dict):
        raise ValueError("reply is not a JSON object")
    return obj


def chat_json(system: str, user: str, temperature: float | None = None, retries: int = 2) -> dict:
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        payload = {
            "model": model(),
            "messages": messages,
            "stream": False,
            "format": "json",
            "keep_alive": settings.get("llm.keep_alive", "10m"),
            "options": {
                "temperature": settings.get("llm.temperature", 0.7) if temperature is None else temperature,
                "num_ctx": int(settings.get("llm.num_ctx", 8192)),
            },
        }
        if settings.get("llm.think") is False:  # reasoning models (qwen3, deepseek-r1): skip thinking tokens
            payload["think"] = False
        try:
            r = requests.post(f"{_base()}/api/chat", json=payload, timeout=int(settings.get("llm.timeout_sec", 600)))
        except requests.RequestException as e:
            last_err = e
            log.warning("Ollama request failed (attempt %s): %s", attempt + 1, e)
            time.sleep(3)
            continue
        if r.status_code != 200:
            last_err = LLMError(f"Ollama HTTP {r.status_code}: {r.text[:300]}")
            time.sleep(2)
            continue
        content = r.json().get("message", {}).get("content", "")
        try:
            return _parse(content)
        except ValueError as e:
            last_err = e
            messages = messages + [
                {"role": "assistant", "content": content[:4000]},
                {"role": "user", "content": "That was not a valid JSON object. Reply again with ONLY the corrected JSON object."},
            ]
    raise LLMError(f"LLM did not return valid JSON: {last_err}")


def unload() -> None:
    """Evict the model from VRAM (8 GB cards need the room for Whisper/NVENC)."""
    if not settings.get("llm.unload_after_use", True):
        return
    try:
        requests.post(f"{_base()}/api/generate", json={"model": model(), "keep_alive": 0}, timeout=30)
    except requests.RequestException:
        pass


def health() -> dict:
    try:
        r = requests.get(f"{_base()}/api/tags", timeout=5)
        names = [m.get("name", "") for m in r.json().get("models", [])]
        m = model()
        present = any(n == m or n == f"{m}:latest" for n in names)
        return {"ok": True, "model": m, "model_present": present}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "model": model(), "error": str(e)[:200]}
