"""Múltiplos de valoración y descuento de flujos.

El objetivo, tal como pide el enunciado, no es encontrar múltiplos bajos sino
empresas cuyo crecimiento no esté recogido en el precio. Eso obliga a tres cosas
que un simple PER no da:

1. Comparar cada múltiplo contra el de su sector y contra su propia historia.
2. Ajustar el múltiplo por el crecimiento y la rentabilidad del capital, porque un
   EV/Ventas de 8 es barato creciendo al 40% con 80% de margen bruto y carísimo
   creciendo al 5%.
3. Poner una cifra de valor intrínseco encima de la mesa, con supuestos
   explícitos, para poder hablar de recorrido al alza y a la baja.

Sobre el DCF: es deliberadamente simple y sus supuestos están todos a la vista en
`DCF_ASSUMPTIONS`. Un DCF con veinte parámetros ajustables no es más preciso, solo
es más fácil de forzar hasta que dé el resultado que uno quería.

Limitación declarada: la comparación con la propia historia solo puede abarcar el
histórico de precios disponible, que con el plan gratuito de Polygon son dos años.
Es poco para un ciclo completo y hay que interpretarlo con esa reserva.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Supuestos del descuento de flujos. Están aquí, juntos y con nombre, para que se
# puedan discutir y cambiar sin bucear en el código.
DCF_ASSUMPTIONS = {
    "years": 10,
    "discount_rate": 0.10,       # coste de capital exigido
    "terminal_growth": 0.025,    # crecimiento a perpetuidad, por debajo del PIB nominal
    "fade_to": 0.04,             # el crecimiento converge a esta tasa al final del periodo
    "max_initial_growth": 0.35,  # techo al crecimiento de partida, por prudencia
    "bear_growth_haircut": 0.5,  # el escenario malo se queda con la mitad del crecimiento
    "bull_growth_uplift": 1.3,
    "bear_discount_rate": 0.13,
    "bull_discount_rate": 0.09,
}


def compute_valuation(
    snapshot: pd.DataFrame,
    sectors: pd.Series,
) -> pd.DataFrame:
    """Añade múltiplos, comparativas sectoriales y escenarios de DCF.

    `snapshot` necesita al menos: close, share_count, revenue_ttm, ebitda_ttm,
    fcf_ttm, net_income_ttm, cash_total, debt_total.
    """
    out = pd.DataFrame(index=snapshot.index)

    close = _num(snapshot, "close")
    shares = _num(snapshot, "share_count")
    market_cap = close * shares
    cash = _num(snapshot, "cash_total")
    debt = _num(snapshot, "debt_total")

    out["market_cap"] = market_cap
    out["enterprise_value"] = market_cap + debt.fillna(0) - cash.fillna(0)

    revenue = _num(snapshot, "revenue_ttm")
    ebitda = _num(snapshot, "ebitda_ttm")
    fcf = _num(snapshot, "fcf_ttm")
    net_income = _num(snapshot, "net_income_ttm")

    # Los múltiplos solo se definen con denominador positivo. Un EV/EBITDA
    # calculado sobre EBITDA negativo produce un número pequeño y positivo que
    # parece barato y significa lo contrario.
    out["ev_sales"] = _ratio(out["enterprise_value"], revenue)
    out["ev_ebitda"] = _ratio(out["enterprise_value"], ebitda)
    out["p_fcf"] = _ratio(market_cap, fcf)
    out["p_e"] = _ratio(market_cap, net_income)
    out["p_s"] = _ratio(market_cap, revenue)

    growth = _num(snapshot, "revenue_ttm_cagr_3y").fillna(_num(snapshot, "revenue_yoy"))
    eps_growth = _num(snapshot, "eps_diluted_ttm_cagr_3y")
    out["peg"] = _ratio(out["p_e"], eps_growth * 100.0)

    # Múltiplo relativo al sector: por debajo de 1 significa más barato que sus
    # comparables. Se usa la mediana y no la media por las erratas de XBRL.
    for column in ("ev_sales", "ev_ebitda", "p_fcf", "p_e"):
        median = out.groupby(sectors)[column].transform("median")
        out[f"{column}_vs_sector"] = _ratio(out[column], median)

    # Crecimiento por unidad de múltiplo: la forma más directa de expresar
    # "crecimiento no reflejado en el precio".
    out["growth_per_ev_sales"] = _ratio(growth, out["ev_sales"])
    out["growth_per_ev_ebitda"] = _ratio(growth, out["ev_ebitda"])
    out["fcf_yield"] = _ratio(fcf, market_cap)
    out["ebitda_yield"] = _ratio(ebitda, out["enterprise_value"])

    scenarios = _dcf_scenarios(snapshot, market_cap)
    return out.join(scenarios)


def _dcf_scenarios(snapshot: pd.DataFrame, market_cap: pd.Series) -> pd.DataFrame:
    """Valor intrínseco en tres escenarios y recorrido implícito.

    Se descuentan flujos de caja libre. Cuando la caja libre actual es negativa,
    que es el caso de muchas de las candidatas que busca el radar, se parte del
    flujo normalizado que tendría con el margen de caja libre mediano de su
    sector; si tampoco se puede estimar, el DCF queda a nulo en lugar de
    inventarse una cifra.
    """
    fcf = _num(snapshot, "fcf_ttm")
    revenue = _num(snapshot, "revenue_ttm")
    growth = _num(snapshot, "revenue_ttm_cagr_3y").fillna(_num(snapshot, "revenue_yoy"))
    net_cash = _num(snapshot, "net_cash")

    assumptions = DCF_ASSUMPTIONS
    base_growth = growth.clip(-0.10, assumptions["max_initial_growth"])

    starting_fcf = fcf.where(fcf > 0)
    normalized = revenue * _num(snapshot, "sector_fcf_margin_median")
    starting_fcf = starting_fcf.fillna(normalized.where(normalized > 0))

    result = {}
    for name, growth_factor, rate in (
        ("bear", assumptions["bear_growth_haircut"], assumptions["bear_discount_rate"]),
        ("base", 1.0, assumptions["discount_rate"]),
        ("bull", assumptions["bull_growth_uplift"], assumptions["bull_discount_rate"]),
    ):
        value = _present_value(
            starting_fcf,
            base_growth * growth_factor,
            rate,
            assumptions["years"],
            assumptions["fade_to"],
            assumptions["terminal_growth"],
        )
        result[f"dcf_{name}"] = value + net_cash.fillna(0)

    frame = pd.DataFrame(result, index=snapshot.index)
    frame["upside_base"] = _ratio(frame["dcf_base"], market_cap) - 1.0
    frame["upside_bull"] = _ratio(frame["dcf_bull"], market_cap) - 1.0
    frame["downside_bear"] = _ratio(frame["dcf_bear"], market_cap) - 1.0

    # Valor esperado con pesos 25 / 50 / 25. No son probabilidades estimadas de
    # nada: son una convención declarada para no fingir una precisión que no hay.
    frame["expected_return"] = (
        0.25 * frame["downside_bear"] + 0.50 * frame["upside_base"] + 0.25 * frame["upside_bull"]
    )
    # Relación recompensa/riesgo. Solo tiene sentido si el escenario malo implica
    # pérdida; si hasta el bear da recorrido positivo, la métrica se anula para no
    # dividir por un número diminuto y obtener un ratio espectacular.
    loss = (-frame["downside_bear"]).where(lambda s: s > 0.02)
    frame["risk_reward"] = _ratio(frame["upside_base"], loss)
    frame["asymmetry"] = frame["upside_bull"] - frame["downside_bear"].abs()
    return frame


def _present_value(
    starting_fcf: pd.Series,
    initial_growth: pd.Series,
    discount_rate: float,
    years: int,
    fade_to: float,
    terminal_growth: float,
) -> pd.Series:
    """DCF en dos etapas con crecimiento que decae linealmente hasta `fade_to`."""
    flow = starting_fcf.copy()
    total = pd.Series(0.0, index=starting_fcf.index)

    for year in range(1, years + 1):
        weight = (year - 1) / max(years - 1, 1)
        growth = initial_growth * (1.0 - weight) + fade_to * weight
        flow = flow * (1.0 + growth)
        total = total + flow / (1.0 + discount_rate) ** year

    terminal = flow * (1.0 + terminal_growth) / (discount_rate - terminal_growth)
    total = total + terminal / (1.0 + discount_rate) ** years
    return total.where(starting_fcf.notna())


def add_sector_medians(snapshot: pd.DataFrame, sectors: pd.Series) -> pd.DataFrame:
    """Medianas sectoriales que necesita el DCF para normalizar."""
    snapshot = snapshot.copy()
    for column, target in (("fcf_margin", "sector_fcf_margin_median"),):
        if column in snapshot.columns:
            snapshot[target] = snapshot.groupby(sectors)[column].transform("median").clip(0.0, 0.4)
        else:
            snapshot[target] = np.nan
    return snapshot


def _num(frame: pd.DataFrame, column: str) -> pd.Series:
    if column in frame.columns:
        return pd.to_numeric(frame[column], errors="coerce")
    return pd.Series(np.nan, index=frame.index, dtype="float64")


def _ratio(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    denominator = denominator.where(denominator > 0)
    return (numerator / denominator).replace([np.inf, -np.inf], np.nan)
