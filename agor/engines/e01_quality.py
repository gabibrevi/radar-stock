"""Motor 1 — Calidad Fundamental (peso 15, el mayor del radar).

Mide si el negocio es bueno, con independencia de a qué precio cotice. Combina
tres bloques: crecimiento a varios plazos, rentabilidad sobre ventas y
rentabilidad sobre el capital.

El bloque de retornos sobre capital pesa deliberadamente más que el de márgenes.
Un margen alto puede venir de un negocio que exige capital enorme para crecer; el
ROIC captura si ese crecimiento crea valor o solo lo consume. Es la diferencia
entre una compounder y una empresa que parece buena en la cuenta de resultados.

La descalificación por deterioro estructural que pide el enunciado se implementa
en `disqualify` y la agregación la propaga al score total: no basta con anular
este motor (eso redistribuiría su peso entre los demás); la empresa queda sin
puntuación final.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Component, ComponentEngine, ScoringContext


class QualityEngine(ComponentEngine):
    engine_id = "e01_quality"
    min_coverage = 0.35

    components = (
        # --- Crecimiento (peso conjunto 10) --------------------------------
        Component("rev_cagr_3y", "revenue_ttm_cagr_3y", "Crecimiento ingresos 3 años", 3.0),
        Component("rev_cagr_5y", "revenue_ttm_cagr_5y", "Crecimiento ingresos 5 años", 2.5),
        Component("rev_cagr_10y", "revenue_ttm_cagr_10y", "Crecimiento ingresos 10 años", 1.0),
        Component("ebitda_cagr_3y", "ebitda_ttm_cagr_3y", "Crecimiento EBITDA 3 años", 1.5),
        Component("eps_cagr_3y", "eps_diluted_ttm_cagr_3y", "Crecimiento EPS 3 años", 1.0),
        Component("fcf_cagr_3y", "fcf_ttm_cagr_3y", "Crecimiento FCF 3 años", 1.0),
        # --- Márgenes (peso conjunto 6) -----------------------------------
        Component("gross_margin", "gross_margin", "Margen bruto", 2.0),
        Component("operating_margin", "operating_margin", "Margen operativo", 2.0),
        Component("net_margin", "net_margin", "Margen neto", 1.0),
        Component("fcf_margin", "fcf_margin", "Margen de caja libre", 1.0),
        # --- Retorno sobre capital (peso conjunto 8) -----------------------
        Component("roic", "roic", "ROIC", 4.0),
        Component("roe", "roe", "ROE", 2.0),
        Component("roa", "roa", "ROA", 2.0),
        # --- Calidad del crecimiento (peso conjunto 6) ---------------------
        Component("incremental_margin", "incremental_margin", "Margen incremental", 2.0),
        Component("ebitda_to_fcf", "ebitda_to_fcf", "Conversión EBITDA a caja", 2.0),
        Component("earnings_quality", "earnings_quality", "Calidad del beneficio", 1.0),
        Component(
            "stability",
            "revenue_yoy_std_5y",
            "Estabilidad de ingresos",
            1.0,
            higher_is_better=False,
        ),
    )

    def disqualify(self, ctx: ScoringContext) -> pd.Series:
        """Deterioro estructural: fuera del radar, no solo mal puntuada.

        Los tres criterios buscan situaciones distintas y a propósito exigen
        varias condiciones a la vez. Una empresa puede tener un año malo sin estar
        deteriorada; lo que descalifica es la combinación de caída sostenida con
        pérdida de rentabilidad, o el riesgo real de quedarse sin caja.
        """
        reasons = pd.Series("", index=ctx.index, dtype="object")

        rev_3y = ctx.column("revenue_ttm_cagr_3y")
        op_margin = ctx.column("operating_margin")
        op_margin_delta = ctx.column("operating_margin_delta_4q")
        equity_ratio = ctx.column("equity_ratio")
        runway = ctx.column("runway_months")
        dilution = ctx.column("dilution_1y")

        shrinking = (rev_3y < -0.05) & (op_margin < 0)
        reasons = reasons.mask(
            shrinking, "Ingresos cayendo más del 5% anual durante 3 años y con pérdidas operativas"
        )

        eroding = (rev_3y < 0.0) & (op_margin_delta < -0.05) & (op_margin < 0.02)
        reasons = reasons.where(
            reasons != "", np.where(eroding, "Ingresos a la baja y márgenes en erosión", "")
        )

        # Menos de nueve meses de caja quemando dinero es un riesgo de dilución
        # forzada, no una oportunidad. Se descalifica salvo que ya casi no queme.
        cash_risk = (runway < 9.0) & (dilution > 0.10)
        reasons = reasons.where(
            reasons != "",
            np.where(cash_risk, "Menos de 9 meses de caja y dilución superior al 10% anual", ""),
        )

        insolvent = equity_ratio < -0.10
        reasons = reasons.where(
            reasons != "", np.where(insolvent, "Patrimonio neto negativo", "")
        )

        return reasons
