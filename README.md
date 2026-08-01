# AGOR · AI Global Opportunity Radar

Sistema de puntuación que escanea las empresas cotizadas en Estados Unidos y las
clasifica de 0 a 100 según su potencial de convertirse en grandes ganadoras en un
horizonte de 5 a 10 años.

---

## Antes de nada: qué hace y qué no hace

Conviene leer esta sección antes de instalar nada, porque marca la diferencia
entre usar la herramienta bien y confiar en ella más de lo que merece.

**Lo que hace.** Descarga los estados financieros completos de unas 5.500 empresas
directamente de la SEC, con más de diez años de historia, calcula alrededor de 130
métricas por empresa y trimestre, y las puntúa comparándolas contra las empresas
de su propio sector. Genera diez rankings distintos y detecta alertas.

**Lo que no hace.** No predice precios. No sustituye el análisis de una empresa. Y
no es asesoramiento de inversión: es un embudo que reduce 10.000 empresas a unas
decenas que merecen que alguien las mire de verdad.

### Cobertura geográfica: solo Estados Unidos, y por qué

El diseño original pedía siete mercados (EEUU, Canadá, Europa, Japón, Corea,
Australia e Israel). Este radar cubre **las empresas cotizadas en EEUU**, y eso
incluye a las extranjeras con listado en Nasdaq o NYSE, que presentan 10-K o 20-F
ante la SEC.

El motivo es que no existe ninguna fuente gratuita, legal y estable de
fundamentales para las empresas que cotizan *únicamente* en Tokio, Seúl, Tel Aviv
o bolsas europeas locales. La alternativa habitual, `yfinance`, no es una API sino
un scraper de la web de Yahoo, se rompe sin avisar y Yahoo bloquea las IPs de
servidores, con lo que resulta inviable para un proceso automático diario.

Merece la pena señalar algo: los cuatro casos que motivaron este proyecto
—**Nvidia, MercadoLibre, Amazon y Palantir**— cotizan todos en EEUU y todos están
en EDGAR. MercadoLibre es argentina y presenta 10-K. Un radar de US-listed no es
"solo EEUU": es el mercado donde de hecho aparecieron los casos que se quieren
replicar.

Si en algún momento quieres cobertura global real, la arquitectura está preparada:
basta añadir un proveedor en `agor/providers/`. El más razonable es EODHD, cuyo
paquete de fundamentales cuesta unos 60 $/mes y cubre más de 70 bolsas.

### Estado de los 16 motores

Seis de los dieciséis motores están implementados y funcionando. El peso de los
que faltan **se redistribuye automáticamente** entre los activos, de modo que el
score sigue siendo una cifra de 0 a 100 coherente; lo que no es, todavía, es la
cifra completa que describe la especificación.

| Motor | Peso original | Estado | Qué falta |
|---|---|---|---|
| 1 · Calidad Fundamental | 15 | **Activo** | — |
| 2 · Salud Financiera | 10 | **Activo** | — |
| 3 · Valoración Inteligente | 10 | **Activo** (requiere clave de Polygon) | — |
| 4 · Calidad del Management | 8 | Pendiente | Form 4 de EDGAR: dato gratuito y disponible |
| 5 · Ventaja Competitiva | 8 | Pendiente | Análisis con LLM de los informes anuales |
| 6 · Tendencias Globales | 8 | Pendiente | Clasificación temática con LLM |
| 7 · Catalizadores | 8 | Pendiente | Formularios 8-K y noticias |
| 8 · Institucional | 8 | Pendiente | 13F y Form 4 de EDGAR: gratuitos |
| 9 · Sentimiento | 5 | Pendiente | Fuente muy limitada sin presupuesto |
| 10 · Técnico | 7 | **Activo** (requiere clave de Polygon) | — |
| 11 · Comparación Histórica | 5 | Pendiente | Requiere el panel histórico completo |
| 12 · Riesgo | 5 | Pendiente | Análisis con LLM de los informes anuales |
| 13 · Macroeconomía | 3 | Pendiente | FRED, gratuito |
| 14 · Momentum Fundamental | 5 | **Activo** | — |
| 15 · IA Predictiva | 3 | Pendiente | Requiere histórico de decisiones propias |
| 16 · Asimetría | 10 | **Activo** (requiere clave de Polygon) | — |

Dos advertencias sobre los motores pendientes que no se resuelven con esfuerzo:

- El **motor 8** solo puede funcionar para EEUU. Los 13F son una obligación
  exclusivamente estadounidense, trimestral y con 45 días de retraso legal. Los
  *dark pools* que pedía la especificación no son datos públicos: lo único oficial
  es un fichero semanal agregado de FINRA.
- El **motor 9** dependía de X/Twitter, Google Trends y Seeking Alpha. La API de X
  cuesta hoy cientos de dólares al mes por un volumen mínimo, Google Trends solo
  es accesible por librerías no oficiales que se bloquean, y Seeking Alpha no
  tiene API y prohíbe el scraping. Será el motor más débil de los dieciséis y es
  mejor saberlo de antemano.

### Un detalle de la especificación original

Los dieciséis pesos definidos sumaban **118, no 100**. El radar los conserva tal
cual en `SPEC_WEIGHTS` (dentro de `agor/config.py`) para que las prioridades
relativas queden auditables, y los normaliza a 100 automáticamente. Si quieres
cambiar el peso de un motor, ese es el único sitio donde hay que tocar.

---

## Instalación

Necesitas un Mac o Linux con Python 3.10 o superior. En el Terminal:

```bash
cd ~/Desktop/radar-stock
./instalar.sh
```

El script crea un entorno aislado, instala las dependencias y genera un fichero
`.env`. **Ese fichero hay que editarlo**:

```bash
open -e .env
```

Dos valores:

1. **`SEC_USER_AGENT`** — obligatorio. Pon tu nombre y tu email, así:
   `"Gabriel Brevi gabi@ejemplo.com"`. No es una clave ni hay que registrarse en
   ningún sitio: la SEC simplemente exige que quien accede a sus datos se
   identifique. Sin esto, rechaza las peticiones.

2. **`POLYGON_API_KEY`** — necesario para los motores 3, 10 y 16. Clave gratuita y
   sin tarjeta en <https://polygon.io/dashboard/api-keys>. Sin ella el radar
   funciona, pero sin precios: no hay valoración, ni análisis técnico, ni
   asimetría.

Comprueba que todo está en orden:

```bash
./radar estado
```

---

## Uso

### La primera vez

```bash
./radar fundamentales     # ~1 hora, descarga unos 3 GB de la SEC
./radar universo          # segundos
./radar precios           # ~100 minutos por el límite de 5 peticiones/minuto
./radar puntuar           # ~2 minutos
```

El paso de fundamentales descarga los *Financial Statement Data Sets* de la SEC:
un fichero por trimestre con todos los hechos numéricos de todas las empresas.
Solo se hace una vez; después, cada ejecución descarga únicamente el trimestre
nuevo cuando la SEC lo publica.

El de precios es lento por el límite del plan gratuito de Polygon, no por el
código. Se puede cortar y reanudar en cualquier momento sin perder nada. Si tienes
prisa, `./radar precios --max-dias 60` trae los tres últimos meses, suficiente
para empezar a ver resultados aunque las medias de 200 sesiones queden incompletas.

### Cada día

```bash
./radar todo
```

### Ver los resultados

Los rankings se imprimen en el Terminal y además se guardan en tres sitios:

- `reports/AAAA-MM-DD/*.csv` — un CSV por ranking, para abrir con Excel.
- `web/data/radar.json` — datos del dashboard.
- `data/history/scores_AAAA-MM.parquet` — histórico de puntuaciones.

Para el dashboard visual:

```bash
cd web && python3 -m http.server 8000
```

y abre <http://localhost:8000> en el navegador.

---

## Automatización en GitHub Actions

El fichero `.github/workflows/radar-diario.yml` ejecuta el radar solo, cada día,
gratis, y publica el dashboard en una URL. Hace falta:

1. Subir el repositorio a GitHub **como público** (los minutos de Actions y
   GitHub Pages solo son gratis en repositorios públicos).
2. En *Settings → Secrets and variables → Actions*, crear:
   `SEC_USER_AGENT` y `POLYGON_API_KEY`.
3. En *Settings → Pages*, seleccionar **GitHub Actions** como origen.

El workflow guarda la base de datos en la caché de Actions y versiona en el
repositorio únicamente el histórico de puntuaciones y la salida web.

---

## Cómo leer las puntuaciones

**La columna de cobertura es tan importante como el score.** Con datos gratuitos
hay huecos constantes, y el radar hace algo deliberado: cuando un motor no puede
puntuar, su peso se reparte entre los que sí, en lugar de contar como cero. Sin
eso, las empresas más grandes y documentadas ganarían siempre por el simple hecho
de publicar más información.

La contrapartida es que una puntuación de 92 con cobertura del 35% no significa lo
mismo que una de 92 con cobertura del 85%. Para que esto no se convierta en una
trampa hay dos salvaguardas:

- Una empresa **no recibe puntuación final** si los motores que la han evaluado no
  representan al menos la mitad del peso activo.
- Ninguna empresa puede alcanzar las bandas superiores sin un mínimo de cobertura
  real. Una candidata a *Exceptional Buy* con datos insuficientes baja de banda
  automáticamente.

Esto se probó y era necesario: sin estos límites, empresas evaluadas por un único
motor con el 6% de sus datos obtenían un 100 sobre 100 y copaban todos los Top 20.

### Las bandas

| Score | Banda |
|---|---|
| 95–100 | Exceptional Buy |
| 90–95 | Strong Buy |
| 85–90 | Buy |
| 80–85 | Watchlist Premium |
| 75–80 | Watchlist |
| < 75 | No invertir |

La especificación afirmaba que la banda 95–100 corresponde al 0,5% superior del
universo. Con puntuaciones absolutas eso no está garantizado, así que cada
ejecución imprime un **informe de calibración** que compara la rareza real de cada
banda con la esperada y dice si los cortes están siendo laxos o severos. Ese
informe es el que permite decidir si conviene moverlos.

---

## Decisiones de diseño que conviene conocer

**Todo se compara por percentiles dentro del sector, no con umbrales fijos.** Un
margen bruto del 45% es excelente en distribución y mediocre en software. Con
umbrales absolutos, el ranking no mediría calidad sino a qué sector pertenece cada
empresa.

**Financieras, seguros e inmobiliarias están excluidas.** No porque sean malas
inversiones, sino porque en sus cuentas el ROIC, el margen bruto y la caja libre
significan otra cosa. Un banco tiene "deuda" que es su materia prima. Mezclarlas
no solo las puntúa mal: distorsiona los percentiles de todas las demás. Se
reactivan quitándolas de `EXCLUDED_SECTORS` en `agor/config.py`.

**Casi todo se calcula sobre los últimos doce meses, no sobre el trimestre.**
Comparar trimestres sueltos mete la estacionalidad del negocio como si fuera
señal, y en retail o semiconductores eso basta para invertir un ranking.

**Los datos ausentes nunca se imputan.** Rellenar con la mediana del sector haría
que una empresa desconocida pareciese del montón en lugar de desconocida, y esa
distinción es justo la que el radar necesita conservar.

**Sobre Wyckoff.** El motor técnico no afirma detectar el esquema de Wyckoff:
sería deshonesto, porque es una lectura interpretativa sin definición cerrada
contra la que validar. Lo que mide son las condiciones observables que la
literatura asocia a una fase de acumulación —rango estrecho sostenido, volumen
sesgado hacia los días de subida, precio que deja de hacer mínimos decrecientes—.
Es una aproximación declarada, no una etiqueta.

**Sobre el DCF.** Los supuestos están todos juntos y con nombre en
`DCF_ASSUMPTIONS`, dentro de `agor/features/valuation.py`. Un DCF con veinte
parámetros ajustables no es más preciso, solo es más fácil de forzar hasta que dé
el resultado que uno quería.

---

## El módulo de aprendizaje continuo

La idea de recalibrar los pesos según qué recomendaciones funcionaron es
excelente, pero tiene un problema de datos que conviene entender.

Para entrenar *hacia atrás* haría falta el panel histórico con fundamentales
**point-in-time** y el universo completo **incluyendo las empresas deslistadas**.
Los datos de los que dispone este radar son cifras reexpresadas de las empresas
que hoy siguen vivas. Entrenar con eso produciría un modelo que ha aprendido de
supervivientes y que sobreestimaría sistemáticamente sus propios aciertos.

Por eso el aprendizaje está diseñado para funcionar **hacia adelante**: desde el
primer día, cada ejecución guarda un snapshot inmutable de todas las puntuaciones
en `data/history/`. Esa tabla nunca se reescribe. A los 3, 6 y 12 meses habrá
datos reales y sin sesgo para medir qué motores predijeron mejor y recalibrar los
pesos con fundamento.

Significa que los pesos no empezarán a ajustarse solos hasta dentro de unos meses.
Es más lento y es la única forma honesta de hacerlo.

---

## Estructura del código

```
agor/
  config.py            Pesos, bandas, umbrales. El sitio donde se ajusta todo.
  sectors.py           Códigos SIC de la SEC → sectores comparables.
  xbrl.py              Etiquetas XBRL → métricas canónicas, por prioridad.
  store.py             Esquema y acceso a DuckDB.
  providers/           Clientes de datos: sec.py, polygon.py.
  ingest/              Descarga y normalización: fundamentales, precios, universo.
  features/            panel.py (fundamentales), technical.py, valuation.py.
  engines/             Un fichero por motor. Cada uno declara sus componentes.
  scoring/             normalize.py (percentiles), aggregate.py (score final).
  output/              rankings.py, alerts.py, export.py.
  pipeline.py          Orquestación de una ejecución completa.
  cli.py               Comandos.
```

Para añadir un motor: crear el fichero en `engines/`, declarar sus componentes,
registrarlo en `ENGINES` dentro de `pipeline.py` y quitarlo de `PENDING_ENGINES`.
El reparto de pesos se ajusta solo.

---

## Fuentes de datos

| Fuente | Qué aporta | Coste | Límite |
|---|---|---|---|
| SEC EDGAR (Financial Statement Data Sets) | Fundamentales, 10+ años, ~5.500 empresas | Gratis | Ninguno relevante |
| SEC EDGAR (API por empresa) | Refresco inmediato tras publicar resultados | Gratis | 10 peticiones/segundo |
| Polygon.io | Precios diarios de todo el mercado | Gratis | 5 peticiones/minuto, 2 años de historia |

Las dos son fuentes oficiales con acceso automatizado permitido. Ninguna se
obtiene por scraping.

---

*No es asesoramiento de inversión. Los datos pueden contener errores, incluidos
los de las propias declaraciones a la SEC.*
