"""Motor 14 — Momentum Fundamental (peso 5).

El enunciado lo llama "lo más importante" y conviene precisar por qué, porque su
peso nominal es pequeño: lo que este motor busca no es que la empresa sea buena,
sino que **esté mejorando rápido**. Es el único motor que puede detectar a una
compañía mediocre justo en el trimestre en que deja de serlo, y ese es el momento
exacto que el radar persigue.

De ahí la asimetría de diseño frente al Motor 1: aquí no se puntúa el nivel de
ningún margen, solo su derivada. Una empresa con margen operativo del 2% que
mejora 4 puntos al año puntúa más que una con el 30% estable.

Salvaguarda importante: la mejora tiene que venir acompañada de ingresos que no
caen. Una empresa que recorta gastos mientras pierde clientes mejora todos los
márgenes durante varios trimestres y luego desaparece.
"""

from __future__ import annotations

import pandas as pd

from .base import Component, ComponentEngine, ScoringContext


class FundamentalMomentumEngine(ComponentEngine):
    engine_id = "e14_fundamental_momentum"
    min_coverage = 0.3

    components = (
        Component("revenue_accel", "revenue_yoy_accel", "Aceleración de ingresos", 3.0),
        Component("revenue_yoy", "revenue_yoy", "Crecimiento de ingresos interanual", 2.0),
        Component("gross_delta", "gross_margin_delta_4q", "Mejora de margen bruto", 2.5),
        Component("operating_delta", "operating_margin_delta_4q", "Mejora de margen operativo", 3.0),
        Component("fcf_delta", "fcf_margin_delta_4q", "Mejora de margen de caja libre", 2.5),
        Component("eps_accel", "eps_yoy_accel", "Aceleración del EPS", 2.0),
        Component("roic_delta", "roic_delta_4q", "Mejora del ROIC", 2.0),
        Component(
            "streak",
            "margin_improving_streak",
            "Trimestres consecutivos mejorando margen",
            2.0,
            absolute=(0.0, 6.0),
        ),
        Component("rule_of_40", "rule_of_40", "Regla de 40", 1.5),
    )

    def adjust(
        self, ctx: ScoringContext, score: pd.Series, components: pd.DataFrame
    ) -> pd.Series:
        """Penaliza la mejora que procede de encogerse.

        Si los márgenes suben mientras los ingresos caen con fuerza, lo que hay
        detrás es normalmente un recorte de costes sobre un negocio en retirada, no
        un punto de inflexión. El motor no lo descalifica, pero le quita la
        capacidad de aparecer entre los mejores.
        """
        revenue_yoy = ctx.column("revenue_yoy")
        margins_up = ctx.column("operating_margin_delta_4q") > 0
        shrinking = revenue_yoy < -0.05
        return score.mask(margins_up & shrinking, score.clip(upper=45.0))
