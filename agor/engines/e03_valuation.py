"""Motor 3 — Valoración Inteligente (peso 10).

No busca lo barato, busca lo mal valorado. La diferencia es todo el motor.

Una empresa con PER 8 que decrece no está barata: está correctamente valorada. Una
con EV/Ventas de 12 creciendo al 45% con 85% de margen bruto y flujo de caja
positivo puede estar regalada. Por eso el peso mayor no va a los múltiplos
absolutos sino a los múltiplos **ajustados por crecimiento** y al descuento frente
a los comparables sectoriales.

Los múltiplos absolutos siguen presentes con peso menor, porque cumplen una
función: evitar que el motor se enamore de historias de crecimiento a cualquier
precio, que es exactamente cómo se pierde dinero en los mercados que este radar
quiere aprovechar.
"""

from __future__ import annotations

import pandas as pd

from .base import Component, ComponentEngine, ScoringContext


class ValuationEngine(ComponentEngine):
    engine_id = "e03_valuation"
    requires_prices = True
    min_coverage = 0.3

    components = (
        # --- Crecimiento por unidad de múltiplo (lo esencial) --------------
        Component("growth_per_sales", "growth_per_ev_sales", "Crecimiento por EV/Ventas", 3.5),
        Component("growth_per_ebitda", "growth_per_ev_ebitda", "Crecimiento por EV/EBITDA", 2.5),
        Component("peg", "peg", "PEG", 2.0, higher_is_better=False),
        # --- Descuento frente al sector ------------------------------------
        Component(
            "ev_sales_rel",
            "ev_sales_vs_sector",
            "EV/Ventas frente al sector",
            2.5,
            higher_is_better=False,
        ),
        Component(
            "ev_ebitda_rel",
            "ev_ebitda_vs_sector",
            "EV/EBITDA frente al sector",
            2.0,
            higher_is_better=False,
        ),
        Component(
            "pe_rel", "p_e_vs_sector", "PER frente al sector", 1.5, higher_is_better=False
        ),
        # --- Rentabilidades implícitas -------------------------------------
        Component("fcf_yield", "fcf_yield", "Rentabilidad de la caja libre", 2.5),
        Component("ebitda_yield", "ebitda_yield", "Rentabilidad del EBITDA sobre EV", 1.5),
        # --- Múltiplos absolutos (contrapeso) ------------------------------
        Component("ev_sales", "ev_sales", "EV/Ventas", 1.0, higher_is_better=False),
        Component("p_fcf", "p_fcf", "Precio / caja libre", 1.0, higher_is_better=False),
        # --- Recorrido implícito del DCF -----------------------------------
        Component("dcf_upside", "upside_base", "Recorrido según DCF", 2.5),
    )
