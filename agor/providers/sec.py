"""Cliente de SEC EDGAR.

Dos vías deliberadamente distintas:

1. **Financial Statement Data Sets** (`fsds_*`). Un ZIP por trimestre con todos
   los hechos numéricos de todos los declarantes. Es la vía masiva: 40 ficheros
   cubren diez años sin una sola petición por empresa y sin límites de ritmo.
   Su defecto es la latencia: la SEC publica cada trimestre con algunas semanas
   de retraso.

2. **API por empresa** (`company_facts`). Cara en peticiones pero inmediata. Se
   usa solo para las empresas que han presentado resultados después del último
   dataset trimestral disponible, que son pocas cada día.

La combinación da profundidad histórica y frescura sin pagar por ninguna de las
dos.
"""

from __future__ import annotations

import datetime as dt
import re
import zipfile
from pathlib import Path

import pandas as pd

from ..config import CACHE_DIR, SEC_REQUESTS_PER_SECOND
from .base import HttpClient, RateLimiter

SEC_BASE = "https://www.sec.gov"
SEC_DATA = "https://data.sec.gov"

# La SEC empezó a publicar los datasets estructurados en el segundo trimestre de
# 2009. Pedir antes de esa fecha devuelve 404.
FSDS_FIRST_YEAR = 2009
FSDS_FIRST_QUARTER = 2


class SecClient:
    def __init__(self, user_agent: str) -> None:
        if not user_agent or "@" not in user_agent:
            raise ValueError(
                "SEC_USER_AGENT debe incluir un email real. La SEC rechaza las "
                "peticiones anónimas. Revisa tu fichero .env."
            )
        self.http = HttpClient(
            user_agent=user_agent,
            rate_limiter=RateLimiter(SEC_REQUESTS_PER_SECOND),
            cache_namespace="sec",
        )
        self.fsds_dir = CACHE_DIR / "sec" / "fsds"
        self.fsds_dir.mkdir(parents=True, exist_ok=True)
        self.datasets_dir = CACHE_DIR / "sec" / "datasets"
        self.datasets_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Universo
    # ------------------------------------------------------------------
    def universe(self, cache_hours: float = 24.0) -> pd.DataFrame:
        """Todas las empresas con ticker y bolsa conocidos en EDGAR."""
        payload = self.http.get_json(
            f"{SEC_BASE}/files/company_tickers_exchange.json", cache_hours=cache_hours
        )
        frame = pd.DataFrame(payload["data"], columns=payload["fields"])
        frame = frame.rename(columns={"exchange": "exchange"})
        frame["cik"] = frame["cik"].astype("int64")
        frame["ticker"] = frame["ticker"].astype(str).str.strip().str.upper()
        frame["exchange"] = frame["exchange"].fillna("").astype(str).str.strip()
        # Un mismo CIK puede tener varias clases de acción (GOOG/GOOGL). Nos
        # quedamos con la primera por orden alfabético para tener una fila por
        # empresa; la clase concreta solo afecta al precio, no a las cuentas.
        frame = frame.sort_values(["cik", "ticker"]).drop_duplicates("cik", keep="first")
        return frame.reset_index(drop=True)

    # ------------------------------------------------------------------
    # Datasets trimestrales masivos
    # ------------------------------------------------------------------
    @staticmethod
    def quarters_since(start_year: int) -> list[tuple[int, int]]:
        today = dt.date.today()
        out: list[tuple[int, int]] = []
        year, quarter = max(start_year, FSDS_FIRST_YEAR), 1
        if year == FSDS_FIRST_YEAR:
            quarter = FSDS_FIRST_QUARTER
        while (year, quarter) <= (today.year, (today.month - 1) // 3 + 1):
            out.append((year, quarter))
            quarter += 1
            if quarter > 4:
                year, quarter = year + 1, 1
        return out

    def fsds_url(self, year: int, quarter: int) -> str:
        return f"{SEC_BASE}/files/dera/data/financial-statement-data-sets/{year}q{quarter}.zip"

    def fsds_download(self, year: int, quarter: int) -> Path | None:
        """Descarga un trimestre. Devuelve None si la SEC aún no lo ha publicado."""
        dest = self.fsds_dir / f"{year}q{quarter}.zip"
        if dest.exists() and dest.stat().st_size > 1_000_000:
            return dest
        url = self.fsds_url(year, quarter)
        if not self.http.head_exists(url):
            return None
        return self.http.download(url, dest)

    @staticmethod
    def fsds_extract_member(zip_path: Path, member: str, dest_dir: Path) -> Path:
        """Extrae un miembro del ZIP a disco.

        `num.txt` ronda los 540 MB descomprimidos, así que se extrae, se filtra y
        se borra en el mismo paso en lugar de cargarlo en memoria.
        """
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / member
        with zipfile.ZipFile(zip_path) as archive, archive.open(member) as source:
            with target.open("wb") as handle:
                while chunk := source.read(1 << 22):
                    handle.write(chunk)
        return target

    # ------------------------------------------------------------------
    # Insiders (formularios 3, 4 y 5)
    # ------------------------------------------------------------------
    def insider_download(self, year: int, quarter: int) -> Path | None:
        """Dataset trimestral de operaciones de insiders. Unos 8 MB."""
        dest = self.datasets_dir / f"{year}q{quarter}_form345.zip"
        if dest.exists() and dest.stat().st_size > 100_000:
            return dest
        url = (
            f"{SEC_BASE}/files/structureddata/data/insider-transactions-data-sets/"
            f"{year}q{quarter}_form345.zip"
        )
        if not self.http.head_exists(url):
            return None
        return self.http.download(url, dest)

    def daily_form4_accessions(self, day: dt.date) -> list[tuple[str, str]]:
        """Formularios 4 presentados un día concreto: (accession, ruta del documento).

        Se usa `master.idx` y no `form.idx` porque el primero viene delimitado por
        barras verticales y el segundo por posiciones fijas de columna, que cambian
        cuando un nombre de empresa es largo.

        El índice lista cada presentación una vez por cada CIK implicado, así que un
        formulario 4 aparece tanto bajo el emisor como bajo el directivo. Se
        deduplica por número de expediente para no descargar dos veces lo mismo.
        """
        quarter = (day.month - 1) // 3 + 1
        url = (
            f"{SEC_BASE}/Archives/edgar/daily-index/{day.year}/QTR{quarter}/"
            f"master.{day:%Y%m%d}.idx"
        )
        self.http.limiter.acquire()
        response = self.http.session.get(url, timeout=self.http.timeout)
        if response.status_code != 200:
            # Fin de semana, festivo o día aún no publicado.
            return []

        seen: dict[str, str] = {}
        for line in response.text.splitlines():
            parts = line.split("|")
            if len(parts) != 5 or parts[2].strip() not in ("4", "4/A"):
                continue
            path = parts[4].strip()
            accession = path.rsplit("/", 1)[-1].removesuffix(".txt")
            seen.setdefault(accession, path)
        return sorted(seen.items())

    def filing_text(self, path: str) -> str:
        """Documento completo de una presentación.

        Se descarga el fichero de texto agregado en lugar de localizar el XML: el XML
        va incrustado dentro, y así se resuelve con una sola petición en vez de dos
        (una para el índice del expediente y otra para el documento).
        """
        return self.http._request(f"{SEC_BASE}/Archives/{path.lstrip('/')}").text

    # ------------------------------------------------------------------
    # 13F
    # ------------------------------------------------------------------
    def list_13f_datasets(self) -> list[str]:
        """URLs de los datasets de 13F, leídas de la página oficial.

        No se construyen por patrón a propósito: la SEC cambió la nomenclatura en
        2024 y ahora publica rangos de tres meses (`01sep2025-30nov2025`) en lugar
        de trimestres (`2023q4`). Coexisten los dos formatos y es previsible que
        vuelva a cambiar, así que leer el índice es lo único robusto.
        """
        self.http.limiter.acquire()
        response = self.http.session.get(
            f"{SEC_BASE}/data-research/sec-markets-data/form-13f-data-sets", timeout=60
        )
        response.raise_for_status()
        found = re.findall(r'[^"\']*form-13f-data-sets/[^"\']*\.zip', response.text)
        urls: list[str] = []
        for href in found:
            url = href if href.startswith("http") else f"{SEC_BASE}/{href.lstrip('/')}"
            if url not in urls:
                urls.append(url)
        return urls

    def download_13f(self, url: str) -> Path:
        return self.http.download(url, self.datasets_dir / url.rsplit("/", 1)[-1])

    # ------------------------------------------------------------------
    # Puente CUSIP -> ticker
    # ------------------------------------------------------------------
    def fails_to_deliver(self, months: int = 6) -> list[Path]:
        """Ficheros de fallos de entrega, usados solo por su columna CUSIP/SYMBOL.

        Se publican dos por mes (quincenas `a` y `b`). Cada uno trae unos miles de
        pares CUSIP-ticker y acumulando varios se cubre casi todo el universo
        negociado. Los que aún no existen devuelven 404 y se ignoran.
        """
        out: list[Path] = []
        today = dt.date.today()
        for offset in range(months):
            month = today.month - offset
            year = today.year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            for half in ("a", "b"):
                name = f"cnsfails{year}{month:02d}{half}.zip"
                dest = self.datasets_dir / name
                if dest.exists() and dest.stat().st_size > 10_000:
                    out.append(dest)
                    continue
                url = f"{SEC_BASE}/files/data/fails-deliver-data/{name}"
                if not self.http.head_exists(url):
                    continue
                out.append(self.http.download(url, dest))
        return out

    # ------------------------------------------------------------------
    # API por empresa (incremental)
    # ------------------------------------------------------------------
    def company_facts(self, cik: int) -> dict | None:
        url = f"{SEC_DATA}/api/xbrl/companyfacts/CIK{cik:010d}.json"
        try:
            return self.http.get_json(url, cache_hours=12.0)
        except Exception:
            # Hay empresas en el fichero de tickers que nunca han presentado XBRL.
            return None

    def submissions(self, cik: int) -> dict | None:
        url = f"{SEC_DATA}/submissions/CIK{cik:010d}.json"
        try:
            return self.http.get_json(url, cache_hours=24.0)
        except Exception:
            return None

    def recent_filers(self, forms: tuple[str, ...] = ("10-K", "10-Q", "20-F")) -> pd.DataFrame:
        """Empresas que han presentado cuentas en los últimos días.

        Usa el índice diario de EDGAR, que es un fichero pequeño por día, para
        saber a quién merece la pena refrescar por API sin recorrer las 10.000.
        """
        rows: list[dict] = []
        today = dt.date.today()
        for offset in range(0, 10):
            day = today - dt.timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            quarter = (day.month - 1) // 3 + 1
            url = (
                f"{SEC_BASE}/Archives/edgar/daily-index/{day.year}/QTR{quarter}/"
                f"form.{day:%Y%m%d}.idx"
            )
            try:
                self.http.limiter.acquire()
                response = self.http.session.get(url, timeout=60)
                if response.status_code != 200:
                    continue
            except Exception:
                continue
            for line in response.text.splitlines():
                parts = [p.strip() for p in line.split("  ") if p.strip()]
                if len(parts) < 4 or parts[0] not in forms:
                    continue
                try:
                    rows.append({"form": parts[0], "cik": int(parts[2]), "date": day})
                except ValueError:
                    continue
        if not rows:
            return pd.DataFrame(columns=["form", "cik", "date"])
        return pd.DataFrame(rows).drop_duplicates("cik")
