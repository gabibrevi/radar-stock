"""Configuración central de AGOR (AI Global Opportunity Radar)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
REPORTS_DIR = ROOT / "reports"
WEB_DATA_DIR = ROOT / "web" / "data"
DB_PATH = DATA_DIR / "agor.duckdb"

load_dotenv(ROOT / ".env")


# --------------------------------------------------------------------------
# Motores y pesos
# --------------------------------------------------------------------------
# Los pesos de la especificación original suman 118, no 100. Se conservan aquí
# tal cual para que las prioridades relativas queden explícitas y auditables, y
# se normalizan a 100 en WEIGHTS. Cambiar un número aquí es la única acción
# necesaria para reponderar el radar.
SPEC_WEIGHTS: dict[str, float] = {
    "e01_quality": 15.0,
    "e02_financial_health": 10.0,
    "e03_valuation": 10.0,
    "e04_management": 8.0,
    "e05_moat": 8.0,
    "e06_megatrends": 8.0,
    "e07_catalysts": 8.0,
    "e08_institutional": 8.0,
    "e09_sentiment": 5.0,
    "e10_technical": 7.0,
    "e11_historical_analogs": 5.0,
    "e12_risk": 5.0,
    "e13_macro": 3.0,
    "e14_fundamental_momentum": 5.0,
    "e15_predictive_ai": 3.0,
    "e16_asymmetry": 10.0,
}

ENGINE_NAMES_ES: dict[str, str] = {
    "e01_quality": "Calidad Fundamental",
    "e02_financial_health": "Salud Financiera",
    "e03_valuation": "Valoración Inteligente",
    "e04_management": "Calidad del Management",
    "e05_moat": "Ventaja Competitiva",
    "e06_megatrends": "Tendencias Globales",
    "e07_catalysts": "Catalizadores",
    "e08_institutional": "Institucional",
    "e09_sentiment": "Sentimiento",
    "e10_technical": "Técnico",
    "e11_historical_analogs": "Comparación Histórica",
    "e12_risk": "Riesgo",
    "e13_macro": "Macroeconomía",
    "e14_fundamental_momentum": "Momentum Fundamental",
    "e15_predictive_ai": "IA Predictiva",
    "e16_asymmetry": "Asimetría",
}


def normalized_weights(raw: dict[str, float] | None = None) -> dict[str, float]:
    raw = raw or SPEC_WEIGHTS
    total = sum(raw.values())
    return {k: v / total * 100.0 for k, v in raw.items()}


WEIGHTS = normalized_weights()


# --------------------------------------------------------------------------
# Bandas de clasificación
# --------------------------------------------------------------------------
# La especificación fija estos cortes y afirma que 95-100 corresponde al 0,5%
# superior. Un score absoluto no garantiza esa rareza, así que el radar mide la
# distribución real en cada ejecución y avisa cuando divergen (ver
# scoring/aggregate.py). Los cortes se mantienen absolutos para que el score de
# una empresa sea comparable entre días distintos, que es lo que necesita el
# módulo de aprendizaje.
BANDS: list[tuple[float, str]] = [
    (95.0, "Exceptional Buy"),
    (90.0, "Strong Buy"),
    (85.0, "Buy"),
    (80.0, "Watchlist Premium"),
    (75.0, "Watchlist"),
    (0.0, "No invertir"),
]

# Rareza esperada de cada banda, para el chequeo de calibración.
EXPECTED_BAND_SHARE: dict[str, float] = {
    "Exceptional Buy": 0.005,
    "Strong Buy": 0.02,
    "Buy": 0.05,
}


def band_for(score: float) -> str:
    for floor, name in BANDS:
        if score >= floor:
            return name
    return "No invertir"


# --------------------------------------------------------------------------
# Universo
# --------------------------------------------------------------------------
# Bolsas de EDGAR que aceptamos. OTC queda fuera por defecto: la calidad de
# reporting es mucho peor y llenaría los rankings de ruido.
ALLOWED_EXCHANGES = {"Nasdaq", "NYSE", "NYSEAmerican", "CBOE"}

# Sectores excluidos del radar. Los financieros y las utilities tienen una
# estructura contable en la que ROIC, margen bruto o FCF no significan lo mismo,
# y compararlos con el resto contamina la normalización. Se pueden reactivar.
EXCLUDED_SECTORS = {"Financiero", "Seguros", "Inmobiliario", "SPAC / Blank check"}

MIN_MARKET_CAP_USD = 50_000_000
MIN_QUARTERS_OF_HISTORY = 8

# Suelos para que una empresa reciba puntuación final.
#
# Sin ellos ocurre lo siguiente, comprobado: una empresa evaluada por un único
# motor con el 6% de sus componentes disponibles obtiene un 100, porque la media
# ponderada de un solo elemento es ese elemento. Como los rankings ordenan por
# puntuación, esas empresas —siempre las más pequeñas y opacas, justo las que
# menos datos publican— acaparan los Top 20 y el radar deja de servir.
#
# MIN_WEIGHT_APPLIED exige que los motores que sí han puntuado representen al
# menos la mitad del peso activo. MIN_OVERALL_COVERAGE exige además que dentro de
# esos motores haya datos reales suficientes.
MIN_WEIGHT_APPLIED = 0.50
MIN_OVERALL_COVERAGE = 0.30


# --------------------------------------------------------------------------
# Credenciales y límites de red
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Settings:
    sec_user_agent: str
    polygon_api_key: str
    anthropic_api_key: str
    fred_api_key: str

    @property
    def has_prices(self) -> bool:
        return bool(self.polygon_api_key)

    @property
    def has_llm(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_macro(self) -> bool:
        return bool(self.fred_api_key)


def load_settings() -> Settings:
    return Settings(
        sec_user_agent=os.getenv("SEC_USER_AGENT", "").strip(),
        polygon_api_key=os.getenv("POLYGON_API_KEY", "").strip(),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        fred_api_key=os.getenv("FRED_API_KEY", "").strip(),
    )


# La SEC permite 10 peticiones/segundo. Nos quedamos por debajo a propósito:
# perder el acceso por exceso costaría mucho más que los segundos que ahorramos.
SEC_REQUESTS_PER_SECOND = 8.0

# Plan gratuito de Polygon: 5 peticiones/minuto.
POLYGON_REQUESTS_PER_MINUTE = 5.0
POLYGON_FREE_TIER_YEARS = 2

# Índice de referencia para la fuerza relativa del motor técnico.
#
# Vive aquí y no como valor por defecto disperso porque la ingesta de precios
# descarta todo lo que no esté en el universo, y SPY es un ETF: no está en
# `universe` y sin esta constante el filtro lo borraría, dejando la fuerza
# relativa sin comparador y en silencio. Cambiar el benchmark exige tocar solo
# esta línea; la ingesta lo preservará automáticamente.
BENCHMARK_TICKER = "SPY"


def ensure_dirs() -> None:
    for d in (DATA_DIR, CACHE_DIR, REPORTS_DIR, WEB_DATA_DIR):
        d.mkdir(parents=True, exist_ok=True)
