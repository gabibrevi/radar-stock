"""Ingesta de precios diarios desde Polygon.

Una petición por sesión bursátil devuelve el mercado entero, así que el coste no
depende del número de empresas. Con el límite gratuito de 5 peticiones por minuto,
rellenar dos años lleva unos 100 minutos la primera vez y la actualización diaria
es una única llamada.

Los festivos se detectan por respuesta vacía y se registran para no volver a
pedirlos: es la única forma de no gastar cuota preguntando por días en los que no
se negoció.
"""

from __future__ import annotations

import datetime as dt

import duckdb
import pandas as pd
from rich.console import Console

from ..config import POLYGON_FREE_TIER_YEARS
from ..providers.polygon import PolygonClient
from ..store import get_watermark, set_watermark, upsert

console = Console()


def backfill_prices(
    con: duckdb.DuckDBPyConnection,
    client: PolygonClient,
    years: int = POLYGON_FREE_TIER_YEARS,
    max_days: int | None = None,
) -> int:
    """Descarga las sesiones que faltan, de la más reciente a la más antigua.

    Se prioriza lo reciente a propósito: si la ejecución se corta a mitad, es
    mejor tener los últimos meses completos (que es lo que necesitan las medias
    móviles y la fuerza relativa) que un tramo antiguo suelto.
    """
    today = dt.date.today()
    start = today - dt.timedelta(days=int(365.25 * years))

    existing = set(
        con.execute("SELECT DISTINCT date FROM prices").fetchdf()["date"].tolist()
    )
    existing = {d if isinstance(d, dt.date) else pd.Timestamp(d).date() for d in existing}
    holidays = set((get_watermark(con, "polygon_holidays") or "").split(",")) - {""}

    candidates = [
        day
        for day in reversed(client.trading_days(start, today))
        if day not in existing and day.isoformat() not in holidays
    ]
    if max_days is not None:
        candidates = candidates[:max_days]

    total = 0
    for day in candidates:
        frame = client.grouped_daily(day)
        if frame.empty:
            holidays.add(day.isoformat())
            set_watermark(con, "polygon_holidays", ",".join(sorted(holidays)))
            continue
        # Se descartan los tickers con sufijos de clases exóticas y warrants, que
        # no corresponden a acciones ordinarias comparables.
        frame = frame[~frame["ticker"].str.contains(r"[.\-]W$|[.\-]U$|[.\-]R$", regex=True)]
        total += upsert(con, "prices", frame, ["ticker", "date"])
        console.print(f"  [green]{day}[/green]: {len(frame):,} tickers")

    return total


def latest_prices(con: duckdb.DuckDBPyConnection, lookback_days: int = 420) -> pd.DataFrame:
    """Ventana de precios suficiente para las medias de 200 sesiones."""
    return con.execute(
        f"""
        SELECT ticker, date, open, high, low, close, volume
        FROM prices
        WHERE date >= (SELECT max(date) - INTERVAL {lookback_days} DAY FROM prices)
        ORDER BY ticker, date
        """
    ).fetchdf()


def benchmark_series(con: duckdb.DuckDBPyConnection, ticker: str = "SPY") -> pd.Series:
    """Serie del índice de referencia para calcular fuerza relativa."""
    frame = con.execute(
        "SELECT date, close FROM prices WHERE ticker = ? ORDER BY date", [ticker]
    ).fetchdf()
    if frame.empty:
        return pd.Series(dtype="float64")
    return frame.set_index("date")["close"]


def average_volume(con: duckdb.DuckDBPyConnection, window: int = 50) -> pd.DataFrame:
    """Volumen medio reciente por ticker, para el filtro de liquidez."""
    return con.execute(
        f"""
        WITH ranked AS (
            SELECT ticker, volume,
                   row_number() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
            FROM prices
        )
        SELECT ticker, avg(volume) AS volume_50d_avg
        FROM ranked WHERE rn <= {window}
        GROUP BY ticker
        """
    ).fetchdf()
