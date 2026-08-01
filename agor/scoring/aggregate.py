"""Agregación de los 16 motores en el Score Final.

La regla de oro de este módulo: **el peso de un motor que no ha podido puntuar se
redistribuye entre los que sí, no se cuenta como cero.** Sin esto, con datos
gratuitos toda empresa arrastraría un lastre proporcional a lo que no sabemos de
ella, y el radar acabaría premiando simplemente a las empresas más documentadas,
que son las grandes. Justo lo contrario de lo que se busca.

La contrapartida es que hay que informar de la cobertura y desconfiar de las
puntuaciones altas obtenidas con poca información. Eso se hace de dos formas: la
cobertura viaja siempre junto al score, y una empresa no puede alcanzar las bandas
superiores sin un mínimo de cobertura real.
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from ..config import (
    BANDS,
    EXPECTED_BAND_SHARE,
    MIN_OVERALL_COVERAGE,
    MIN_WEIGHT_APPLIED,
    WEIGHTS,
    band_for,
)
from ..engines.base import EngineResult, ScoringContext

# Cobertura mínima para acceder a cada banda. Una empresa de la que solo sabemos
# la mitad no entra en "Exceptional Buy" por muy bien que puntúe lo que sabemos.
BAND_COVERAGE_FLOOR = {
    "Exceptional Buy": 0.75,
    "Strong Buy": 0.65,
    "Buy": 0.55,
    "Watchlist Premium": 0.45,
    "Watchlist": 0.35,
}


def aggregate(
    results: list[EngineResult],
    ctx: ScoringContext,
    weights: dict[str, float] | None = None,
) -> pd.DataFrame:
    weights = weights or WEIGHTS
    scores = pd.DataFrame({r.engine_id: r.score for r in results}, index=ctx.index)
    coverage = pd.DataFrame({r.engine_id: r.coverage for r in results}, index=ctx.index)

    weight_series = pd.Series(weights, dtype="float64").reindex(scores.columns).fillna(0.0)

    available = scores.notna()
    effective = available.mul(weight_series, axis=1)
    total_weight = effective.sum(axis=1)

    weighted = (scores.fillna(0.0) * effective).sum(axis=1)
    total = weighted / total_weight.replace(0, np.nan)

    # Cobertura global: qué fracción del peso teórico del radar se ha podido
    # aplicar, ponderada además por la cobertura interna de cada motor.
    inner = (coverage.reindex(columns=scores.columns).fillna(0.0) * effective).sum(axis=1)
    weight_total = weight_series.sum()
    overall_coverage = (inner / weight_total).clip(0, 1)

    weight_applied = total_weight / weight_total

    # Una puntuación que se apoya en muy pocos motores o en muy pocos datos no es
    # una puntuación baja: es la ausencia de puntuación, y así se representa.
    insufficient = (weight_applied < MIN_WEIGHT_APPLIED) | (
        overall_coverage < MIN_OVERALL_COVERAGE
    )
    total = total.mask(insufficient)

    frame = pd.DataFrame(
        {
            "total": total.round(2),
            "coverage": overall_coverage.round(3),
            "engines_scored": available.sum(axis=1),
            "weight_applied": weight_applied.round(3),
        },
        index=ctx.index,
    )
    frame["band"] = _bands_with_coverage_floor(frame["total"], frame["coverage"])
    return frame.join(scores.add_prefix("score_")).join(coverage.add_prefix("cov_"))


def _bands_with_coverage_floor(total: pd.Series, coverage: pd.Series) -> pd.Series:
    """Asigna banda y degrada la que no cumpla su suelo de cobertura."""
    bands = total.map(lambda s: band_for(s) if pd.notna(s) else "Sin datos")
    order = [name for _, name in BANDS]

    for _ in range(len(order)):
        floors = bands.map(BAND_COVERAGE_FLOOR)
        failing = floors.notna() & (coverage < floors)
        if not failing.any():
            break
        # Baja un escalón a las que no llegan al suelo de cobertura.
        positions = bands.map({name: i for i, name in enumerate(order)})
        bands = bands.mask(failing, positions.add(1).clip(upper=len(order) - 1).map(dict(enumerate(order))))
    return bands


def calibration_report(frame: pd.DataFrame) -> pd.DataFrame:
    """Compara la rareza real de cada banda con la que declara la especificación.

    El enunciado afirma que 95-100 corresponde al 0,5% superior del universo. Con
    puntuaciones absolutas eso no está garantizado, y saber si el radar es
    demasiado generoso o demasiado severo es imprescindible antes de fiarse de las
    bandas. Este informe es el que permite decidir si hay que mover los cortes.
    """
    scored = frame["total"].notna().sum()
    if scored == 0:
        return pd.DataFrame()

    rows = []
    for _, name in BANDS:
        if name not in EXPECTED_BAND_SHARE:
            continue
        actual = (frame["band"] == name).sum()
        expected = EXPECTED_BAND_SHARE[name]
        rows.append(
            {
                "banda": name,
                "empresas": actual,
                "cuota_real": round(actual / scored, 5),
                "cuota_esperada": expected,
                "veredicto": _verdict(actual / scored, expected),
            }
        )
    return pd.DataFrame(rows)


def _verdict(actual: float, expected: float) -> str:
    if actual == 0:
        return "ninguna empresa alcanza la banda: cortes demasiado exigentes"
    ratio = actual / expected
    if ratio > 3:
        return "demasiadas: los cortes son laxos"
    if ratio < 0.33:
        return "muy pocas: los cortes son severos"
    return "en línea"


def to_snapshot_tables(
    frame: pd.DataFrame,
    results: list[EngineResult],
    metadata: pd.DataFrame,
    as_of: dt.date,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Prepara las dos tablas que se persisten: totales y detalle por motor."""
    totals = frame.join(metadata, how="left").reset_index().rename(columns={"index": "cik"})
    totals["as_of"] = as_of
    totals = totals[
        [
            "as_of",
            "cik",
            "ticker",
            "name",
            "sector",
            "market_cap",
            "total",
            "band",
            "coverage",
            "engines_scored",
        ]
    ]

    detail = pd.concat([r.as_long(as_of) for r in results], ignore_index=True)
    detail = detail.merge(
        metadata["ticker"].rename("ticker_meta"), left_on="cik", right_index=True, how="left"
    )
    detail["ticker"] = detail["ticker_meta"]
    detail = detail.drop(columns="ticker_meta")
    detail = detail[detail["score"].notna()]
    return totals, detail
