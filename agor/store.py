"""Almacén de datos sobre DuckDB.

Un solo fichero `data/agor.duckdb` contiene todo: universo, fundamentales,
precios, scores y alertas. DuckDB permite consultas analíticas sobre millones de
filas sin servidor y el fichero se puede versionar en git, que es lo que hace
posible que GitHub Actions retome el estado del día anterior sin base de datos
externa ni coste.

La tabla `score_snapshots` es append-only por diseño: es la memoria del radar y
la única fuente de verdad del módulo de aprendizaje. Nunca se reescribe un score
pasado, porque hacerlo destruiría la capacidad de medir si las decisiones de hace
seis meses fueron acertadas.
"""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import duckdb
import pandas as pd

from .config import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS universe (
    cik              BIGINT PRIMARY KEY,
    ticker           VARCHAR,
    name             VARCHAR,
    exchange         VARCHAR,
    sic              INTEGER,
    sic_description  VARCHAR,
    sector           VARCHAR,
    fiscal_year_end  VARCHAR,
    is_foreign_filer BOOLEAN,
    state_of_inc     VARCHAR,
    active           BOOLEAN DEFAULT TRUE,
    updated_at       TIMESTAMP
);

-- Hechos XBRL en formato largo, tal como los publica la SEC. No se transforma
-- nada aquí a propósito: los ajustes van en la capa de features, para poder
-- rehacerlos sin volver a descargar.
-- Tabla de PASO. Se reconstruye desde los ZIP de la SEC y no se versiona: es
-- grande y siempre regenerable. Lo valioso que sí se conserva es el panel
-- derivado (fundamentals_q) y el histórico de scores.
--
-- qtrs replica el campo de la SEC: 0 = saldo puntual (balance), 1 = un
-- trimestre, 4 = un año. Sin él es imposible distinguir un ingreso trimestral de
-- uno anual, que es el error que más veces arruina un panel de fundamentales.
--
-- Se guarda un único valor por (empresa, concepto, periodo): el de la
-- presentación más reciente. Eso significa que el panel refleja cifras
-- reexpresadas, no las publicadas originalmente. Es una limitación consciente y
-- conviene tenerla presente: por eso el módulo de aprendizaje mide hacia
-- adelante y nunca reentrena sobre el pasado.
CREATE TABLE IF NOT EXISTS fundamentals_raw (
    cik        BIGINT,
    concept    VARCHAR,
    unit       VARCHAR,
    period_end DATE,
    qtrs       INTEGER,
    fy         INTEGER,
    fp         VARCHAR,
    form       VARCHAR,
    val        DOUBLE,
    accn       VARCHAR,
    filed      DATE,
    PRIMARY KEY (cik, concept, unit, period_end, qtrs)
);

-- Metadatos del declarante extraídos de sub.txt. Evita 10.000 llamadas a la API
-- de submissions: el propio dataset trimestral ya trae SIC, nombre, cierre
-- fiscal y país de constitución de cada empresa que ha presentado cuentas.
CREATE TABLE IF NOT EXISTS filer_meta (
    cik           BIGINT PRIMARY KEY,
    name          VARCHAR,
    sic           INTEGER,
    fye           VARCHAR,
    country_inc   VARCHAR,
    latest_form   VARCHAR,
    latest_period DATE
);

CREATE TABLE IF NOT EXISTS prices (
    ticker       VARCHAR,
    date         DATE,
    open         DOUBLE,
    high         DOUBLE,
    low          DOUBLE,
    close        DOUBLE,
    volume       DOUBLE,
    vwap         DOUBLE,
    PRIMARY KEY (ticker, date)
);

CREATE TABLE IF NOT EXISTS shares_outstanding (
    cik        BIGINT,
    period_end DATE,
    shares     DOUBLE,
    PRIMARY KEY (cik, period_end)
);

-- Operaciones de insiders (formularios 3, 4 y 5) desde el dataset trimestral de
-- la SEC. El campo `planned_10b5_1` es el que da valor a esta tabla: permite
-- separar la venta programada meses antes por un plan automático, que no informa
-- de nada, de la venta discrecional, que sí.
CREATE TABLE IF NOT EXISTS insider_transactions (
    accn            VARCHAR,
    trans_sk        VARCHAR,
    issuer_cik      BIGINT,
    ticker          VARCHAR,
    owner_cik       BIGINT,
    owner_name      VARCHAR,
    relationship    VARCHAR,
    title           VARCHAR,
    is_officer      BOOLEAN,
    is_director     BOOLEAN,
    is_ten_percent  BOOLEAN,
    trans_date      DATE,
    filing_date     DATE,
    trans_code      VARCHAR,
    shares          DOUBLE,
    price           DOUBLE,
    value_usd       DOUBLE,
    acquired        BOOLEAN,
    shares_after    DOUBLE,
    direct          BOOLEAN,
    planned_10b5_1  BOOLEAN,
    PRIMARY KEY (accn, trans_sk)
);

-- Posiciones declaradas en los 13F, a nivel de gestora y valor. Solo se conservan
-- los trimestres recientes: lo que aporta señal es la variación entre trimestres,
-- no el histórico profundo, y la tabla crece más de un millón de filas por
-- trimestre.
CREATE TABLE IF NOT EXISTS institutional_holdings (
    quarter     DATE,
    manager_cik BIGINT,
    cusip       VARCHAR,
    value_usd   DOUBLE,
    shares      DOUBLE,
    PRIMARY KEY (quarter, manager_cik, cusip)
);

-- Puente CUSIP -> ticker construido con los ficheros de fallos de entrega de la
-- SEC. Los 13F identifican las posiciones por CUSIP y las tablas oficiales de
-- CUSIP son de licencia comercial; este es el camino gratuito y legal.
CREATE TABLE IF NOT EXISTS cusip_map (
    cusip       VARCHAR PRIMARY KEY,
    ticker      VARCHAR,
    description VARCHAR,
    last_seen   DATE
);

-- Append-only. Memoria histórica del radar.
CREATE TABLE IF NOT EXISTS score_snapshots (
    as_of     DATE,
    cik       BIGINT,
    ticker    VARCHAR,
    engine_id VARCHAR,
    score     DOUBLE,
    coverage  DOUBLE,
    PRIMARY KEY (as_of, cik, engine_id)
);

CREATE TABLE IF NOT EXISTS score_totals (
    as_of           DATE,
    cik             BIGINT,
    ticker          VARCHAR,
    name            VARCHAR,
    sector          VARCHAR,
    market_cap      DOUBLE,
    total           DOUBLE,
    band            VARCHAR,
    coverage        DOUBLE,
    engines_scored  INTEGER,
    PRIMARY KEY (as_of, cik)
);

CREATE TABLE IF NOT EXISTS alerts (
    as_of    DATE,
    cik      BIGINT,
    ticker   VARCHAR,
    rule_id  VARCHAR,
    severity VARCHAR,
    detail   VARCHAR,
    PRIMARY KEY (as_of, cik, rule_id)
);

-- Registro de ejecuciones y de marcas de agua de ingesta, para poder ser
-- incremental y no volver a pedir lo ya pedido.
CREATE TABLE IF NOT EXISTS run_log (
    run_id     VARCHAR,
    step       VARCHAR,
    started_at TIMESTAMP,
    ended_at   TIMESTAMP,
    status     VARCHAR,
    rows       BIGINT,
    detail     VARCHAR
);

CREATE TABLE IF NOT EXISTS watermarks (
    key        VARCHAR PRIMARY KEY,
    value      VARCHAR,
    updated_at TIMESTAMP
);
"""


def connect(path: Path | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    ensure_dirs()
    con = duckdb.connect(str(path or DB_PATH), read_only=read_only)
    if not read_only:
        con.execute(SCHEMA)
    return con


@contextmanager
def db(path: Path | None = None, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    con = connect(path, read_only=read_only)
    try:
        yield con
    finally:
        con.close()


def upsert(
    con: duckdb.DuckDBPyConnection,
    table: str,
    frame: pd.DataFrame,
    key_columns: list[str],
) -> int:
    """Inserta o reemplaza filas por clave primaria.

    DuckDB no tiene un `MERGE` cómodo para este caso, así que se borran las
    claves colisionantes y se insertan todas. Es la operación más rápida cuando
    el lote es grande respecto a la tabla, que es siempre nuestro caso.
    """
    if frame.empty:
        return 0

    columns = [c for c in _table_columns(con, table) if c in frame.columns]
    payload = frame[columns].copy()

    con.register("_incoming", payload)
    on = " AND ".join(f"t.{k} = s.{k}" for k in key_columns)
    con.execute(f"DELETE FROM {table} t WHERE EXISTS (SELECT 1 FROM _incoming s WHERE {on})")
    con.execute(f"INSERT INTO {table} ({', '.join(columns)}) SELECT {', '.join(columns)} FROM _incoming")
    con.unregister("_incoming")
    return len(payload)


def append(con: duckdb.DuckDBPyConnection, table: str, frame: pd.DataFrame) -> int:
    if frame.empty:
        return 0
    columns = [c for c in _table_columns(con, table) if c in frame.columns]
    con.register("_incoming", frame[columns])
    con.execute(f"INSERT INTO {table} ({', '.join(columns)}) SELECT {', '.join(columns)} FROM _incoming")
    con.unregister("_incoming")
    return len(frame)


def _table_columns(con: duckdb.DuckDBPyConnection, table: str) -> list[str]:
    rows = con.execute(f"PRAGMA table_info('{table}')").fetchall()
    return [r[1] for r in rows]


def get_watermark(con: duckdb.DuckDBPyConnection, key: str) -> str | None:
    row = con.execute("SELECT value FROM watermarks WHERE key = ?", [key]).fetchone()
    return row[0] if row else None


def set_watermark(con: duckdb.DuckDBPyConnection, key: str, value: str) -> None:
    con.execute(
        """
        INSERT INTO watermarks (key, value, updated_at) VALUES (?, ?, now())
        ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        [key, value],
    )


def table_count(con: duckdb.DuckDBPyConnection, table: str) -> int:
    return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def freshness(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Hasta qué fecha llega cada fuente, y cuántos días de retraso acumula.

    Es una salida de primer orden, no un diagnóstico secundario. Las fuentes del
    radar tienen retrasos estructurales muy distintos y confundirlos lleva a leer
    como actual algo que no lo es: los precios llegan del día anterior, pero los
    datasets de insiders y de 13F se publican por trimestres y pueden ir cuatro
    meses por detrás. Un motor 4 en 90 puntos no significa que los directivos estén
    comprando hoy, sino que compraron en el último trimestre publicado.
    """
    sources = [
        ("prices", "max(date)", "Cotizaciones"),
        ("fundamentals_raw", "max(filed)", "Fundamentales (SEC)"),
        ("insider_transactions", "max(filing_date)", "Operaciones de directivos"),
        ("institutional_holdings", "max(quarter)", "Posiciones institucionales (13F)"),
    ]
    today = dt.date.today()
    rows = []
    for table, expression, label in sources:
        try:
            latest = con.execute(f"SELECT {expression} FROM {table}").fetchone()[0]
        except duckdb.Error:
            latest = None
        rows.append(
            {
                "fuente": label,
                "ultimo_dato": latest,
                "dias_de_retraso": (today - latest).days if latest else None,
            }
        )
    return pd.DataFrame(rows)


def summary(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    tables = [
        "universe",
        "fundamentals_raw",
        "prices",
        "shares_outstanding",
        "insider_transactions",
        "institutional_holdings",
        "cusip_map",
        "score_snapshots",
        "score_totals",
        "alerts",
    ]
    rows = [{"tabla": t, "filas": table_count(con, t)} for t in tables]
    return pd.DataFrame(rows)
