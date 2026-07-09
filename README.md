# Optimizador de Rutas de Recolección — v5

Aplicación web (Streamlit) que calcula rutas óptimas de recolección para una
flota de camiones, con horarios estimados, restricciones de capacidad,
análisis de costos, mapa interactivo y exportación a formatos GIS y de
navegación.

---

## Qué cambió de la v4 a la v5

### 1. Multi-camión real (el cambio más importante)

| | v4 | v5 |
|---|---|---|
| Vehículos | 1 solo, fijo en el código | Los que definas, cada uno con nombre propio |
| Capacidad | Un límite global que solo **avisaba** si se excedía, después de calcular | Restricción **dura** dentro del optimizador: ningún camión puede exceder su capacidad |
| Reparto | No existía — todo iba a un camión | OR-Tools reparte los puntos entre camiones automáticamente |

En la v4, el solver tenía `num_vehicles=1` fijo: aunque existieran más
camiones, se ignoraban. La v5 usa `AddDimensionWithVehicleCapacity` de
OR-Tools, que convierte la capacidad en una restricción del problema: si el
peso total no cabe en un camión, el reparto entre varios es obligatorio.

**Comportamiento a conocer:** si todo el peso cabe en un solo camión, el
optimizador usa uno solo — es la solución de menor distancia. Para forzar el
reparto entre todos igualmente, existe el checkbox **"Balancear rutas entre
camiones"** en la barra lateral.

### 2. Capacidad individual por camión

Nueva pestaña **Camiones**: una fila por camión con su capacidad propia
(ej.: Camión 1 = 5.000 kg, Camión 2 = 15.000 kg). El optimizador respeta la
capacidad de **cada uno**, no un promedio ni un total.

### 3. Asignación manual de puntos a camiones

La tabla de puntos tiene una columna nueva **"Camión"**: con "Auto" el
optimizador decide; eligiendo un camión específico, ese punto queda fijado a
él y el resto de la ruta se optimiza alrededor de esa decisión.

### 4. Depot de salida y de llegada distintos

En la v4 la ruta era siempre circular (salía y volvía al mismo punto). En la
v5, un checkbox en la barra lateral ("El depot de llegada es distinto")
permite definir coordenadas de llegada separadas — útil cuando los camiones
terminan en un relleno sanitario, centro de acopio u otra bodega. El
optimizador considera ese destino final al ordenar las paradas. En el mapa,
la salida se marca con casa roja y la llegada con bandera verde.

### 5. Pestaña de Costos (nueva)

Dos secciones independientes:

- **Comparación de modelos de ruta** — 4 entradas: toneladas y precio por
  tonelada del *modelo actual*, y toneladas y precio por tonelada del
  *modelo nuevo*. Las toneladas del modelo nuevo se pre-llenan con el peso
  de las rutas calculadas (editables). Muestra el costo de cada modelo y la
  diferencia con porcentaje de ahorro.
- **Combustible y operación (informativo)** — rendimiento km/L, precio del
  combustible, otros costos por km y fijos del día. Estima el costo
  operativo de las rutas calculadas. **No se suma** a la comparación de
  arriba; es solo de referencia.

### 6. Interfaz reorganizada y rediseñada

- **Pestañas**: Puntos / Camiones / Resultados / Costos / Exportar, en lugar
  de una sola página larga y saturada.
- **Accesibilidad**: tipografía base más grande (17 px), pestañas a 1.4 rem,
  métricas y tablas ampliadas — pensado para vista reducida.
- **Estilo profesional**: emojis solo donde aportan (pestañas y títulos),
  botones con bordes definidos, tema claro consistente vía
  `.streamlit/config.toml`.
- **Popups del mapa rediseñados**: tarjeta con nombre del punto, camión,
  hora de llegada y peso en tabla alineada, sin quiebres de texto.

### 7. Mapa mejorado

- Un **color por camión** (ruta y marcadores numerados).
- Control de **capas**: Mapa estándar / Satélite (Esri) / Claro / Oscuro.
- Botón de **pantalla completa**.
- Altura ampliada (760 px) para ver más zona de una vez.

### 8. Exportadores multi-camión

Todos los formatos ahora incluyen todas las rutas con su camión como
atributo:

| Formato | Contenido |
|---|---|
| CSV | Todas las paradas con columna "Camión" |
| GeoJSON | Una línea por camión + puntos con propiedades |
| Shapefile (.zip) | Capas `rutas_lineas` y `rutas_puntos` |
| GPX | Un track por camión + waypoints |
| KML | Una carpeta por camión, líneas con el color del camión |
| Google Maps | Links por camión, divididos en segmentos de máx. 10 paradas |
| Waze | Links parada por parada (Waze no soporta multi-paradas) |

### 9. Arquitectura: archivo único

La v4 usaba dos archivos (`app.py` + `db.py`), lo que causó errores de
desincronización al actualizar solo uno. La v5 integra la capa de base de
datos dentro de `app.py`: **un solo archivo**, imposible de desincronizar.

### 10. Otras mejoras técnicas

- OSRM: una sola llamada multi-waypoint por camión (v4 hacía una llamada
  HTTP por cada tramo).
- Tiempo límite del optimizador dinámico según cantidad de puntos
  (10–60 s), en lugar de 10 s fijos.
- Links de Google Maps segmentados automáticamente cuando hay más de 10
  paradas (v4 truncaba la ruta en la parada 10).
- Alerta preventiva: la pestaña Puntos avisa si el peso ya excede la
  capacidad de la flota, antes de calcular.

---

## Estructura del proyecto

```
modelo_rutas\
├── app.py                  Aplicación completa (incluye la base de datos)
├── rutas.db                Se crea sola; guarda puntos, camiones y configuración
├── requirements.txt        Dependencias
└── .streamlit\
    └── config.toml         Tema claro de la aplicación
```

## Instalación y ejecución

```bash
pip install -r requirements.txt
python -m streamlit run app.py
```

La primera vez se crea `rutas.db` automáticamente. Para reiniciar todos los
datos, basta con borrar ese archivo.

## Compatibilidad de datos

La base `rutas.db` de la v4 es compatible: la tabla de camiones se crea
sola al arrancar y los puntos guardados se conservan. Los parámetros de la
pestaña Costos usan claves nuevas, por lo que deben cargarse una vez y
guardarse.

## Flujo de uso típico

1. **Camiones**: definir la flota con capacidades.
2. **Puntos**: cargar paradas (coordenadas o dirección + geocodificar),
   pesos y, si aplica, camión fijo.
3. Barra lateral: hora de inicio, velocidad, depot(s).
4. **Calcular Rutas Óptimas**.
5. **Resultados**: mapa por colores y detalle por camión.
6. **Costos**: comparación de modelos y estimación de combustible.
7. **Exportar**: CSV, GIS o navegación.
