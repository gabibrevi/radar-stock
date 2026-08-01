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

# Se publican las puntuaciones de los ocho motores, no solo las de algunos. El
# diseño entero del radar se apoya en poder responder "¿por qué esta empresa saca
# 91?" sin adivinar, y esa respuesta es el desglose por motor. Publicar la nota final
# sin sus componentes convierte el radar en una caja negra, que es justo lo que no
# debe ser.
DISPLAY_COLUMNS = [
    "ticker",
    "name",
    "sector",
    "total",
    "band",
    "coverage",
    "conviction",
    "market_cap",
    "revenue_ttm_cagr_3y",
    "gross_margin",
    "operating_margin",
    "roic",
    "net_cash",
    "score_e01_quality",
    "score_e02_financial_health",
    "score_e03_valuation",
    "score_e04_management",
    "score_e05_moat",
    "score_e06_megatrends",
    "score_e07_catalysts",
    "score_e08_institutional",
    "score_e09_sentiment",
    "score_e10_technical",
    "score_e11_historical_analogs",
    "score_e12_risk",
    "score_e13_macro",
    "score_e14_fundamental_momentum",
    "score_e15_predictive_ai",
    "score_e16_asymmetry",
    # Contexto de los motores de propiedad: sin estas cifras, un 90 en institucional
    # no dice si entraron veinte gestoras o si ya estaban todas dentro.
    "ins_net_buyers_90d",
    "ins_ownership_pct",
    "inst_holders",
    "inst_holders_change_pct",
    "inst_ownership_pct",
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
        "Gestoras entrando en el valor y directivos comprando en mercado abierto, "
        "cuando todavía queda sitio para que entre más dinero profesional. Se calcula "
        "sobre los datasets trimestrales de la SEC: describe el último trimestre "
        "publicado, no la sesión de hoy.",
        # Se exige flujo institucional y que no esté ya saturado. Sin el segundo
        # filtro el ranking se llenaría de valores con el 95% del capital en manos
        # institucionales, donde el descubrimiento que busca el radar ya ocurrió.
        lambda f: (_series(f, "inst_holders_change_pct") > 0.10)
        & (_series(f, "inst_holders") >= 25)
        & (_series(f, "inst_ownership_pct").fillna(0.0) < 0.85),
        sort_by="score_e08_institutional",
        requires=("score_e08_institutional", "inst_holders_change_pct"),
    ),
    Ranking(
        "insider_conviction",
        "Top 20 convicción de los directivos",
        "Varios directivos comprando acciones con su propio dinero y a precio de "
        "mercado, sin ventas discrecionales en paralelo. Excluye acciones entregadas "
        "como retribución y ejercicios de opciones, que llegan por calendario y no "
        "son decisiones de inversión.",
        lambda f: (_series(f, "ins_net_buyers_90d") >= 2)
        & (_series(f, "ins_net_to_mcap") >= 0.002),
        sort_by="score_e04_management",
        requires=("score_e04_management", "ins_net_buyers_90d"),
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
