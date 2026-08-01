"""Motor 12 — Riesgo (peso 5).

Responde a: ¿cuánto puede romperse esta tesis por fragilidad del propio negocio,
no por el precio de la acción?

Capa numérica (panel + precios) siempre; capa cualitativa (gobernanza, litigios,
concentración) vía Gemini en el Top N. Sin LLM esos componentes quedan NaN y su
peso se redistribuye — no se inventan proxies.

Se solapa parcialmente con el motor 2 (salud financiera) a propósito: aquel
pregunta si puede crecer sin pedir dinero; este pregunta si el perfil de riesgo
es aceptable para una apuesta a 5-10 años.
"""

from __future__ import annotations

import pandas as pd

from .base import Component, ComponentEngine, ScoringContext


class RiskEngine(ComponentEngine):
    engine_id = "e12_risk"
    min_coverage = 0.30

    components = (
        Component(
            "revenue_volatility",
            "revenue_yoy_std_5y",
            "Volatilidad de ingresos (5 años)",
            3.0,
            higher_is_better=False,
        ),
        Component(
            "profit_consistency",
            "profitable_quarters_share",
            "Trimestres rentables sobre los últimos cinco años",
            2.5,
            absolute=(0.2, 0.95),
        ),
        Component(
            "dilution_risk",
            "dilution_3y",
            "Dependencia de diluir para financiarse",
            2.0,
            higher_is_better=False,
        ),
        Component(
            "interest_coverage",
            "interest_coverage",
            "Margen frente a una subida de tipos",
            2.0,
            absolute=(1.0, 12.0),
        ),
        Component(
            "runway",
            "runway_months",
            "Meses de caja si quema dinero",
            1.5,
            absolute=(6.0, 36.0),
        ),
        # Solo aportan si hay precios. Sin Polygon, el motor sigue en pie con lo
        # fundamental; su cobertura baja y el peso se redistribuye.
        Component(
            "price_volatility",
            "volatility_60d",
            "Volatilidad de cotización (60 sesiones)",
            1.5,
            higher_is_better=False,
        ),
        Component(
            "atr_pct",
            "atr_pct",
            "Rango medio diario sobre el precio",
            1.0,
            higher_is_better=False,
            absolute=(0.01, 0.08),
        ),
        # Capa cualitativa LLM (Top N). Ausente ⇒ peso redistribuido.
        Component(
            "risk_qual",
            "risk_qual_score",
            "Riesgo cualitativo global (LLM)",
            2.0,
            absolute=(20.0, 90.0),
        ),
        Component(
            "risk_governance",
            "risk_governance",
            "Gobernanza / dilución (LLM)",
            1.0,
            absolute=(20.0, 90.0),
        ),
        Component(
            "risk_litigation",
            "risk_litigation",
            "Perfil litigioso inferido (LLM)",
            0.75,
            absolute=(20.0, 90.0),
        ),
        Component(
            "risk_concentration",
            "risk_concentration",
            "Diversificación / no fragilidad (LLM)",
            0.75,
            absolute=(20.0, 90.0),
        ),
    )

    def adjust(
        self, ctx: ScoringContext, score: pd.Series, components: pd.DataFrame
    ) -> pd.Series:
        """Quemar caja sin runway y diluyendo a la vez pone techo a la nota.

        Es el patrón de riesgo existencial que el resto de componentes puede
        suavizar si la volatilidad histórica todavía no lo refleja.
        """
        burning = (ctx.column("fcf_ttm") < 0) & (ctx.column("runway_months") < 12)
        diluting = ctx.column("dilution_1y") > 0.15
        fragile = burning & diluting
        return score.mask(fragile.fillna(False) & (score > 35.0), 35.0)

    def disqualify(self, ctx: ScoringContext) -> pd.Series:
        """Sin caja y sin acceso plausible a financiación: fuera del radar."""
        reasons = pd.Series("", index=ctx.index, dtype="object")
        no_runway = (ctx.column("fcf_ttm") < 0) & (ctx.column("runway_months") < 3)
        heavy_dilution = ctx.column("dilution_1y") > 0.40
        reasons = reasons.mask(
            no_runway.fillna(False) & heavy_dilution.fillna(False),
            "Quema caja con menos de 3 meses de runway y dilución >40% en un año",
        )
        return reasons
