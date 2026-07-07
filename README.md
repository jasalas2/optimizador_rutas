# 🚚 Optimizador de Rutas de Recolección

Calcula la ruta óptima de recolección con horarios estimados, peso por
parada, mapa interactivo y exportación a múltiples formatos (CSV, Google
Maps, GeoJSON, Shapefile, GPX, KML, Waze). Soporta múltiples camiones con
capacidad real y frecuencia de recolección por día.

## Novedades de esta versión (Fase 4)

- **Multi-camión con capacidad real**: ya no es un solo camión — configurás
  cuántos camiones tenés disponibles y la capacidad (kg) de cada uno. El
  optimizador (OR-Tools, VRP capacitado) reparte los puntos entre la flota
  respetando la capacidad de cada camión como restricción dura: si uno se
  llenaría, el resto de los puntos se los lleva otro camión con espacio.
- **Aviso de flota insuficiente**: si ni sumando la capacidad de todos los
  camiones alcanza para cubrir todos los puntos, la app te dice exactamente
  cuáles quedaron sin asignar, para que agregues más camiones o capacidad.
- **Frecuencia de recolección por día**: cada punto puede tener un patrón de
  días (todos los días, Lunes a Sábado, Lunes/Miércoles/Viernes,
  Martes/Jueves/Sábado, o un solo día). Antes de calcular, elegís para qué
  día es la ruta y la app filtra automáticamente los puntos que tocan ese día
  — no hace falta borrar puntos manualmente según el día.
- **Mapa con todas las rutas superpuestas**, un color por camión, con capas
  togglables (calles / satélite / modo oscuro) y control de capas para
  mostrar/ocultar la ruta de cada camión.
- **Exportación por camión**: detalle y exportación (CSV, GeoJSON,
  Shapefile, GPX, KML, Waze) individual por camión seleccionado, además de un
  CSV combinado con todos los camiones del día.
- Se mantiene todo lo de fases anteriores: persistencia en SQLite,
  geocodificación de direcciones con Nominatim, y manejo visible de errores
  de OSRM.

## Instalación local

```
pip install -r requirements.txt
python -m streamlit run app.py
```

La primera vez se crea automáticamente el archivo `rutas.db` en la misma
carpeta — ahí se guardan tus puntos y configuración. Si ya tenías una base
de datos de una versión anterior, se migra sola (se le agrega la columna de
frecuencia sin perder tus puntos existentes). Si querés "resetear" todo,
simplemente borrá el archivo `rutas.db`.

## Desplegar en la nube (gratis, recomendado para uso interno)

**Streamlit Community Cloud** es la opción más simple — queda accesible
desde cualquier navegador (celular incluido) sin que nadie tenga que
instalar nada:

1. Subí esta carpeta (`app.py`, `db.py`, `requirements.txt`) a un repositorio
   de GitHub.
2. Entrá a [share.streamlit.io](https://share.streamlit.io) con tu cuenta
   de GitHub.
3. Elegí el repo, la rama, y como archivo principal `app.py`.
4. Listo — te da una URL pública para compartir con el equipo.

> ⚠️ **Importante sobre `rutas.db` en la nube:** Streamlit Community Cloud
> no garantiza que el disco persista entre reinicios del contenedor (puede
> "dormirse" por inactividad y reiniciar). Para uso interno con pocos
> reinicios es aceptable, pero si notás que los datos se resetean
> inesperadamente, el siguiente paso sería mover la base de datos a algo
> persistente de verdad (ej. Supabase o Turso, ambos con plan gratuito).

## Cómo funciona el reparto entre camiones

1. Configurás en la barra lateral: número de camiones disponibles y
   capacidad (kg) por camión.
2. Elegís el día a calcular — la app filtra los puntos programados para ese
   día según la columna "Días de recolección" de la tabla.
3. El optimizador reparte esos puntos entre los camiones disponibles,
   minimizando distancia total y respetando que ningún camión supere su
   capacidad real.
4. Si algún punto queda sin camión (flota insuficiente), se avisa
   explícitamente en vez de fallar en silencio o ignorarlo.

## Formatos de exportación disponibles

| Formato | Uso principal |
|---|---|
| CSV (combinado o por camión) | Resumen de la ruta para oficina / reportes |
| Google Maps (link) | Navegación rápida desde el celular, hasta ~10 paradas intermedias |
| GeoJSON | Análisis en QGIS, ArcGIS, geojson.io, Felt |
| Shapefile (.zip) | Capas de línea y puntos para SIG (QGIS/ArcGIS) |
| GPX | Navegación GPS completa con voz (OsmAnd, Garmin) |
| KML | Google Earth / Google My Maps |
| Waze (links por parada) | Navegación turno por turno, parada por parada |
| Mapa interactivo (HTML) | Ver todas las rutas del día sin conexión a internet |

## Próximos pasos

Ideas identificadas para versiones futuras, de mayor a menor impacto
práctico:

- **Balanceo de carga entre camiones por tiempo/paradas** (no solo por peso),
  para que ningún chofer termine con una ruta mucho más larga que otro.
- **Ventanas de tiempo (VRPTW)**: zonas comerciales solo de madrugada, zonas
  escolares evitando horas de entrada/salida, restricción vehicular por
  horario.
- **Métricas y comparación antes/después**: costo por km, costo por parada,
  % de utilización de capacidad por camión, para cuantificar el ahorro real
  frente a la ruta manual anterior.
- Caché de la matriz de distancias en SQLite para no recalcular contra OSRM
  si los puntos no cambiaron.
- Importar puntos desde Excel/CSV con `st.file_uploader`.
- Servidor OSRM propio (Docker) en vez del endpoint público.
- Historial de rutas calculadas (fecha, km totales, peso, camiones usados).
