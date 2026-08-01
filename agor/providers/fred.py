"""Cliente FRED (Federal Reserve Economic Data).

La API es gratuita: 120 peticiones/minuto con una clave que se obtiene en dos
minutos sin tarjeta. Documentación: https://fred.stlouisfed.org/docs/api/api_key.html

Se usa la API v1 con `api_key` en query string: es la que documenta la mayoría de
ejemplos públicos y basta para series diarias/mensuales. Las respuestas se
cachean en disco; las series macro no cambian intradía de forma que importe al
radar, así que un día de caché es conservador.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from .base import HttpClient, RateLimiter

FRED_BASE = "https://api.stlouisfed.org/fred"
# Por debajo del límite oficial (120/min) a propósito.
FRED_REQUESTS_PER_SECOND = 1.5


class FredClient:
    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("Falta FRED_API_KEY en el fichero .env")
        self.api_key = api_key
        self.http = HttpClient(
            user_agent="AGOR/0.1 (macro engine)",
            rate_limiter=RateLimiter(FRED_REQUESTS_PER_SECOND),
            cache_namespace="fred",
        )

    def series(
        self,
        series_id: str,
        start: dt.date | None = None,
        end: dt.date | None = None,
    ) -> pd.Series:
        """Observaciones de una serie, indexadas por fecha, sin huecos marcados '.'."""
        end = end or dt.date.today()
        start = start or (end - dt.timedelta(days=365 * 5))
        payload = self.http.get_json(
            f"{FRED_BASE}/series/observations",
            params={
                "series_id": series_id,
                "api_key": self.api_key,
                "file_type": "json",
                "observation_start": start.isoformat(),
                "observation_end": end.isoformat(),
                "sort_order": "asc",
            },
            cache_hours=24.0,
        )
        rows = payload.get("observations") or []
        if not rows:
            return pd.Series(dtype="float64")
        frame = pd.DataFrame(rows)
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
        frame["value"] = pd.to_numeric(frame["value"], errors="coerce")
        frame = frame.dropna(subset=["date", "value"]).set_index("date")["value"]
        return frame.sort_index()
