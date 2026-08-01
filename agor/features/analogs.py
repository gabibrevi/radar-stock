"""Enriquecimiento de análogos históricos (motor 11).

Compara el perfil de puntuaciones reciente de cada empresa con el de compañías
del mismo sector que en el histórico del radar ya alcanzaron bandas altas
(Watchlist+). También mide persistencia si hay varias fechas en score_totals.

Con poca historia el motor degrada con gracia (poca cobertura).
"""

from __future__ import annotations

import datetime as dt

import duckdb
import numpy as np
import pandas as pd
from rich.console import Console

console = Console()

ENGINE_PROFILE = (
    "e01_quality",
    "e02_financial_health",
    "e03_valuation",
    "e14_fundamental_momentum",
    "e10_technical",
    "e16_asymmetry",
)


def enrich_with_analogs(
    con: duckdb.DuckDBPyConnection,
    snapshot: pd.DataFrame,
    as_of: dt.date,
) -> pd.DataFrame:
    history = _load_history(con, as_of)
    if history.empty:
        console.print("[dim]Análogos: sin histórico de scores todavía.[/dim]")
        return snapshot

    winners = history[history["total"] >= 70.0]
    if winners.empty:
        winners = history.nlargest(min(200, len(history)), "total")

    profiles = _winner_profiles(con, winners)
    persistence = _persistence(history)
    current = _latest_profiles(con, as_of)
    if current.empty:
        current = _current_profile_proxy(snapshot)

    out = snapshot.copy()
    out["analog_similarity"] = np.nan
    out["analog_sector_strength"] = np.nan

    if not profiles.empty and not current.empty:
        for cik in snapshot.index:
            sector = snapshot.at[cik, "sector"] if "sector" in snapshot.columns else None
            if sector is None or sector not in profiles.index or cik not in current.index:
                continue
            target = profiles.loc[sector]
            vec = current.loc[cik]
            sim = _cosine(vec.reindex(target.index), target)
            out.at[cik, "analog_similarity"] = sim
            out.at[cik, "analog_sector_strength"] = float(
                pd.to_numeric(target, errors="coerce").mean()
            )

    if not persistence.empty:
        out = out.join(persistence, how="left")

    n = int(out["analog_similarity"].notna().sum()) if "analog_similarity" in out.columns else 0
    console.print(f"Análogos históricos: señal en [bold]{n}[/bold] empresas")
    return out


def _latest_profiles(con: duckdb.DuckDBPyConnection, as_of: dt.date) -> pd.DataFrame:
    try:
        frame = con.execute(
            """
            SELECT s.cik, s.engine_id, s.score
            FROM score_snapshots s
            WHERE s.as_of = (
                SELECT max(as_of) FROM score_totals WHERE as_of <= ? AND total IS NOT NULL
            )
              AND s.score IS NOT NULL
              AND s.engine_id IN (?, ?, ?, ?, ?, ?)
            """,
            [as_of, *ENGINE_PROFILE],
        ).fetchdf()
    except duckdb.Error:
        return pd.DataFrame()
    if frame.empty:
        return pd.DataFrame()
    return frame.pivot_table(index="cik", columns="engine_id", values="score", aggfunc="mean")


def _load_history(con: duckdb.DuckDBPyConnection, as_of: dt.date) -> pd.DataFrame:
    try:
        return con.execute(
            """
            SELECT as_of, cik, ticker, sector, total, band, coverage
            FROM score_totals
            WHERE total IS NOT NULL AND as_of <= ?
            """,
            [as_of],
        ).fetchdf()
    except duckdb.Error:
        return pd.DataFrame()


def _winner_profiles(
    con: duckdb.DuckDBPyConnection, winners: pd.DataFrame
) -> pd.DataFrame:
    if winners.empty:
        return pd.DataFrame()
    try:
        snaps = con.execute(
            """
            SELECT as_of, cik, engine_id, score
            FROM score_snapshots
            WHERE score IS NOT NULL
            """
        ).fetchdf()
    except duckdb.Error:
        return pd.DataFrame()
    if snaps.empty:
        return pd.DataFrame()

    key = winners[["as_of", "cik", "sector"]].drop_duplicates()
    # Normalizar tipos de fecha
    key["as_of"] = pd.to_datetime(key["as_of"]).dt.date
    snaps["as_of"] = pd.to_datetime(snaps["as_of"]).dt.date
    merged = snaps.merge(key, on=["as_of", "cik"], how="inner")
    merged = merged[merged["engine_id"].isin(ENGINE_PROFILE)]
    if merged.empty:
        return pd.DataFrame()

    piv = merged.pivot_table(
        index=["sector", "cik", "as_of"], columns="engine_id", values="score", aggfunc="mean"
    )
    return piv.groupby(level=0).mean()


def _persistence(history: pd.DataFrame) -> pd.DataFrame:
    if history.empty:
        return pd.DataFrame()
    g = history.groupby("cik")["total"]
    counts = g.count()
    out = pd.DataFrame(
        {
            "analog_persistence": g.mean(),
            "analog_stability": 100.0 - (g.std().fillna(0) * 2.0).clip(0, 100),
            "analog_obs": counts,
        }
    )
    # Sin al menos 2 observaciones la estabilidad no dice nada.
    out.loc[counts < 2, "analog_stability"] = np.nan
    return out


def _current_profile_proxy(snapshot: pd.DataFrame) -> pd.DataFrame:
    """Aproxima un perfil 0-100 con fundamentales cuando aún no hay engines."""
    mapping = {
        "e01_quality": ["roic", "operating_margin", "revenue_ttm_cagr_3y"],
        "e02_financial_health": ["equity_ratio", "interest_coverage", "fcf_margin"],
        "e03_valuation": ["earnings_yield", "fcf_yield", "ev_ebitda"],
        "e14_fundamental_momentum": ["revenue_yoy", "revenue_yoy_accel", "roic_delta_4q"],
        "e10_technical": ["rs_12m", "price_vs_ma200", "adx14"],
        "e16_asymmetry": ["risk_reward", "asymmetry", "expected_return"],
    }
    rows = {}
    for engine, cols in mapping.items():
        pieces = []
        for c in cols:
            if c not in snapshot.columns:
                continue
            s = pd.to_numeric(snapshot[c], errors="coerce")
            # Para yields tipo valoración, más alto a menudo es más barato = mejor.
            pieces.append(s.rank(pct=True) * 100.0)
        if pieces:
            rows[engine] = pd.concat(pieces, axis=1).mean(axis=1, skipna=True)
    if not rows:
        return pd.DataFrame(index=snapshot.index)
    return pd.DataFrame(rows, index=snapshot.index)


def _cosine(a: pd.Series, b: pd.Series) -> float:
    a = pd.to_numeric(a, errors="coerce")
    b = pd.to_numeric(b, errors="coerce")
    mask = a.notna() & b.notna()
    if mask.sum() < 2:
        return float("nan")
    av = a[mask].to_numpy(dtype=float)
    bv = b[mask].to_numpy(dtype=float)
    denom = np.linalg.norm(av) * np.linalg.norm(bv)
    if denom == 0:
        return float("nan")
    # Mapear coseno [-1,1] → [0,100]
    return float((av @ bv / denom + 1.0) * 50.0)
