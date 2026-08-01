"""Utilidades compartidas para enriquecimientos LLM (moat, temas, riesgo)."""

from __future__ import annotations

from typing import Any

import duckdb
import numpy as np
import pandas as pd


def select_top_candidates(
    con: duckdb.DuckDBPyConnection,
    snapshot: pd.DataFrame,
    as_of,
    top_n: int,
) -> pd.DataFrame:
    ranked = snapshot.copy()
    prior = prior_totals(con, as_of)
    if not prior.empty:
        ranked = ranked.join(prior.rename("prior_total"), how="left")
        ranked = ranked.sort_values("prior_total", ascending=False, na_position="last")
    else:
        ranked["_proxy"] = proxy_rank(ranked)
        ranked = ranked.sort_values("_proxy", ascending=False, na_position="last")
    return ranked.head(top_n)


def prior_totals(con: duckdb.DuckDBPyConnection, as_of) -> pd.Series:
    try:
        frame = con.execute(
            """
            SELECT cik, total
            FROM score_totals
            WHERE as_of = (
                SELECT max(as_of) FROM score_totals WHERE as_of <= ?
            )
            """,
            [as_of],
        ).fetchdf()
    except duckdb.Error:
        return pd.Series(dtype="float64")
    if frame.empty:
        return pd.Series(dtype="float64")
    return frame.set_index("cik")["total"]


def proxy_rank(snapshot: pd.DataFrame) -> pd.Series:
    def col(name: str) -> pd.Series:
        if name in snapshot.columns:
            return pd.to_numeric(snapshot[name], errors="coerce")
        return pd.Series(np.nan, index=snapshot.index)

    pieces = [
        col("roic").rank(pct=True),
        col("operating_margin").rank(pct=True),
        col("revenue_ttm_cagr_3y").rank(pct=True),
        col("fcf_margin").rank(pct=True),
    ]
    return pd.concat(pieces, axis=1).mean(axis=1, skipna=True)


def to_score_0_100(value: Any) -> float:
    if value is None:
        return float("nan")
    if isinstance(value, str):
        text = value.strip().lower()
        ordinal = {
            "none": 10.0,
            "low": 25.0,
            "weak": 25.0,
            "medium": 55.0,
            "moderate": 55.0,
            "high": 80.0,
            "strong": 85.0,
            "very high": 92.0,
        }
        if text in ordinal:
            return ordinal[text]
        try:
            value = float(text.replace("%", "").strip())
        except ValueError:
            return float("nan")
    try:
        num = float(value)
    except (TypeError, ValueError):
        return float("nan")
    if num != num:
        return float("nan")
    if 0.0 <= num <= 1.0:
        num *= 100.0
    return float(np.clip(num, 0.0, 100.0))


def clamp_keys(data: dict[str, Any], key_aliases: tuple[str, ...]) -> float:
    for key in key_aliases:
        if key in data:
            scored = to_score_0_100(data[key])
            if scored == scored:
                return scored
    return float("nan")


def company_fiche(row: pd.Series) -> str:
    def fmt(name: str, pct: bool = False) -> str:
        val = row.get(name)
        try:
            num = float(val)
        except (TypeError, ValueError):
            return "n/d"
        if num != num:
            return "n/d"
        if pct:
            return f"{num * 100:.1f}%"
        if abs(num) >= 1e9:
            return f"{num / 1e9:.2f}B"
        if abs(num) >= 1e6:
            return f"{num / 1e6:.1f}M"
        return f"{num:.2f}"

    return f"""Empresa: {row.get('name', '?')} ({row.get('ticker', '?')})
Sector: {row.get('sector', 'n/d')}
Cap. mercado: {fmt('market_cap')}
Ingresos TTM: {fmt('revenue_ttm')}
CAGR ingresos 3a: {fmt('revenue_ttm_cagr_3y', pct=True)}
CAGR ingresos 5a: {fmt('revenue_ttm_cagr_5y', pct=True)}
Margen bruto: {fmt('gross_margin', pct=True)}
Margen operativo: {fmt('operating_margin', pct=True)}
Margen neto: {fmt('net_margin', pct=True)}
Margen FCF: {fmt('fcf_margin', pct=True)}
ROIC: {fmt('roic', pct=True)}
ROE: {fmt('roe', pct=True)}
Estabilidad ingresos (std YoY 5a): {fmt('revenue_yoy_std_5y')}
Dilución 1a: {fmt('dilution_1y', pct=True)}
Runway meses: {fmt('runway_months')}
"""
