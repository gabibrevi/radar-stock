"""Los 16 motores de puntuación.

Cada motor es independiente, recibe el mismo contexto y devuelve una puntuación
de 0 a 100 acompañada de su cobertura y del desglose por componente. Esa
independencia es lo que permite añadir, quitar o reponderar motores sin tocar el
resto, y lo que hará posible que el módulo de aprendizaje mida la aportación real
de cada uno.
"""

from .base import Component, ComponentEngine, EngineResult, ScoringContext

__all__ = ["Component", "ComponentEngine", "EngineResult", "ScoringContext"]
