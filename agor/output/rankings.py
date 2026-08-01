"""Los diez rankings que produce cada ejecución.

Cada ranking es una pregunta distinta sobre el mismo conjunto de puntuaciones, no
una reordenación cosmética. "Compounders" y "posibles 10x" buscan cosas casi
opuestas: el primero quiere consistencia demostrada durante años, el segundo
quiere una base pequeña con crecimiento explosivo y margen para multiplicarse. Una
empresa que aparece en los dos es sospechosa, no doblemente buena.

Los rankings que dependen de datos que aún no tenemos se generan vacíos y
declarados como tales, en lugar de rellenarse con un criterio aproximado que
parecería equivalente sin serlo.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

TOP_N = 20

DISPLAY_COLUMNS = [
    "ticker",
    "name",
    "sector",
    "total",
    "band",
    "coverage",
    "market_cap",
    "revenue_ttm_cagr_3y",
    "gross_margin",
    "operating_margin",
    "roic",
    "net_cash",
    "score_e01_quality",
    "score_e14_fundamental_momentum",
    "score_e03_valuation",
    "score_e10_technical",
    "score_e16_asymmetry",
]


@dataclass(frozen=True)
class Ranking:
    key: str
    title: str
    description: str
    selector: Callable[[pd.DataFrame], pd.Series]
    sort_by: str = "total"
    requires: tuple[str, ...] = ()


def _has(frame: pd.DataFrame, columns: tuple[str, ...]) -> bool:
    return all(c in frame.columns for c in columns)


def _series(frame: pd.DataFrame, column: str, default: float = np.nan) -> pd.Series:
    if column in frame.columns:
        return pd.to_numeric(frame[column], errors="coerce")
    return pd.Series(default, index=frame.index, dtype="float64")


RANKINGS: tuple[Ranking, ...] = (
    Ranking(
        "exceptional_buy",
        "Top 20 Exceptional Buy",
        "Puntuación de 95 o más: el extremo superior del universo.",
        lambda f: f["band"] == "Exceptional Buy",
    ),
    Ranking(
        "strong_buy",
        "Top 20 Strong Buy",
        "Puntuación entre 90 y 95.",
        lambda f: f["band"] == "Strong Buy",
    ),
    Ranking(
        "small_caps",
        "Top 20 Small Caps ocultas",
        "Menos de 2.000 millones de capitalización y calidad alta. Es donde una "
        "revalorización de diez veces sigue siendo aritméticamente posible.",
        lambda f: (_series(f, "market_cap") < 2e9)
        & (_series(f, "market_cap") > 5e7)
        & (_series(f, "score_e01_quality") > 65),
    ),
    Ranking(
        "mid_caps",
        "Top 20 Mid Caps de alta calidad",
        "Entre 2.000 y 10.000 millones: ya han demostrado que el modelo funciona y "
        "todavía tienen recorrido de tamaño.",
        lambda f: (_series(f, "market_cap") >= 2e9)
        & (_series(f, "market_cap") < 1e10)
        & (_series(f, "score_e01_quality") > 70),
    ),
    Ranking(
        "ai_semis",
        "Top 20 IA y semiconductores",
        "Sectores de semiconductores, equipamiento, software y servicios de datos.",
        lambda f: f["sector"].isin(
            [
                "Semiconductores",
                "Equipamiento semiconductores",
                "Software",
                "Internet / Software",
                "Servicios IT / Datos",
                "Servicios IT",
                "Electrónica",
            ]
        ),
    ),
    Ranking(
        "accumulation",
        "Top 20 en fase de acumulación",
        "Base técnica estrecha, volumen construyéndose y precio sostenido sobre la "
        "media de 200 sesiones.",
        lambda f: _series(f, "accumulation_score") > 60,
        sort_by="accumulation_score",
        requires=("accumulation_score",),
    ),
    Ranking(
        "compounders",
        "Top 20 Compounders",
        "Crecimiento sostenido a cinco años, ROIC alto, márgenes estables y sin "
        "dilución. Negocios capaces de crecer durante décadas.",
        lambda f: (_series(f, "revenue_ttm_cagr_5y") > 0.10)
        & (_series(f, "roic") > 0.12)
        & (_series(f, "dilution_3y") < 0.10)
        & (_series(f, "profitable_quarters_share") > 0.7),
    ),
    Ranking(
        "possible_10x",
        "Top 20 posibles 10x",
        "Capitalización pequeña, crecimiento muy alto, margen bruto que permite "
        "escalar y caja suficiente para llegar. Alto riesgo por construcción.",
        lambda f: (_series(f, "market_cap") < 3e9)
        & (_series(f, "revenue_ttm_cagr_3y") > 0.25)
        & (_series(f, "gross_margin") > 0.35)
        & (_series(f, "runway_months").fillna(999) > 18),
    ),
    Ranking(
        "turnaround",
        "Top 20 Turnaround",
        "Fundamentales mejorando con fuerza desde una base débil: márgenes al alza "
        "durante varios trimestres tras haber estado en pérdidas.",
        lambda f: (_series(f, "margin_improving_streak") >= 3)
        & (_series(f, "operating_margin_delta_4q") > 0.02)
        & (_series(f, "profitable_quarters_share") < 0.6),
        sort_by="score_e14_fundamental_momentum",
    ),
    Ranking(
        "institutional",
        "Top 20 compras institucionales recientes",
        "PENDIENTE: requiere formularios 13F y Form 4, que ya están disponibles en "
        "EDGAR pero cuyo motor (el 8) todavía no está implementado.",
        lambda f: pd.Series(False, index=f.index),
        requires=("score_e08_institutional",),
    ),
)


def build_rankings(frame: pd.DataFrame, top_n: int = TOP_N) -> dict[str, pd.DataFrame]:
    """Aplica cada selector y devuelve las tablas listas para publicar."""
    out: dict[str, pd.DataFrame] = {}
    scored = frame[frame["total"].notna()]

    for ranking in RANKINGS:
        if ranking.requires and not _has(scored, ranking.requires):
            out[ranking.key] = pd.DataFrame(columns=DISPLAY_COLUMNS)
            continue
        try:
            mask = ranking.selector(scored)
        except KeyError:
            out[ranking.key] = pd.DataFrame(columns=DISPLAY_COLUMNS)
            continue

        subset = scored[mask.fillna(False)]
        sort_column = ranking.sort_by if ranking.sort_by in subset.columns else "total"
        subset = subset.sort_values(sort_column, ascending=False).head(top_n)
        columns = [c for c in DISPLAY_COLUMNS if c in subset.columns]
        out[ranking.key] = subset[columns].reset_index()

    return out


def ranking_metadata() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"clave": r.key, "titulo": r.title, "descripcion": r.description}
            for r in RANKINGS
        ]
    )
