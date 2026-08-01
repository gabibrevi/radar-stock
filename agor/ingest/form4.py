"""Formularios 4 del día, leídos uno a uno desde el índice diario de EDGAR.

Existe porque el dataset trimestral de insiders, que es la fuente principal, llega
con mucho retraso: la SEC lo publica por trimestres y en el peor momento del año va
cuatro meses por detrás. Para el resto del radar eso es tolerable —los fundamentales
también son trimestrales— pero para este motor no, porque la señal es precisamente
el momento: que varios directivos compren esta semana significa algo distinto de que
compraran en enero, y una alerta con cuatro meses de retraso invita a actuar con una
urgencia que el dato no respalda.

Este módulo cubre solo el hueco entre el último trimestre publicado y hoy. Cuando la
SEC publica el trimestre siguiente, el hueco se encoge solo y las filas de aquí
quedan sustituidas por las oficiales, que traen validaciones que este parseo no hace.

El coste es asumible pero no despreciable: hay unos 350 formularios 4 únicos por
sesión, que al ritmo autoimpuesto de 8 peticiones por segundo son unos 45 segundos.
Recuperar cuatro meses de golpe serían tres horas, así que la carga está acotada por
ejecución y avanza del día más antiguo al más reciente, retomando donde lo dejó.
"""

from __future__ import annotations

import datetime as dt
import re
import xml.etree.ElementTree as ET

import duckdb
import pandas as pd
from rich.console import Console

from ..providers.sec import SecClient
from ..store import get_watermark, set_watermark
from .insiders import KEPT_CODES

console = Console()

OWNERSHIP_BLOCK = re.compile(r"<ownershipDocument>.*?</ownershipDocument>", re.S)


def topup(
    con: duckdb.DuckDBPyConnection,
    client: SecClient,
    max_days: int = 5,
    horizon_days: int = 200,
) -> int:
    """Carga los formularios 4 de los días que faltan. Devuelve operaciones insertadas.

    El punto de partida es el día siguiente al último ya procesado o, la primera vez,
    el día siguiente al último dato del dataset trimestral: no tiene sentido descargar
    de uno en uno lo que la fuente masiva ya trajo de golpe.
    """
    start = _resume_from(con, horizon_days)
    today = dt.date.today()
    if start > today:
        return 0

    total = 0
    day = start
    processed = 0
    while day <= today and processed < max_days:
        filings = client.daily_form4_accessions(day)
        if not filings:
            # Fin de semana o festivo: se marca como hecho para no reintentarlo, pero
            # no cuenta contra el límite, que existe para acotar descargas reales.
            set_watermark(con, "form4_last_day", day.isoformat())
            day += dt.timedelta(days=1)
            continue

        rows: list[dict] = []
        for accession, path in filings:
            try:
                rows.extend(_parse(client.filing_text(path), accession))
            except (ET.ParseError, ValueError, KeyError):
                # Un formulario mal formado no debe abortar el día entero. Son raros y
                # el dataset trimestral los traerá corregidos más adelante.
                continue

        inserted = _merge(con, rows)
        total += inserted
        set_watermark(con, "form4_last_day", day.isoformat())
        console.print(
            f"  [green]form 4 {day}[/green]: {len(filings)} presentaciones, "
            f"{inserted:,} operaciones"
        )
        processed += 1
        day += dt.timedelta(days=1)

    return total


def _resume_from(con: duckdb.DuckDBPyConnection, horizon_days: int) -> dt.date:
    mark = get_watermark(con, "form4_last_day")
    if mark:
        return dt.date.fromisoformat(mark) + dt.timedelta(days=1)

    bulk_latest = con.execute(
        "SELECT max(filing_date) FROM insider_transactions"
    ).fetchone()[0]
    if bulk_latest is None:
        return dt.date.today() - dt.timedelta(days=horizon_days)
    if isinstance(bulk_latest, dt.datetime):
        bulk_latest = bulk_latest.date()
    # El dataset trimestral incluye alguna presentación aislada con fecha errónea en
    # el futuro, así que no se toma su máximo a ciegas.
    return min(bulk_latest, dt.date.today()) + dt.timedelta(days=1)


def _parse(text: str, accession: str) -> list[dict]:
    """Extrae las operaciones no derivadas de un formulario 4."""
    match = OWNERSHIP_BLOCK.search(text)
    if not match:
        return []
    root = ET.fromstring(match.group(0))

    if _text(root, "documentType") not in ("4", "4/A"):
        return []

    issuer = root.find("issuer")
    issuer_cik = _int(_text(issuer, "issuerCik"))
    if issuer_cik is None:
        return []
    symbol = (_text(issuer, "issuerTradingSymbol") or "").strip().upper()
    if symbol in ("", "NONE", "N/A", "NA", "-", "0"):
        symbol = None

    filed = _date(_text(root, "periodOfReport"))
    planned = _flag_anywhere(root, "aff10b5one")

    # Un formulario puede declararse por varios titulares. Se toma el primero, igual
    # que en la carga masiva, para no multiplicar la operación por firmante.
    owner = root.find("reportingOwner")
    owner_cik = _int(_text(owner.find("reportingOwnerId"), "rptOwnerCik")) if owner is not None else None
    owner_name = _text(owner.find("reportingOwnerId"), "rptOwnerName") if owner is not None else None
    rel = owner.find("reportingOwnerRelationship") if owner is not None else None
    is_officer = _bool(_text(rel, "isOfficer"))
    is_director = _bool(_text(rel, "isDirector"))
    is_ten = _bool(_text(rel, "isTenPercentOwner"))
    title = _text(rel, "officerTitle") or ""
    # Se reconstruye la misma cadena que usa el dataset trimestral para que cualquier
    # consulta escrita contra una fuente funcione igual contra la otra.
    relationship = ",".join(
        label
        for flag, label in ((is_director, "Director"), (is_officer, "Officer"), (is_ten, "TenPercentOwner"))
        if flag
    )

    table = root.find("nonDerivativeTable")
    if table is None:
        return []

    rows: list[dict] = []
    for index, node in enumerate(table.findall("nonDerivativeTransaction")):
        coding = node.find("transactionCoding")
        amounts = node.find("transactionAmounts")
        shares = _float(_value(amounts, "transactionShares"))
        if not shares:
            continue
        code = (_text(coding, "transactionCode") or "").strip().upper()
        # Se filtra por los mismos códigos que la carga trimestral. Sin esto, la tabla
        # tendría semánticas distintas según por qué vía entró cada fila, y una
        # consulta daría un resultado diferente antes y después de que la SEC publique
        # el trimestre.
        if code not in KEPT_CODES:
            continue
        price = _float(_value(amounts, "transactionPricePerShare"))
        rows.append(
            {
                "accn": accession,
                # Prefijo propio: distingue las filas de esta vía de las del dataset
                # trimestral, cuyas claves son surrogadas y numéricas.
                "trans_sk": f"d{index}",
                "issuer_cik": issuer_cik,
                "ticker": symbol,
                "owner_cik": owner_cik,
                "owner_name": owner_name,
                "relationship": relationship,
                "title": title,
                "is_officer": is_officer,
                "is_director": is_director,
                "is_ten_percent": is_ten,
                "trans_date": _date(_value(node, "transactionDate")),
                "filing_date": filed,
                "trans_code": code,
                "shares": shares,
                "price": price,
                "value_usd": shares * (price or 0.0),
                "acquired": (_value(amounts, "transactionAcquiredDisposedCode") or "").strip().upper()
                == "A",
                "shares_after": _float(
                    _value(node.find("postTransactionAmounts"), "sharesOwnedFollowingTransaction")
                ),
                "direct": (
                    _value(node.find("ownershipNature"), "directOrIndirectOwnership") or "D"
                ).strip().upper()
                == "D",
                "planned_10b5_1": planned,
            }
        )
    return rows


def _merge(con: duckdb.DuckDBPyConnection, rows: list[dict]) -> int:
    if not rows:
        return 0
    frame = pd.DataFrame(rows)
    con.register("_form4", frame)
    try:
        # Se borra por expediente completo, no por operación: si el mismo formulario
        # ya estaba, esta lectura lo reemplaza entero y no quedan mezcladas dos
        # versiones parciales.
        con.execute(
            "DELETE FROM insider_transactions WHERE accn IN (SELECT DISTINCT accn FROM _form4)"
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
            FROM _form4
            """
        )
    finally:
        con.unregister("_form4")
    return len(frame)


# ----------------------------------------------------------------------
# Lectura defensiva del XML
# ----------------------------------------------------------------------
# Los formularios 4 los generan decenas de programas distintos y los campos
# opcionales aparecen vacíos, ausentes o con el elemento presente pero sin texto. Se
# accede a todo con funciones que toleran las tres formas en lugar de comprobarlo en
# cada uso.
def _text(node, tag: str) -> str | None:
    if node is None:
        return None
    child = node.find(tag)
    return child.text.strip() if child is not None and child.text else None


def _value(node, tag: str) -> str | None:
    """Muchos campos envuelven el dato en un `<value>` junto a notas al pie."""
    if node is None:
        return None
    child = node.find(tag)
    if child is None:
        return None
    return _text(child, "value") or (child.text.strip() if child.text else None)


def _flag_anywhere(root, tag_lower: str) -> bool:
    for element in root.iter():
        if element.tag.lower() == tag_lower:
            return _bool(element.text)
    return False


def _bool(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("1", "true", "y", "yes")


def _int(raw: str | None) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _float(raw: str | None) -> float | None:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _date(raw: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat((raw or "")[:10])
    except ValueError:
        return None
