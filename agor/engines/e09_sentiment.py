"""Motor 9 — Sentimiento (peso 5), proxies free.

Sin API de noticias. Usa señales de mercado y de flujos que ya tenemos:
fuerza relativa, volumen, posición vs máximos, compras de directivos y cambio
institucional. Es un proxy burdo de "apetito" hacia el valor, no sentimiento
mediático.
"""

from __future__ import annotations

from .base import Component, ComponentEngine


class SentimentEngine(ComponentEngine):
    engine_id = "e09_sentiment"
    min_coverage = 0.30

    components = (
        Component("rs_6m", "rs_6m", "Fuerza relativa 6 meses", 2.0),
        Component("rs_12m", "rs_12m", "Fuerza relativa 12 meses", 1.5),
        Component(
            "off_high",
            "pct_off_52w_high",
            "No estar destrozado vs máximo 52s",
            1.5,
            absolute=(-0.55, -0.02),
        ),
        Component(
            "volume_surge",
            "volume_surge",
            "Interés (volumen relativo)",
            1.0,
            absolute=(0.8, 1.8),
        ),
        Component(
            "insider_buyers",
            "ins_net_buyers_90d",
            "Directivos compradores netos (90d)",
            1.5,
            absolute=(-2.0, 3.0),
        ),
        Component(
            "ins_buy_share",
            "ins_buy_share",
            "Proporción de compras vs ventas insider",
            1.0,
            absolute=(0.2, 0.9),
        ),
        Component(
            "inst_flow",
            "inst_holders_change_pct",
            "Cambio en nº de gestoras 13F",
            1.0,
            absolute=(-0.15, 0.25),
        ),
        Component(
            "volatility_calm",
            "volatility_60d",
            "Ausencia de pánico (vol. baja)",
            0.5,
            higher_is_better=False,
        ),
    )
