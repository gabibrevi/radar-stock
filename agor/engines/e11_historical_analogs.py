"""Motor 11 — Comparación Histórica / Análogos (peso 5).

¿Se parece el perfil de esta empresa a compañías del mismo sector que el radar
ya ha visto en bandas altas? ¿Es persistente su puntuación en el tiempo?
"""

from __future__ import annotations

from .base import Component, ComponentEngine


class HistoricalAnalogsEngine(ComponentEngine):
    engine_id = "e11_historical_analogs"
    min_coverage = 0.35

    components = (
        Component(
            "similarity",
            "analog_similarity",
            "Similitud a ganadores históricos del sector",
            4.0,
            absolute=(40.0, 95.0),
        ),
        Component(
            "sector_strength",
            "analog_sector_strength",
            "Nivel medio del perfil ganador del sector",
            2.0,
            absolute=(40.0, 85.0),
        ),
        Component(
            "persistence",
            "analog_persistence",
            "Nivel medio histórico propio",
            2.0,
            absolute=(40.0, 80.0),
        ),
        Component(
            "stability",
            "analog_stability",
            "Estabilidad de la puntuación en el tiempo",
            2.0,
            absolute=(40.0, 95.0),
        ),
    )
