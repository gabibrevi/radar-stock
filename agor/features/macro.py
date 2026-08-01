"""Indicadores macro desde FRED y sensibilidad sectorial.

El motor 13 no puntúa "cómo de buena es la economía" en abstracto: puntúa cómo de
favorable es el régimen actual **para esta empresa**. Un entorno de curva
invertida y spreads de crédito altos castiga más a un semi cíclico apalancado
que a un software con caja neta.

Series elegidas (todas públicas y estables en FRED):

- T10Y2Y — pendiente de la curva 10y-2y. Invertida anticipa restricción de crédito.
- BAMLH0A0HYM2 — OAS high yield. Estrés de crédito corporativo.
- VIXCLS — miedo de mercado.
- DTWEXBGS — dólar amplio. Fuerte = viento en contra para exportadores/emergentes.
- T5YIE — inflación esperada a 5 años.
- DFF — fed funds efectivo (nivel de tipos).
"""

from __future__ import annotations

import datetime as dt

import numpy as np
import pandas as pd

from ..providers.fred import FredClient

# Sensibilidad cíclica por sector AGOR. 1.0 = muy expuesto al ciclo; 0.0 = defensivo.
SECTOR_CYCLICALITY: dict[str, float] = {
    "Semiconductores": 1.0,
    "Equipamiento semiconductores": 1.0,
    "Construcción": 1.0,
    "Automoción": 1.0,
    "Componentes automoción": 1.0,
    "Transporte y Automoción": 0.9,
    "Industrial": 0.85,
    "Metalurgia": 0.85,
    "Petróleo y Gas": 0.8,
    "Minería": 0.8,
    "Refino": 0.75,
    "Química": 0.7,
    "Hardware": 0.7,
    "Electrónica": 0.7,
    "Comercio electrónico": 0.65,
    "Retail": 0.6,
    "Software": 0.35,
    "Internet / Software": 0.4,
    "Servicios IT": 0.45,
    "Farmacéutico": 0.25,
    "Biotecnología": 0.35,
    "Dispositivos médicos": 0.3,
    "Alimentación y Bebidas": 0.2,
    "Utilities": 0.15,
    "Aeroespacial y Defensa": 0.4,
}

SERIES = {
    "curve": "T10Y2Y",
    "hy_oas": "BAMLH0A0HYM2",
    "vix": "VIXCLS",
    "dollar": "DTWEXBGS",
    "breakeven_5y": "T5YIE",
    "fed_funds": "DFF",
}


def fetch_macro_snapshot(client: FredClient, as_of: dt.date | None = None) -> dict[str, float]:
    """Último valor útil de cada serie y percentiles de régimen a 5 años."""
    as_of = as_of or dt.date.today()
    out: dict[str, float] = {}
    raw: dict[str, pd.Series] = {}
    for key, series_id in SERIES.items():
        series = client.series(series_id, end=as_of)
        raw[key] = series
        if series.empty:
            out[key] = float("nan")
            continue
        usable = series[series.index <= as_of]
        out[key] = float(usable.iloc[-1]) if len(usable) else float("nan")

    # Percentiles de "estrés": valor alto = peor para el régimen (salvo la curva,
    # donde lo malo es lo negativo).
    out["curve_stress"] = _low_is_stress(raw.get("curve"), out.get("curve"))
    out["credit_stress"] = _high_is_stress(raw.get("hy_oas"), out.get("hy_oas"))
    out["vol_stress"] = _high_is_stress(raw.get("vix"), out.get("vix"))
    out["dollar_stress"] = _high_is_stress(raw.get("dollar"), out.get("dollar"))
    # Tipos reales aproximados: fed funds - breakeven. Alto = restrictivo.
    if raw.get("fed_funds") is not None and raw.get("breakeven_5y") is not None:
        aligned = pd.concat(
            [raw["fed_funds"].rename("ff"), raw["breakeven_5y"].rename("be")],
            axis=1,
            join="inner",
        ).dropna()
        if len(aligned):
            real = aligned["ff"] - aligned["be"]
            current_real = float(out["fed_funds"] - out["breakeven_5y"]) if (
                out.get("fed_funds") == out.get("fed_funds")
                and out.get("breakeven_5y") == out.get("breakeven_5y")
            ) else float("nan")
            out["real_rate"] = current_real
            out["rate_stress"] = _high_is_stress(real, current_real)
        else:
            out["real_rate"] = float("nan")
            out["rate_stress"] = float("nan")
    else:
        out["real_rate"] = float("nan")
        out["rate_stress"] = float("nan")

    stresses = [
        out.get("curve_stress"),
        out.get("credit_stress"),
        out.get("vol_stress"),
        out.get("rate_stress"),
    ]
    valid = [s for s in stresses if s == s]
    # Régimen 0-100: poco estrés → 100.
    out["macro_regime"] = float(100.0 * (1.0 - float(np.mean(valid)))) if valid else float("nan")
    return out


def enrich_with_macro(snapshot: pd.DataFrame, macro: dict[str, float]) -> pd.DataFrame:
    """Añade columnas macro y la exposición cíclica de cada empresa."""
    if not macro or macro.get("macro_regime") != macro.get("macro_regime"):
        return snapshot

    out = snapshot.copy()
    for key, value in macro.items():
        if key == "as_of":
            continue
        # Las claves del snapshot ya vienen con sentido propio (`macro_regime`,
        # `curve_stress`…). Solo se prefija lo que no lo trae.
        col = key if key.startswith("macro_") else f"macro_{key}"
        out[col] = value

    sector = out["sector"] if "sector" in out.columns else pd.Series("", index=out.index)
    cyclicality = sector.map(SECTOR_CYCLICALITY).fillna(0.5)
    out["macro_cyclicality"] = cyclicality

    regime = float(macro["macro_regime"])
    # Viento a favor/en contra: los cíclicos se mueven con el régimen; los
    # defensivos se quedan cerca de neutro.
    out["macro_tailwind"] = 50.0 + cyclicality * (regime - 50.0)

    # Vulnerabilidad a tipos: deuda y cobertura. Se combina con el estrés de tipos
    # del régimen para no castigar a todo el mundo cuando los tipos están bajos.
    rate_stress = float(macro.get("rate_stress") or 0.0)
    net_debt = pd.to_numeric(out.get("net_debt_to_ebitda"), errors="coerce")
    coverage = pd.to_numeric(out.get("interest_coverage"), errors="coerce")
    debt_score = (1.0 - ((net_debt.clip(0, 4) / 4.0).fillna(0.5))).clip(0, 1)
    cov_score = ((coverage.clip(0, 12) / 12.0).fillna(0.5)).clip(0, 1)
    solidity = 0.6 * debt_score + 0.4 * cov_score
    # Si no hay estrés de tipos, la vulnerabilidad apenas mueve la nota.
    out["macro_rate_resilience"] = 50.0 + rate_stress * (solidity - 0.5) * 100.0
    out["macro_rate_resilience"] = out["macro_rate_resilience"].clip(0, 100)

    credit_stress = float(macro.get("credit_stress") or 0.0)
    out["macro_credit_resilience"] = 50.0 + credit_stress * (solidity - 0.5) * 100.0
    out["macro_credit_resilience"] = out["macro_credit_resilience"].clip(0, 100)

    return out


def _high_is_stress(history: pd.Series | None, current: float | None) -> float:
    if history is None or history.empty or current is None or current != current:
        return float("nan")
    # Percentil empírico del nivel actual: 1.0 = en el máximo histórico de la ventana.
    return float((history <= current).mean())


def _low_is_stress(history: pd.Series | None, current: float | None) -> float:
    if history is None or history.empty or current is None or current != current:
        return float("nan")
    # Para la curva: valores bajos (inversión) son estrés.
    return float((history >= current).mean())
