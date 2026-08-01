"""Motor 10 — Técnico (peso 7).

En un radar cuyo horizonte son cinco a diez años, el análisis técnico no sirve
para decidir *qué* comprar sino *cuándo* mirar. Por eso este motor está diseñado
para premiar la tendencia sana y la base construida, y no el impulso extremo: una
acción que ha subido un 300% en tres meses puntúa peor que una que lleva nueve
meses construyendo una base estrecha por encima de su media de 200 sesiones.

Esa es una elección de diseño con consecuencias. El motor va a dejar pasar
verticalidades espectaculares. A cambio, no llenará los rankings de acciones que
ya lo han hecho todo, que es el fallo que hace inútiles a la mayoría de los
screeners de momentum.
"""

from __future__ import annotations

import pandas as pd

from .base import Component, ComponentEngine, ScoringContext


class TechnicalEngine(ComponentEngine):
    engine_id = "e10_technical"
    requires_prices = True
    min_coverage = 0.35

    components = (
        # --- Tendencia -----------------------------------------------------
        Component(
            "above_ma200",
            "price_vs_ma200",
            "Precio sobre la media de 200 sesiones",
            2.5,
            absolute=(-0.15, 0.30),
        ),
        Component(
            "ma_alignment",
            "ma50_vs_ma200",
            "Media de 50 sobre la de 200",
            2.0,
            absolute=(-0.10, 0.20),
        ),
        Component("adx", "adx14", "Fuerza de la tendencia (ADX)", 1.5, absolute=(12.0, 35.0)),
        # --- Base y acumulación --------------------------------------------
        Component("accumulation", "accumulation_score", "Fase de acumulación", 3.0),
        Component("base_tightness", "base_tightness", "Estrechez de la base", 2.0),
        Component("effort_ratio", "effort_ratio", "Volumen comprador frente a vendedor", 2.0),
        Component("volume_surge", "volume_surge", "Repunte de volumen", 1.5, absolute=(0.9, 1.6)),
        # --- Fuerza relativa -----------------------------------------------
        Component("rs_12m", "rs_12m", "Fuerza relativa a 12 meses", 2.5),
        Component("rs_6m", "rs_6m", "Fuerza relativa a 6 meses", 2.0),
        # --- Posición en el rango y riesgo ---------------------------------
        Component(
            "off_high",
            "pct_off_52w_high",
            "Distancia al máximo de 52 semanas",
            1.5,
            absolute=(-0.40, -0.02),
        ),
        Component(
            "volatility",
            "volatility_60d",
            "Volatilidad a 60 sesiones",
            1.0,
            higher_is_better=False,
        ),
    )

    def adjust(
        self, ctx: ScoringContext, score: pd.Series, components: pd.DataFrame
    ) -> pd.Series:
        """Rebaja los casos ya extendidos y los ilíquidos.

        Un valor que cotiza un 60% por encima de su media de 200 sesiones no está
        entrando en una fase: está terminándola. Y por debajo de un volumen mínimo
        la señal técnica es ruido, porque cualquier orden mediana mueve el precio.
        """
        extended = ctx.column("price_vs_ma200") > 0.60
        score = score.mask(extended, score.clip(upper=50.0))

        turnover = ctx.column("close") * ctx.column("volume_50d_avg")
        illiquid = turnover < 1_000_000
        return score.mask(illiquid, score.clip(upper=40.0))
