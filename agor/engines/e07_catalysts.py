"""Motor 7 — Catalizadores (peso 8).

Piloto Gemini sobre Top N. Busca visibilidad de creación de valor a 1-5 años
inferible del perfil (no eventos inventados). Sin LLM no puntúa.
"""

from __future__ import annotations

from .base import Component, ComponentEngine


class CatalystsEngine(ComponentEngine):
    engine_id = "e07_catalysts"
    min_coverage = 0.50
    requires_llm = True

    components = (
        Component(
            "catalyst_strength",
            "catalyst_score",
            "Fuerza del catalizador (LLM)",
            5.0,
            absolute=(20.0, 90.0),
        ),
        Component(
            "catalyst_clarity",
            "catalyst_clarity",
            "Claridad del camino (LLM)",
            2.5,
            absolute=(20.0, 90.0),
        ),
        # Horizonte más corto (1-3 años) suele ser más accionable para el radar.
        Component(
            "catalyst_horizon",
            "catalyst_horizon_years",
            "Horizonte del catalizador (años)",
            0.5,
            higher_is_better=False,
            absolute=(1.0, 8.0),
        ),
    )
