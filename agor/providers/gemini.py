"""Cliente Gemini (Google AI Studio).

Plan gratuito: ~15 RPM y ~1.500 RPD según el modelo. Nos quedamos por debajo
del RPM a propósito. Las respuestas se cachean en DuckDB (tabla `llm_moat`), no
solo en disco: un score de moat no debe reconsultarse el mismo día.

Documentación: https://ai.google.dev/gemini-api/docs
"""

from __future__ import annotations

import json
import re
from typing import Any

from .base import HttpClient, RateLimiter

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
# Por debajo del límite típico del free tier (~15/min).
GEMINI_REQUESTS_PER_MINUTE = 10.0
DEFAULT_MODEL = "gemini-flash-lite-latest"


class GeminiClient:
    def __init__(self, api_key: str, model: str | None = None) -> None:
        if not api_key:
            raise ValueError("Falta GEMINI_API_KEY en el fichero .env")
        self.api_key = api_key
        self.model = model or DEFAULT_MODEL
        self.http = HttpClient(
            user_agent="AGOR/0.1 (moat engine)",
            rate_limiter=RateLimiter(GEMINI_REQUESTS_PER_MINUTE / 60.0),
            cache_namespace="gemini",
            timeout=90.0,
        )

    def generate_json(self, prompt: str, *, temperature: float = 0.2) -> dict[str, Any]:
        """Pide JSON estricto; si el modelo envuelve markdown, se limpia."""
        url = f"{GEMINI_BASE}/models/{self.model}:generateContent"
        payload = self.http.post_json(
            url,
            params={"key": self.api_key},
            json_body={
                "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": temperature,
                    "responseMimeType": "application/json",
                },
            },
            cache_hours=0.0,
        )
        text = _extract_text(payload)
        return _parse_json(text)


def _extract_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        raise RuntimeError(f"Gemini sin candidatos: {payload.get('error') or payload}")
    parts = ((candidates[0].get("content") or {}).get("parts")) or []
    chunks = [p.get("text", "") for p in parts if isinstance(p, dict)]
    text = "".join(chunks).strip()
    if not text:
        raise RuntimeError("Gemini devolvió respuesta vacía")
    return text


def _parse_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise RuntimeError("Gemini no devolvió un objeto JSON")
    return data
