# 🚚 Optimizador de Rutas de Recolección

Calcula la ruta óptima de recolección con horarios estimados, peso por
parada, mapa interactivo y exportación a múltiples formatos (CSV, Google
Maps, GeoJSON, Shapefile, GPX, KML, Waze).

## Novedades de esta versión (Fase 2)

- **Exportación para SIG / GPS / navegación**, además del CSV y el link a
  Google Maps de la fase anterior:
  - **GeoJSON** — se abre directo en QGIS, ArcGIS, geojson.io, Felt, etc.
  - **Shapefile (.zip)** — capas de línea (ruta) y puntos (paradas), listas
    para QGIS o ArcGIS.
  - **GPX** — para importar en OsmAnd, Garmin BaseCamp, Maps.me y apps de
    navegación GPS en general.
  - **KML** — para abrir en Google Earth o importar en Google My Maps.
- **Integración con Waze**: como Waze no tiene API pública de multi-paradas,
  se generan links individuales por parada (`waze.com/ul?...&navigate=yes`)
  para que el chofer los abra uno por uno, con la alternativa recomendada de
  importar el GPX en OsmAnd para navegación de ruta completa con voz.
- Se mantiene todo lo de la Fase 2: persistencia en SQLite, geocodificación
  de direcciones con Nominatim, y manejo visible de errores de OSRM.

## Instalación local

```
pip install -r requirements.txt
python -m streamlit run app.py
```

La primera vez se crea automáticamente el archivo `rutas.db` en la misma
carpeta — ahí se guardan tus puntos y configuración. Si querés "resetear"
todo, simplemente borrá ese archivo.

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

## Próximos pasos (Fase 3 — multi-camión)

| Formato | Uso principal |
|---|---|
| CSV | Resumen de la ruta para oficina / reportes |
| Google Maps (link) | Navegación rápida desde el celular, hasta ~10 paradas intermedias |
| GeoJSON | Análisis en QGIS, ArcGIS, geojson.io, Felt |
| Shapefile (.zip) | Capas de línea y puntos para SIG (QGIS/ArcGIS) |
| GPX | Navegación GPS completa con voz (OsmAnd, Garmin) |
| KML | Google Earth / Google My Maps |
| Waze (links por parada) | Navegación turno por turno, parada por parada |

## Próximos pasos (Fase 4 — multi-camión)

La tabla `puntos` en `db.py` ya incluye una columna `camion_asignado` (vacía
por ahora), pensada para cuando se agregue optimización con múltiples
vehículos — así no habrá que migrar datos cuando llegue ese momento.

Otras mejoras identificadas para más adelante:
- Ventanas de tiempo por parada (clientes que solo reciben en cierto horario).
- Caché de la matriz de distancias en SQLite para no recalcular contra OSRM
  si los puntos no cambiaron.
- Importar puntos desde Excel/CSV con `st.file_uploader`.
- Servidor OSRM propio (Docker) en vez del endpoint público.
- Historial de rutas calculadas (fecha, km totales, peso).
