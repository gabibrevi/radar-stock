"""Conversión de métricas crudas a puntuaciones comparables de 0 a 100.

Por qué percentiles y no umbrales absolutos
-------------------------------------------
Un margen bruto del 45% es excelente en distribución alimentaria y mediocre en
software. Un ROIC del 10% es notable en semiconductores en año de inversión y
pobre en servicios profesionales. Si el radar puntuase con umbrales fijos, el
ranking no mediría calidad: mediría a qué sector pertenece cada empresa, y los
Top 20 se llenarían del mismo sector una y otra vez.

Por eso cada métrica se convierte en su percentil **dentro de su grupo de
comparación** (sector, y opcionalmente tramo de capitalización). La puntuación
responde a "¿qué tal lo hace esta empresa frente a sus verdaderos comparables?",
que es la única pregunta que tiene respuesta útil.

El coste de esta decisión, que conviene tener presente: en un sector donde todas
las empresas son malas, la mejor de todas seguirá sacando percentil alto. Eso lo
corrige la capa de agregación con los suelos absolutos, no esta.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Un percentil calculado sobre cuatro empresas no es un percentil. Por debajo de
# este tamaño el grupo se ignora y se compara contra el universo completo.
MIN_GROUP_SIZE = 25


def percentile_score(
    values: pd.Series,
    groups: pd.Series | None = None,
    higher_is_better: bool = True,
    min_group_size: int = MIN_GROUP_SIZE,
) -> pd.Series:
    """Percentil de 0 a 100 dentro del grupo, con caída a global si es pequeño.

    Los valores ausentes se propagan como ausentes: nunca se imputan. Imputar la
    mediana a una empresa sin datos la haría parecer del montón en lugar de
    desconocida, y esa diferencia es justo la que el radar necesita registrar.
    """
    values = pd.to_numeric(values, errors="coerce")
    if not higher_is_better:
        values = -values

    result = pd.Series(np.nan, index=values.index, dtype="float64")

    if groups is None:
        return _rank_pct(values)

    sizes = groups.map(groups.value_counts())
    big = sizes >= min_group_size

    if big.any():
        result.loc[big] = (
            values.loc[big].groupby(groups.loc[big], sort=False).transform(_rank_pct)
        )
    if (~big).any():
        # Los grupos pequeños se comparan contra el universo entero, no entre sí.
        global_ranks = _rank_pct(values)
        result.loc[~big] = global_ranks.loc[~big]

    return result


def _rank_pct(series: pd.Series) -> pd.Series:
    valid = series.notna()
    if valid.sum() == 0:
        return pd.Series(np.nan, index=series.index, dtype="float64")
    if valid.sum() == 1:
        return pd.Series(np.where(valid, 50.0, np.nan), index=series.index)
    ranked = series.rank(pct=True, na_option="keep") * 100.0
    return ranked


def clipped_linear(
    values: pd.Series,
    low: float,
    high: float,
    higher_is_better: bool = True,
) -> pd.Series:
    """Escala absoluta de 0 a 100 entre dos referencias.

    Se usa donde el percentil no vale porque existe un criterio objetivo: un
    ratio corriente de 0,4 es peligroso con independencia de lo que hagan los
    competidores, y un interest coverage de 30 no es mejor que uno de 20.
    """
    values = pd.to_numeric(values, errors="coerce")
    scaled = (values - low) / (high - low)
    if not higher_is_better:
        scaled = 1.0 - scaled
    return (scaled.clip(0.0, 1.0) * 100.0).where(values.notna())


def winsorize(values: pd.Series, lower: float = 0.01, upper: float = 0.99) -> pd.Series:
    """Recorta la cola por cuantiles.

    Los datos XBRL contienen erratas reales (un decimal mal puesto en un 10-Q
    convierte un margen en 4.000%). Sin recorte, una sola errata desplaza toda la
    distribución del sector.
    """
    values = pd.to_numeric(values, errors="coerce")
    if values.notna().sum() < 10:
        return values
    low, high = values.quantile([lower, upper])
    return values.clip(low, high)


def size_bucket(market_cap: pd.Series) -> pd.Series:
    """Tramos de capitalización, en dólares."""
    edges = [0, 300e6, 2e9, 10e9, 50e9, np.inf]
    labels = ["Nano", "Small", "Mid", "Large", "Mega"]
    return pd.cut(market_cap, bins=edges, labels=labels, right=False).astype("object")
