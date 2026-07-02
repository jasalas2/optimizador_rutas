# 🚚 Optimizador de Rutas de Recolección

Calcula la ruta óptima de recolección con horarios estimados, peso por
parada, mapa interactivo y exportación a CSV / Google Maps.

## Novedades de esta versión (Fase 2)

- **Persistencia con SQLite** (`rutas.db`): los puntos de recolección y la
  configuración (hora de inicio, velocidad, capacidad, depot) ya no se
  pierden al recargar la página.
- **Errores de OSRM visibles**: si el servicio de rutas no responde, la app
  te dice por qué (timeout, sin conexión, etc.) en vez de fallar en
  silencio.
- **Geocodificación opcional**: podés escribir una dirección en texto y la
  app le busca las coordenadas (usa Nominatim/OpenStreetMap, gratis).
- **Link directo a Google Maps**: además del CSV, podés abrir la ruta ya
  ordenada directamente en Google Maps desde el celular.

## Instalación local

```bash
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

La tabla `puntos` en `db.py` ya incluye una columna `camion_asignado`
(vacía por ahora), pensada para cuando se agregue optimización con
múltiples vehículos — así no habrá que migrar datos cuando llegue ese
momento.
