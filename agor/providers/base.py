"""Utilidades comunes de red: límite de ritmo, reintentos y caché en disco.

Todas las fuentes gratuitas que usa AGOR imponen límites y ninguna nos debe nada.
Respetarlos no es cortesía: si la SEC nos bloquea la IP, el radar deja de existir.
Por eso el limitador es global por dominio y va deliberadamente por debajo del
máximo permitido.
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path

import requests
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import CACHE_DIR


class RateLimiter:
    """Cubo de fichas sencillo, seguro entre hilos."""

    def __init__(self, rate_per_second: float) -> None:
        self._min_interval = 1.0 / rate_per_second
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_allowed - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval


class RetryableHTTPError(requests.HTTPError):
    """Error que merece reintento (429 o 5xx)."""


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        rate_limiter: RateLimiter,
        cache_namespace: str,
        timeout: float = 60.0,
    ) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Encoding": "gzip, deflate",
            }
        )
        self.limiter = rate_limiter
        self.timeout = timeout
        self.cache_dir = CACHE_DIR / cache_namespace
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    @retry(
        retry=retry_if_exception_type(
            (RetryableHTTPError, requests.ConnectionError, requests.Timeout)
        ),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _request(self, url: str, params: dict | None = None, stream: bool = False):
        self.limiter.acquire()
        response = self.session.get(url, params=params, timeout=self.timeout, stream=stream)
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableHTTPError(f"{response.status_code} en {url}", response=response)
        response.raise_for_status()
        return response

    # ------------------------------------------------------------------
    def get_json(self, url: str, params: dict | None = None, cache_hours: float = 0.0):
        cache_path = self._cache_path(url, params, ".json")
        if cache_hours and self._is_fresh(cache_path, cache_hours):
            import json

            return json.loads(cache_path.read_text())

        response = self._request(url, params)
        if cache_hours:
            cache_path.write_bytes(response.content)
        return response.json()

    @retry(
        retry=retry_if_exception_type(
            (RetryableHTTPError, requests.ConnectionError, requests.Timeout)
        ),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def post_json(
        self,
        url: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        cache_hours: float = 0.0,
    ):
        """POST JSON. La caché en disco solo se usa si cache_hours > 0."""
        import json

        cache_path = self._cache_path(url, {**(params or {}), "_body": repr(json_body)}, ".json")
        if cache_hours and self._is_fresh(cache_path, cache_hours):
            return json.loads(cache_path.read_text())

        self.limiter.acquire()
        response = self.session.post(
            url, params=params, json=json_body, timeout=self.timeout
        )
        if response.status_code == 429 or response.status_code >= 500:
            raise RetryableHTTPError(f"{response.status_code} en {url}", response=response)
        response.raise_for_status()
        if cache_hours:
            cache_path.write_bytes(response.content)
        return response.json()

    def download(self, url: str, dest: Path, skip_if_exists: bool = True) -> Path:
        """Descarga en streaming. Usado para los ZIP de decenas de megas."""
        if skip_if_exists and dest.exists() and dest.stat().st_size > 0:
            return dest

        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_suffix(dest.suffix + ".part")
        response = self._request(url, stream=True)
        with partial.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 20):
                handle.write(chunk)
        partial.replace(dest)
        return dest

    def head_exists(self, url: str) -> bool:
        self.limiter.acquire()
        try:
            response = self.session.head(url, timeout=self.timeout, allow_redirects=True)
            return response.status_code == 200
        except requests.RequestException:
            return False

    # ------------------------------------------------------------------
    def _cache_path(self, url: str, params: dict | None, suffix: str) -> Path:
        key = url + ("?" + repr(sorted(params.items())) if params else "")
        digest = hashlib.sha256(key.encode()).hexdigest()[:24]
        return self.cache_dir / f"{digest}{suffix}"

    @staticmethod
    def _is_fresh(path: Path, hours: float) -> bool:
        return path.exists() and (time.time() - path.stat().st_mtime) < hours * 3600
