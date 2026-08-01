"""Escritura de resultados: histórico versionable, CSV y JSON para el dashboard."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import ENGINE_NAMES_ES, REPORTS_DIR, ROOT, WEB_DATA_DIR, ensure_dirs
from .rankings import RANKINGS

# El histórico se versiona en git particionado por mes. Es la memoria del radar y
# lo único de la base de datos que no se puede reconstruir descargando otra vez.
HISTORY_DIR = ROOT / "data" / "history"


def write_history(totals: pd.DataFrame, as_of: dt.date) -> Path:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = HISTORY_DIR / f"scores_{as_of:%Y-%m}.parquet"

    if path.exists():
        previous = pd.read_parquet(path)
        previous = previous[pd.to_datetime(previous["as_of"]).dt.date != as_of]
        combined = pd.concat([previous, totals], ignore_index=True)
    else:
        combined = totals

    combined.to_parquet(path, index=False, compression="zstd")
    return path


def write_reports(rankings: dict[str, pd.DataFrame], alerts: pd.DataFrame, as_of: dt.date) -> Path:
    ensure_dirs()
    day_dir = REPORTS_DIR / f"{as_of:%Y-%m-%d}"
    day_dir.mkdir(parents=True, exist_ok=True)

    for ranking in RANKINGS:
        frame = rankings.get(ranking.key)
        if frame is None:
            continue
        frame.to_csv(day_dir / f"{ranking.key}.csv", index=False)

    alerts.to_csv(day_dir / "alertas.csv", index=False)
    return day_dir


def write_web_data(
    rankings: dict[str, pd.DataFrame],
    alerts: pd.DataFrame,
    frame: pd.DataFrame,
    calibration: pd.DataFrame,
    as_of: dt.date,
    weights: dict[str, float],
    freshness: pd.DataFrame | None = None,
) -> Path:
    """JSON que consume el dashboard estático.

    Se escribe todo en un único fichero por simplicidad: el dashboard es una
    página sin servidor y así solo hace una petición.
    """
    ensure_dirs()
    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)

    payload = {
        "generado": as_of.isoformat(),
        "universo": int(frame["total"].notna().sum()),
        "pesos": {ENGINE_NAMES_ES.get(k, k): round(v, 2) for k, v in weights.items()},
        "distribucion": _distribution(frame),
        "calibracion": _records(calibration),
        # Va en el payload y no solo en los registros de ejecución porque quien mira
        # el dashboard necesita saber que el flujo institucional que está leyendo
        # puede ser de hace cuatro meses.
        "frescura": _records(freshness if freshness is not None else pd.DataFrame()),
        "rankings": [
            {
                "clave": r.key,
                "titulo": r.title,
                "descripcion": r.description,
                "filas": _records(rankings.get(r.key, pd.DataFrame())),
            }
            for r in RANKINGS
        ],
        "alertas": _records(alerts),
    }

    path = WEB_DATA_DIR / "radar.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=_encode))
    return path


def _distribution(frame: pd.DataFrame) -> list[dict]:
    scored = frame[frame["total"].notna()]
    if scored.empty:
        return []
    counts = scored["band"].value_counts()
    return [{"banda": band, "empresas": int(n)} for band, n in counts.items()]


def _records(frame: pd.DataFrame) -> list[dict]:
    if frame is None or frame.empty:
        return []
    clean = frame.replace([np.inf, -np.inf], np.nan)
    return json.loads(clean.to_json(orient="records", date_format="iso"))


def _encode(value):
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if np.isnan(value) else float(value)
    return str(value)
