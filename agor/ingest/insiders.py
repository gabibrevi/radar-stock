"""Ingesta de operaciones de insiders desde el dataset trimestral de la SEC.

Este dataset es, con diferencia, el mejor dato gratuito que existe para el radar:
son 8 MB por trimestre y contienen todas las compras y ventas declaradas por
consejeros, directivos y accionistas de más del 10% de cualquier cotizada
estadounidense.

Dos distinciones deciden si esta señal vale algo o es ruido, y casi todos los
buscadores que se anuncian con "compras de directivos" las ignoran:

- **El código de la operación.** Solo `P` es una compra en mercado abierto, con
  dinero propio y a precio de mercado. Los códigos `A` (concesión de acciones como
  retribución), `M` y `X` (ejercicio de opciones) y `F` (entrega de acciones para
  pagar impuestos) no son decisiones de inversión: aparecen en el calendario de
  compensación, no en el criterio de nadie. Contarlos como compras convierte
  cualquier plan retributivo en una señal falsa de confianza.

- **El plan 10b5-1.** Una venta programada con meses de antelación por un plan
  automático no informa de nada: el directivo la firmó cuando no sabía lo que
  sabe hoy. El dataset la marca en `AFF10B5ONE` y aquí se guarda aparte, porque
  mezclarla con la venta discrecional hace que directivos que solo diversifican
  parezcan estar huyendo.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import duckdb
from rich.console import Console

from ..config import CACHE_DIR
from ..providers.sec import SecClient
from ..store import get_watermark, set_watermark

console = Console()

TMP_DIR = CACHE_DIR / "sec" / "tmp_insiders"

# Códigos que se conservan. Se guardan también los retributivos porque sirven para
# medir dilución por compensación y para calcular la participación del directivo,
# pero el motor los trata de forma muy distinta a una compra real.
KEPT_CODES = ("P", "S", "A", "M", "F", "G", "D", "C", "X")


def ingest_quarter(
    con: duckdb.DuckDBPyConnection,
    client: SecClient,
    year: int,
    quarter: int,
) -> int:
    zip_path = client.insider_download(year, quarter)
    if zip_path is None:
        return 0

    work_dir = TMP_DIR / f"{year}q{quarter}"
    try:
        submission = client.fsds_extract_member(zip_path, "SUBMISSION.tsv", work_dir)
        owners = client.fsds_extract_member(zip_path, "REPORTINGOWNER.tsv", work_dir)
        trans = client.fsds_extract_member(zip_path, "NONDERIV_TRANS.tsv", work_dir)

        codes = ", ".join(f"'{c}'" for c in KEPT_CODES)
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE ins_sub AS
            SELECT
                ACCESSION_NUMBER                                   AS accn,
                TRY_CAST(ISSUERCIK AS BIGINT)                      AS issuer_cik,
                -- Cuando la empresa no cotiza con símbolo, el campo no viene vacío:
                -- trae literales como 'NONE' o 'N/A' que sin esto pasarían por ticker.
                CASE WHEN upper(trim(coalesce(ISSUERTRADINGSYMBOL, ''))) IN
                          ('', 'NONE', 'N/A', 'NA', '-', '0')
                     THEN NULL ELSE upper(trim(ISSUERTRADINGSYMBOL)) END AS ticker,
                {_date('FILING_DATE')}                             AS filing_date,
                coalesce(AFF10B5ONE, '0') IN ('1', 'true', 'TRUE') AS planned_10b5_1
            FROM read_csv('{_p(submission)}', delim='\t', header=true, all_varchar=true,
                          quote='', ignore_errors=true, null_padding=true)
            WHERE DOCUMENT_TYPE IN ('4', '4/A')
              AND TRY_CAST(ISSUERCIK AS BIGINT) IS NOT NULL
            """
        )

        # Un mismo formulario puede declararse por varios titulares. Se colapsan a
        # uno para no multiplicar la operación por el número de firmantes.
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE ins_owner AS
            SELECT accn, owner_cik, owner_name, relationship, title,
                   relationship ILIKE '%officer%'   AS is_officer,
                   relationship ILIKE '%director%'  AS is_director,
                   relationship ILIKE '%10%'        AS is_ten_percent
            FROM (
                SELECT
                    ACCESSION_NUMBER              AS accn,
                    TRY_CAST(RPTOWNERCIK AS BIGINT) AS owner_cik,
                    RPTOWNERNAME                  AS owner_name,
                    coalesce(RPTOWNER_RELATIONSHIP, '') AS relationship,
                    coalesce(RPTOWNER_TITLE, '')  AS title,
                    row_number() OVER (PARTITION BY ACCESSION_NUMBER
                                       ORDER BY RPTOWNERCIK) AS rn
                FROM read_csv('{_p(owners)}', delim='\t', header=true, all_varchar=true,
                              quote='', ignore_errors=true, null_padding=true)
            ) WHERE rn = 1
            """
        )

        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE ins_trans AS
            SELECT
                ACCESSION_NUMBER                        AS accn,
                NONDERIV_TRANS_SK                       AS trans_sk,
                {_date('TRANS_DATE')}                    AS trans_date,
                upper(trim(TRANS_CODE))                 AS trans_code,
                TRY_CAST(TRANS_SHARES AS DOUBLE)        AS shares,
                TRY_CAST(TRANS_PRICEPERSHARE AS DOUBLE) AS price,
                upper(trim(TRANS_ACQUIRED_DISP_CD)) = 'A' AS acquired,
                TRY_CAST(SHRS_OWND_FOLWNG_TRANS AS DOUBLE) AS shares_after,
                -- Se distingue la titularidad directa de la indirecta (a través de
                -- fideicomisos, fondos familiares o sociedades). Sumar las dos
                -- contaría dos veces las mismas acciones al medir cuánto capital
                -- tiene el directivo comprometido.
                upper(trim(coalesce(DIRECT_INDIRECT_OWNERSHIP, 'D'))) = 'D' AS direct
            FROM read_csv('{_p(trans)}', delim='\t', header=true, all_varchar=true,
                          quote='', ignore_errors=true, null_padding=true)
            WHERE upper(trim(TRANS_CODE)) IN ({codes})
              AND TRY_CAST(TRANS_SHARES AS DOUBLE) > 0
            """
        )

        rows = con.execute(
            """
            CREATE OR REPLACE TEMP TABLE ins_stage AS
            SELECT
                t.accn, t.trans_sk, s.issuer_cik, s.ticker,
                o.owner_cik, o.owner_name, o.relationship, o.title,
                o.is_officer, o.is_director, o.is_ten_percent,
                t.trans_date, s.filing_date, t.trans_code,
                t.shares, t.price,
                t.shares * coalesce(t.price, 0) AS value_usd,
                t.acquired, t.shares_after, t.direct, s.planned_10b5_1
            FROM ins_trans t
            JOIN ins_sub s USING (accn)
            LEFT JOIN ins_owner o USING (accn);
            SELECT count(*) FROM ins_stage
            """
        ).fetchone()[0]

        if rows == 0:
            return 0

        # Se borra por expediente completo y no por operación individual. Los mismos
        # formularios pueden haber entrado antes por la vía diaria de `form4.py`, que
        # los lee del índice de EDGAR con claves propias para cubrir el hueco hasta
        # que la SEC publica el trimestre. Borrando por expediente, esta carga —que es
        # la autoritativa y trae las validaciones de la SEC— sustituye limpiamente a
        # aquella en lugar de convivir con ella duplicando operaciones.
        con.execute(
            """
            DELETE FROM insider_transactions t
            WHERE t.accn IN (SELECT DISTINCT accn FROM ins_stage)
            """
        )
        con.execute(
            """
            INSERT INTO insider_transactions (
                accn, trans_sk, issuer_cik, ticker, owner_cik, owner_name,
                relationship, title, is_officer, is_director, is_ten_percent,
                trans_date, filing_date, trans_code, shares, price, value_usd,
                acquired, shares_after, direct, planned_10b5_1
            )
            SELECT accn, trans_sk, issuer_cik, ticker, owner_cik, owner_name,
                   relationship, title, is_officer, is_director, is_ten_percent,
                   trans_date, filing_date, trans_code, shares, price, value_usd,
                   acquired, shares_after, direct, planned_10b5_1
            FROM ins_stage
            """
        )
        return int(rows)
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def backfill(
    con: duckdb.DuckDBPyConnection,
    client: SecClient,
    quarters: int = 12,
) -> int:
    """Carga los últimos `quarters` trimestres de operaciones de insiders.

    Tres años bastan: lo que mide el motor es el comportamiento reciente y la
    participación actual. Un histórico más profundo pesaría más y no cambiaría
    ninguna puntuación.
    """
    done = set((get_watermark(con, "insider_quarters_done") or "").split(",")) - {""}
    total = 0

    available = client.quarters_since(2000)[-quarters:]
    for year, quarter in available:
        key = f"{year}q{quarter}"
        if key in done:
            continue
        rows = ingest_quarter(con, client, year, quarter)
        if rows == 0:
            console.print(f"  [dim]insiders {key}: no publicado todavía[/dim]")
            continue
        total += rows
        done.add(key)
        set_watermark(con, "insider_quarters_done", ",".join(sorted(done)))
        console.print(f"  [green]insiders {key}[/green]: {rows:,} operaciones")

    return total


def _date(column: str) -> str:
    """Las fechas vienen como `31-OCT-2025`: en inglés y en mayúsculas.

    Se usa `try_strptime` y no `strptime`: el segundo lanza excepción ante una
    fecha malformada y abortaría la carga del trimestre entero por una sola fila
    corrupta, que en estos ficheros aparece de vez en cuando.
    """
    return f"TRY_CAST(try_strptime(trim({column}), '%d-%b-%Y') AS DATE)"


def _p(path: Path) -> str:
    return str(path).replace("'", "''")
