"""Motor 6 — Tendencias Globales / Megatrends (peso 8).

Piloto Gemini sobre Top N. Pregunta: ¿está el negocio expuesto a vientos de cola
estructurales a 5-10 años? Sin LLM el motor no puntúa.
"""

from __future__ import annotations

from .base import Component, ComponentEngine


class MegatrendsEngine(ComponentEngine):
    engine_id = "e06_megatrends"
    min_coverage = 0.50
    requires_llm = True

    components = (
        Component(
            "mega_exposure",
            "mega_score",
            "Exposición a megatendencias (LLM)",
            5.0,
            absolute=(20.0, 90.0),
        ),
        Component(
            "mega_alignment",
            "mega_alignment",
            "Alineación sector/perfil (LLM)",
            3.0,
            absolute=(20.0, 90.0),
        ),
    )
