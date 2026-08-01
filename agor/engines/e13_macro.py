"""Motor 13 — Macroeconomía (peso 3).

Mide si el régimen macro actual ayuda o perjudica a esta empresa. El peso es el
más bajo del radar a propósito: la macro no elige ganadores a 5-10 años, pero sí
explica por qué un sector entero se atasca aunque las cuentas individuales
lucieran.

Datos: FRED (gratis con API key). Sin clave el motor se desactiva sin romper nada.
"""

from __future__ import annotations

from .base import Component, ComponentEngine


class MacroEngine(ComponentEngine):
    engine_id = "e13_macro"
    min_coverage = 0.4
    # Se activa solo cuando el pipeline ha podido enriquecer el snapshot con FRED.
    # No usamos requires_prices: la macro no depende de Polygon.

    components = (
        Component(
            "tailwind",
            "macro_tailwind",
            "Viento macro según ciclicidad del sector",
            3.0,
            absolute=(20.0, 80.0),
        ),
        Component(
            "rate_resilience",
            "macro_rate_resilience",
            "Resistencia a un régimen de tipos restrictivo",
            2.0,
            absolute=(20.0, 80.0),
        ),
        Component(
            "credit_resilience",
            "macro_credit_resilience",
            "Resistencia a estrés de crédito",
            2.0,
            absolute=(20.0, 80.0),
        ),
        Component(
            "regime",
            "macro_regime",
            "Régimen macro agregado",
            1.0,
            absolute=(20.0, 80.0),
        ),
    )
