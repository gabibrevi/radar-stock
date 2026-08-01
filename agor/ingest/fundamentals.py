"""Ingesta de fundamentales desde los Financial Statement Data Sets de la SEC.

El trabajo pesado se hace en SQL sobre DuckDB leyendo directamente los ficheros
de texto extraídos, no en pandas. `num.txt` ronda los 540 MB por trimestre y
cargarlo en memoria sería innecesario: DuckDB lo recorre en streaming y solo
materializa las filas que nos interesan, que son alrededor del 2%.

Dos filtros son imprescindibles y su ausencia es el fallo más común al usar estos
datasets:

- `segments` vacío: si no, entran los desgloses por segmento y geografía, y los
  ingresos de una empresa aparecen repetidos y sumados de más.
- `coreg` vacío: descarta las cifras de co-declarantes (filiales que presentan
  dentro del mismo documento).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb
import pandas as pd
from rich.console import Console

from ..config import CACHE_DIR
from ..providers.sec import SecClient
from ..store import get_watermark, set_watermark
from ..xbrl import all_tags

console = Console()

ACCEPTED_FORMS = ("10-K", "10-Q", "20-F", "40-F", "10-K/A", "10-Q/A", "20-F/A")
ACCEPTED_UNITS = ("USD", "shares", "USD/shares", "pure")

TMP_DIR = CACHE_DIR / "sec" / "tmp"


def ingest_quarter(
    con: duckdb.DuckDBPyConnection,
    client: SecClient,
    year: int,
    quarter: int,
    keep_zip: bool = True,
) -> int:
    """Carga un trimestre completo. Devuelve el número de filas nuevas."""
    zip_path = client.fsds_download(year, quarter)
    if zip_path is None:
        return 0

    work_dir = TMP_DIR / f"{year}q{quarter}"
    try:
        sub_path = client.fsds_extract_member(zip_path, "sub.txt", work_dir)
        num_path = client.fsds_extract_member(zip_path, "num.txt", work_dir)

        _register_wanted_tags(con)
        _load_submissions(con, sub_path)
        _load_numbers(con, num_path)

        rows = con.execute(
            """
            WITH joined AS (
                SELECT
                    s.cik,
                    n.tag        AS concept,
                    n.uom        AS unit,
                    n.period_end,
                    n.qtrs,
                    s.fy,
                    s.fp,
                    s.form,
                    n.val,
                    s.adsh       AS accn,
                    s.filed
                FROM num_stage n
                JOIN sub_stage s USING (adsh)
            ),
            ranked AS (
                SELECT *, row_number() OVER (
                    PARTITION BY cik, concept, unit, period_end, qtrs
                    ORDER BY filed DESC, accn DESC
                ) AS rn
                FROM joined
            )
            SELECT cik, concept, unit, period_end, qtrs, fy, fp, form, val, accn, filed
            FROM ranked WHERE rn = 1
            """
        ).fetchdf()

        inserted = _merge_facts(con, rows)
        _merge_filer_meta(con)
        return inserted
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        if not keep_zip and zip_path.exists():
            zip_path.unlink()


def _register_wanted_tags(con: duckdb.DuckDBPyConnection) -> None:
    tags = pd.DataFrame({"tag": sorted(all_tags())})
    con.register("_wanted_tags", tags)
    con.execute("CREATE OR REPLACE TEMP TABLE wanted_tags AS SELECT tag FROM _wanted_tags")
    con.unregister("_wanted_tags")


def _load_submissions(con: duckdb.DuckDBPyConnection, path: Path) -> None:
    forms = ", ".join(f"'{f}'" for f in ACCEPTED_FORMS)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE sub_stage AS
        SELECT
            adsh,
            TRY_CAST(cik AS BIGINT)                        AS cik,
            name,
            TRY_CAST(sic AS INTEGER)                       AS sic,
            fye,
            countryinc                                     AS country_inc,
            form,
            TRY_CAST(strptime(fy, '%Y') AS DATE)           AS fy_date,
            TRY_CAST(fy AS INTEGER)                        AS fy,
            fp,
            TRY_CAST(strptime(filed, '%Y%m%d') AS DATE)    AS filed,
            TRY_CAST(strptime(period, '%Y%m%d') AS DATE)   AS period
        FROM read_csv(
            '{_sql_path(path)}',
            delim='\t', header=true, all_varchar=true, quote='',
            ignore_errors=true, null_padding=true
        )
        WHERE form IN ({forms})
          AND coalesce(prevrpt, '0') = '0'
          AND TRY_CAST(cik AS BIGINT) IS NOT NULL
        """
    )


def _load_numbers(con: duckdb.DuckDBPyConnection, path: Path) -> None:
    units = ", ".join(f"'{u}'" for u in ACCEPTED_UNITS)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE num_stage AS
        SELECT
            adsh,
            tag,
            uom,
            TRY_CAST(strptime(ddate, '%Y%m%d') AS DATE) AS period_end,
            TRY_CAST(qtrs AS INTEGER)                   AS qtrs,
            TRY_CAST(value AS DOUBLE)                   AS val
        FROM read_csv(
            '{_sql_path(path)}',
            delim='\t', header=true, all_varchar=true, quote='',
            ignore_errors=true, null_padding=true
        )
        WHERE tag IN (SELECT tag FROM wanted_tags)
          AND coalesce(segments, '') = ''
          AND coalesce(coreg, '') = ''
          AND uom IN ({units})
          AND value IS NOT NULL AND value <> ''
          -- 0 = saldo, 1 = trimestre, 4 = año. Se aceptan también 2 y 3
          -- (acumulados) porque muchas extranjeras con 20-F reportan por
          -- semestres y sin ellos su serie quedaría vacía; el panel los convierte
          -- a trimestres por diferencias.
          AND TRY_CAST(qtrs AS INTEGER) IN (0, 1, 2, 3, 4)
        """
    )


def _merge_facts(con: duckdb.DuckDBPyConnection, rows: pd.DataFrame) -> int:
    """Inserta conservando, ante empate de periodo, la presentación más reciente."""
    if rows.empty:
        return 0
    con.register("_facts", rows)
    con.execute(
        """
        DELETE FROM fundamentals_raw t
        WHERE EXISTS (
            SELECT 1 FROM _facts s
            WHERE t.cik = s.cik AND t.concept = s.concept AND t.unit = s.unit
              AND t.period_end = s.period_end AND t.qtrs = s.qtrs
              AND s.filed >= t.filed
        )
        """
    )
    inserted = con.execute(
        """
        INSERT INTO fundamentals_raw
            (cik, concept, unit, period_end, qtrs, fy, fp, form, val, accn, filed)
        SELECT s.cik, s.concept, s.unit, s.period_end, s.qtrs, s.fy, s.fp,
               s.form, s.val, s.accn, s.filed
        FROM _facts s
        WHERE NOT EXISTS (
            SELECT 1 FROM fundamentals_raw t
            WHERE t.cik = s.cik AND t.concept = s.concept AND t.unit = s.unit
              AND t.period_end = s.period_end AND t.qtrs = s.qtrs
        )
        """
    ).fetchall()
    con.unregister("_facts")
    return len(rows)


def _merge_filer_meta(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE meta_stage AS
        SELECT cik, name, sic, fye, country_inc, form AS latest_form,
               period AS latest_period
        FROM (
            SELECT *, row_number() OVER (PARTITION BY cik ORDER BY filed DESC) AS rn
            FROM sub_stage
        ) WHERE rn = 1
        """
    )
    con.execute(
        """
        DELETE FROM filer_meta
        WHERE cik IN (
            SELECT m.cik FROM meta_stage m
            JOIN filer_meta f USING (cik)
            WHERE m.latest_period IS NOT NULL
              AND (f.latest_period IS NULL OR m.latest_period >= f.latest_period)
        )
        """
    )
    con.execute(
        """
        INSERT INTO filer_meta (cik, name, sic, fye, country_inc, latest_form, latest_period)
        SELECT cik, name, sic, fye, country_inc, latest_form, latest_period
        FROM meta_stage m
        WHERE NOT EXISTS (SELECT 1 FROM filer_meta f WHERE f.cik = m.cik)
        """
    )


def backfill(
    con: duckdb.DuckDBPyConnection,
    client: SecClient,
    start_year: int,
    keep_zips: bool = False,
) -> int:
    """Carga todos los trimestres disponibles desde `start_year`.

    Es incremental: la marca de agua evita repetir trimestres ya cargados, así que
    la ejecución diaria en CI solo descarga algo cuando la SEC publica un dataset
    nuevo (una vez por trimestre).
    """
    done = set((get_watermark(con, "fsds_quarters_done") or "").split(",")) - {""}
    total = 0

    quarters = client.quarters_since(start_year)
    for year, quarter in quarters:
        key = f"{year}q{quarter}"
        if key in done:
            continue
        rows = ingest_quarter(con, client, year, quarter, keep_zip=keep_zips)
        if rows == 0:
            # Aún no publicado. No se marca como hecho para reintentarlo mañana.
            console.print(f"  [dim]{key}: no publicado todavía[/dim]")
            continue
        total += rows
        done.add(key)
        set_watermark(con, "fsds_quarters_done", ",".join(sorted(done)))
        console.print(f"  [green]{key}[/green]: {rows:,} hechos")

    return total


def _sql_path(path: Path) -> str:
    return str(path).replace("'", "''")
