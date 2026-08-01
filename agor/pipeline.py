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
from .engines.e04_management import ManagementEngine
from .engines.e05_moat import MoatEngine
from .engines.e06_megatrends import MegatrendsEngine
from .engines.e07_catalysts import CatalystsEngine
from .engines.e08_institutional import InstitutionalEngine
from .engines.e09_sentiment import SentimentEngine
from .engines.e10_technical import TechnicalEngine
from .engines.e11_historical_analogs import HistoricalAnalogsEngine
from .engines.e12_risk import RiskEngine
from .engines.e13_macro import MacroEngine
from .engines.e14_fundamental_momentum import FundamentalMomentumEngine
from .engines.e15_predictive_ai import PredictiveAIEngine
from .engines.e16_asymmetry import AsymmetryEngine, compute_conviction
from .features.analogs import enrich_with_analogs
from .features.macro import enrich_with_macro, fetch_macro_snapshot
from .features.moat import enrich_with_moat
from .features.llm_themes import enrich_with_llm_themes
from .features.ownership import institutional_metrics, insider_metrics
from .features.panel import build_panel
from .features.technical import compute_technicals
from .features.valuation import add_sector_medians, compute_valuation
from .ingest.prices import average_volume, benchmark_series, latest_prices
from .ingest.universe import investable_universe
from .providers.fred import FredClient
from .scoring.aggregate import aggregate, calibration_report, to_snapshot_tables
from .store import append, upsert

console = Console()

ENGINES = (
    QualityEngine(),
    FinancialHealthEngine(),
    FundamentalMomentumEngine(),
    ValuationEngine(),
    ManagementEngine(),
    MoatEngine(),
    MegatrendsEngine(),
    CatalystsEngine(),
    InstitutionalEngine(),
    SentimentEngine(),
    RiskEngine(),
    MacroEngine(),
    TechnicalEngine(),
    HistoricalAnalogsEngine(),
    PredictiveAIEngine(),
)

PENDING_ENGINES: tuple[str, ...] = ()


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


def enrich_with_ownership(
    con: duckdb.DuckDBPyConnection, snapshot: pd.DataFrame, as_of: dt.date
) -> pd.DataFrame:
    """Añade el comportamiento de directivos y el flujo institucional.

    Los importes en dólares se convierten aquí en proporciones. Sin ese paso, cien
    mil dólares de compra pesarían lo mismo en una empresa de 200 millones que en
    una de cien mil millones, y el ranking de convicción directiva se ordenaría por
    tamaño. El denominador preferido es la capitalización; cuando no hay precios se
    usa el activo total, que es peor referencia pero mantiene el motor en pie en la
    instalación sin clave de Polygon.
    """
    shares = _numeric(snapshot, "share_count")
    scale = _numeric(snapshot, "market_cap")
    if scale.notna().sum() == 0:
        scale = _numeric(snapshot, "assets")
    scale = scale.where(scale > 0)

    insiders = insider_metrics(con, as_of)
    if not insiders.empty:
        snapshot = snapshot.join(insiders, how="left")
        buys = _numeric(snapshot, "ins_buy_usd")
        sells = _numeric(snapshot, "ins_sell_usd")
        traded = buys + sells

        snapshot["ins_net_to_mcap"] = _numeric(snapshot, "ins_net_usd") / scale
        snapshot["ins_net_buyers_90d"] = _numeric(snapshot, "ins_buyers_90d") - _numeric(
            snapshot, "ins_sellers_90d"
        )
        # Sin ninguna operación en la ventana el reparto no existe, y forzarlo a cero
        # diría "solo vendieron", que es una afirmación distinta de "no hicieron nada".
        snapshot["ins_buy_share"] = (buys / traded).where(traded > 0)
        snapshot["ins_ownership_pct"] = _numeric(snapshot, "ins_shares_held") / shares.where(
            shares > 0
        )
        snapshot["ins_grant_dilution"] = _numeric(snapshot, "ins_granted_shares") / shares.where(
            shares > 0
        )
        snapshot["strat_net_to_mcap"] = _numeric(snapshot, "strat_net_usd") / scale

    institutional = institutional_metrics(con, as_of)
    if not institutional.empty:
        snapshot = snapshot.join(institutional, how="left")
        held = _numeric(snapshot, "inst_shares")
        pct = held / shares.where(shares > 0)
        # Las declaraciones se solapan entre gestoras y arrastran clases de acciones
        # distintas bajo el mismo CUSIP, así que la suma puede superar el capital
        # existente. Un exceso pequeño se recorta; uno grande significa que el mapeo
        # es incorrecto para esa empresa y se declara desconocido en lugar de
        # inventar un 300% de propiedad institucional.
        snapshot["inst_ownership_pct"] = pct.where(pct <= 1.5).clip(upper=1.0)

    return snapshot


def _numeric(frame: pd.DataFrame, column: str) -> pd.Series:
    if column in frame.columns:
        return pd.to_numeric(frame[column], errors="coerce")
    return pd.Series(np.nan, index=frame.index, dtype="float64")


def enrich_snapshot_with_macro(snapshot, settings, as_of: dt.date):
    """Añade régimen FRED si hay clave. Sin ella el motor 13 queda sin puntuar."""
    if not settings.has_macro:
        console.print(
            "[dim]Sin FRED_API_KEY: el motor 13 (macro) queda desactivado. "
            "Clave gratuita en https://fred.stlouisfed.org/docs/api/api_key.html[/dim]"
        )
        return snapshot
    try:
        client = FredClient(settings.fred_api_key)
        macro = fetch_macro_snapshot(client, as_of=as_of)
        regime = macro.get("macro_regime")
        if regime == regime:
            console.print(f"Régimen macro FRED: [bold]{regime:.0f}[/bold]/100")
        return enrich_with_macro(snapshot, macro)
    except Exception as exc:  # noqa: BLE001 — no debe tumbar la puntuación entera
        console.print(f"[yellow]FRED no disponible ({exc}); motor 13 omitido.[/yellow]")
        return snapshot


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
    snapshot = enrich_with_ownership(con, snapshot, as_of)
    snapshot = enrich_snapshot_with_macro(snapshot, settings, as_of)
    snapshot = enrich_with_moat(con, snapshot, settings, as_of)
    snapshot = enrich_with_llm_themes(con, snapshot, settings, as_of)
    snapshot = enrich_with_analogs(con, snapshot, as_of)
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
