"""Métricas técnicas a partir de precios de cierre diarios.

Todo lo que hay aquí sale de OHLCV, sin excepción, así que es exacto y
reproducible. Lo que no se puede hacer con honestidad es afirmar que se "detecta
Wyckoff": el esquema de Wyckoff es una lectura interpretativa y no existe una
definición cerrada contra la que validar. Lo que sí se puede medir, y es lo que
hace `accumulation_score`, son las condiciones observables que la literatura de
Wyckoff asocia a una fase de acumulación: rango estrecho y sostenido, volumen que
repunta en los días de subida frente a los de bajada, y precio que deja de hacer
mínimos decrecientes. Es una aproximación declarada, no una etiqueta.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def compute_technicals(prices: pd.DataFrame, benchmark: pd.Series | None = None) -> pd.DataFrame:
    """Una fila por ticker con el estado técnico más reciente.

    `prices` debe venir ordenado por ticker y fecha, con columnas
    ticker/date/open/high/low/close/volume.
    """
    if prices.empty:
        return pd.DataFrame()

    prices = prices.sort_values(["ticker", "date"]).copy()
    grouped = prices.groupby("ticker", sort=False)

    prices["ma20"] = grouped["close"].transform(lambda s: s.rolling(20, min_periods=15).mean())
    prices["ma50"] = grouped["close"].transform(lambda s: s.rolling(50, min_periods=35).mean())
    prices["ma200"] = grouped["close"].transform(lambda s: s.rolling(200, min_periods=140).mean())
    prices["vol50"] = grouped["volume"].transform(lambda s: s.rolling(50, min_periods=30).mean())
    prices["vol200"] = grouped["volume"].transform(lambda s: s.rolling(200, min_periods=140).mean())

    prices["ret1"] = grouped["close"].pct_change()
    prices["tr"] = _true_range(prices, grouped)
    prices["atr14"] = grouped["tr"].transform(lambda s: s.rolling(14, min_periods=10).mean())

    prices["high252"] = grouped["high"].transform(lambda s: s.rolling(252, min_periods=120).max())
    prices["low252"] = grouped["low"].transform(lambda s: s.rolling(252, min_periods=120).min())
    prices["high63"] = grouped["high"].transform(lambda s: s.rolling(63, min_periods=40).max())
    prices["low63"] = grouped["low"].transform(lambda s: s.rolling(63, min_periods=40).min())

    # Volatilidad anualizada de 60 sesiones.
    prices["vol_60d"] = grouped["ret1"].transform(
        lambda s: s.rolling(60, min_periods=40).std()
    ) * np.sqrt(252)

    # Volumen relativo en días de subida frente a días de bajada, 60 sesiones.
    prices["up_volume"] = np.where(prices["ret1"] > 0, prices["volume"], 0.0)
    prices["down_volume"] = np.where(prices["ret1"] < 0, prices["volume"], 0.0)
    up = grouped["up_volume"].transform(lambda s: s.rolling(60, min_periods=40).sum())
    down = grouped["down_volume"].transform(lambda s: s.rolling(60, min_periods=40).sum())
    prices["effort_ratio"] = up / down.replace(0, np.nan)

    prices["adx14"] = _adx(prices, grouped)

    latest = prices.groupby("ticker", sort=False).tail(1).set_index("ticker")
    out = pd.DataFrame(index=latest.index)

    close = latest["close"]
    out["close"] = close
    out["price_vs_ma50"] = close / latest["ma50"] - 1.0
    out["price_vs_ma200"] = close / latest["ma200"] - 1.0
    out["ma50_vs_ma200"] = latest["ma50"] / latest["ma200"] - 1.0
    out["pct_off_52w_high"] = close / latest["high252"] - 1.0
    out["pct_above_52w_low"] = close / latest["low252"] - 1.0
    out["atr_pct"] = latest["atr14"] / close
    out["volatility_60d"] = latest["vol_60d"]
    out["volume_surge"] = latest["vol50"] / latest["vol200"]
    out["effort_ratio"] = latest["effort_ratio"]
    out["adx14"] = latest["adx14"]

    # Estrechez de la base: rango de los últimos tres meses respecto al precio.
    out["base_tightness"] = 1.0 - (latest["high63"] - latest["low63"]) / close
    out["near_breakout"] = (close / latest["high63"]).clip(upper=1.5)

    for window, label in ((21, "1m"), (63, "3m"), (126, "6m"), (252, "12m")):
        out[f"return_{label}"] = _trailing_return(prices, window)

    if benchmark is not None and not benchmark.empty:
        out = out.join(_relative_strength(prices, benchmark), how="left")

    out["accumulation_score"] = _accumulation(out)
    return out


# ---------------------------------------------------------------------------
def _true_range(prices: pd.DataFrame, grouped) -> pd.Series:
    previous_close = grouped["close"].shift(1)
    ranges = pd.concat(
        [
            prices["high"] - prices["low"],
            (prices["high"] - previous_close).abs(),
            (prices["low"] - previous_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def _adx(prices: pd.DataFrame, grouped, window: int = 14) -> pd.Series:
    """ADX de Wilder, en su versión con medias simples.

    Suficiente para ordenar empresas por fuerza de tendencia, que es todo lo que
    el radar necesita; no se usa para operar.
    """
    up_move = grouped["high"].diff()
    down_move = -grouped["low"].diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    frame = prices[["ticker"]].copy()
    frame["plus_dm"] = plus_dm
    frame["minus_dm"] = minus_dm
    frame["tr"] = prices["tr"]

    by_ticker = frame.groupby("ticker", sort=False)
    atr = by_ticker["tr"].transform(lambda s: s.rolling(window, min_periods=window).sum())
    plus_di = 100.0 * by_ticker["plus_dm"].transform(
        lambda s: s.rolling(window, min_periods=window).sum()
    ) / atr.replace(0, np.nan)
    minus_di = 100.0 * by_ticker["minus_dm"].transform(
        lambda s: s.rolling(window, min_periods=window).sum()
    ) / atr.replace(0, np.nan)

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    dx_frame = prices[["ticker"]].copy()
    dx_frame["dx"] = dx
    return dx_frame.groupby("ticker", sort=False)["dx"].transform(
        lambda s: s.rolling(window, min_periods=window).mean()
    )


def _trailing_return(prices: pd.DataFrame, window: int) -> pd.Series:
    def last_return(series: pd.Series) -> float:
        if len(series) <= window:
            return np.nan
        return float(series.iloc[-1] / series.iloc[-1 - window] - 1.0)

    return prices.groupby("ticker", sort=False)["close"].apply(last_return)


def _relative_strength(prices: pd.DataFrame, benchmark: pd.Series) -> pd.DataFrame:
    """Fuerza relativa frente al mercado en 6 y 12 meses."""
    bench = benchmark.sort_index()
    out = {}
    for window, label in ((126, "6m"), (252, "12m")):
        if len(bench) <= window:
            out[f"rs_{label}"] = pd.Series(dtype="float64")
            continue
        bench_return = float(bench.iloc[-1] / bench.iloc[-1 - window] - 1.0)
        stock_return = _trailing_return(prices, window)
        out[f"rs_{label}"] = stock_return - bench_return
    return pd.DataFrame(out)


def _accumulation(features: pd.DataFrame) -> pd.Series:
    """Condiciones observables de una fase de acumulación.

    No pretende identificar el esquema de Wyckoff, sino puntuar la coincidencia de
    cuatro hechos medibles: base estrecha, precio por encima de la media de 200
    sesiones sin estar extendido, volumen creciente y sesgo del volumen hacia los
    días de subida.
    """
    tight = features["base_tightness"].clip(0, 1)
    above_trend = ((features["price_vs_ma200"] > -0.05) & (features["price_vs_ma200"] < 0.35)).astype(float)
    volume_building = (features["volume_surge"] > 1.05).astype(float)
    demand = (features["effort_ratio"] > 1.0).astype(float)
    return (0.4 * tight + 0.2 * above_trend + 0.2 * volume_building + 0.2 * demand) * 100.0
