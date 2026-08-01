"""Interfaz de línea de comandos de AGOR.

Los comandos están en español y son deliberadamente pocos. Para el uso normal solo
hace falta uno: `todo`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from rich.console import Console
from rich.table import Table

from .config import ENGINE_NAMES_ES, SPEC_WEIGHTS, ensure_dirs, load_settings
from .ingest.fundamentals import backfill as backfill_fundamentals
from .ingest.holdings import backfill as backfill_holdings
from .ingest.holdings import refresh_cusip_map
from .ingest.insiders import backfill as backfill_insiders
from .ingest.prices import backfill_prices
from .ingest.universe import refresh_universe, universe_report
from .output import alerts as alerts_module
from .output.export import write_history, write_reports, write_web_data
from .output.rankings import RANKINGS, build_rankings
from .pipeline import PENDING_ENGINES, _active_weights, score
from .providers.polygon import PolygonClient
from .providers.sec import SecClient
from .scoring.aggregate import calibration_report
from .store import db, freshness, summary

console = Console()


def cmd_estado(args) -> int:
    settings = load_settings()
    console.print("[bold]Configuración[/bold]")
    console.print(f"  SEC_USER_AGENT   {'OK' if settings.sec_user_agent else 'FALTA (obligatorio)'}")
    console.print(
        f"  POLYGON_API_KEY  {'OK' if settings.has_prices else 'ausente → sin motores 3, 10 y 16'}"
    )
    console.print(
        f"  ANTHROPIC_API_KEY {'OK' if settings.has_llm else 'ausente → motores cualitativos desactivados'}"
    )

    with db() as con:
        console.print("\n[bold]Contenido de la base de datos[/bold]")
        console.print(summary(con).to_string(index=False))
        console.print("\n[bold]Frescura de cada fuente[/bold]")
        console.print(freshness(con).to_string(index=False))
        console.print(
            "[dim]Los datasets de insiders y de 13F se publican por trimestres: "
            "pueden ir varios meses por detrás. Los motores 4 y 8 describen el último "
            "trimestre publicado, no el día de hoy.[/dim]"
        )
        try:
            console.print("\n[bold]Universo por bolsa[/bold]")
            console.print(universe_report(con).to_string(index=False))
        except Exception:
            console.print("[dim]Universo todavía no construido.[/dim]")

    console.print("\n[bold]Motores[/bold]")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Motor")
    table.add_column("Nombre")
    table.add_column("Peso spec", justify="right")
    table.add_column("Peso activo", justify="right")
    table.add_column("Estado")
    active = _active_weights()
    for engine_id, spec_weight in SPEC_WEIGHTS.items():
        pending = engine_id in PENDING_ENGINES
        table.add_row(
            engine_id,
            ENGINE_NAMES_ES[engine_id],
            f"{spec_weight:.0f}",
            "—" if pending else f"{active.get(engine_id, 0):.1f}",
            "[yellow]pendiente[/yellow]" if pending else "[green]activo[/green]",
        )
    console.print(table)
    console.print(
        "\n[dim]Los pesos de la especificación suman 118, no 100. El radar los "
        "normaliza y reparte además el peso de los motores pendientes entre los "
        "activos.[/dim]"
    )
    return 0


def cmd_universo(args) -> int:
    settings = load_settings()
    client = SecClient(settings.sec_user_agent)
    with db() as con:
        frame = refresh_universe(con, client)
        console.print(f"Universo actualizado: [bold]{len(frame):,}[/bold] empresas con ticker")
        console.print(universe_report(con).to_string(index=False))
    return 0


def cmd_fundamentales(args) -> int:
    settings = load_settings()
    client = SecClient(settings.sec_user_agent)
    with db() as con:
        console.print(
            f"Descargando datasets trimestrales de la SEC desde {args.desde}. "
            "La primera vez son unos 3 GB y puede tardar cerca de una hora."
        )
        rows = backfill_fundamentals(con, client, args.desde, keep_zips=args.conservar_zips)
        console.print(f"\n[bold green]Hechos financieros cargados: {rows:,}[/bold green]")
    return 0


def cmd_propiedad(args) -> int:
    """Insiders y 13F: los datos que alimentan los motores 4 y 8."""
    settings = load_settings()
    client = SecClient(settings.sec_user_agent)
    with db() as con:
        console.print("[bold]Operaciones de directivos[/bold] (unos 8 MB por trimestre)")
        rows = backfill_insiders(con, client, quarters=args.trimestres_insiders)
        console.print(f"  operaciones cargadas: {rows:,}")

        console.print(
            "\n[bold]Puente CUSIP → ticker[/bold]\n"
            "[dim]Los 13F identifican las posiciones por CUSIP y las tablas oficiales "
            "son de licencia comercial. Se construye con los ficheros de fallos de "
            "entrega de la SEC, que son públicos.[/dim]"
        )
        pairs = refresh_cusip_map(con, client, months=args.meses_cusip)
        console.print(f"  pares conocidos: {pairs:,}")

        console.print(
            "\n[bold]Posiciones institucionales 13F[/bold]\n"
            "[dim]Unos 80 MB comprimidos y 320 MB en texto por trimestre. "
            "Hacen falta al menos dos para poder medir entradas y salidas.[/dim]"
        )
        rows = backfill_holdings(con, client, files=args.trimestres_13f)
        console.print(f"  posiciones cargadas: {rows:,}")
    return 0


def cmd_precios(args) -> int:
    settings = load_settings()
    if not settings.has_prices:
        console.print(
            "[red]Falta POLYGON_API_KEY.[/red] Consigue una clave gratuita en "
            "https://polygon.io/dashboard/api-keys y añádela al fichero .env"
        )
        return 1
    client = PolygonClient(settings.polygon_api_key)
    with db() as con:
        console.print(
            "El plan gratuito permite 5 peticiones por minuto. Cada petición trae "
            "un día completo de mercado."
        )
        rows = backfill_prices(con, client, years=args.anios, max_days=args.max_dias)
        console.print(f"\n[bold green]Cotizaciones cargadas: {rows:,}[/bold green]")
    return 0


def cmd_puntuar(args) -> int:
    as_of = dt.date.fromisoformat(args.fecha) if args.fecha else dt.date.today()
    with db() as con:
        frame, results, totals = score(con, as_of=as_of)

        rankings = build_rankings(frame)
        detected = alerts_module.detect(con, frame, as_of)
        if not detected.empty:
            from .store import upsert

            upsert(con, "alerts", detected, ["as_of", "cik", "rule_id"])

        history_path = write_history(totals, as_of)
        report_dir = write_reports(rankings, detected, as_of)
        web_path = write_web_data(
            rankings,
            detected,
            frame,
            calibration_report(frame),
            as_of,
            _active_weights(),
            freshness(con),
        )

    _print_rankings(rankings)
    console.print(f"\n[bold]Alertas generadas:[/bold] {len(detected)}")
    for rule_id, label in alerts_module.PENDING_RULES.items():
        console.print(f"  [yellow]pendiente[/yellow] {label}")

    console.print(f"\nHistórico    {history_path}")
    console.print(f"Informes CSV {report_dir}")
    console.print(f"Dashboard    {web_path}")
    return 0


def cmd_todo(args) -> int:
    settings = load_settings()
    ensure_dirs()

    client = SecClient(settings.sec_user_agent)
    with db() as con:
        backfill_fundamentals(con, client, args.desde, keep_zips=args.conservar_zips)
        refresh_universe(con, client)
        # El universo tiene que existir antes que los 13F: el mapeo de posiciones
        # pasa por el ticker, y sin universo no habría nada con lo que emparejar.
        backfill_insiders(con, client, quarters=8)
        refresh_cusip_map(con, client, months=6)
        backfill_holdings(con, client, files=4)

    if settings.has_prices:
        polygon = PolygonClient(settings.polygon_api_key)
        with db() as con:
            backfill_prices(con, polygon, max_days=args.max_dias_precios)

    return cmd_puntuar(argparse.Namespace(fecha=None))


def _print_rankings(rankings: dict) -> None:
    for ranking in RANKINGS:
        frame = rankings.get(ranking.key)
        console.print(f"\n[bold cyan]{ranking.title}[/bold cyan]")
        if frame is None or frame.empty:
            console.print("  [dim]sin empresas que cumplan el criterio[/dim]")
            continue
        columns = [c for c in ("ticker", "name", "sector", "total", "band") if c in frame.columns]
        console.print(frame[columns].head(20).to_string(index=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="radar",
        description="AGOR — radar de oportunidades de inversión a 5-10 años",
    )
    sub = parser.add_subparsers(dest="comando", required=True)

    p = sub.add_parser("estado", help="Muestra configuración, datos y motores")
    p.set_defaults(func=cmd_estado)

    p = sub.add_parser("universo", help="Actualiza la lista de empresas")
    p.set_defaults(func=cmd_universo)

    p = sub.add_parser("fundamentales", help="Descarga los datos financieros de la SEC")
    p.add_argument("--desde", type=int, default=2015, help="Año inicial (por defecto 2015)")
    p.add_argument("--conservar-zips", action="store_true", dest="conservar_zips")
    p.set_defaults(func=cmd_fundamentales)

    p = sub.add_parser(
        "propiedad", help="Descarga operaciones de directivos (motor 4) y 13F (motor 8)"
    )
    p.add_argument("--trimestres-insiders", type=int, default=8, dest="trimestres_insiders")
    p.add_argument("--trimestres-13f", type=int, default=4, dest="trimestres_13f")
    p.add_argument("--meses-cusip", type=int, default=6, dest="meses_cusip")
    p.set_defaults(func=cmd_propiedad)

    p = sub.add_parser("precios", help="Descarga cotizaciones desde Polygon")
    p.add_argument("--anios", type=int, default=2)
    p.add_argument("--max-dias", type=int, default=None, dest="max_dias")
    p.set_defaults(func=cmd_precios)

    p = sub.add_parser("puntuar", help="Ejecuta los motores y genera los rankings")
    p.add_argument("--fecha", type=str, default=None)
    p.set_defaults(func=cmd_puntuar)

    p = sub.add_parser("todo", help="Ingesta completa y puntuación")
    p.add_argument("--desde", type=int, default=2015)
    p.add_argument("--conservar-zips", action="store_true", dest="conservar_zips")
    p.add_argument("--max-dias-precios", type=int, default=None, dest="max_dias_precios")
    p.set_defaults(func=cmd_todo)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
