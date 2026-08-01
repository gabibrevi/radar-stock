"""Motor 15 — IA Predictiva (peso 3), heurística sin modelo entrenado.

No hay red neuronal ni labels forward todavía. Este motor combina señales que
históricamente anticipan compounders (aceleración, ROIC al alza, rule of 40,
asimetría de valoración) en una puntuación 0-100 explícita y auditable.

Cuando exista un modelo entrenado sobre el histórico del radar, sustituirá estos
componentes; hasta entonces el peso es el menor del sistema (3) a propósito.
"""

from __future__ import annotations

from .base import Component, ComponentEngine


class PredictiveAIEngine(ComponentEngine):
    engine_id = "e15_predictive_ai"
    min_coverage = 0.30

    components = (
        Component(
            "rev_accel",
            "revenue_yoy_accel",
            "Aceleración de ingresos YoY",
            2.5,
            absolute=(-0.10, 0.20),
        ),
        Component(
            "roic_delta",
            "roic_delta_4q",
            "Mejora de ROIC (4 trimestres)",
            2.0,
            absolute=(-0.05, 0.10),
        ),
        Component(
            "rule_of_40",
            "rule_of_40",
            "Rule of 40",
            2.0,
            absolute=(10.0, 60.0),
        ),
        Component(
            "margin_streak",
            "margin_improving_streak",
            "Racha de mejora de márgenes",
            1.5,
            absolute=(0.0, 6.0),
        ),
        Component(
            "expected_return",
            "expected_return",
            "Rentabilidad esperada (DCF)",
            2.0,
            absolute=(-0.05, 0.25),
        ),
        Component(
            "risk_reward",
            "risk_reward",
            "Recompensa / riesgo",
            2.0,
            absolute=(0.5, 4.0),
        ),
        Component(
            "earnings_quality",
            "earnings_quality",
            "Calidad del beneficio",
            1.0,
        ),
    )
