"""Traducción de códigos SIC de la SEC a sectores utilizables.

La SEC no publica GICS. Lo único que trae cada empresa en EDGAR es su código
SIC, que es una clasificación de 1937 y agrupa cosas muy dispares. Este módulo
la reagrupa en sectores con sentido para comparar empresas entre sí, porque toda
la normalización del radar es relativa al sector: un margen bruto del 40% es
excelente en distribución y mediocre en software.

Los códigos de 4 dígitos tienen prioridad sobre los rangos, para poder separar
semiconductores o biotecnología de "manufactura" genérica.
"""

from __future__ import annotations

SIC_EXACT: dict[int, str] = {
    3674: "Semiconductores",
    3672: "Semiconductores",
    3559: "Equipamiento semiconductores",
    3827: "Equipamiento semiconductores",
    7372: "Software",
    7371: "Servicios IT",
    7373: "Servicios IT",
    7374: "Servicios IT / Datos",
    7370: "Internet / Software",
    7375: "Internet / Software",
    7379: "Servicios IT",
    2836: "Biotecnología",
    8731: "Biotecnología / I+D",
    2834: "Farmacéutico",
    2835: "Diagnóstico",
    3841: "Dispositivos médicos",
    3845: "Dispositivos médicos",
    3826: "Instrumentación",
    3661: "Equipos de comunicación",
    3663: "Equipos de comunicación",
    3669: "Equipos de comunicación",
    3711: "Automoción",
    3714: "Componentes automoción",
    3721: "Aeroespacial y Defensa",
    3724: "Aeroespacial y Defensa",
    3728: "Aeroespacial y Defensa",
    3761: "Aeroespacial y Defensa",
    3764: "Aeroespacial y Defensa",
    3812: "Defensa / Electrónica",
    5961: "Comercio electrónico",
    7389: "Servicios empresariales",
    6770: "SPAC / Blank check",
    6199: "Financiero",
    6798: "Inmobiliario",
    4911: "Utilities",
    4931: "Utilities",
    1311: "Petróleo y Gas",
    1040: "Minería",
    1090: "Minería",
}

SIC_RANGES: list[tuple[int, int, str]] = [
    (100, 999, "Agricultura"),
    (1000, 1099, "Minería"),
    (1200, 1299, "Minería"),
    (1300, 1399, "Petróleo y Gas"),
    (1400, 1499, "Minería"),
    (1500, 1799, "Construcción"),
    (2000, 2199, "Alimentación y Bebidas"),
    (2200, 2399, "Textil y Consumo"),
    (2400, 2599, "Industrial"),
    (2600, 2699, "Papel y Envases"),
    (2700, 2799, "Medios y Editorial"),
    (2800, 2899, "Química"),
    (2900, 2999, "Refino"),
    (3000, 3299, "Industrial"),
    (3300, 3399, "Metalurgia"),
    (3400, 3569, "Industrial"),
    (3570, 3579, "Hardware"),
    (3580, 3599, "Industrial"),
    (3600, 3699, "Electrónica"),
    (3700, 3799, "Transporte y Automoción"),
    (3800, 3899, "Instrumentación"),
    (3900, 3999, "Manufactura diversa"),
    (4000, 4499, "Transporte y Logística"),
    (4500, 4599, "Aerolíneas"),
    (4600, 4799, "Transporte y Logística"),
    (4800, 4899, "Telecomunicaciones"),
    (4900, 4999, "Utilities"),
    (5000, 5199, "Distribución"),
    (5200, 5799, "Retail"),
    (5800, 5899, "Restauración"),
    (5900, 5999, "Retail"),
    (6000, 6199, "Financiero"),
    (6200, 6299, "Mercados de capitales"),
    (6300, 6499, "Seguros"),
    (6500, 6599, "Inmobiliario"),
    (6600, 6799, "Financiero"),
    (7000, 7099, "Hoteles y Ocio"),
    (7200, 7299, "Servicios al consumo"),
    (7300, 7399, "Servicios IT"),
    (7500, 7699, "Servicios"),
    (7800, 7999, "Entretenimiento"),
    (8000, 8099, "Salud / Servicios"),
    (8200, 8299, "Educación"),
    (8300, 8399, "Servicios sociales"),
    (8400, 8999, "Servicios profesionales"),
    (9100, 9999, "Sector público"),
]

# Sectores donde el crecimiento estructural es la norma. El radar los usa para
# ajustar la severidad de algunos filtros, no para dar puntos gratis.
GROWTH_SECTORS = {
    "Semiconductores",
    "Equipamiento semiconductores",
    "Software",
    "Internet / Software",
    "Servicios IT",
    "Servicios IT / Datos",
    "Biotecnología",
    "Biotecnología / I+D",
    "Comercio electrónico",
    "Aeroespacial y Defensa",
    "Dispositivos médicos",
}

UNKNOWN = "Sin clasificar"


def sector_from_sic(sic: int | str | None) -> str:
    if sic in (None, "", "None"):
        return UNKNOWN
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return UNKNOWN

    if code in SIC_EXACT:
        return SIC_EXACT[code]
    for low, high, name in SIC_RANGES:
        if low <= code <= high:
            return name
    return UNKNOWN
