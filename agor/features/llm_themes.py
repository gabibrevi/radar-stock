"""Enriquecimiento LLM combinado: megatrends + catalizadores + riesgo cualitativo.

Una sola llamada Gemini por empresa del Top N (no tres), para respetar el free
tier. Resultados en `llm_themes`. Sin clave, no toca el snapshot.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import duckdb
import pandas as pd
from rich.console import Console

from ..config import LLM_MOAT_TOP_N, Settings
from ..providers.gemini import GeminiClient
from ..store import upsert
from .llm_common import clamp_keys, company_fiche, select_top_candidates

console = Console()

THEME_COLUMNS = (
    "mega_score",
    "mega_alignment",
    "mega_themes",
    "catalyst_score",
    "catalyst_clarity",
    "catalyst_horizon_years",
    "risk_qual_score",
    "risk_governance",
    "risk_litigation",
    "risk_concentration",
)


def enrich_with_llm_themes(
    con: duckdb.DuckDBPyConnection,
    snapshot: pd.DataFrame,
    settings: Settings,
    as_of: dt.date,
    *,
    top_n: int | None = None,
) -> pd.DataFrame:
    if not settings.has_gemini:
        console.print(
            "[dim]Sin GEMINI_API_KEY: motores 6, 7 y capa cualitativa del 12 desactivados.[/dim]"
        )
        return snapshot

    top_n = top_n if top_n is not None else LLM_MOAT_TOP_N
    candidates = select_top_candidates(con, snapshot, as_of, top_n)
    if candidates.empty:
        console.print("[yellow]Temas LLM: sin candidatos.[/yellow]")
        return snapshot

    client = GeminiClient(settings.gemini_api_key, model=settings.gemini_model)
    cached = _load_cached(con, as_of, client.model)
    pending = candidates[~candidates.index.isin(cached.index)]

    console.print(
        f"Temas LLM e06/e07/e12q (Gemini/{client.model}): "
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
                    **scored,
                    "raw_json": json.dumps(parsed, ensure_ascii=False)[:8000],
                }
            )
            if i == 1 or i % 10 == 0 or i == len(pending):
                console.print(f"  · Gemini temas {i}/{len(pending)} ({row.get('ticker', '?')})")
        except Exception as exc:  # noqa: BLE001
            console.print(
                f"[yellow]  · Gemini temas falló en {row.get('ticker', cik)}: {exc}[/yellow]"
            )

    if fresh_rows:
        upsert(con, "llm_themes", pd.DataFrame(fresh_rows), ["cik", "as_of", "model"])

    all_scores = _load_cached(con, as_of, client.model)
    if all_scores.empty:
        return snapshot

    joined = snapshot.join(all_scores, how="left")
    covered = joined["mega_score"].notna().sum()
    console.print(f"Temas LLM disponibles para [bold]{covered}[/bold] empresas")
    return joined


def _load_cached(
    con: duckdb.DuckDBPyConnection, as_of: dt.date, model: str
) -> pd.DataFrame:
    cols = ", ".join(THEME_COLUMNS)
    try:
        frame = con.execute(
            f"""
            SELECT cik, {cols}
            FROM llm_themes
            WHERE as_of = ? AND model = ?
            """,
            [as_of, model],
        ).fetchdf()
    except duckdb.Error:
        return pd.DataFrame(columns=list(THEME_COLUMNS))
    if frame.empty:
        return pd.DataFrame(columns=list(THEME_COLUMNS))
    return frame.set_index("cik")


def _prompt_for(row: pd.Series) -> str:
    return f"""Eres analista de inversión a 5-10 años. Evalúa SOLO con la ficha.
No inventes hechos concretos (juicios, clientes, países) que no se deduzcan de los datos.
Responde ÚNICAMENTE un JSON con estas claves (scores 0-100, no 0-1):

Megatrends:
- mega_score: exposición estructural a tendencias de largo plazo (IA, electrificación, demografía, ciberseguridad, salud, cloud, etc.)
- mega_alignment: 0-100 qué tan alineado está el sector/perfil con esas tendencias
- mega_themes: string corto con 1-3 temas (ej. "IA,semiconductores")

Catalizadores:
- catalyst_score: probabilidad de catalizadores positivos en 1-5 años inferible del perfil (crecimiento, márgenes, FCF, tamaño)
- catalyst_clarity: claridad/visibilidad del camino de creación de valor
- catalyst_horizon_years: número 1-10 (años hasta que el catalizador importaría)

Riesgo cualitativo (más alto = MENOS riesgo / mejor perfil):
- risk_qual_score: solidez cualitativa global (gobernanza implícita, concentración, litigios plausibles)
- risk_governance: 0-100 (alto = gobernanza aparentemente sana / dilución controlada)
- risk_litigation: 0-100 (alto = bajo riesgo litigioso inferible; sin datos concretos usa confianza baja)
- risk_concentration: 0-100 (alto = negocio diversificado / no frágil)

- confidence: 0-100 confianza en el juicio
- rationale: 1-2 frases en español

{company_fiche(row)}
"""


def _normalize_response(data: dict[str, Any]) -> dict[str, Any]:
    themes = str(data.get("mega_themes") or data.get("themes") or "").strip()[:120]
    horizon = data.get("catalyst_horizon_years", data.get("horizon_years"))
    try:
        horizon_f = float(horizon)
        if horizon_f != horizon_f:
            horizon_f = float("nan")
        else:
            horizon_f = float(max(1.0, min(10.0, horizon_f)))
    except (TypeError, ValueError):
        horizon_f = float("nan")

    return {
        "mega_score": clamp_keys(data, ("mega_score",)),
        "mega_alignment": clamp_keys(data, ("mega_alignment", "alignment")),
        "mega_themes": themes,
        "catalyst_score": clamp_keys(data, ("catalyst_score",)),
        "catalyst_clarity": clamp_keys(data, ("catalyst_clarity", "clarity")),
        "catalyst_horizon_years": horizon_f,
        "risk_qual_score": clamp_keys(data, ("risk_qual_score",)),
        "risk_governance": clamp_keys(data, ("risk_governance", "governance")),
        "risk_litigation": clamp_keys(data, ("risk_litigation", "litigation")),
        "risk_concentration": clamp_keys(data, ("risk_concentration", "concentration")),
        "rationale": str(data.get("rationale") or "").strip()[:500],
    }
