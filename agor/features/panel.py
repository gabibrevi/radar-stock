"""Construcción del panel trimestral de fundamentales.

Convierte los hechos XBRL en formato largo en una tabla ancha con una fila por
empresa y trimestre, y de ahí en las métricas que consumen los motores.

Tres decisiones importantes:

1. **Resolución de etiquetas por prioridad.** Cuando una empresa reporta los
   ingresos con dos etiquetas distintas en el mismo trimestre, gana la de mayor
   prioridad según `xbrl.CONCEPTS`, no la mayor ni la primera que aparezca.

2. **TTM en vez de trimestre suelto.** Casi todas las métricas se calculan sobre
   los últimos doce meses. Comparar el trimestre aislado introduce la
   estacionalidad del negocio como si fuera señal, y en retail o semiconductores
   eso basta para invertir el ranking.

3. **Exigir cuatro trimestres consecutivos reales.** Un TTM calculado con huecos
   es una cifra inventada. Cuando falta algún trimestre, la métrica queda a nulo
   y la cobertura del motor baja, que es la respuesta honesta.
"""

from __future__ import annotations

import duckdb
import numpy as np
import pandas as pd

from ..xbrl import CONCEPTS, FLOW, STOCK

# Trimestres de desplazamiento para cada horizonte de crecimiento.
HORIZONS = {"1y": 4, "3y": 12, "5y": 20, "10y": 40}


def build_panel(con: duckdb.DuckDBPyConnection, min_period: str = "2010-01-01") -> pd.DataFrame:
    """Devuelve el panel ancho con TTM, márgenes, crecimientos y ratios.

    `min_period` recorta los periodos antiguos que arrastran las comparativas de
    los informes: un 10-K de 2018 puede contener cifras etiquetadas con fechas de
    los años noventa, y dejarlas dentro llena el panel de filas huérfanas que
    rompen los cálculos de ventanas móviles.

    Por el otro extremo se descartan las fechas futuras. Son errores de tecleo en el
    formulario y en la carga de diez años había quince, con años como 2923 y 2215.
    Son poquísimas, pero el daño no es proporcional a su número: el radar toma como
    estado actual de cada empresa su periodo más reciente, así que una sola fila con
    el año mal escrito convierte a esa empresa en un trimestre casi vacío del siglo
    XXX. Afectaba a ocho empresas, que habrían quedado sin puntuación sin motivo
    aparente.
    """
    wide = _pivot(con, min_period)
    if wide.empty:
        return wide
    wide = _fill_quarterly_from_cumulative(wide)
    wide = _add_ttm(wide)
    wide = _add_derived(wide)
    return wide


# ---------------------------------------------------------------------------
# 1. Pivotado con resolución de prioridad
# ---------------------------------------------------------------------------
def _pivot(con: duckdb.DuckDBPyConnection, min_period: str) -> pd.DataFrame:
    priority_rows = [
        {"concept": tag, "metric": metric, "priority": i, "kind": kind}
        for metric, (kind, tags) in CONCEPTS.items()
        for i, tag in enumerate(tags)
    ]
    con.register("_priority", pd.DataFrame(priority_rows))

    # Los flujos se toman de qtrs=1..4 y los saldos de qtrs=0. Se resuelve la
    # prioridad de etiqueta antes de pivotar para no arrastrar duplicados.
    query = f"""
    WITH tagged AS (
        SELECT f.cik, f.period_end, f.qtrs, f.val, f.filed,
               p.metric, p.priority, p.kind
        FROM fundamentals_raw f
        JOIN _priority p ON p.concept = f.concept
        WHERE f.period_end >= DATE '{min_period}'
          -- Margen de un año sobre hoy: los cierres de ejercicio se declaran por
          -- adelantado y un trimestre futuro legítimo es normal.
          AND f.period_end <= CURRENT_DATE + INTERVAL 1 YEAR
          AND (
                (p.kind = 'stock' AND f.qtrs = 0)
             OR (p.kind = 'flow'  AND f.qtrs BETWEEN 1 AND 4)
          )
    ),
    best AS (
        SELECT *, row_number() OVER (
            PARTITION BY cik, period_end, metric, qtrs
            ORDER BY priority ASC, filed DESC
        ) AS rn
        FROM tagged
    )
    SELECT cik, period_end, metric, qtrs, val
    FROM best WHERE rn = 1
    """
    long = con.execute(query).fetchdf()
    con.unregister("_priority")
    if long.empty:
        return pd.DataFrame()

    stock_metrics = {m for m, (k, _) in CONCEPTS.items() if k == STOCK}

    stocks = long[long["metric"].isin(stock_metrics)].pivot_table(
        index=["cik", "period_end"], columns="metric", values="val", aggfunc="first"
    )
    flows = long[~long["metric"].isin(stock_metrics)]
    # Se guarda cada acumulado por separado: q1 es el trimestre puro, q4 el año.
    flow_frames = {}
    for qtrs in (1, 2, 3, 4):
        subset = flows[flows["qtrs"] == qtrs]
        if subset.empty:
            continue
        pivoted = subset.pivot_table(
            index=["cik", "period_end"], columns="metric", values="val", aggfunc="first"
        )
        flow_frames[qtrs] = pivoted.add_suffix(f"__q{qtrs}")

    wide = stocks
    for frame in flow_frames.values():
        wide = wide.join(frame, how="outer")
    return wide.reset_index().sort_values(["cik", "period_end"])


# ---------------------------------------------------------------------------
# 2. Trimestre puro a partir de acumulados
# ---------------------------------------------------------------------------
def _fill_quarterly_from_cumulative(wide: pd.DataFrame) -> pd.DataFrame:
    """Deriva el trimestre suelto cuando la empresa solo reporta acumulados.

    Un declarante semestral publica el acumulado de 2 y 4 trimestres. El segundo
    trimestre se obtiene restando: acumulado(2) - acumulado(1). Cuando no hay
    forma de aislar el trimestre, la celda queda a nulo en lugar de rellenarse
    con una estimación.
    """
    flow_metrics = [m for m, (k, _) in CONCEPTS.items() if k == FLOW]
    for metric in flow_metrics:
        pure = f"{metric}__q1"
        if pure not in wide.columns:
            wide[pure] = np.nan
        for cumulative in (2, 3, 4):
            source = f"{metric}__q{cumulative}"
            if source not in wide.columns:
                continue
            previous = f"{metric}__q{cumulative - 1}"
            if previous not in wide.columns:
                continue
            derived = wide[source] - wide[previous]
            wide[pure] = wide[pure].fillna(derived)
    return wide


# ---------------------------------------------------------------------------
# 3. TTM
# ---------------------------------------------------------------------------
def _add_ttm(wide: pd.DataFrame) -> pd.DataFrame:
    flow_metrics = [m for m, (k, _) in CONCEPTS.items() if k == FLOW]
    grouped = wide.groupby("cik", sort=False)

    for metric in flow_metrics:
        column = f"{metric}__q1"
        if column not in wide.columns:
            continue
        rolled = grouped[column].rolling(4, min_periods=4).sum().reset_index(level=0, drop=True)
        annual = f"{metric}__q4"
        if annual in wide.columns:
            # Si la empresa publica el año directamente y no tenemos los cuatro
            # trimestres, se usa el anual reportado antes que dejarlo vacío.
            rolled = rolled.fillna(wide[annual])
        wide[f"{metric}_ttm"] = rolled

    return wide


# ---------------------------------------------------------------------------
# 4. Métricas derivadas
# ---------------------------------------------------------------------------
def _add_derived(wide: pd.DataFrame) -> pd.DataFrame:
    w = wide

    def col(name: str) -> pd.Series:
        return w[name] if name in w.columns else pd.Series(np.nan, index=w.index)

    revenue = col("revenue_ttm")
    gross = col("gross_profit_ttm")
    # Cuando no se reporta margen bruto pero sí el coste de ventas, se calcula.
    gross = gross.fillna(revenue - col("cost_of_revenue_ttm"))

    operating = col("operating_income_ttm")
    net = col("net_income_ttm")
    ocf = col("operating_cash_flow_ttm")
    capex = col("capex_ttm").abs()
    da = col("depreciation_amortization_ttm")

    w["gross_profit_ttm"] = gross
    w["fcf_ttm"] = ocf - capex
    w["ebitda_ttm"] = operating + da

    # --- Márgenes -------------------------------------------------------
    w["gross_margin"] = _safe_div(gross, revenue)
    w["operating_margin"] = _safe_div(operating, revenue)
    w["net_margin"] = _safe_div(net, revenue)
    w["fcf_margin"] = _safe_div(w["fcf_ttm"], revenue)
    w["ebitda_margin"] = _safe_div(w["ebitda_ttm"], revenue)
    w["rd_intensity"] = _safe_div(col("rd_expense_ttm"), revenue)
    w["sbc_intensity"] = _safe_div(col("stock_comp_ttm"), revenue)

    # --- Retornos sobre capital ----------------------------------------
    equity = col("equity")
    assets = col("assets")

    # La caja se exige reportada: rellenar con cero convertiría "no lo sabemos"
    # en "no tiene caja", que puntuaría como una quiebra inminente. La deuda sí se
    # rellena con cero, pero solo cuando existe balance: no declarar ninguna
    # etiqueta de deuda teniendo balance significa de verdad que no hay deuda.
    has_balance = assets.notna() | equity.notna()
    cash_total = col("cash") + col("short_term_investments").fillna(0)
    debt_total = (col("long_term_debt").fillna(0) + col("short_term_debt").fillna(0)).where(
        has_balance
    )

    tax_rate = _safe_div(col("income_tax_ttm"), col("pretax_income_ttm")).clip(0.0, 0.45)
    tax_rate = tax_rate.fillna(0.21)
    nopat = operating * (1.0 - tax_rate)
    invested_capital = (equity + debt_total - cash_total).where(lambda s: s > 0)

    w["roic"] = _safe_div(nopat, invested_capital)
    w["roe"] = _safe_div(net, equity.where(equity > 0))
    w["roa"] = _safe_div(net, assets)
    w["nopat_ttm"] = nopat
    w["invested_capital"] = invested_capital

    # --- Balance --------------------------------------------------------
    w["net_cash"] = cash_total - debt_total
    w["cash_total"] = cash_total
    w["debt_total"] = debt_total
    w["net_debt_to_ebitda"] = _safe_div(-w["net_cash"], w["ebitda_ttm"].where(lambda s: s > 0))
    w["current_ratio"] = _safe_div(col("current_assets"), col("current_liabilities"))
    w["quick_ratio"] = _safe_div(
        col("current_assets") - col("inventory").fillna(0), col("current_liabilities")
    )
    w["interest_coverage"] = _safe_div(
        operating, col("interest_expense_ttm").abs().where(lambda s: s > 0)
    )
    w["equity_ratio"] = _safe_div(equity, assets)
    w["goodwill_ratio"] = _safe_div(col("goodwill"), assets)

    # Meses de caja que aguanta una empresa que quema dinero. Para las rentables
    # no aplica y queda a nulo en lugar de a infinito.
    burn = (-w["fcf_ttm"]).where(lambda s: s > 0)
    w["runway_months"] = _safe_div(cash_total, burn / 12.0)

    # --- Conversión y calidad del beneficio -----------------------------
    w["ebitda_to_fcf"] = _safe_div(w["fcf_ttm"], w["ebitda_ttm"].where(lambda s: s > 0))
    w["earnings_quality"] = _safe_div(ocf, net.where(net > 0))

    # --- Crecimiento ----------------------------------------------------
    growth_metrics = (
        "revenue_ttm",
        "ebitda_ttm",
        "net_income_ttm",
        "fcf_ttm",
        "gross_profit_ttm",
        "eps_diluted_ttm",
    )
    for label, lag in HORIZONS.items():
        years = lag / 4.0
        for metric in growth_metrics:
            if metric not in w.columns:
                continue
            past = _lag(w, metric, lag)
            w[f"{metric}_cagr_{label}"] = _cagr(w[metric], past, years)

    # Dilución: crecimiento del número de acciones. Negativo significa recompra.
    # Se prefiere la media diluida ponderada del TTM porque incluye el efecto de
    # opciones y convertibles, que es donde de verdad se diluye al accionista.
    if "shares_diluted_ttm" in w.columns:
        shares = _safe_div(col("shares_diluted_ttm"), 4.0)
        shares = shares.fillna(col("shares_outstanding"))
    else:
        shares = col("shares_outstanding")
    w["share_count"] = shares
    w["dilution_1y"] = _pct_change(shares, _lag(w, "share_count", 4))
    w["dilution_3y"] = _pct_change(shares, _lag(w, "share_count", 12))

    # Margen incremental: de cada dólar extra de ingresos, cuánto llega al
    # resultado operativo. Es la métrica que separa el crecimiento que crea valor
    # del que solo mueve volumen.
    revenue_delta = revenue - _lag(w, "revenue_ttm", 4)
    operating_delta = operating - _lag(w, "operating_income_ttm", 4)
    w["incremental_margin"] = _safe_div(
        operating_delta, revenue_delta.where(lambda s: s.abs() > 0)
    ).clip(-3, 3)

    # Estabilidad: dispersión del crecimiento interanual de ingresos en 5 años.
    w["revenue_yoy"] = _pct_change(revenue, _lag(w, "revenue_ttm", 4))
    yoy = w["revenue_yoy"]
    w["revenue_yoy_std_5y"] = _rolling(w, "revenue_yoy", 20, 8, "std")
    if "net_income__q1" in w.columns:
        w["_profitable"] = (w["net_income__q1"] > 0).astype(float)
        w["profitable_quarters_share"] = _rolling(w, "_profitable", 20, 8, "mean")
        w = w.drop(columns="_profitable")
    else:
        w["profitable_quarters_share"] = np.nan

    # Regla de 40: crecimiento más margen de caja libre. Referencia estándar para
    # negocios en expansión, donde exigir rentabilidad ya alta descartaría
    # justamente a las candidatas que busca el radar.
    w["rule_of_40"] = (yoy.fillna(0) + w["fcf_margin"].fillna(0)) * 100.0

    # --- Momentum: variación interanual de cada margen -------------------
    # Se compara contra el mismo trimestre del año anterior y no contra el
    # trimestre previo, para que la estacionalidad no se confunda con mejora.
    for margin in ("gross_margin", "operating_margin", "net_margin", "fcf_margin", "ebitda_margin"):
        w[f"{margin}_delta_4q"] = w[margin] - _lag(w, margin, 4)

    w["roic_delta_4q"] = w["roic"] - _lag(w, "roic", 4)
    w["revenue_yoy_accel"] = w["revenue_yoy"] - _lag(w, "revenue_yoy", 1)
    w["eps_yoy"] = _pct_change(col("eps_diluted_ttm"), _lag(w, "eps_diluted_ttm", 4))
    w["eps_yoy_accel"] = w["eps_yoy"] - _lag(w, "eps_yoy", 1)
    w["fcf_yoy"] = _pct_change(w["fcf_ttm"], _lag(w, "fcf_ttm", 4))

    # Trimestres consecutivos de mejora de margen operativo. El enunciado pide
    # avisar a partir de tres, así que se cuenta explícitamente en vez de
    # aproximarlo con una pendiente.
    improving = (w["operating_margin_delta_4q"] > 0).astype(float)
    w["margin_improving_streak"] = _consecutive_streak(w, improving)

    # --- Ratios que necesitan los motores y no dependen del precio -------
    w["net_cash_to_assets"] = _safe_div(w["net_cash"], assets)
    w["buyback_intensity"] = _safe_div(col("buybacks_ttm").abs(), revenue)
    w["capex_intensity"] = _safe_div(capex, revenue)

    return w


def _consecutive_streak(frame: pd.DataFrame, flags: pd.Series) -> pd.Series:
    """Longitud de la racha de valores 1 que termina en cada fila, por empresa."""
    out = pd.Series(0.0, index=frame.index)
    streak: dict[int, float] = {}
    for idx, (cik, flag) in enumerate(zip(frame["cik"].values, flags.values)):
        if np.isnan(flag):
            streak[cik] = 0.0
        elif flag > 0:
            streak[cik] = streak.get(cik, 0.0) + 1.0
        else:
            streak[cik] = 0.0
        out.iloc[idx] = streak[cik]
    return out


# ---------------------------------------------------------------------------
def _lag(frame: pd.DataFrame, column: str, periods: int) -> pd.Series:
    """Valor de `periods` trimestres antes, dentro de la misma empresa."""
    if column not in frame.columns:
        return pd.Series(np.nan, index=frame.index)
    return frame.groupby("cik", sort=False)[column].shift(periods)


def _rolling(frame: pd.DataFrame, column: str, window: int, min_periods: int, how: str) -> pd.Series:
    rolled = frame.groupby("cik", sort=False)[column].rolling(window, min_periods=min_periods)
    return getattr(rolled, how)().reset_index(level=0, drop=True)


def _safe_div(numerator: pd.Series, denominator) -> pd.Series:
    if isinstance(denominator, (int, float)):
        denominator = pd.Series(denominator, index=numerator.index)
    result = numerator / denominator.replace(0, np.nan)
    return result.replace([np.inf, -np.inf], np.nan)


def _pct_change(current: pd.Series, past: pd.Series) -> pd.Series:
    return _safe_div(current - past, past.abs())


def _cagr(current: pd.Series, past: pd.Series, years: float) -> pd.Series:
    """Tasa compuesta anual.

    Solo se define cuando ambos extremos son positivos. Una CAGR desde una base
    negativa no significa nada, y calcularla de todos modos produce números
    espectaculares que envenenarían el ranking.
    """
    valid = (past > 0) & (current > 0)
    ratio = (current / past).where(valid)
    return (ratio ** (1.0 / years) - 1.0).replace([np.inf, -np.inf], np.nan)
