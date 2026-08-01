"""Cliente de Polygon.io para precios de cierre.

El plan gratuito permite 5 peticiones por minuto y dos años de historia, lo que
haría inviable pedir 10.000 tickers uno a uno. La solución es el endpoint
`grouped daily`, que devuelve el OHLCV de **todo el mercado estadounidense de un
día en una única petición**. Así el coste real es de una llamada por sesión
bursátil: unos 500 para rellenar los dos años iniciales y exactamente una al día
a partir de entonces.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from ..config import POLYGON_REQUESTS_PER_MINUTE
from .base import HttpClient, RateLimiter

POLYGON_BASE = "https://api.polygon.io"


class PolygonClient:
    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("Falta POLYGON_API_KEY en el fichero .env")
        self.api_key = api_key
        self.http = HttpClient(
            user_agent="AGOR/0.1",
            rate_limiter=RateLimiter(POLYGON_REQUESTS_PER_MINUTE / 60.0),
            cache_namespace="polygon",
        )

    def grouped_daily(self, day: dt.date) -> pd.DataFrame:
        """OHLCV de todas las acciones de EEUU en una sesión.

        Devuelve un DataFrame vacío en festivos y fines de semana, que es la
        respuesta legítima de la API (`resultsCount = 0`), no un error.
        """
        url = f"{POLYGON_BASE}/v2/aggs/grouped/locale/us/market/stocks/{day:%Y-%m-%d}"
        payload = self.http.get_json(
            url,
            params={"adjusted": "true", "apiKey": self.api_key},
            # Una sesión cerrada nunca cambia, así que se cachea de forma
            # indefinida y las re-ejecuciones no consumen cuota.
            cache_hours=24 * 365 * 10,
        )
        results = payload.get("results") or []
        if not results:
            return pd.DataFrame(columns=["ticker", "date", "open", "high", "low", "close", "volume", "vwap"])

        frame = pd.DataFrame(results).rename(
            columns={
                "T": "ticker",
                "o": "open",
                "h": "high",
                "l": "low",
                "c": "close",
                "v": "volume",
                "vw": "vwap",
            }
        )
        frame["date"] = day
        keep = ["ticker", "date", "open", "high", "low", "close", "volume", "vwap"]
        frame = frame[[c for c in keep if c in frame.columns]]
        frame["ticker"] = frame["ticker"].astype(str).str.upper()
        return frame

    @staticmethod
    def trading_days(start: dt.date, end: dt.date) -> list[dt.date]:
        """Días laborables del rango. Los festivos se detectan por respuesta vacía."""
        days: list[dt.date] = []
        day = start
        while day <= end:
            if day.weekday() < 5:
                days.append(day)
            day += dt.timedelta(days=1)
        return days
