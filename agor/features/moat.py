"""Enriquecimiento de ventaja competitiva (moat) vía Gemini.

Piloto deliberadamente acotado: solo el Top N por señal numérica previa (o por
proxy fundamental si aún no hay scores). El free tier de Gemini no aguanta el
universo entero; 100 llamadas/día caben holgadas en ~1.500 RPD.

Los resultados viven en `llm_moat` por (cik, as_of, model). Re-ejecutar el mismo
día reutiliza caché y no gasta cuota.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import duckdb
import numpy as np
import pandas as pd
from rich.console import Console

from ..config import LLM_MOAT_TOP_N, Settings
from ..providers.gemini import GeminiClient
from ..store import upsert

console = Console()

MOAT_COLUMNS = (
    "moat_score",
    "moat_durability",
    "moat_confidence",
    "moat_type",
)


def enrich_with_moat(
    con: duckdb.DuckDBPyConnection,
    snapshot: pd.DataFrame,
    settings: Settings,
    as_of: dt.date,
    *,
    top_n: int | None = None,
) -> pd.DataFrame:
    """Añade columnas moat_* al snapshot. Sin Gemini, no toca nada."""
    if not settings.has_gemini:
        console.print(
            "[dim]Sin GEMINI_API_KEY: el motor 5 (moat) queda desactivado. "
            "Clave gratuita en https://aistudio.google.com/apikey[/dim]"
        )
        return snapshot

    top_n = top_n if top_n is not None else LLM_MOAT_TOP_N
    candidates = _select_candidates(con, snapshot, as_of, top_n)
    if candidates.empty:
        console.print("[yellow]Moat LLM: sin candidatos.[/yellow]")
        return snapshot

    client = GeminiClient(settings.gemini_api_key, model=settings.gemini_model)
    cached = _load_cached(con, as_of, client.model)
    pending = candidates[~candidates.index.isin(cached.index)]

    console.print(
        f"Moat LLM (Gemini/{client.model}): "
        f"[bold]{len(candidates)}[/bold] candidatas · "
        f"{len(cached)} en caché · {len(pending)} nuevas"
    )

    fresh_rows: list[dict[str, Any]] = []
    for i, (cik, row) in enumerate(pending.iterrows(), start=1):
        try:
            parsed = client.generate_json(_prompt_for(row))
            scored = _normalize_response(parsed)
            fresh_rows.append(
                {
                    "cik": int(cik),
                    "as_of": as_of,
                    "model": client.model,
                    "moat_score": scored["moat_score"],
                    "moat_durability": scored["moat_durability"],
                    "moat_confidence": scored["moat_confidence"],
                    "moat_type": scored["moat_type"],
                    "rationale": scored["rationale"],
                    "raw_json": json.dumps(parsed, ensure_ascii=False)[:8000],
                }
            )
            if i == 1 or i % 10 == 0 or i == len(pending):
                console.print(f"  · Gemini {i}/{len(pending)} ({row.get('ticker', '?')})")
        except Exception as exc:  # noqa: BLE001 — una falla no tumba el piloto
            console.print(
                f"[yellow]  · Gemini falló en {row.get('ticker', cik)}: {exc}[/yellow]"
            )

    if fresh_rows:
        upsert(con, "llm_moat", pd.DataFrame(fresh_rows), ["cik", "as_of", "model"])

    all_scores = _load_cached(con, as_of, client.model)
    if all_scores.empty:
        return snapshot

    joined = snapshot.join(all_scores, how="left")
    covered = joined["moat_score"].notna().sum()
    console.print(f"Moat disponible para [bold]{covered}[/bold] empresas")
    return joined


def _select_candidates(
    con: duckdb.DuckDBPyConnection,
    snapshot: pd.DataFrame,
    as_of: dt.date,
    top_n: int,
) -> pd.DataFrame:
    """Top N por score previo; si no hay, por proxy de calidad/crecimiento."""
    ranked = snapshot.copy()
    prior = _prior_totals(con, as_of)
    if not prior.empty:
        ranked = ranked.join(prior.rename("prior_total"), how="left")
        ranked = ranked.sort_values("prior_total", ascending=False, na_position="last")
    else:
        ranked["_proxy"] = _proxy_rank(ranked)
        ranked = ranked.sort_values("_proxy", ascending=False, na_position="last")
    return ranked.head(top_n)


def _prior_totals(con: duckdb.DuckDBPyConnection, as_of: dt.date) -> pd.Series:
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


def _proxy_rank(snapshot: pd.DataFrame) -> pd.Series:
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


def _load_cached(
    con: duckdb.DuckDBPyConnection, as_of: dt.date, model: str
) -> pd.DataFrame:
    try:
        frame = con.execute(
            """
            SELECT cik, moat_score, moat_durability, moat_confidence, moat_type
            FROM llm_moat
            WHERE as_of = ? AND model = ?
            """,
            [as_of, model],
        ).fetchdf()
    except duckdb.Error:
        return pd.DataFrame(columns=list(MOAT_COLUMNS))
    if frame.empty:
        return pd.DataFrame(columns=list(MOAT_COLUMNS))
    return frame.set_index("cik")


def _prompt_for(row: pd.Series) -> str:
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

    return f"""Eres analista de ventaja competitiva (moat) al estilo Buffett/Morningstar.
Evalúa SOLO con la ficha numérica. No inventes hechos no inferibles de los datos.
Responde ÚNICAMENTE un JSON con estas claves numéricas (todas 0-100, no 0-1):
- moat_score: número 0-100 (fortaleza actual del foso)
- durability: número 0-100 (probabilidad de que el foso dure 10+ años)
- confidence: número 0-100 (confianza en tu juicio dado lo limitado de los datos)
- moat_type: exactamente una de: brand, network, cost, switching, regulation, scale, none
- rationale: 1-2 frases en español

Empresa: {row.get('name', '?')} ({row.get('ticker', '?')})
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
"""


def _normalize_response(data: dict[str, Any]) -> dict[str, Any]:
    def to_score(value: Any) -> float:
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
        # Algunos modelos devuelven 0-1 en lugar de 0-100.
        if 0.0 <= num <= 1.0:
            num *= 100.0
        return float(np.clip(num, 0.0, 100.0))

    def clamp(key_aliases: tuple[str, ...]) -> float:
        for key in key_aliases:
            if key in data:
                scored = to_score(data[key])
                if scored == scored:
                    return scored
        return float("nan")

    moat_type = str(data.get("moat_type") or "none").strip().lower()
    for token in ("brand", "network", "cost", "switching", "regulation", "scale"):
        if token in moat_type:
            moat_type = token
            break
    else:
        moat_type = "none"

    rationale = str(data.get("rationale") or data.get("reason") or "").strip()[:500]
    return {
        "moat_score": clamp(("moat_score", "score")),
        "moat_durability": clamp(("durability", "moat_durability")),
        "moat_confidence": clamp(("confidence", "moat_confidence")),
        "moat_type": moat_type,
        "rationale": rationale,
    }
