"""Motor 8 — Institucional (peso 8).

Mide si el dinero profesional está descubriendo el valor ahora, cuando todavía
sirve de algo, o si llegó hace años y ya está todo dentro del precio.

La pregunta que hace este motor está deliberadamente invertida respecto a cómo se
suele usar el 13F. Que una empresa tenga el 95% del capital en manos
institucionales no es una virtud para este radar: significa que las mil gestoras
que la iban a descubrir ya la descubrieron, y que el recorrido que buscamos
—convertirse en la próxima Nvidia desde una posición ignorada— en gran medida ya
ocurrió. Lo valioso es la combinación de validación suficiente y sitio para crecer:
que haya profesionales dentro, que estén entrando más, y que todavía queden
muchos fuera.

Límites del dato, que son severos y hay que tener presentes:

- Solo declaran las gestoras con más de 100 millones bajo gestión.
- Solo posiciones largas. Un fondo puede tener una participación enorme y estar
  cubierto con derivados sin que aquí se vea nada.
- Se publica con hasta 45 días de retraso sobre el cierre del trimestre, y el
  radar solo puede comparar trimestres completos. En el mejor de los casos este
  motor mira entre uno y cuatro meses atrás. Jamás debe leerse como "lo que están
  comprando los fondos hoy".
"""

from __future__ import annotations

import pandas as pd

from .base import Component, ComponentEngine, ScoringContext

# Propiedad institucional que se considera el punto óptimo: suficiente para que el
# negocio esté validado por analistas profesionales, con sitio de sobra para que
# entren los que faltan. Por debajo hay poca validación; muy por encima, la historia
# ya está contada y descontada.
OWNERSHIP_SWEET_SPOT = 0.45


def _distance_to_sweet_spot(values: pd.Series) -> pd.Series:
    """Convierte la U invertida en algo monótono.

    Los componentes del motor son monótonos por construcción: más es mejor o menos
    es mejor. La propiedad institucional no es ninguna de las dos cosas, así que se
    puntúa la cercanía al punto óptimo en lugar del nivel, y con el signo cambiado
    para que "más cerca" siga significando "mejor".
    """
    return -(values - OWNERSHIP_SWEET_SPOT).abs()


class InstitutionalEngine(ComponentEngine):
    engine_id = "e08_institutional"
    min_coverage = 0.35

    components = (
        # Variación del número de gestoras. Es la medida central y la única inmune a
        # los desdoblamientos de acciones, que multiplican las acciones declaradas
        # por cada fondo sin que nadie haya comprado nada.
        Component(
            "manager_inflow",
            "inst_holders_change_pct",
            "Variación del número de gestoras",
            3.0,
        ),
        # Aperturas menos cierres de posición sobre la base anterior: distingue a la
        # gestora que amplía de la que descubre el valor por primera vez.
        Component(
            "net_positions",
            "inst_net_position_ratio",
            "Aperturas netas de posición",
            2.5,
        ),
        Component(
            "fresh_discovery",
            "inst_new_position_ratio",
            "Gestoras que abren posición por primera vez",
            1.5,
        ),
        # Nivel de propiedad institucional, puntuado como cercanía al punto óptimo.
        Component(
            "room_to_grow",
            "inst_ownership_pct",
            "Margen para que entre más dinero profesional",
            2.0,
            transform=_distance_to_sweet_spot,
        ),
        # Un suelo de validación: que exista un número mínimo de casas siguiendo el
        # valor. Cero gestoras en una cotizada estadounidense no es una oportunidad
        # oculta, casi siempre es un problema de liquidez o de calidad contable.
        Component(
            "validation_floor",
            "inst_holders",
            "Gestoras que ya lo siguen",
            1.0,
            absolute=(0.0, 80.0),
        ),
        # Compra estratégica: el accionista del 10% que no ocupa cargo. Son empresas
        # del sector ampliando en una participada, matrices y fondos soberanos. No es
        # convicción directiva, y por eso vive aquí y no en el motor 4, pero es
        # dinero muy informado.
        Component(
            "strategic_buying",
            "strat_net_to_mcap",
            "Compra neta de accionistas estratégicos",
            1.5,
            absolute=(-0.01, 0.01),
        ),
    )

    def adjust(
        self, ctx: ScoringContext, score: pd.Series, components: pd.DataFrame
    ) -> pd.Series:
        """Una salida generalizada de gestoras pone techo a la nota.

        Que una cuarta parte de las casas que seguían el valor cierre posición en un
        solo trimestre es un juicio colectivo, y no debería quedar compensado por
        tener todavía una propiedad institucional cómoda.
        """
        exodus = ctx.column("inst_holders_change_pct") < -0.25
        return score.mask(exodus.fillna(False) & (score > 35.0), 35.0)
