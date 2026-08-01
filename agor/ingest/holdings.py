"""Ingesta de posiciones institucionales (13F) y del puente CUSIP -> ticker.

Los 13F identifican cada posición por CUSIP, no por ticker ni por CIK, y las
tablas oficiales de CUSIP son un producto de licencia comercial que no se puede
redistribuir. El camino gratuito y legal que usa AGOR son los ficheros de fallos
de entrega que publica la propia SEC: se pensaron para vigilar las ventas en corto,
pero incluyen las columnas CUSIP y SYMBOL, que es exactamente el puente que hace
falta.

Sobre los límites de este dato, que conviene tener presentes al leer el motor 8:

- Solo declaran las gestoras con más de 100 millones de dólares bajo gestión.
- Solo posiciones largas. Un fondo puede declarar una participación enorme y estar
  cubierto con derivados sin que aquí se vea.
- Se publican con hasta 45 días de retraso sobre el cierre del trimestre. Es un
  indicador rezagado y jamás debe leerse como "lo que están comprando hoy".
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import duckdb
from rich.console import Console

from ..config import CACHE_DIR
from ..providers.sec import SecClient
from ..store import get_watermark, set_watermark

console = Console()

TMP_DIR = CACHE_DIR / "sec" / "tmp_13f"

# Trimestres de 13F que se conservan. Lo que puntúa el motor es la variación entre
# trimestres consecutivos, así que con cuatro sobra y la tabla se mantiene en unos
# pocos millones de filas en lugar de decenas.
KEEP_QUARTERS = 6


# ----------------------------------------------------------------------
# Puente CUSIP -> ticker
# ----------------------------------------------------------------------
def refresh_cusip_map(
    con: duckdb.DuckDBPyConnection, client: SecClient, months: int = 6
) -> int:
    """Acumula pares CUSIP-ticker de los ficheros de fallos de entrega.

    Es acumulativo a propósito: cada fichero solo contiene los valores que tuvieron
    algún fallo de entrega en esa quincena, así que ninguno cubre el mercado
    entero, pero la unión de varios meses sí cubre prácticamente todo lo líquido.
    """
    paths = client.fails_to_deliver(months=months)
    if not paths:
        return 0

    work_dir = TMP_DIR / "ftd"
    shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        extracted: list[Path] = []
        for zip_path in paths:
            try:
                with zipfile.ZipFile(zip_path) as archive:
                    for member in archive.namelist():
                        if member.endswith("/"):
                            continue
                        target = work_dir / f"{zip_path.stem}_{Path(member).name}.txt"
                        with archive.open(member) as source, target.open("wb") as handle:
                            shutil.copyfileobj(source, handle)
                        extracted.append(target)
            except zipfile.BadZipFile:
                console.print(f"  [yellow]fichero corrupto, se ignora: {zip_path.name}[/yellow]")

        if not extracted:
            return 0

        files = ", ".join(f"'{_p(p)}'" for p in extracted)
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE ftd_stage AS
            SELECT cusip, ticker, description, max(seen) AS last_seen
            FROM (
                SELECT
                    upper(trim("CUSIP"))    AS cusip,
                    upper(trim("SYMBOL"))   AS ticker,
                    "DESCRIPTION"           AS description,
                    TRY_CAST(strptime("SETTLEMENT DATE", '%Y%m%d') AS DATE) AS seen
                FROM read_csv([{files}], delim='|', header=true, all_varchar=true,
                              quote='', ignore_errors=true, null_padding=true)
                WHERE length(trim(coalesce("CUSIP", ''))) = 9
                  AND length(trim(coalesce("SYMBOL", ''))) BETWEEN 1 AND 8
            )
            GROUP BY cusip, ticker, description
            """
        )
        # Un CUSIP puede aparecer con más de un símbolo tras un cambio de ticker.
        # Se conserva el más reciente, que es el que coincide con nuestro universo.
        con.execute(
            """
            CREATE OR REPLACE TEMP TABLE cusip_stage AS
            SELECT cusip, ticker, description, last_seen FROM (
                SELECT *, row_number() OVER (PARTITION BY cusip ORDER BY last_seen DESC) AS rn
                FROM ftd_stage
            ) WHERE rn = 1
            """
        )
        con.execute(
            """
            DELETE FROM cusip_map t
            WHERE EXISTS (SELECT 1 FROM cusip_stage s
                          WHERE s.cusip = t.cusip
                            AND (t.last_seen IS NULL OR s.last_seen >= t.last_seen))
            """
        )
        rows = con.execute(
            """
            INSERT INTO cusip_map (cusip, ticker, description, last_seen)
            SELECT cusip, ticker, description, last_seen FROM cusip_stage s
            WHERE NOT EXISTS (SELECT 1 FROM cusip_map t WHERE t.cusip = s.cusip);
            SELECT count(*) FROM cusip_map
            """
        ).fetchone()[0]
        return int(rows)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


# ----------------------------------------------------------------------
# 13F
# ----------------------------------------------------------------------
def ingest_13f_file(
    con: duckdb.DuckDBPyConnection, client: SecClient, url: str
) -> int:
    """Carga un dataset de 13F, quedándose solo con lo que mapea a nuestro universo."""
    zip_path = client.download_13f(url)
    work_dir = TMP_DIR / Path(url).stem
    try:
        submission = client.fsds_extract_member(zip_path, "SUBMISSION.tsv", work_dir)
        info = client.fsds_extract_member(zip_path, "INFOTABLE.tsv", work_dir)

        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE h_sub AS
            SELECT
                ACCESSION_NUMBER                AS accn,
                TRY_CAST(CIK AS BIGINT)         AS manager_cik,
                TRY_CAST(try_strptime(trim(PERIODOFREPORT), '%d-%b-%Y') AS DATE) AS quarter
            FROM read_csv('{_p(submission)}', delim='\t', header=true, all_varchar=true,
                          quote='', ignore_errors=true, null_padding=true)
            WHERE SUBMISSIONTYPE IN ('13F-HR', '13F-HR/A')
              AND TRY_CAST(CIK AS BIGINT) IS NOT NULL
            """
        )

        # INFOTABLE ronda los 320 MB descomprimidos. El filtro por CUSIP conocido se
        # aplica aquí, en el propio recorrido del fichero, para no materializar los
        # millones de filas de renta fija y de valores fuera de nuestro universo.
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE h_info AS
            SELECT
                ACCESSION_NUMBER                    AS accn,
                upper(trim(CUSIP))                  AS cusip,
                TRY_CAST(VALUE AS DOUBLE)           AS value_raw,
                TRY_CAST(SSHPRNAMT AS DOUBLE)       AS shares
            FROM read_csv('{_p(info)}', delim='\t', header=true, all_varchar=true,
                          quote='', ignore_errors=true, null_padding=true)
            WHERE coalesce(trim(PUTCALL), '') = ''        -- fuera opciones: no son acciones
              AND upper(coalesce(trim(SSHPRNAMTTYPE), 'SH')) = 'SH'
              AND TRY_CAST(SSHPRNAMT AS DOUBLE) > 0
              AND TRY_CAST(VALUE AS DOUBLE) > 0
              AND upper(trim(CUSIP)) IN (SELECT cusip FROM cusip_map)
            """
        )

        # La SEC cambió en 2023 la unidad de la columna VALUE: antes eran miles de
        # dólares y ahora son dólares. En vez de fiarse de una fecha de corte, se
        # deduce de los datos: si el precio implícito por acción de un documento es
        # absurdamente bajo, ese documento está en miles.
        con.execute(
            """
            CREATE OR REPLACE TEMP TABLE h_scale AS
            SELECT accn,
                   CASE WHEN median(value_raw / shares) < 0.5 THEN 1000.0 ELSE 1.0 END AS factor
            FROM h_info GROUP BY accn
            """
        )

        rows = con.execute(
            """
            CREATE OR REPLACE TEMP TABLE h_stage AS
            SELECT s.quarter, s.manager_cik, i.cusip,
                   sum(i.value_raw * c.factor) AS value_usd,
                   sum(i.shares)               AS shares
            FROM h_info i
            JOIN h_sub s USING (accn)
            JOIN h_scale c USING (accn)
            WHERE s.quarter IS NOT NULL
            GROUP BY 1, 2, 3;
            SELECT count(*) FROM h_stage
            """
        ).fetchone()[0]

        if rows == 0:
            return 0

        con.execute(
            """
            DELETE FROM institutional_holdings t
            WHERE EXISTS (SELECT 1 FROM h_stage s
                          WHERE s.quarter = t.quarter AND s.manager_cik = t.manager_cik
                            AND s.cusip = t.cusip)
            """
        )
        con.execute(
            """
            INSERT INTO institutional_holdings (quarter, manager_cik, cusip, value_usd, shares)
            SELECT quarter, manager_cik, cusip, value_usd, shares FROM h_stage
            """
        )
        return int(rows)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def backfill(
    con: duckdb.DuckDBPyConnection,
    client: SecClient,
    files: int = 4,
) -> int:
    """Carga los `files` datasets de 13F más recientes.

    Hacen falta al menos dos para que el motor 8 exista: lo que puntúa no es cuánto
    tienen los fondos, sino si están entrando o saliendo, y eso exige comparar dos
    trimestres consecutivos.
    """
    if con.execute("SELECT count(*) FROM cusip_map").fetchone()[0] == 0:
        console.print("  [yellow]sin puente CUSIP: se omiten los 13F[/yellow]")
        return 0

    done = set((get_watermark(con, "f13_files_done") or "").split(",")) - {""}
    total = 0

    for url in client.list_13f_datasets()[:files]:
        key = Path(url).name
        if key in done:
            continue
        rows = ingest_13f_file(con, client, url)
        if rows == 0:
            continue
        total += rows
        done.add(key)
        set_watermark(con, "f13_files_done", ",".join(sorted(done)))
        console.print(f"  [green]13F {key}[/green]: {rows:,} posiciones")

    _prune(con)
    return total


def _prune(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        f"""
        DELETE FROM institutional_holdings
        WHERE quarter < (
            SELECT min(quarter) FROM (
                SELECT DISTINCT quarter FROM institutional_holdings
                ORDER BY quarter DESC LIMIT {KEEP_QUARTERS}
            )
        )
        """
    )


def _p(path: Path) -> str:
    return str(path).replace("'", "''")
