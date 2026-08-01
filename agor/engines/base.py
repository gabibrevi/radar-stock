"""Contrato común de los motores.

Un motor no devuelve solo un número. Devuelve además:

- **cobertura**: qué fracción de sus componentes ha podido calcular. Con datos
  gratuitos hay huecos constantes, y un motor que puntúa 70 con el 20% de los
  datos no dice lo mismo que uno que puntúa 70 con el 95%. Confundirlos es la
  forma más rápida de que el radar recomiende empresas de las que no sabe nada.
- **desglose**: la puntuación de cada componente, para poder responder "¿por qué
  esta empresa saca 91?" sin adivinar.
- **descalificaciones**: motivos por los que una empresa queda fuera pese a
  puntuar bien, que es lo que pide el enunciado del Motor 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from ..scoring.normalize import percentile_score, winsorize


@dataclass
class ScoringContext:
    """Todo lo que un motor puede necesitar, ya alineado por CIK.

    `snapshot` tiene una fila por empresa con la última observación disponible del
    panel de fundamentales, los metadatos del universo y, si hay precios, las
    métricas técnicas y de valoración.
    """

    snapshot: pd.DataFrame
    groups: pd.Series
    has_prices: bool = False
    has_llm: bool = False

    def column(self, name: str) -> pd.Series:
        if name in self.snapshot.columns:
            return pd.to_numeric(self.snapshot[name], errors="coerce")
        return pd.Series(np.nan, index=self.snapshot.index, dtype="float64")

    @property
    def index(self) -> pd.Index:
        return self.snapshot.index


@dataclass(frozen=True)
class Component:
    """Un ingrediente de un motor."""

    name: str
    column: str
    label: str
    weight: float = 1.0
    higher_is_better: bool = True
    # Escala absoluta opcional (low, high). Si se define, no se usa percentil.
    absolute: tuple[float, float] | None = None
    # Transformación previa, por si la métrica cruda necesita ajuste.
    transform: Callable[[pd.Series], pd.Series] | None = None


@dataclass
class EngineResult:
    engine_id: str
    score: pd.Series
    coverage: pd.Series
    components: pd.DataFrame
    disqualified: pd.Series = field(default_factory=lambda: pd.Series(dtype="object"))

    def as_long(self, as_of) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "as_of": as_of,
                "cik": self.score.index,
                "engine_id": self.engine_id,
                "score": self.score.values,
                "coverage": self.coverage.reindex(self.score.index).values,
            }
        )


class ComponentEngine:
    """Motor declarativo: se define por su lista de componentes.

    La puntuación es la media ponderada de los componentes disponibles. Si falta
    un componente, su peso se redistribuye entre los presentes en lugar de contar
    como cero, porque un dato ausente no es un dato malo.
    """

    engine_id: str = ""
    components: tuple[Component, ...] = ()
    # Fracción mínima de peso disponible para emitir puntuación.
    min_coverage: float = 0.4
    # Si el motor necesita precios y no los hay, devuelve todo nulo.
    requires_prices: bool = False
    requires_llm: bool = False

    def run(self, ctx: ScoringContext) -> EngineResult:
        empty = pd.Series(np.nan, index=ctx.index, dtype="float64")
        if (self.requires_prices and not ctx.has_prices) or (
            self.requires_llm and not ctx.has_llm
        ):
            return EngineResult(
                engine_id=self.engine_id,
                score=empty,
                coverage=pd.Series(0.0, index=ctx.index),
                components=pd.DataFrame(index=ctx.index),
            )

        scored: dict[str, pd.Series] = {}
        for component in self.components:
            values = ctx.column(component.column)
            if component.transform is not None:
                values = component.transform(values)
            values = winsorize(values)

            if component.absolute is not None:
                from ..scoring.normalize import clipped_linear

                low, high = component.absolute
                scored[component.name] = clipped_linear(
                    values, low, high, component.higher_is_better
                )
            else:
                scored[component.name] = percentile_score(
                    values, ctx.groups, component.higher_is_better
                )

        components = pd.DataFrame(scored, index=ctx.index)
        weights = pd.Series(
            {c.name: c.weight for c in self.components}, dtype="float64"
        ).reindex(components.columns)

        available = components.notna()
        weight_matrix = available.mul(weights, axis=1)
        total_weight = weight_matrix.sum(axis=1)
        coverage = (total_weight / weights.sum()).fillna(0.0)

        weighted = (components.fillna(0.0) * weight_matrix).sum(axis=1)
        score = (weighted / total_weight.replace(0, np.nan)).where(coverage >= self.min_coverage)

        score = self.adjust(ctx, score, components)
        disqualified = self.disqualify(ctx)
        if not disqualified.empty:
            score = score.mask(disqualified.notna() & (disqualified != ""))

        return EngineResult(
            engine_id=self.engine_id,
            score=score.clip(0, 100),
            coverage=coverage,
            components=components,
            disqualified=disqualified,
        )

    # ------------------------------------------------------------------
    # Puntos de extensión
    # ------------------------------------------------------------------
    def adjust(
        self, ctx: ScoringContext, score: pd.Series, components: pd.DataFrame
    ) -> pd.Series:
        """Ajuste final del motor. Por defecto no hace nada."""
        return score

    def disqualify(self, ctx: ScoringContext) -> pd.Series:
        """Motivo de descalificación por empresa, o cadena vacía."""
        return pd.Series("", index=ctx.index, dtype="object")
