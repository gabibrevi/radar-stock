"""Motor 16 — Asimetría (peso 10, el segundo mayor).

El enunciado lo llama el motor más importante y tiene razón por un motivo
concreto: los otros quince miden atributos de la empresa, y este mide la calidad
de la **apuesta**. Una empresa magnífica a un precio absurdo es una mala apuesta;
una empresa correcta a un precio de liquidación puede ser una excelente.

Lo que se busca no es maximizar el recorrido al alza sino la asimetría: que lo que
se puede ganar si acierta sea varias veces lo que se puede perder si falla. Por eso
el componente de mayor peso no es el recorrido esperado, es la relación
recompensa/riesgo.

Una advertencia que conviene no perder de vista: estas cifras salen de un DCF con
supuestos declarados, no de una predicción. Su valor está en ordenar empresas
entre sí bajo un criterio consistente, no en acertar el precio objetivo de
ninguna.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Component, ComponentEngine, EngineResult, ScoringContext


class AsymmetryEngine(ComponentEngine):
    engine_id = "e16_asymmetry"
    requires_prices = True
    min_coverage = 0.3

    components = (
        Component("risk_reward", "risk_reward", "Relación recompensa / riesgo", 3.5),
        Component("asymmetry", "asymmetry", "Asimetría entre escenarios", 3.0),
        Component("expected_return", "expected_return", "Rentabilidad esperada", 2.5),
        Component("upside", "upside_base", "Recorrido al alza (escenario base)", 2.0),
        Component(
            "downside",
            "downside_bear",
            "Pérdida en el escenario adverso",
            2.0,
        ),
        Component("conviction", "conviction", "Convicción", 2.0),
    )


def compute_conviction(
    engine_scores: pd.DataFrame, engine_coverage: pd.DataFrame
) -> pd.Series:
    """Convicción: cuánto merece fiarse de la puntuación de esta empresa.

    Combina dos cosas distintas que suelen confundirse:

    - **Cuánto sabemos**: la cobertura media de datos. Una empresa evaluada con el
      40% de los datos no puede generar convicción alta aunque puntúe bien.
    - **Cuánto coinciden las señales**: si calidad, momentum y valoración apuntan
      en la misma dirección, la tesis es coherente. Si una punta muy alto y las
      otras muy bajo, hay algo que no encaja y la convicción baja.

    Se devuelve en la escala 0-100 para poder usarse como cualquier otro
    componente.
    """
    if engine_scores.empty:
        return pd.Series(dtype="float64")

    core = [c for c in ("e01_quality", "e03_valuation", "e14_fundamental_momentum") if c in engine_scores.columns]
    knowledge = engine_coverage.mean(axis=1).clip(0, 1)

    if len(core) >= 2:
        subset = engine_scores[core]
        # Desviación típica normalizada: 0 cuando todas las señales coinciden.
        dispersion = (subset.std(axis=1) / 50.0).clip(0, 1)
        agreement = 1.0 - dispersion
        level = subset.mean(axis=1) / 100.0
    else:
        agreement = pd.Series(0.5, index=engine_scores.index)
        level = engine_scores.mean(axis=1) / 100.0

    conviction = (0.4 * knowledge + 0.3 * agreement + 0.3 * level) * 100.0
    return conviction.reindex(engine_scores.index)
