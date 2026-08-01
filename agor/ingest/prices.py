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

from ..config import BENCHMARK_TICKER, POLYGON_FREE_TIER_YEARS
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

    # La conversión a `date` es incondicional a propósito. DuckDB devuelve estas
    # fechas como pandas.Timestamp, que hereda de datetime.date, así que un
    # `isinstance(d, dt.date)` las da por buenas y las deja sin convertir. Pero un
    # Timestamp a medianoche no es igual —ni comparte hash— con el date del mismo
    # día, de modo que ninguna sesión se reconocía como ya descargada: cada
    # ejecución volvía a empezar por la más reciente y el trabajo hecho no contaba.
    # Con cinco peticiones por minuto de cuota, reanudar mal cuesta horas.
    existing = {
        pd.Timestamp(d).date()
        for d in con.execute("SELECT DISTINCT date FROM prices").fetchdf()["date"]
    }
    holidays = set((get_watermark(con, "polygon_holidays") or "").split(",")) - {""}

    candidates = [
        day
        for day in reversed(client.trading_days(start, today))
        if day not in existing and day.isoformat() not in holidays
    ]
    if max_days is not None:
        candidates = candidates[:max_days]

    keep = _tickers_to_keep(con)

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
        frame = _resolve_ticker_collisions(frame)
        frame = frame[frame["ticker"].isin(keep)]
        total += upsert(con, "prices", frame, ["ticker", "date"])
        console.print(f"  [green]{day}[/green]: {len(frame):,} tickers")

    return total


def _tickers_to_keep(con: duckdb.DuckDBPyConnection) -> set[str]:
    """Símbolos que merece la pena almacenar de cada sesión de Polygon.

    La descarga no se puede acotar: `grouped_daily` devuelve el mercado entero en
    una sola petición, así que pedir veinte tickers cuesta lo mismo que pedir doce
    mil. Lo que sí se puede acotar es lo que se guarda y lo que después hay que
    recorrer para calcular medias móviles, y ahí sobra más de la mitad: Polygon
    incluye ETFs, fondos cerrados, warrants, preferentes y vehículos que no
    presentan cuentas a la SEC y que por tanto el radar nunca podrá puntuar.

    Se filtra contra `universe` y deliberadamente NO contra `investable_universe`.
    Este último aplica los filtros de sector y capitalización vigentes hoy, y
    purgar con ese criterio dejaría sin historial a una empresa que hoy no los pasa
    pero los pase dentro de seis meses: llegaría al radar sin media de 200 sesiones
    ni fuerza relativa justo cuando empieza a interesar, y recuperar ese pasado
    exigiría rehacer la descarga entera. El universo amplio elimina lo que nunca
    se va a puntuar sin abrir huecos futuros.

    El benchmark se añade a mano porque es la excepción que rompe la regla: SPY es
    un ETF y no está en `universe`, de modo que el filtro lo borraría y
    `benchmark_series()` devolvería una serie vacía. El motor técnico no fallaría,
    simplemente se quedaría sin comparador de fuerza relativa y nadie se enteraría.
    """
    tickers = con.execute(
        "SELECT DISTINCT ticker FROM universe WHERE ticker IS NOT NULL"
    ).fetchdf()["ticker"]
    return set(tickers) | {BENCHMARK_TICKER}


def _resolve_ticker_collisions(frame: pd.DataFrame) -> pd.DataFrame:
    """Deja una sola fila por ticker y sesión.

    Polygon devuelve de vez en cuando el mismo símbolo dos veces en la misma sesión,
    y no con una diferencia de céntimos: en la sesión del 31 de julio de 2026, BCPC
    aparecía a la vez a 24,10 y a 167,56 dólares, y TPC a 17,15 y a 83,75. Son dos
    compañías distintas compartiendo símbolo, normalmente porque una acaba de cambiar
    de ticker y la otra lo ha heredado.

    Se conserva la fila con más volumen en dólares, que es la cotización principal:
    en el caso de BCPC, la de 167,56 con 346.000 títulos negociados frente a los
    24.000 de la otra, que corresponde a Balchem. Descartar las dos sería peor, porque
    perdería una empresa legítima por culpa de un residuo.

    Sin esto la descarga entera aborta con un error de clave duplicada en la primera
    sesión que contenga una colisión, que es exactamente lo que ocurrió.
    """
    if frame.empty or not frame["ticker"].duplicated().any():
        return frame
    turnover = frame["close"] * frame["volume"]
    return (
        frame.assign(_turnover=turnover)
        .sort_values("_turnover", ascending=False)
        .drop_duplicates(subset=["ticker", "date"], keep="first")
        .drop(columns="_turnover")
        .sort_values("ticker")
        .reset_index(drop=True)
    )


def purge_out_of_universe(con: duckdb.DuckDBPyConnection) -> tuple[int, int]:
    """Borra de `prices` lo que la ingesta filtrada ya no volvería a guardar.

    Necesaria porque el filtro se añadió con la tabla a medio llenar y solo actúa
    sobre lo que entra. Devuelve (filas, tickers) eliminados.

    No se ejecuta automáticamente en cada descarga: si un refresco del universo
    fallara o EDGAR omitiera temporalmente una empresa, una purga automática
    borraría años de cotizaciones que costaría horas recuperar. Se invoca a mano
    con `radar precios --purgar`.
    """
    keep = _tickers_to_keep(con)
    con.execute("CREATE OR REPLACE TEMP TABLE _keep (ticker VARCHAR)")
    con.executemany("INSERT INTO _keep VALUES (?)", [(t,) for t in sorted(keep)])

    rows, tickers = con.execute(
        """
        SELECT count(*), count(DISTINCT ticker) FROM prices
        WHERE ticker NOT IN (SELECT ticker FROM _keep)
        """
    ).fetchone()
    if rows:
        con.execute("DELETE FROM prices WHERE ticker NOT IN (SELECT ticker FROM _keep)")
    con.execute("DROP TABLE _keep")
    return int(rows), int(tickers)


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


def benchmark_series(
    con: duckdb.DuckDBPyConnection, ticker: str = BENCHMARK_TICKER
) -> pd.Series:
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
