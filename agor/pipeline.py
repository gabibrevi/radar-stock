"""Orquestación de una ejecución completa del radar."""

from __future__ import annotations

import datetime as dt

import duckdb
import numpy as np
import pandas as pd
from rich.console import Console

from .config import MIN_QUARTERS_OF_HISTORY, WEIGHTS, load_settings
from .engines.base import EngineResult, ScoringContext
from .engines.e01_quality import QualityEngine
from .engines.e02_financial_health import FinancialHealthEngine
from .engines.e03_valuation import ValuationEngine
from .engines.e10_technical import TechnicalEngine
from .engines.e14_fundamental_momentum import FundamentalMomentumEngine
from .engines.e16_asymmetry import AsymmetryEngine, compute_conviction
from .features.panel import build_panel
from .features.technical import compute_technicals
from .features.valuation import add_sector_medians, compute_valuation
from .ingest.prices import average_volume, benchmark_series, latest_prices
from .ingest.universe import investable_universe
from .scoring.aggregate import aggregate, calibration_report, to_snapshot_tables
from .store import append, upsert

console = Console()

# Los motores implementados. Los nueve restantes de la especificación necesitan
# datos cualitativos o alternativos y se irán incorporando; mientras no existan,
# su peso se redistribuye automáticamente entre estos.
ENGINES = (
    QualityEngine(),
    FinancialHealthEngine(),
    FundamentalMomentumEngine(),
    ValuationEngine(),
    TechnicalEngine(),
)

PENDING_ENGINES = (
    "e04_management",
    "e05_moat",
    "e06_megatrends",
    "e07_catalysts",
    "e08_institutional",
    "e09_sentiment",
    "e11_historical_analogs",
    "e12_risk",
    "e13_macro",
    "e15_predictive_ai",
)


def build_snapshot(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """Una fila por empresa con su estado más reciente conocido.

    Se exige un mínimo de trimestres de historia porque casi todas las métricas del
    radar son comparaciones temporales. Una empresa recién salida a bolsa no puede
    puntuarse en crecimiento a tres años, y forzarlo produciría un ranking de
    salidas a bolsa recientes con datos inventados.
    """
    universe = investable_universe(con).set_index("cik")
    if universe.empty:
        return pd.DataFrame()

    panel = build_panel(con)
    if panel.empty:
        return pd.DataFrame()

    panel = panel[panel["cik"].isin(universe.index)]
    quarters = panel.groupby("cik")["period_end"].transform("count")
    panel = panel[quarters >= MIN_QUARTERS_OF_HISTORY]

    latest = panel.sort_values("period_end").groupby("cik", sort=False).tail(1)
    latest = latest.set_index("cik")

    snapshot = universe.join(latest, how="inner")
    snapshot["quarters_of_history"] = (
        panel.groupby("cik")["period_end"].count().reindex(snapshot.index)
    )
    return snapshot


def enrich_with_prices(
    con: duckdb.DuckDBPyConnection, snapshot: pd.DataFrame
) -> tuple[pd.DataFrame, bool]:
    """Añade métricas técnicas y de valoración si hay precios cargados."""
    count = con.execute("SELECT count(*) FROM prices").fetchone()[0]
    if count == 0:
        console.print(
            "[yellow]Sin precios cargados: los motores 3, 10 y 16 quedan sin puntuar.[/yellow]\n"
            "[dim]Añade POLYGON_API_KEY al fichero .env y ejecuta `radar precios`.[/dim]"
        )
        return snapshot, False

    prices = latest_prices(con)
    technicals = compute_technicals(prices, benchmark_series(con))
    volumes = average_volume(con).set_index("ticker")

    by_ticker = snapshot.reset_index().set_index("ticker")
    by_ticker = by_ticker.join(technicals, how="left").join(volumes, how="left")
    snapshot = by_ticker.reset_index().set_index("cik")

    snapshot = add_sector_medians(snapshot, snapshot["sector"])
    valuation = compute_valuation(snapshot, snapshot["sector"])
    snapshot = snapshot.join(valuation, how="left")
    return snapshot, True


def score(
    con: duckdb.DuckDBPyConnection,
    as_of: dt.date | None = None,
    persist: bool = True,
) -> tuple[pd.DataFrame, list[EngineResult], pd.DataFrame]:
    as_of = as_of or dt.date.today()
    settings = load_settings()

    snapshot = build_snapshot(con)
    if snapshot.empty:
        raise RuntimeError(
            "No hay datos suficientes para puntuar. Ejecuta primero `radar fundamentales`."
        )
    console.print(f"Empresas con datos suficientes: [bold]{len(snapshot):,}[/bold]")

    snapshot, has_prices = enrich_with_prices(con, snapshot)
    ctx = ScoringContext(
        snapshot=snapshot,
        groups=snapshot["sector"].fillna("Sin clasificar"),
        has_prices=has_prices,
        has_llm=settings.has_llm,
    )

    # Primera pasada: todos los motores salvo el de asimetría, que necesita saber
    # qué han dicho los demás para calcular la convicción.
    results = [engine.run(ctx) for engine in ENGINES]

    scores = pd.DataFrame({r.engine_id: r.score for r in results}, index=ctx.index)
    coverage = pd.DataFrame({r.engine_id: r.coverage for r in results}, index=ctx.index)
    snapshot["conviction"] = compute_conviction(scores, coverage)

    ctx = ScoringContext(
        snapshot=snapshot,
        groups=ctx.groups,
        has_prices=has_prices,
        has_llm=settings.has_llm,
    )
    results.append(AsymmetryEngine().run(ctx))

    frame = aggregate(results, ctx, weights=_active_weights())
    metadata = snapshot[["ticker", "name", "sector"]].copy()
    metadata["market_cap"] = (
        snapshot["market_cap"] if "market_cap" in snapshot.columns else np.nan
    )

    totals, detail = to_snapshot_tables(frame, results, metadata, as_of)

    if persist:
        upsert(con, "score_totals", totals, ["as_of", "cik"])
        upsert(con, "score_snapshots", detail, ["as_of", "cik", "engine_id"])

    console.print("\n[bold]Calibración de bandas[/bold]")
    report = calibration_report(frame)
    if not report.empty:
        console.print(report.to_string(index=False))

    return frame.join(snapshot, rsuffix="_snap"), results, totals


def _active_weights() -> dict[str, float]:
    """Pesos limitados a los motores implementados, renormalizados a 100.

    Se hace explícito en lugar de dejar que la agregación lo resuelva sola, para
    que quede registrado con qué reparto real se generó cada ejecución. Cuando se
    añada un motor nuevo, el reparto cambia y los scores no serán directamente
    comparables con los anteriores: eso hay que saberlo, no descubrirlo.
    """
    active = {k: v for k, v in WEIGHTS.items() if k not in PENDING_ENGINES}
    total = sum(active.values())
    return {k: v / total * 100.0 for k, v in active.items()}
