"""Las ocho alertas de la especificación.

Siete se pueden generar hoy con los datos disponibles y una no. La que no, se
declara explícitamente como pendiente en `PENDING_RULES` en lugar de sustituirse
por un sucedáneo: una alerta que dice "revisa al alza sus previsiones" y en realidad
mide otra cosa es peor que no tener la alerta.

Las dos de propiedad —compras de directivos y entrada de fondos— tienen un matiz
que se traslada al texto de cada aviso: se calculan sobre datasets trimestrales de
la SEC, así que describen el último trimestre publicado y no la sesión de hoy. Una
alerta que da a entender que los directivos están comprando ahora mismo, cuando el
dato es de hace tres meses, invita a actuar con una urgencia que el dato no
respalda, y por eso cada aviso lleva su fecha.

Las dos primeras reglas necesitan que exista histórico de puntuaciones, así que no
producen nada en la primera ejecución. Eso no es un fallo: es que el radar todavía
no tiene memoria. A partir del segundo día empiezan a funcionar.
"""

from __future__ import annotations

import datetime as dt

import duckdb
import pandas as pd

AVAILABLE_RULES = {
    "cruza_90": "Supera 90 puntos por primera vez",
    "salto_10_puntos": "Mejora más de 10 puntos en un mes",
    "ruptura_con_volumen": "Rompe una base técnica con volumen",
    "margenes_3t": "Mejora márgenes durante tres trimestres consecutivos",
    "descuento_historico": "Cotiza con descuento frente a su valoración histórica",
    "insiders": "Compras relevantes de directivos en mercado abierto",
    "fondos_entrando": "Entra en carteras de nuevas gestoras",
}

PENDING_RULES = {
    "revision_al_alza": (
        "Revisa al alza sus previsiones — requiere estimaciones de analistas, que "
        "no existen en ninguna fuente gratuita"
    ),
}


def detect(
    con: duckdb.DuckDBPyConnection,
    frame: pd.DataFrame,
    as_of: dt.date,
) -> pd.DataFrame:
    rows: list[dict] = []
    history = _history(con, as_of)

    rows += _rule_crosses_90(frame, history, as_of)
    rows += _rule_ten_point_jump(frame, history, as_of)
    rows += _rule_breakout(frame, as_of)
    rows += _rule_margin_streak(frame, as_of)
    rows += _rule_historic_discount(frame, as_of)
    rows += _rule_insider_buying(frame, as_of)
    rows += _rule_funds_entering(frame, as_of)

    if not rows:
        return pd.DataFrame(columns=["as_of", "cik", "ticker", "rule_id", "severity", "detail"])
    return pd.DataFrame(rows).drop_duplicates(subset=["cik", "rule_id"])


def _history(con: duckdb.DuckDBPyConnection, as_of: dt.date) -> pd.DataFrame:
    return con.execute(
        """
        SELECT cik, as_of, total, band
        FROM score_totals
        WHERE as_of < ?
        """,
        [as_of],
    ).fetchdf()


def _rule_crosses_90(frame: pd.DataFrame, history: pd.DataFrame, as_of: dt.date) -> list[dict]:
    if frame.empty:
        return []
    above = frame[frame["total"] >= 90.0]
    if above.empty:
        return []

    if history.empty:
        previously = set()
    else:
        previously = set(history[history["total"] >= 90.0]["cik"].tolist())

    out = []
    for cik, row in above.iterrows():
        if cik in previously:
            continue
        out.append(
            {
                "as_of": as_of,
                "cik": cik,
                "ticker": row.get("ticker"),
                "rule_id": "cruza_90",
                "severity": "alta",
                "detail": f"Primera vez que supera 90 puntos (score {row['total']:.1f})",
            }
        )
    return out


def _rule_ten_point_jump(frame: pd.DataFrame, history: pd.DataFrame, as_of: dt.date) -> list[dict]:
    if history.empty or frame.empty:
        return []
    window_start = as_of - dt.timedelta(days=31)
    recent = history[pd.to_datetime(history["as_of"]).dt.date >= window_start]
    if recent.empty:
        return []

    baseline = recent.sort_values("as_of").groupby("cik")["total"].first()
    current = frame["total"]
    delta = (current - baseline.reindex(current.index)).dropna()
    jumped = delta[delta > 10.0]

    return [
        {
            "as_of": as_of,
            "cik": cik,
            "ticker": frame.loc[cik].get("ticker"),
            "rule_id": "salto_10_puntos",
            "severity": "alta",
            "detail": f"Mejora de {value:.1f} puntos en el último mes",
        }
        for cik, value in jumped.items()
    ]


def _rule_breakout(frame: pd.DataFrame, as_of: dt.date) -> list[dict]:
    needed = ("near_breakout", "volume_surge", "base_tightness")
    if not all(c in frame.columns for c in needed):
        return []
    mask = (
        (frame["near_breakout"] >= 0.99)
        & (frame["volume_surge"] > 1.25)
        & (frame["base_tightness"] > 0.75)
    )
    subset = frame[mask.fillna(False)]
    return [
        {
            "as_of": as_of,
            "cik": cik,
            "ticker": row.get("ticker"),
            "rule_id": "ruptura_con_volumen",
            "severity": "media",
            "detail": (
                f"Rompe el máximo de 3 meses con volumen {row['volume_surge']:.2f}x "
                "sobre su media"
            ),
        }
        for cik, row in subset.iterrows()
    ]


def _rule_margin_streak(frame: pd.DataFrame, as_of: dt.date) -> list[dict]:
    if "margin_improving_streak" not in frame.columns:
        return []
    subset = frame[frame["margin_improving_streak"].fillna(0) >= 3]
    return [
        {
            "as_of": as_of,
            "cik": cik,
            "ticker": row.get("ticker"),
            "rule_id": "margenes_3t",
            "severity": "media",
            "detail": (
                f"{int(row['margin_improving_streak'])} trimestres consecutivos "
                "mejorando margen operativo"
            ),
        }
        for cik, row in subset.iterrows()
    ]


def _rule_insider_buying(frame: pd.DataFrame, as_of: dt.date) -> list[dict]:
    """Compra en grupo de directivos, no una compra suelta.

    Se exigen tres condiciones a la vez y ninguna sobra. Que compren al menos dos
    personas distintas, porque una compra aislada puede responder a una circunstancia
    personal y no a una lectura del negocio. Que el importe llegue al 0,2% del valor
    de la empresa, porque una compra simbólica de un consejero es a veces un gesto de
    imagen. Y que nadie esté vendiendo por decisión propia al mismo tiempo, porque un
    equipo dividido no es una señal.
    """
    needed = ("ins_buyers_90d", "ins_net_to_mcap")
    if not all(c in frame.columns for c in needed):
        return []
    mask = (
        (frame["ins_buyers_90d"].fillna(0) >= 2)
        & (frame["ins_net_to_mcap"].fillna(0) >= 0.002)
        & (frame.get("ins_sellers_90d", pd.Series(0, index=frame.index)).fillna(0) == 0)
    )
    subset = frame[mask.fillna(False)]
    return [
        {
            "as_of": as_of,
            "cik": cik,
            "ticker": row.get("ticker"),
            "rule_id": "insiders",
            "severity": "alta",
            "detail": (
                f"{int(row['ins_buyers_90d'])} directivos compraron en mercado abierto "
                f"el {row['ins_net_to_mcap'] * 100:.2f}% de la empresa, sin ventas "
                "discrecionales (dato del último trimestre publicado por la SEC)"
            ),
        }
        for cik, row in subset.iterrows()
    ]


def _rule_funds_entering(frame: pd.DataFrame, as_of: dt.date) -> list[dict]:
    """Entrada amplia de gestoras que antes no tenían el valor.

    Los umbrales se fijaron midiendo la distribución real, no a ojo. Con un 15% de
    crecimiento saltaban 201 avisos sobre 1.386 empresas, porque ese valor está en el
    percentil 88 y describe un trimestre corriente; entre ellos, Corning y
    Halliburton, que difícilmente son un descubrimiento. Al 25% quedan unas decenas.

    El filtro de saturación es igual de necesario: en un valor con el 95% del capital
    ya en manos institucionales, la entrada de gestoras nuevas no anticipa el
    recorrido que busca el radar, porque el descubrimiento ya ocurrió.

    El techo lo impone `features/ownership.py`, que declara desconocida cualquier
    variación que duplique la base. Sin él esta alerta avisaría sobre todo de
    escisiones y cambios de CUSIP.
    """
    needed = ("inst_holders_change_pct", "inst_new_positions", "inst_holders")
    if not all(c in frame.columns for c in needed):
        return []
    saturation = frame.get("inst_ownership_pct")
    not_crowded = (
        saturation.fillna(0.0) < 0.85 if saturation is not None else pd.Series(True, index=frame.index)
    )
    mask = (
        (frame["inst_holders_change_pct"] >= 0.25)
        & (frame["inst_holders"] >= 25)
        & (frame["inst_new_positions"] >= 10)
        & not_crowded
    )
    subset = frame[mask.fillna(False)]
    stamp = ""
    if "inst_quarter" in frame.columns:
        quarters = pd.to_datetime(frame["inst_quarter"], errors="coerce").dropna()
        if not quarters.empty:
            stamp = f" (13F del trimestre cerrado el {quarters.iloc[0].date()})"
    return [
        {
            "as_of": as_of,
            "cik": cik,
            "ticker": row.get("ticker"),
            "rule_id": "fondos_entrando",
            "severity": "media",
            "detail": (
                f"{int(row['inst_new_positions'])} gestoras abren posición y el total "
                f"crece un {row['inst_holders_change_pct'] * 100:.0f}%, hasta "
                f"{int(row['inst_holders'])}{stamp}"
            ),
        }
        for cik, row in subset.iterrows()
    ]


def _rule_historic_discount(frame: pd.DataFrame, as_of: dt.date) -> list[dict]:
    """Descuento frente al sector como aproximación declarada.

    La especificación pide descuento frente a la valoración *histórica* de la
    propia empresa. Con dos años de precios no hay historia suficiente para que esa
    comparación signifique algo, así que aquí se usa el descuento frente a los
    comparables sectoriales y se dice cuál es la diferencia.
    """
    if "ev_sales_vs_sector" not in frame.columns:
        return []
    mask = (frame["ev_sales_vs_sector"] < 0.7) & (frame["total"] >= 80.0)
    subset = frame[mask.fillna(False)]
    return [
        {
            "as_of": as_of,
            "cik": cik,
            "ticker": row.get("ticker"),
            "rule_id": "descuento_historico",
            "severity": "media",
            "detail": (
                f"EV/Ventas un {(1 - row['ev_sales_vs_sector']) * 100:.0f}% por debajo "
                "de la mediana de su sector (aproximación: falta histórico propio)"
            ),
        }
        for cik, row in subset.iterrows()
    ]
