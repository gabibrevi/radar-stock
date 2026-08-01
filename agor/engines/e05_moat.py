"""Motor 5 — Ventaja Competitiva / Moat (peso 8).

Piloto con Gemini sobre un subconjunto (Top N). Sin clave LLM el motor no
puntúa y su peso se redistribuye: mejor eso que inventar un proxy de foso con
márgenes, que ya miden los motores 1 y 2.

Los componentes son escalas absolutas 0-100 porque el modelo ya emite scores
calibrados; no tiene sentido re-percentilarlos contra el Top 100 del día.
"""

from __future__ import annotations

from .base import Component, ComponentEngine


class MoatEngine(ComponentEngine):
    engine_id = "e05_moat"
    min_coverage = 0.50
    requires_llm = True

    components = (
        Component(
            "moat_strength",
            "moat_score",
            "Fortaleza del foso (LLM)",
            5.0,
            absolute=(20.0, 90.0),
        ),
        Component(
            "moat_durability",
            "moat_durability",
            "Durabilidad a 10+ años (LLM)",
            2.0,
            absolute=(20.0, 90.0),
        ),
        Component(
            "moat_confidence",
            "moat_confidence",
            "Confianza del juicio (LLM)",
            1.0,
            absolute=(30.0, 90.0),
        ),
    )
