"""Construcción del universo invertible.

Parte de las ~10.400 empresas con ticker en EDGAR y aplica los filtros que
convierten esa lista en un universo comparable. Cada exclusión tiene un motivo y
todas son reversibles desde `config.py`, porque son decisiones discutibles y no
verdades.

La exclusión de financieras y utilities merece explicación: no es que sean malas
inversiones, es que sus estados financieros hacen que ROIC, margen bruto o caja
libre signifiquen algo distinto. Un banco tiene "deuda" que es su materia prima.
Mezclarlos con el resto no solo puntúa mal a los bancos: distorsiona los
percentiles de todos los demás.
"""

from __future__ import annotations

import datetime as dt

import duckdb
import pandas as pd

from ..config import ALLOWED_EXCHANGES, EXCLUDED_SECTORS
from ..providers.sec import SecClient
from ..sectors import sector_from_sic
from ..store import upsert


def refresh_universe(con: duckdb.DuckDBPyConnection, client: SecClient) -> pd.DataFrame:
    tickers = client.universe()
    meta = con.execute("SELECT * FROM filer_meta").fetchdf()

    merged = tickers.merge(meta, on="cik", how="left", suffixes=("", "_meta"))
    merged["name"] = merged["name_meta"].fillna(merged["name"])
    merged["sector"] = merged["sic"].map(sector_from_sic)
    merged["sic_description"] = ""
    merged["is_foreign_filer"] = merged["latest_form"].isin(["20-F", "40-F", "20-F/A"])
    merged["fiscal_year_end"] = merged["fye"]
    merged["state_of_inc"] = merged["country_inc"]
    merged["active"] = True
    merged["updated_at"] = dt.datetime.now()

    columns = [
        "cik",
        "ticker",
        "name",
        "exchange",
        "sic",
        "sic_description",
        "sector",
        "fiscal_year_end",
        "is_foreign_filer",
        "state_of_inc",
        "active",
        "updated_at",
    ]
    frame = merged[columns].copy()
    frame["sic"] = pd.to_numeric(frame["sic"], errors="coerce").astype("Int64")
    upsert(con, "universe", frame, ["cik"])
    return frame


def investable_universe(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Universo tras aplicar los filtros de bolsa y sector."""
    exchanges = ", ".join(f"'{e}'" for e in sorted(ALLOWED_EXCHANGES))
    excluded = ", ".join(f"'{s}'" for s in sorted(EXCLUDED_SECTORS))
    return con.execute(
        f"""
        SELECT cik, ticker, name, exchange, sic, sector, is_foreign_filer
        FROM universe
        WHERE active
          AND exchange IN ({exchanges})
          AND sector NOT IN ({excluded})
          AND ticker IS NOT NULL AND ticker <> ''
        ORDER BY cik
        """
    ).fetchdf()


def universe_report(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Recuento por bolsa y sector, para poder ver qué se está descartando."""
    return con.execute(
        """
        SELECT exchange,
               count(*) AS total,
               count(*) FILTER (WHERE sector NOT IN ('Financiero','Seguros','Inmobiliario','SPAC / Blank check')) AS tras_filtro_sector
        FROM universe WHERE active
        GROUP BY exchange ORDER BY total DESC
        """
    ).fetchdf()
