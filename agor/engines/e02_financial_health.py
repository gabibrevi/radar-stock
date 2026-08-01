"""Motor 2 — Salud Financiera (peso 10).

Responde a una sola pregunta: ¿puede esta empresa financiar su propio crecimiento
sin depender de que el mercado le preste dinero o le compre acciones nuevas?

Aquí sí se usan escalas absolutas en buena parte de los componentes, al contrario
que en el resto del radar. Un current ratio de 0,6 es preocupante aunque todo el
sector esté igual de mal, y un interest coverage de 25 no aporta nada sobre uno de
15. El percentil se reserva para lo que solo tiene sentido comparado.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Component, ComponentEngine, ScoringContext


def _net_cash_over_assets(values: pd.Series) -> pd.Series:
    return values


class FinancialHealthEngine(ComponentEngine):
    engine_id = "e02_financial_health"
    min_coverage = 0.35

    components = (
        # Caja neta sobre activos: mide el colchón real, y es comparable entre
        # tamaños muy distintos sin necesitar el precio de la acción.
        Component("net_cash_ratio", "net_cash_to_assets", "Caja neta sobre activos", 3.0),
        Component(
            "net_debt_ebitda",
            "net_debt_to_ebitda",
            "Deuda neta / EBITDA",
            3.0,
            higher_is_better=False,
            absolute=(0.0, 4.0),
        ),
        Component(
            "current_ratio",
            "current_ratio",
            "Current ratio",
            2.0,
            absolute=(0.8, 2.5),
        ),
        Component(
            "quick_ratio",
            "quick_ratio",
            "Quick ratio",
            1.5,
            absolute=(0.5, 2.0),
        ),
        Component(
            "interest_coverage",
            "interest_coverage",
            "Cobertura de intereses",
            2.0,
            absolute=(1.0, 12.0),
        ),
        # Autofinanciación: caja libre suficiente para pagar su propia inversión.
        Component("self_funding", "fcf_margin", "Capacidad de autofinanciarse", 3.0),
        Component(
            "runway",
            "runway_months",
            "Meses de caja si quema dinero",
            1.5,
            absolute=(6.0, 36.0),
        ),
        # Dilución histórica: cada acción nueva reparte el futuro entre más manos.
        Component(
            "dilution_3y",
            "dilution_3y",
            "Dilución a 3 años",
            2.5,
            higher_is_better=False,
        ),
        Component(
            "buyback_intensity",
            "buyback_intensity",
            "Programa de recompra",
            1.0,
        ),
        Component(
            "equity_ratio",
            "equity_ratio",
            "Solvencia (patrimonio / activos)",
            1.5,
            absolute=(0.1, 0.7),
        ),
    )

    def adjust(
        self, ctx: ScoringContext, score: pd.Series, components: pd.DataFrame
    ) -> pd.Series:
        """Una empresa con caja neta positiva y caja libre positiva no puede
        suspender este motor por mucho que otros componentes falten: es
        objetivamente sólida. Se aplica un suelo para evitar que la ausencia de
        datos secundarios la penalice."""
        solid = (ctx.column("net_cash") > 0) & (ctx.column("fcf_ttm") > 0)
        return score.mask(solid & (score < 55.0), 55.0)
