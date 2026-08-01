"""Métricas de propiedad: qué hacen los directivos y qué hacen las instituciones.

Tres decisiones de este módulo son las que hacen que la señal valga algo. Ninguna
es evidente y las tres se tomaron después de ver los datos reales:

1. **Directivos y accionistas del 10% no se mezclan.** Al inspeccionar un trimestre,
   las mayores "compras de insiders" no eran directivos convencidos: eran Genmab
   comprando 7.400 millones de su socia Merus, el fondo soberano de Singapur, o una
   matriz ampliando en su participada. Eso es una operación estratégica o
   institucional, no confianza de la dirección en su propio negocio. Los directivos
   y consejeros alimentan el motor 4; los accionistas del 10% que no son directivos,
   el motor 8.

2. **Solo los trimestres completos de 13F entran en la comparación.** Los datasets
   se organizan por fecha de presentación, así que un fichero reciente contiene
   también declaraciones tardías y enmiendas de trimestres antiguos: en la prueba,
   el trimestre de junio de 2025 aparecía con 240 gestoras frente a las 8.700 de uno
   completo. Compararlos daría una fuga institucional inventada.

3. **Ausencia de datos y ausencia de actividad son cosas distintas.** Que una
   empresa no tenga compras de directivos en seis meses es información real y vale
   como neutro. Que no aparezca en el dataset es desconocimiento y debe quedar nulo,
   para que el motor no la puntúe. Un 14,6% del universo no tiene puente CUSIP y
   caería en el segundo caso.
"""

from __future__ import annotations

import datetime as dt

import duckdb
import pandas as pd

# Ventanas de observación. Seis meses para el saldo de compraventa, porque es el
# horizonte en el que un directivo actúa sobre información sobre su negocio sin que
# se diluya en ruido; tres meses para la amplitud, porque varios directivos
# comprando en el mismo trimestre es lo que constituye una compra en grupo.
WINDOW_LONG = 180
WINDOW_SHORT = 90

# Un precio declarado que se desvía más de este factor de la mediana de la propia
# empresa es un error de relleno del formulario, no una operación. El caso real que
# motivó el filtro: un declarante puso el importe total de la compra (15.000.000)
# en la casilla del precio por acción, generando una operación de 225 billones de
# dólares que habría encabezado cualquier ranking.
PRICE_SANITY_FACTOR = 10.0

# Gestoras que debía tener el valor el trimestre anterior para que su variación
# signifique algo. Por debajo de esto, un puñado de altas convierte cualquier
# porcentaje en un número enorme que no describe ningún flujo real.
MIN_PRIOR_HOLDERS = 15

# Banda dentro de la cual una variación del número de gestoras se puede interpretar
# como flujo real. Fuera de ella la variación se declara desconocida.
#
# Que una empresa ya seguida por gestoras duplique su base en un solo trimestre no
# ocurre por descubrimiento: ocurre porque cambió de CUSIP, se escindió, se fusionó
# o volvió a cotizar, y los mismos fondos de siempre reaparecen bajo otra
# identidad. En la prueba real, AstraZeneca pasaba de 52 a 1.313 gestoras por el
# cambio de CUSIP de su ADR y Pinnacle Financial de 48 a 631 por una fusión. Ambas
# encabezaban el ranking de entrada institucional.
#
# No se puede distinguir con este dato una reidentificación de una acumulación
# genuina, así que no se puntúa ninguna de las dos. Se pierde algún caso real a
# cambio de no promocionar decenas de operaciones corporativas.
MAX_HOLDER_GROWTH = 1.0
MIN_HOLDER_DECLINE = -0.5


def insider_metrics(con: duckdb.DuckDBPyConnection, as_of: dt.date) -> pd.DataFrame:
    """Comportamiento de directivos y consejeros, indexado por CIK del emisor."""
    long_start = as_of - dt.timedelta(days=WINDOW_LONG)
    short_start = as_of - dt.timedelta(days=WINDOW_SHORT)

    return con.execute(
        """
        WITH price_ref AS (
            -- Referencia robusta del precio de cada empresa: la mediana de todo lo
            -- que han declarado sus insiders. No hace falta ningún dato de mercado.
            SELECT issuer_cik, median(price) AS ref_price
            FROM insider_transactions
            WHERE price > 0
            GROUP BY issuer_cik
        ),
        clean AS (
            SELECT t.*
            FROM insider_transactions t
            LEFT JOIN price_ref r USING (issuer_cik)
            WHERE t.filing_date <= $as_of          -- no se sabe antes de presentarse
              AND (
                    t.price IS NULL OR t.price = 0 OR r.ref_price IS NULL
                 OR (t.price BETWEEN r.ref_price / $factor AND r.ref_price * $factor)
              )
        ),
        -- Quién es quién: se excluye al accionista del 10% que no ocupa cargo.
        mgmt AS (SELECT * FROM clean WHERE is_officer OR is_director),
        strategic AS (
            SELECT * FROM clean
            WHERE is_ten_percent AND NOT is_officer AND NOT is_director
        ),
        long_win AS (
            SELECT
                issuer_cik AS cik,
                sum(value_usd) FILTER (WHERE trans_code = 'P')                     AS ins_buy_usd,
                sum(value_usd) FILTER (WHERE trans_code = 'S'
                                         AND NOT planned_10b5_1)                   AS ins_sell_usd,
                sum(value_usd) FILTER (WHERE trans_code = 'S'
                                         AND planned_10b5_1)                       AS ins_sell_planned_usd,
                count(*)       FILTER (WHERE trans_code = 'P')                     AS ins_buy_ops,
                max(CASE WHEN trans_code = 'P' AND is_officer THEN 1 ELSE 0 END)    AS ins_officer_bought,
                sum(shares)    FILTER (WHERE trans_code = 'A')                      AS ins_granted_shares
            FROM mgmt
            WHERE trans_date >= $long_start AND trans_date <= $as_of
            GROUP BY 1
        ),
        short_win AS (
            SELECT
                issuer_cik AS cik,
                count(DISTINCT owner_cik) FILTER (WHERE trans_code = 'P') AS ins_buyers_90d,
                count(DISTINCT owner_cik) FILTER (WHERE trans_code = 'S'
                                                    AND NOT planned_10b5_1) AS ins_sellers_90d
            FROM mgmt
            WHERE trans_date >= $short_start AND trans_date <= $as_of
            GROUP BY 1
        ),
        strat AS (
            SELECT issuer_cik AS cik,
                   sum(value_usd) FILTER (WHERE trans_code = 'P') AS strat_buy_usd,
                   sum(value_usd) FILTER (WHERE trans_code = 'S') AS strat_sell_usd
            FROM strategic
            WHERE trans_date >= $long_start AND trans_date <= $as_of
            GROUP BY 1
        ),
        -- Capital comprometido: se toma la última posición declarada por cada
        -- titular y solo la titularidad directa, porque sumar la indirecta contaría
        -- dos veces las mismas acciones a través de fideicomisos y sociedades.
        stake AS (
            SELECT cik, sum(shares_after) AS ins_shares_held FROM (
                SELECT issuer_cik AS cik, owner_cik, shares_after,
                       row_number() OVER (PARTITION BY issuer_cik, owner_cik
                                          ORDER BY trans_date DESC, filing_date DESC) AS rn
                FROM mgmt
                WHERE direct AND shares_after IS NOT NULL
                  AND trans_date >= $as_of - INTERVAL 400 DAY
            ) WHERE rn = 1
            GROUP BY cik
        ),
        -- Universo cubierto por el dataset: presentar algún formulario alguna vez es
        -- lo que distingue "no ha habido movimientos" de "no tenemos ni idea".
        covered AS (SELECT DISTINCT issuer_cik AS cik FROM clean)
        SELECT
            c.cik,
            TRUE                                        AS ins_covered,
            coalesce(l.ins_buy_usd, 0)                  AS ins_buy_usd,
            coalesce(l.ins_sell_usd, 0)                 AS ins_sell_usd,
            coalesce(l.ins_buy_usd, 0)
                - coalesce(l.ins_sell_usd, 0)           AS ins_net_usd,
            coalesce(l.ins_buy_ops, 0)                  AS ins_buy_ops,
            coalesce(s.ins_buyers_90d, 0)               AS ins_buyers_90d,
            coalesce(s.ins_sellers_90d, 0)              AS ins_sellers_90d,
            coalesce(l.ins_officer_bought, 0)           AS ins_officer_bought,
            coalesce(l.ins_granted_shares, 0)           AS ins_granted_shares,
            k.ins_shares_held,
            coalesce(st.strat_buy_usd, 0)
                - coalesce(st.strat_sell_usd, 0)        AS strat_net_usd,
            -- Proporción de la venta que estaba programada. Un 100% significa que
            -- nadie ha vendido por decisión propia, lo que es buena señal aunque el
            -- volumen vendido sea alto.
            CASE WHEN coalesce(l.ins_sell_usd, 0) + coalesce(l.ins_sell_planned_usd, 0) > 0
                 THEN coalesce(l.ins_sell_planned_usd, 0)
                      / (coalesce(l.ins_sell_usd, 0) + coalesce(l.ins_sell_planned_usd, 0))
            END                                         AS ins_planned_sell_share
        FROM covered c
        LEFT JOIN long_win  l USING (cik)
        LEFT JOIN short_win s USING (cik)
        LEFT JOIN strat     st USING (cik)
        LEFT JOIN stake     k USING (cik)
        """,
        {
            "as_of": as_of,
            "long_start": long_start,
            "short_start": short_start,
            "factor": PRICE_SANITY_FACTOR,
        },
    ).fetchdf().set_index("cik")


def institutional_metrics(con: duckdb.DuckDBPyConnection, as_of: dt.date) -> pd.DataFrame:
    """Flujo institucional entre los dos últimos trimestres completos de 13F.

    Lo que se puntúa son los **recuentos de gestoras**, no el número de acciones en
    su poder. La razón es un desdoblamiento de acciones: multiplica por diez las
    acciones que declara cada fondo sin que nadie haya comprado nada, y en la prueba
    real generaba variaciones de treinta millones por ciento. El recuento de
    gestoras es inmune a los desdoblamientos, y para el propósito del radar dice
    además algo más pertinente: no cuánto dinero hay dentro, sino cuántas casas han
    descubierto el valor.
    """
    quarters = usable_quarters(con, as_of)
    if len(quarters) < 2:
        return pd.DataFrame()
    latest, previous = quarters[0], quarters[1]

    return con.execute(
        """
        WITH ticker_cik AS (
            -- Un ticker puede repetirse en el universo (clases de acciones, CIK
            -- heredados de fusiones). Sin colapsarlo, cada posición se contaría
            -- tantas veces como filas comparta el ticker.
            SELECT ticker, min(cik) AS cik FROM universe
            WHERE ticker IS NOT NULL GROUP BY ticker
        ),
        mapped AS (
            -- El 13F habla en CUSIP; el resto del radar, en CIK.
            SELECT h.quarter, h.manager_cik, t.cik, h.value_usd, h.shares
            FROM institutional_holdings h
            JOIN cusip_map  c USING (cusip)
            JOIN ticker_cik t ON t.ticker = c.ticker
            WHERE h.quarter IN ($latest, $previous)
        ),
        agg AS (
            SELECT cik, quarter,
                   count(DISTINCT manager_cik) AS holders,
                   sum(shares)                 AS shares,
                   sum(value_usd)              AS value_usd
            FROM mapped GROUP BY 1, 2
        ),
        now_q  AS (SELECT * FROM agg WHERE quarter = $latest),
        prev_q AS (SELECT * FROM agg WHERE quarter = $previous),
        -- Base comparable: existía el trimestre anterior con suficientes gestoras y
        -- la variación cae dentro de la banda que admite lectura como flujo.
        comparable AS (
            SELECT p.cik, p.holders AS prev_holders
            FROM prev_q p
            LEFT JOIN now_q n USING (cik)
            WHERE p.holders >= $min_base
              AND (coalesce(n.holders, 0) - p.holders) * 1.0 / p.holders
                  BETWEEN $min_decline AND $max_growth
        ),
        -- Aperturas y cierres de posición, que distinguen a la gestora que amplía
        -- de la que descubre el valor por primera vez.
        moves AS (
            SELECT
                coalesce(n.cik, p.cik) AS cik,
                count(*) FILTER (WHERE p.manager_cik IS NULL) AS new_positions,
                count(*) FILTER (WHERE n.manager_cik IS NULL) AS closed_positions
            FROM (SELECT cik, manager_cik FROM mapped WHERE quarter = $latest) n
            FULL OUTER JOIN
                 (SELECT cik, manager_cik FROM mapped WHERE quarter = $previous) p
              ON n.cik = p.cik AND n.manager_cik = p.manager_cik
            GROUP BY 1
        )
        SELECT
            coalesce(n.cik, p.cik)                          AS cik,
            $latest                                         AS inst_quarter,
            coalesce(n.holders, 0)                          AS inst_holders,
            n.shares                                        AS inst_shares,
            n.value_usd                                     AS inst_value_usd,
            coalesce(m.new_positions, 0)                    AS inst_new_positions,
            coalesce(m.closed_positions, 0)                 AS inst_closed_positions,
            -- Las variaciones solo existen cuando hay base comparable. Una empresa
            -- que no estaba el trimestre pasado, o que cambió de identidad, no tiene
            -- una variación enorme: tiene una variación desconocida.
            cmp.prev_holders IS NULL                        AS inst_change_unknown,
            CASE WHEN cmp.prev_holders IS NOT NULL
                 THEN coalesce(n.holders, 0) - cmp.prev_holders
            END                                             AS inst_holders_change,
            CASE WHEN cmp.prev_holders IS NOT NULL
                 THEN (coalesce(n.holders, 0) - cmp.prev_holders) * 1.0 / cmp.prev_holders
            END                                             AS inst_holders_change_pct,
            CASE WHEN cmp.prev_holders IS NOT NULL
                 THEN coalesce(m.new_positions, 0) * 1.0 / cmp.prev_holders
            END                                             AS inst_new_position_ratio,
            CASE WHEN cmp.prev_holders IS NOT NULL
                 THEN (coalesce(m.new_positions, 0) - coalesce(m.closed_positions, 0))
                      * 1.0 / cmp.prev_holders
            END                                             AS inst_net_position_ratio,
            -- Diagnóstico, no se puntúa: queda expuesto para poder auditar un valor,
            -- pero un desdoblamiento lo distorsiona y no debe entrar en la nota.
            CASE WHEN coalesce(p.shares, 0) > 0
                 THEN (coalesce(n.shares, 0) - p.shares) / p.shares
            END                                             AS inst_shares_change_pct,
            p.cik IS NULL                                   AS inst_first_quarter
        FROM now_q n
        FULL OUTER JOIN prev_q p USING (cik)
        LEFT JOIN moves m      ON m.cik   = coalesce(n.cik, p.cik)
        LEFT JOIN comparable cmp ON cmp.cik = coalesce(n.cik, p.cik)
        """,
        {
            "latest": latest,
            "previous": previous,
            "min_base": MIN_PRIOR_HOLDERS,
            "max_growth": MAX_HOLDER_GROWTH,
            "min_decline": MIN_HOLDER_DECLINE,
        },
    ).fetchdf().set_index("cik")


def usable_quarters(
    con: duckdb.DuckDBPyConnection, as_of: dt.date, min_share: float = 0.5
) -> list[dt.date]:
    """Trimestres de 13F con presentación suficientemente completa, del más reciente al más antiguo.

    El umbral se fija sobre el número de gestoras declarantes del trimestre más
    poblado. Los trimestres que solo contienen declaraciones tardías quedan uno o
    dos órdenes de magnitud por debajo y se descartan solos.
    """
    rows = con.execute(
        """
        WITH q AS (
            SELECT quarter, count(DISTINCT manager_cik) AS managers
            FROM institutional_holdings
            WHERE quarter <= $as_of
            GROUP BY 1
        )
        SELECT quarter FROM q
        WHERE managers >= $min_share * (SELECT max(managers) FROM q)
        ORDER BY quarter DESC
        """,
        {"as_of": as_of, "min_share": min_share},
    ).fetchall()
    return [r[0] for r in rows]
