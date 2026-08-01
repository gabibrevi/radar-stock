"""Motor 4 — Calidad del Management (peso 8).

Este motor mide una sola cosa, pero la mide sobre hechos verificables: qué hacen
los directivos con su propio dinero. No opina sobre si el consejero delegado es
visionario ni sobre su historial; eso queda para los motores cualitativos, que
necesitan un modelo de lenguaje. Aquí solo hay operaciones declaradas ante la SEC.

Tres criterios separan esta señal del ruido que suele venderse como "compras de
directivos":

- **Solo cuentan las compras en mercado abierto** (código `P`). Las acciones
  concedidas como retribución (`A`) y los ejercicios de opciones (`M`, `X`) no son
  decisiones de inversión: llegan por calendario. Contarlas convierte cualquier plan
  de compensación en una señal falsa de confianza.

- **Solo cuentan las ventas discrecionales.** Una venta programada por un plan
  10b5-1 se firmó meses antes, cuando el directivo no sabía lo que sabe hoy. En un
  trimestre real, las ventas programadas eran 12.928 operaciones por 14.400 millones
  y las discrecionales 9.537 por 119.500 millones: mezclarlas hace que quien solo
  diversifica su patrimonio parezca estar huyendo.

- **Los accionistas del 10% que no ocupan cargo quedan fuera** y van al motor 8. Sus
  mayores movimientos son participaciones estratégicas de empresas y fondos
  soberanos, no confianza de la dirección en el negocio que gestiona.

Lo que este motor no puede ver, y conviene no olvidar al leerlo: si el fundador
sigue al mando, la rotación del equipo directivo, y si las adquisiciones pasadas
crearon o destruyeron valor. Nada de eso está en un formulario 4.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Component, ComponentEngine, ScoringContext


def _log_scale(values: pd.Series) -> pd.Series:
    """Comprime el importe conservando el signo.

    Las compras de insiders se reparten en órdenes de magnitud muy distintos: la
    mediana ronda decenas de miles de dólares y la cola llega a cientos de millones.
    Sin comprimir, media docena de operaciones enormes aplasta al resto del universo
    contra el cero y la escala deja de distinguir entre no comprar nada y comprar de
    verdad.
    """
    return np.sign(values) * np.log1p(values.abs())


class ManagementEngine(ComponentEngine):
    engine_id = "e04_management"
    # Un motor que solo dispone de datos de insiders no puede exigir mucha
    # cobertura: en la mayoría de empresas no hay ninguna operación en seis meses, y
    # eso es información legítima, no un hueco.
    min_coverage = 0.30

    components = (
        # Compra neta sobre el valor de la empresa. Es la medida central: cien mil
        # dólares en una empresa de 200 millones dicen mucho más que diez millones
        # en una de cien mil millones.
        #
        # La escala es absoluta y simétrica en torno a cero, no un percentil, y esa
        # elección corrige un defecto real. En la mayoría de las empresas no hay
        # ninguna operación en seis meses, y con percentiles todo ese bloque empatado
        # caía a unos 30 puntos. Como las empresas que no aparecen en el dataset no
        # puntúan y su peso se reparte entre los demás motores, quedaban por encima
        # de las que sí tenían datos: publicar información penalizaba. Con una escala
        # centrada, "no ha pasado nada" vale exactamente 50 y solo se separa de ahí
        # quien compra o quien vende por decisión propia.
        Component(
            "net_buying",
            "ins_net_to_mcap",
            "Compra neta de directivos sobre capitalización",
            3.0,
            absolute=(-0.005, 0.005),
        ),
        # Amplitud con signo: directivos que compran menos los que venden por decisión
        # propia. Una compra aislada puede ser una circunstancia personal; cuatro a la
        # vez es una lectura compartida del negocio, y cuatro vendiendo también.
        Component(
            "cluster_buying",
            "ins_net_buyers_90d",
            "Directivos comprando menos directivos vendiendo (90 días)",
            2.5,
            absolute=(-4.0, 4.0),
        ),
        # Que compre un directivo ejecutivo, y no solo un consejero: quien dirige la
        # operación diaria conoce el trimestre en curso.
        Component(
            "officer_conviction",
            "ins_officer_bought",
            "Ha comprado un directivo ejecutivo",
            2.0,
            absolute=(-1.0, 1.0),
        ),
        Component(
            "buy_sell_balance",
            "ins_buy_share",
            "Peso de la compra frente a la venta discrecional",
            2.0,
            absolute=(0.0, 1.0),
        ),
        # Qué parte de lo vendido estaba programado. Un 100% significa que nadie
        # vendió por decisión propia, aunque el volumen vendido fuese alto.
        Component(
            "selling_is_planned",
            "ins_planned_sell_share",
            "Ventas programadas sobre el total vendido",
            1.0,
            absolute=(0.0, 1.0),
        ),
        # Capital comprometido. Solo titularidad directa: sumar la indirecta contaría
        # dos veces las mismas acciones a través de fideicomisos y sociedades.
        Component(
            "skin_in_the_game",
            "ins_ownership_pct",
            "Participación directa de la dirección",
            2.5,
        ),
        # Dilución por retribución en acciones: reparte el futuro entre más manos y
        # es el coste que casi nunca aparece en la cuenta de resultados.
        Component(
            "grant_dilution",
            "ins_grant_dilution",
            "Dilución por acciones entregadas como retribución",
            1.0,
            higher_is_better=False,
            transform=_log_scale,
        ),
    )

    def adjust(
        self, ctx: ScoringContext, score: pd.Series, components: pd.DataFrame
    ) -> pd.Series:
        """Venta masiva y discrecional sin ninguna compra pone techo a la nota.

        Es el patrón que más veces precede a un deterioro que todavía no aparece en
        las cuentas: los directivos venden por decisión propia, no por calendario, y
        ninguno compra. Por bien que salgan los demás componentes, no puede quedar
        como un buen equipo alineado.
        """
        sells = ctx.column("ins_sell_usd")
        buys = ctx.column("ins_buy_usd")
        mcap = ctx.column("market_cap")
        heavy = (sells / mcap.replace(0, np.nan) > 0.01) & (buys <= 0)
        return score.mask(heavy.fillna(False) & (score > 40.0), 40.0)
