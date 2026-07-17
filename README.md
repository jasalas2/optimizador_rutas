# Optimizador de Rutas de Recolección de Residuos — v7

Aplicación web (Streamlit) que calcula rutas óptimas de recolección para una
flota de camiones, con horarios estimados, restricciones de capacidad,
múltiples viajes por camión, cálculo por Cantón/Distrito, costo real por
tonelada, mapa interactivo, y exportación a formatos GIS y de navegación.

---

## Metodología

El núcleo del sistema es un problema de ruteo de vehículos (VRP) resuelto
con OR-Tools (Google). Cada camión se modela con su propia capacidad, su
propio plantel (de dónde sale y a dónde vuelve cada día), y la posibilidad
de hacer varios viajes en el mismo día si se llena antes de terminar. Las
distancias entre puntos se calculan sobre la red vial real mediante OSRM,
en lugar de asumir línea recta.

Sobre esa base, el costo de operar las rutas se construye desde cinco
categorías (inversión, mano de obra, combustible, mantenimiento y
administrativa), cada una prorrateada a un equivalente diario, y se compara
contra lo que cuesta el modelo de recolección actual.

## Estructura de pestañas

| Pestaña | Qué hace |
|---|---|
| **Puntos** | Carga de paradas: nombre, dirección, coordenadas, peso, cantón, distrito, camión asignado (opcional). Avisa si algún punto pesa más de lo que cualquier camión puede levantar en un solo viaje. |
| **Camiones** | Flota: capacidad, personas, viajes máximos por día, plantel (obligatorio), y Cantón/Distrito asignado (opcional, para restringir en qué zonas puede trabajar). |
| **Resultados** | Mapa con selector de rutas individuales (color propio por camión y por viaje), detalle de paradas y horarios. Cuando se calcula por lotes, todas las zonas quedan combinadas en un único resultado — cada camión etiquetado con su zona, y el mismo selector de rutas permite ver una zona sola, varias juntas, o todas a la vez. |
| **Costos** | Comparación modelo actual vs. modelo nuevo; estructura completa de costos (inversión, mano de obra, combustible, mantenimiento, administrativa); costo real por tonelada calculado automáticamente. |
| **Exportar** | CSV, GeoJSON, Shapefile, GPX, KML, y links directos a Google Maps y Waze. |
| **Red propia (Beta)** | Rutea sobre un shapefile de calles propio en vez de la red pública de OpenStreetMap. |
| **Recolección en vía (Beta)** | Estima kilogramos adicionales según el tipo de calle que atraviesa cada ruta (autopista, primaria, residencial, etc.), con mapa de verificación y capas activables. Opcionalmente, ese kg extra puede sumarse como recolección real y reflejarse en Costos. |

## Cálculo por zona geográfica

Arriba del botón "Calcular Rutas Óptimas" hay un selector de modo:

- **Todos los puntos juntos** — el comportamiento clásico, sin agrupar.
- **Una ruta por Distrito** / **Una ruta por Cantón** — calcula una ruta
  **independiente** por cada valor distinto de esa columna en Puntos, cada
  una con su propia flota completa (los camiones no se comparten entre
  zonas, salvo que se restrinjan — ver más abajo).
- **Mixto** — se elige, cantón por cantón, si ese cantón se calcula como
  una sola ruta completa o dividido por distrito (por ejemplo, un cantón a
  nivel cantonal y otro dividido en sus distritos, en el mismo cálculo).

En los tres modos por zona, el resultado de todas las zonas calculadas
queda **combinado en una sola vista** en la pestaña Resultados — no hace
falta cambiar entre ellas, se muestran o esconden individualmente con el
selector de rutas del mapa.

### Restringir camiones a zonas específicas

En la pestaña Camiones, las columnas opcionales **"Cantón asignado"** y
**"Distrito asignado"** permiten limitar en qué zonas puede trabajar cada
camión:

- Ambas vacías → el camión es comodín, disponible en cualquier zona.
- Con Cantón asignado → disponible en cualquier distrito de ese cantón.
- Con Distrito asignado → restringido solo a ese distrito puntual.

Si una zona se queda sin ningún camión disponible, la app avisa
exactamente cuál y por qué.

## Recolección en vía: detalle

Esta pestaña es un análisis de solo lectura sobre las rutas ya calculadas
— no modifica pesos, capacidades ni costos por defecto:

- Descarga la red vial clasificada de OpenStreetMap una sola vez por
  cálculo (no una vez por camión), y **deduplica por vía física real**: si
  un camión pasa dos veces por la misma calle (varios viajes), o dos
  camiones comparten un tramo, esa vía se cuenta una sola vez.
- Los tramos largos del recorrido se subdividen en pedazos de ~150 m antes
  de clasificar el tipo de vía, para no depender de un solo punto medio
  cuando un tramo en realidad cruza dos tipos de vía distintos.
- El mapa tiene capas activables por camión, distinguiendo lo que sí se
  contó de lo que se saltó por estar repetido, con el kilometraje de cada
  capa en su propio nombre.
- Con el checkbox "Sumar este kg extra a la cantidad total recolectada",
  ese peso se refleja en la pestaña Costos (toneladas del modelo nuevo), y
  se puede ver el detalle progresivo de cómo se va llenando cada camión
  parada por parada. Recalcular las rutas invalida automáticamente estos
  datos, para que nunca queden desactualizados sin darse cuenta.

## Instalación

```bash
pip install -r requirements.txt
python -m streamlit run app.py
```

La primera vez se crea `rutas.db` automáticamente. Las bases de datos de
versiones anteriores se migran solas al arrancar — las columnas nuevas se
agregan sin perder los datos existentes.

Dependencias: `networkx` (Red propia) y `osmnx` (Recolección en vía), ya
incluidas en `requirements.txt`.
