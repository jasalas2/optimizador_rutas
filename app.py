"""
Panel Web - Optimizador de Rutas de Recolección — v5.1 (archivo único)
=====================================================
Novedades v5:
- Multi-camión REAL: OR-Tools reparte los puntos entre camiones respetando
  la capacidad individual de cada uno (AddDimensionWithVehicleCapacity).
- Capacidad por camión (ej: Camión 1 = 5000 kg, Camión 2 = 15000 kg).
- Asignación manual opcional: columna "Camión" en la tabla de puntos
  ("Auto" deja que el optimizador decida).
- Pestaña de Costos: comparación modelo actual (costo por tonelada)
  vs modelo nuevo (combustible por km + otros costos).
- Interfaz reorganizada en pestañas para reducir la saturación visual.

TODO EN UN SOLO ARCHIVO: ya no se necesita db.py (si existe, se ignora;
podés borrarlo). La base de datos rutas.db se sigue usando igual.

Instalación:
    pip install -r requirements.txt
Correr:
    python -m streamlit run app.py
"""

import math
import time
from datetime import datetime, timedelta

import folium
from folium.plugins import Fullscreen
import pandas as pd
import requests
import streamlit as st
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from streamlit_folium import st_folium



# ═════════════════════════════════════════════
# BASE DE DATOS (integrada — antes era db.py)
# ═════════════════════════════════════════════
import sqlite3
from contextlib import contextmanager

DB_PATH = "rutas.db"

DB_TO_UI = {
    "nombre": "Nombre",
    "direccion": "Dirección",
    "latitud": "Latitud",
    "longitud": "Longitud",
    "peso_kg": "Peso (kg)",
    "camion_asignado": "Camión",
}
UI_TO_DB = {v: k for k, v in DB_TO_UI.items()}


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS puntos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL,
                direccion TEXT,
                latitud REAL,
                longitud REAL,
                peso_kg REAL DEFAULT 0,
                camion_asignado TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                clave TEXT PRIMARY KEY,
                valor TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS camiones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL,
                capacidad_kg REAL DEFAULT 1000
            )
        """)


# ── Puntos ────────────────────────────────────────────────────────────────
def hay_puntos_guardados():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM puntos").fetchone()[0] > 0


def cargar_puntos():
    with get_conn() as conn:
        df = pd.read_sql_query(
            "SELECT nombre, direccion, latitud, longitud, peso_kg, camion_asignado "
            "FROM puntos ORDER BY id",
            conn,
        )
    df = df.rename(columns=DB_TO_UI)
    df["Camión"] = df["Camión"].fillna("Auto")
    return df


def guardar_puntos(df_ui):
    df = df_ui.rename(columns=UI_TO_DB).copy()
    for col in ["nombre", "direccion", "latitud", "longitud", "peso_kg", "camion_asignado"]:
        if col not in df.columns:
            df[col] = None
    df = df[["nombre", "direccion", "latitud", "longitud", "peso_kg", "camion_asignado"]]
    df = df.dropna(subset=["nombre"])
    with get_conn() as conn:
        conn.execute("DELETE FROM puntos")
        if len(df) > 0:
            df.to_sql("puntos", conn, if_exists="append", index=False)


# ── Camiones ──────────────────────────────────────────────────────────────
def hay_camiones_guardados():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM camiones").fetchone()[0] > 0


def cargar_camiones():
    with get_conn() as conn:
        df = pd.read_sql_query(
            "SELECT nombre, capacidad_kg FROM camiones ORDER BY id", conn
        )
    return df.rename(columns={"nombre": "Nombre", "capacidad_kg": "Capacidad (kg)"})


def guardar_camiones(df_ui):
    df = df_ui.rename(columns={"Nombre": "nombre", "Capacidad (kg)": "capacidad_kg"}).copy()
    df = df.dropna(subset=["nombre"])
    with get_conn() as conn:
        conn.execute("DELETE FROM camiones")
        if len(df) > 0:
            df[["nombre", "capacidad_kg"]].to_sql("camiones", conn, if_exists="append", index=False)


# ── Config ────────────────────────────────────────────────────────────────
def obtener_config(clave, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT valor FROM config WHERE clave = ?", (clave,)).fetchone()
        return row[0] if row is not None else default


def guardar_config(clave, valor):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO config (clave, valor) VALUES (?, ?)
               ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor""",
            (clave, str(valor)),
        )


def guardar_configuracion_general(**kwargs):
    with get_conn() as conn:
        for clave, valor in kwargs.items():
            conn.execute(
                """INSERT INTO config (clave, valor) VALUES (?, ?)
                   ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor""",
                (clave, str(valor)),
            )


class _DB:
    """Espacio de nombres para mantener las llamadas db.xxx() del resto del código."""
    pass


db = _DB()
for _f in (init_db, hay_puntos_guardados, cargar_puntos, guardar_puntos,
           hay_camiones_guardados, cargar_camiones, guardar_camiones,
           obtener_config, guardar_config, guardar_configuracion_general):
    setattr(db, _f.__name__, _f)


st.set_page_config(page_title="Optimizador de Rutas", layout="wide")
st.markdown("""
<style>
footer {visibility: hidden;}

/* Tipografía base más grande y legible */
html, body, [data-testid="stAppViewContainer"] {font-size: 17px;}
h1 {font-size: 2.1rem; font-weight: 700; letter-spacing: -0.01em;}
h2 {font-size: 1.5rem; font-weight: 650;}
h3 {font-size: 1.25rem; font-weight: 600;}
p, li, label {font-size: 1.02rem;}
[data-testid="stCaptionContainer"] {font-size: 0.95rem;}

/* Pestañas grandes y visibles (selectores para todas las versiones de Streamlit) */
.stTabs [data-baseweb="tab-list"] button [data-testid="stMarkdownContainer"] p,
.stTabs [data-baseweb="tab"] p,
.stTabs [data-baseweb="tab"] {
    font-size: 1.5rem !important;
    font-weight: 700 !important;
}
.stTabs [data-baseweb="tab-list"] button {
    padding: 1.0rem 2.0rem !important;
    height: auto !important;
}
.stTabs [data-baseweb="tab-list"] {
    gap: 0.6rem;
    position: sticky; top: 0; z-index: 999;
    background: white;
    border-bottom: 2px solid #E5EAF2;
    padding-top: 0.3rem;
}
.stTabs [data-baseweb="tab-highlight"] {
    background-color: #2563EB; height: 4px;
}
.stTabs [aria-selected="true"] {
    background: #EFF4FF; border-radius: 8px 8px 0 0;
}

/* Botones claros, con borde definido y buen tamaño */
.stButton button, .stDownloadButton button, .stLinkButton a {
    font-size: 1.05rem !important;
    padding: 0.6rem 1.1rem !important;
    border-radius: 8px !important;
    border: 1.5px solid #C7D2E5 !important;
}
.stDownloadButton button, .stLinkButton a {
    background: #F7F9FC !important;
}
.stButton button:hover, .stDownloadButton button:hover, .stLinkButton a:hover {
    border-color: #2563EB !important;
    color: #2563EB !important;
}

/* Métricas grandes */
[data-testid="stMetricValue"] {font-size: 1.7rem; font-weight: 650;}
[data-testid="stMetricLabel"] {font-size: 1.0rem; color: #4B5563;}

/* Expandibles con borde claro */
div[data-testid="stExpander"] {
    border: 1.5px solid #D6DEEA; border-radius: 8px;
}
div[data-testid="stExpander"] summary,
div[data-testid="stExpander"] summary p,
div[data-testid="stExpander"] summary [data-testid="stMarkdownContainer"] p {
    font-size: 1.25rem !important; font-weight: 650 !important;
}

/* Tablas de detalle grandes y legibles */
[data-testid="stTable"] table {font-size: 1.05rem;}
[data-testid="stTable"] th {
    font-size: 1.0rem; font-weight: 650;
    background: #F4F6FA;
}
[data-testid="stTable"] td, [data-testid="stTable"] th {
    padding: 0.55rem 0.8rem !important;
}

/* Inputs un poco más altos */
input {font-size: 1.02rem !important;}
</style>
""", unsafe_allow_html=True)

st.title("Optimizador de Rutas de Recolección")
st.caption("Planificación de rutas multi-camión con restricciones de capacidad, "
           "análisis de costos y exportación a formatos GIS.")

db.init_db()

# Colores por camión (mapa y KML)
COLORES = ["#E74C3C", "#2980B9", "#27AE60", "#8E44AD", "#F39C12", "#16A085", "#D35400", "#7F8C8D"]
COLORES_KML = ["ff3c4ce7", "ffb98029", "ff60ae27", "ffad448e", "ff12c9f3", "ff85a016", "ff0054d3", "ff8d8c7f"]

# ─────────────────────────────────────────────
# FUNCIONES
# ─────────────────────────────────────────────
def haversine(c1, c2):
    R = 6_371_000
    lat1, lon1 = math.radians(c1[0]), math.radians(c1[1])
    lat2, lon2 = math.radians(c2[0]), math.radians(c2[1])
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return int(R * 2 * math.asin(math.sqrt(a)))


def geocodificar_direccion(direccion):
    url = "https://nominatim.openstreetmap.org/search"
    params = {"q": direccion, "format": "json", "limit": 1}
    headers = {"User-Agent": "optimizador-rutas-app/1.0"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"]), None
        return None, None, "no se encontraron resultados"
    except requests.exceptions.Timeout:
        return None, None, "timeout consultando Nominatim"
    except requests.exceptions.ConnectionError:
        return None, None, "sin conexión a Nominatim"
    except Exception as e:
        return None, None, f"error inesperado ({e})"


def obtener_matriz_osrm(locations):
    coords_str = ";".join(f"{lon},{lat}" for lat, lon in locations)
    url = f"http://router.project-osrm.org/table/v1/driving/{coords_str}?annotations=distance"
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == "Ok":
            return [[int(d) for d in row] for row in data["distances"]], True, None
        error_msg = f"OSRM respondió con código '{data.get('code')}'"
    except requests.exceptions.Timeout:
        error_msg = "OSRM no respondió a tiempo (timeout)."
    except requests.exceptions.ConnectionError:
        error_msg = "No se pudo conectar con OSRM (revisá tu conexión a internet)."
    except Exception as e:
        error_msg = f"Error inesperado consultando OSRM: {e}"
    n = len(locations)
    matriz = [[0 if i == j else haversine(locations[i], locations[j]) for j in range(n)] for i in range(n)]
    return matriz, False, error_msg


def obtener_ruta_completa_osrm(stops):
    """
    UNA sola llamada OSRM para todo el recorrido de un camión (multi-waypoint).
    Devuelve (camino, dist_legs_m, error):
    - camino: lista (lat, lon) con la geometría completa por carretera
    - dist_legs_m: distancia en metros de cada tramo parada-a-parada
    """
    if len(stops) < 2:
        return list(stops), [], None
    coords_str = ";".join(f"{lon},{lat}" for lat, lon in stops)
    url = (f"http://router.project-osrm.org/route/v1/driving/{coords_str}"
           f"?overview=full&geometries=geojson")
    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == "Ok":
            ruta = data["routes"][0]
            camino = [(lat, lon) for lon, lat in ruta["geometry"]["coordinates"]]
            dist_legs = [leg["distance"] for leg in ruta["legs"]]
            return camino, dist_legs, None
        err = f"OSRM código '{data.get('code')}'"
    except requests.exceptions.Timeout:
        err = "timeout"
    except requests.exceptions.ConnectionError:
        err = "sin conexión"
    except Exception as e:
        err = f"error inesperado ({e})"
    # Fallback: línea recta entre paradas
    camino = list(stops)
    dist_legs = [haversine(stops[i], stops[i + 1]) for i in range(len(stops) - 1)]
    return camino, dist_legs, err


def resolver_vrp(distancias, demandas, capacidades, asignaciones=None, balancear=False, end_node=None):
    """
    VRP multi-vehículo con restricción de capacidad POR CAMIÓN.

    - distancias: matriz NxN en metros (nodo 0 = depot)
    - demandas: peso en kg de cada nodo (demandas[0] = 0)
    - capacidades: lista con la capacidad en kg de cada camión
    - asignaciones: dict {nodo: índice_camión} para fijar manualmente
    - balancear: si True, penaliza que un camión recorra mucho más que otro
      (fuerza a repartir aunque todo quepa en un solo camión)

    Devuelve lista de rutas, una por camión: [[0, 3, 1, 0], [0, 2, 4, 0], ...]
    Un camión sin paradas devuelve [0, 0].
    """
    n_vehiculos = len(capacidades)
    if end_node is None:
        # Salida y llegada en el mismo depot (nodo 0)
        manager = pywrapcp.RoutingIndexManager(len(distancias), n_vehiculos, 0)
    else:
        # Depot de salida (nodo 0) distinto al de llegada (end_node)
        manager = pywrapcp.RoutingIndexManager(
            len(distancias), n_vehiculos,
            [0] * n_vehiculos, [end_node] * n_vehiculos,
        )
    routing = pywrapcp.RoutingModel(manager)

    def cb_dist(from_idx, to_idx):
        return distancias[manager.IndexToNode(from_idx)][manager.IndexToNode(to_idx)]

    t = routing.RegisterTransitCallback(cb_dist)
    routing.SetArcCostEvaluatorOfAllVehicles(t)

    # ── Restricción de capacidad (la clave del reparto entre camiones) ──
    def cb_demanda(from_idx):
        return int(demandas[manager.IndexToNode(from_idx)])

    d = routing.RegisterUnaryTransitCallback(cb_demanda)
    routing.AddDimensionWithVehicleCapacity(
        d,
        0,                                   # sin holgura
        [int(c) for c in capacidades],       # capacidad individual por camión
        True,                                # el acumulado arranca en 0
        "Capacidad",
    )

    # ── Asignación manual de puntos a camiones ──
    if asignaciones:
        for nodo, veh in asignaciones.items():
            index = manager.NodeToIndex(nodo)
            routing.VehicleVar(index).SetValues([veh])

    # ── Balanceo opcional de rutas ──
    if balancear:
        routing.AddDimension(t, 0, 3_000_000, True, "Distancia")
        dist_dim = routing.GetDimensionOrDie("Distancia")
        dist_dim.SetGlobalSpanCostCoefficient(100)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    n_nodos = len(distancias)
    params.time_limit.seconds = max(10, min(60, n_nodos * 2))

    sol = routing.SolveWithParameters(params)
    if not sol:
        return None

    rutas = []
    for v in range(n_vehiculos):
        idx = routing.Start(v)
        ruta = []
        while not routing.IsEnd(idx):
            ruta.append(manager.IndexToNode(idx))
            idx = sol.Value(routing.NextVar(idx))
        ruta.append(manager.IndexToNode(idx))
        rutas.append(ruta)
    return rutas


def generar_links_google_maps(locations_in_order):
    """Divide la ruta en segmentos de máx. 10 puntos (límite de Google Maps sin API)."""
    CHUNK = 9
    links = []
    puntos = locations_in_order
    i = 0
    seg_num = 1
    while i < len(puntos) - 1:
        chunk = puntos[i: i + CHUNK + 2]
        if len(chunk) < 2:
            break
        origin, destination = chunk[0], chunk[-1]
        waypoints = chunk[1:-1]
        url = ("https://www.google.com/maps/dir/?api=1"
               f"&origin={origin[0]},{origin[1]}"
               f"&destination={destination[0]},{destination[1]}")
        if waypoints:
            url += "&waypoints=" + "|".join(f"{lat},{lon}" for lat, lon in waypoints)
        url += "&travelmode=driving"
        es_ultimo = (i + CHUNK + 1 >= len(puntos) - 1)
        links.append((f"Segmento {seg_num}" + (" (final)" if es_ultimo else ""), url))
        i += CHUNK + 1
        seg_num += 1
    return links


# ─────────────────────────────────────────────
# EXPORTADORES (multi-camión)
# ─────────────────────────────────────────────
def exportar_geojson(res):
    import json
    features = []
    for c in res["camiones"]:
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString",
                         "coordinates": [[lon, lat] for lat, lon in c["camino"]]},
            "properties": {"camion": c["nombre"], "tipo": "ruta",
                           "distancia_km": round(c["dist_total_m"] / 1000, 2)},
        })
        for fila in c["resumen"]:
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point",
                             "coordinates": [fila["lon"], fila["lat"]]},
                "properties": {
                    "camion": c["nombre"], "orden": fila["orden"],
                    "nombre": fila["Nombre"], "hora_llegada": fila["Hora llegada"],
                    "peso_kg": fila["Peso recogido (kg)"],
                    "tipo": "depot" if str(fila["Nombre"]).startswith("DEPOT") else "parada",
                },
            })
    return json.dumps({"type": "FeatureCollection", "features": features},
                      ensure_ascii=False, indent=2).encode("utf-8")


def exportar_shapefile(res):
    import io, zipfile, tempfile, os
    import geopandas as gpd
    from shapely.geometry import LineString, Point

    lineas, puntos = [], []
    for c in res["camiones"]:
        lineas.append({"camion": c["nombre"],
                       "dist_km": round(c["dist_total_m"] / 1000, 2),
                       "geometry": LineString([(lon, lat) for lat, lon in c["camino"]])})
        for fila in c["resumen"]:
            peso = fila["Peso recogido (kg)"]
            puntos.append({"camion": c["nombre"], "orden": fila["orden"],
                           "nombre": fila["Nombre"], "hora": fila["Hora llegada"],
                           "peso_kg": float(peso) if str(peso) not in ("", "-") else 0.0,
                           "geometry": Point(fila["lon"], fila["lat"])})

    gdf_lineas = gpd.GeoDataFrame(lineas, crs="EPSG:4326")
    gdf_puntos = gpd.GeoDataFrame(puntos, crs="EPSG:4326")

    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for nombre_capa, gdf in [("rutas_lineas", gdf_lineas), ("rutas_puntos", gdf_puntos)]:
                capa_dir = os.path.join(tmpdir, nombre_capa)
                os.makedirs(capa_dir)
                gdf.to_file(os.path.join(capa_dir, f"{nombre_capa}.shp"), driver="ESRI Shapefile")
                for fname in os.listdir(capa_dir):
                    zf.write(os.path.join(capa_dir, fname), fname)
    buf.seek(0)
    return buf.read()


def exportar_gpx(res):
    from xml.etree.ElementTree import Element, SubElement, tostring
    from xml.dom import minidom
    gpx = Element("gpx", {"version": "1.1", "creator": "Optimizador de Rutas",
                          "xmlns": "http://www.topografix.com/GPX/1/1"})
    for c in res["camiones"]:
        for fila in c["resumen"]:
            wpt = SubElement(gpx, "wpt", {"lat": str(fila["lat"]), "lon": str(fila["lon"])})
            SubElement(wpt, "name").text = f"[{c['nombre']}] {fila['Nombre']}"
            SubElement(wpt, "desc").text = (f"Orden: {fila['orden']} | Hora: {fila['Hora llegada']} | "
                                            f"Peso: {fila['Peso recogido (kg)']} kg")
        trk = SubElement(gpx, "trk")
        SubElement(trk, "name").text = f"Ruta {c['nombre']}"
        trkseg = SubElement(trk, "trkseg")
        for lat, lon in c["camino"]:
            SubElement(trkseg, "trkpt", {"lat": str(lat), "lon": str(lon)})
    raw = tostring(gpx, encoding="unicode")
    return minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8")


def exportar_kml(res):
    from xml.etree.ElementTree import Element, SubElement, tostring
    from xml.dom import minidom
    kml = Element("kml", {"xmlns": "http://www.opengis.net/kml/2.2"})
    doc = SubElement(kml, "Document")
    SubElement(doc, "name").text = "Rutas de recolección"
    for i in range(len(COLORES_KML)):
        style = SubElement(doc, "Style", {"id": f"ruta{i}"})
        ls = SubElement(style, "LineStyle")
        SubElement(ls, "color").text = COLORES_KML[i]
        SubElement(ls, "width").text = "4"
    for vi, c in enumerate(res["camiones"]):
        folder = SubElement(doc, "Folder")
        SubElement(folder, "name").text = c["nombre"]
        for fila in c["resumen"]:
            pm = SubElement(folder, "Placemark")
            SubElement(pm, "name").text = fila["Nombre"]
            SubElement(pm, "description").text = (
                f"{c['nombre']} | Orden: {fila['orden']} | Hora: {fila['Hora llegada']} | "
                f"Peso: {fila['Peso recogido (kg)']} kg")
            pt = SubElement(pm, "Point")
            SubElement(pt, "coordinates").text = f"{fila['lon']},{fila['lat']},0"
        pm_ruta = SubElement(folder, "Placemark")
        SubElement(pm_ruta, "name").text = f"Recorrido {c['nombre']}"
        SubElement(pm_ruta, "styleUrl").text = f"#ruta{vi % len(COLORES_KML)}"
        ls2 = SubElement(pm_ruta, "LineString")
        SubElement(ls2, "tessellate").text = "1"
        SubElement(ls2, "coordinates").text = " ".join(
            f"{lon},{lat},0" for lat, lon in c["camino"])
    raw = tostring(kml, encoding="unicode")
    return minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8")


# ─────────────────────────────────────────────
# SESSION STATE Y DATOS INICIALES
# ─────────────────────────────────────────────
if "resultados" not in st.session_state:
    st.session_state.resultados = None

datos_default_puntos = pd.DataFrame({
    "Nombre":    ["Punto 1", "Punto 2", "Punto 3", "Punto 4", "Punto 5", "Punto 6"],
    "Dirección": ["", "", "", "", "", ""],
    "Latitud":   [9.934804, 9.936133, 9.931150, 9.979572, 10.016073, 9.996015],
    "Longitud":  [-84.081784, -84.082634, -84.093640, -84.152163, -84.215665, -84.118091],
    "Peso (kg)": [50, 80, 120, 60, 90, 110],
    "Camión":    ["Auto"] * 6,
})
datos_default_camiones = pd.DataFrame({
    "Nombre": ["Camión 1"],
    "Capacidad (kg)": [1000.0],
})

if db.hay_puntos_guardados():
    datos_puntos = db.cargar_puntos()
else:
    datos_puntos = datos_default_puntos
    db.guardar_puntos(datos_default_puntos)

if db.hay_camiones_guardados():
    datos_camiones = db.cargar_camiones()
else:
    datos_camiones = datos_default_camiones
    db.guardar_camiones(datos_default_camiones)

# ─────────────────────────────────────────────
# SIDEBAR — configuración general
# ─────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Configuración")
    hora_inicio_str = st.text_input("Hora de inicio (HH:MM)",
                                    value=db.obtener_config("hora_inicio", "08:00"))
    try:
        hora_inicio = datetime.strptime(hora_inicio_str, "%H:%M").time()
    except ValueError:
        st.warning("Formato inválido, usando 08:00.")
        hora_inicio = datetime.strptime("08:00", "%H:%M").time()

    velocidad_kmh = st.number_input("Velocidad promedio (km/h)", 10, 120,
                                    value=int(db.obtener_config("velocidad_kmh", 40)))
    tiempo_parada = st.number_input("Tiempo por parada (min)", 1, 60,
                                    value=int(db.obtener_config("tiempo_parada", 10)))
    balancear = st.checkbox(
        "Balancear rutas entre camiones",
        value=db.obtener_config("balancear", "0") == "1",
        help="Si está activo, reparte las paradas entre todos los camiones aunque "
             "el peso quepa en uno solo. Si está inactivo, usa la menor cantidad "
             "de camiones posible (menor distancia total).",
    )
    st.divider()
    st.header("📍 Depot de salida")
    depot_lat = st.number_input("Latitud", value=float(db.obtener_config("depot_lat", 9.964356)), format="%.6f")
    depot_lon = st.number_input("Longitud", value=float(db.obtener_config("depot_lon", -84.161528)), format="%.6f")

    depot_distinto = st.checkbox(
        "El depot de llegada es distinto",
        value=db.obtener_config("depot_distinto", "0") == "1",
        help="Activalo si los camiones terminan la jornada en un lugar "
             "diferente al de salida (otra bodega, plantel, relleno, etc.).",
    )
    if depot_distinto:
        st.header("🏁 Depot de llegada (fijo)")
        st.caption("El punto de llegada es fijo y no cambia entre rutas. "
                   "Solo el de salida se ajusta día a día.")
        editar_llegada = st.checkbox(
            "Desbloquear para corregir", value=False,
            help="Activar únicamente si hay que corregir las coordenadas "
                 "del punto de llegada. Recordá guardar después.",
        )
        depot2_lat = st.number_input(
            "Latitud (llegada)",
            value=float(db.obtener_config("depot2_lat", 9.964356)),
            format="%.6f", disabled=not editar_llegada)
        depot2_lon = st.number_input(
            "Longitud (llegada)",
            value=float(db.obtener_config("depot2_lon", -84.161528)),
            format="%.6f", disabled=not editar_llegada)
    else:
        depot2_lat, depot2_lon = depot_lat, depot_lon

    if st.button("Guardar configuración", use_container_width=True):
        db.guardar_configuracion_general(
            hora_inicio=hora_inicio_str, velocidad_kmh=velocidad_kmh,
            tiempo_parada=tiempo_parada, balancear="1" if balancear else "0",
            depot_lat=depot_lat, depot_lon=depot_lon,
            depot_distinto="1" if depot_distinto else "0",
            depot2_lat=depot2_lat, depot2_lon=depot2_lon,
        )
        st.success("Guardada")

# ─────────────────────────────────────────────
# PESTAÑAS
# ─────────────────────────────────────────────
tab_puntos, tab_camiones, tab_resultados, tab_costos, tab_exportar = st.tabs(
    ["📋 Puntos", "🚛 Camiones", "🗺️ Resultados", "💰 Costos", "📤 Exportar"]
)

# ══════════════ TAB CAMIONES ══════════════
with tab_camiones:
    st.subheader("🚛 Flota de camiones")
    st.caption("Agregá una fila por camión. Cada uno puede tener capacidad distinta.")
    tabla_camiones = st.data_editor(
        datos_camiones, num_rows="dynamic", use_container_width=True,
        column_config={
            "Capacidad (kg)": st.column_config.NumberColumn(min_value=1, format="%.0f"),
        },
        key="editor_camiones",
    )
    if st.button("Guardar camiones"):
        db.guardar_camiones(tabla_camiones)
        st.success("Camiones guardados (recargá para ver los nombres en la tabla de puntos)")

    cams_validos = tabla_camiones.dropna(subset=["Nombre", "Capacidad (kg)"])
    if len(cams_validos) > 0:
        cap_total = cams_validos["Capacidad (kg)"].sum()
        st.metric("Capacidad total de la flota", f"{cap_total:,.0f} kg")

nombres_camiones = tabla_camiones.dropna(subset=["Nombre"])["Nombre"].tolist()

# ══════════════ TAB PUNTOS ══════════════
with tab_puntos:
    st.subheader("📋 Puntos de Recolección")
    st.caption('Columna **Camión**: "Auto" deja que el optimizador decida; '
               "elegí un camión específico para forzar que ese punto vaya con él.")
    tabla = st.data_editor(
        datos_puntos, num_rows="dynamic", use_container_width=True,
        column_config={
            "Latitud":   st.column_config.NumberColumn(format="%.6f"),
            "Longitud":  st.column_config.NumberColumn(format="%.6f"),
            "Peso (kg)": st.column_config.NumberColumn(min_value=0),
            "Dirección": st.column_config.TextColumn(width="large"),
            "Camión":    st.column_config.SelectboxColumn(
                options=["Auto"] + nombres_camiones, default="Auto"),
        },
        key="editor_puntos",
    )

    peso_total_puntos = tabla["Peso (kg)"].fillna(0).sum()
    cap_flota = tabla_camiones.dropna(subset=["Capacidad (kg)"])["Capacidad (kg)"].sum()
    c1, c2 = st.columns(2)
    c1.metric("Peso total a recolectar", f"{peso_total_puntos:,.0f} kg")
    if peso_total_puntos > cap_flota:
        c2.error(f"Excede la capacidad de la flota ({cap_flota:,.0f} kg). "
                 "Agregá camiones o capacidad antes de calcular.")
    else:
        c2.success(f"Dentro de la capacidad de la flota ({cap_flota:,.0f} kg)")

    col_b1, col_b2, _ = st.columns([1, 1, 2])
    with col_b1:
        if st.button("Geocodificar direcciones"):
            pendientes = tabla[
                tabla["Dirección"].fillna("").str.strip().ne("")
                & (tabla["Latitud"].isna() | tabla["Longitud"].isna())
            ]
            if len(pendientes) == 0:
                st.info("No hay direcciones pendientes.")
            else:
                with st.spinner(f"Geocodificando {len(pendientes)}..."):
                    errores_geo = []
                    for idx in pendientes.index:
                        direccion = tabla.loc[idx, "Dirección"]
                        lat, lon, err = geocodificar_direccion(direccion)
                        if lat is not None:
                            tabla.loc[idx, "Latitud"] = lat
                            tabla.loc[idx, "Longitud"] = lon
                        else:
                            errores_geo.append(f"{direccion}: {err}")
                        time.sleep(1)
                    db.guardar_puntos(tabla)
                    if errores_geo:
                        st.warning("No se pudieron geocodificar:\n" + "\n".join(errores_geo))
                    st.rerun()
    with col_b2:
        if st.button("Guardar puntos"):
            db.guardar_puntos(tabla)
            st.success("Guardados")

# ══════════════ CÁLCULO (botón siempre visible bajo las pestañas) ══════════════
st.divider()
if st.button("🔍 Calcular Rutas Óptimas", type="primary", use_container_width=True):
    db.guardar_puntos(tabla)
    db.guardar_camiones(tabla_camiones)

    puntos = tabla.dropna(subset=["Latitud", "Longitud"])
    cams = tabla_camiones.dropna(subset=["Nombre", "Capacidad (kg)"])

    if len(puntos) < 1:
        st.error("Necesitás al menos 1 punto con coordenadas.")
        st.stop()
    if len(cams) < 1:
        st.error("Necesitás al menos 1 camión (pestaña Camiones).")
        st.stop()

    DEPOT = (depot_lat, depot_lon)
    LOCATIONS = [DEPOT] + list(zip(puntos["Latitud"], puntos["Longitud"]))
    NOMBRES = ["DEPOT"] + puntos["Nombre"].tolist()
    PESOS = [0] + puntos["Peso (kg)"].fillna(0).tolist()

    end_node = None
    if depot_distinto:
        LOCATIONS.append((depot2_lat, depot2_lon))
        NOMBRES[0] = "DEPOT SALIDA"
        NOMBRES.append("DEPOT LLEGADA")
        PESOS.append(0)
        end_node = len(LOCATIONS) - 1
    CAPACIDADES = cams["Capacidad (kg)"].tolist()
    NOMBRES_CAM = cams["Nombre"].tolist()

    if sum(PESOS) > sum(CAPACIDADES):
        st.error(f"El peso total ({sum(PESOS):,.0f} kg) excede la capacidad de la "
                 f"flota ({sum(CAPACIDADES):,.0f} kg). Agregá camiones o capacidad.")
        st.stop()

    # Asignaciones manuales: nodo → índice de camión
    asignaciones = {}
    camion_col = puntos["Camión"].fillna("Auto").tolist()
    for i, cam_nombre in enumerate(camion_col):
        if cam_nombre != "Auto" and cam_nombre in NOMBRES_CAM:
            asignaciones[i + 1] = NOMBRES_CAM.index(cam_nombre)  # +1 por el depot

    with st.spinner("Consultando OSRM y optimizando rutas..."):
        distancias, uso_osrm, error_matriz = obtener_matriz_osrm(LOCATIONS)
        rutas = resolver_vrp(distancias, PESOS, CAPACIDADES,
                             asignaciones=asignaciones or None, balancear=balancear,
                             end_node=end_node)

        if rutas is None:
            st.error("No se encontró solución. Posibles causas: asignaciones manuales "
                     "imposibles de cumplir con las capacidades, o capacidad insuficiente.")
            st.stop()

        camiones_res = []
        errores_osrm = []
        for v, ruta_nodos in enumerate(rutas):
            if len(ruta_nodos) <= 2:
                continue  # camión sin paradas
            stops = [LOCATIONS[n] for n in ruta_nodos]
            camino, dist_legs, err = obtener_ruta_completa_osrm(stops)
            if err:
                errores_osrm.append(f"{NOMBRES_CAM[v]}: {err}")

            hora_actual = datetime.combine(datetime.today(), hora_inicio)
            peso_acum = 0
            resumen = []
            for i, node in enumerate(ruta_nodos):
                lat, lon = LOCATIONS[node]
                if i == 0:
                    resumen.append({
                        "orden": 0, "lat": lat, "lon": lon,
                        "Parada": "Inicio (Depot)", "Nombre": NOMBRES[node],
                        "Hora llegada": hora_actual.strftime("%H:%M"),
                        "Peso recogido (kg)": 0, "Peso acumulado (kg)": 0,
                        "Distancia tramo (km)": "-",
                    })
                else:
                    dist_m = dist_legs[i - 1]
                    hora_actual += timedelta(hours=(dist_m / 1000) / velocidad_kmh)
                    es_ultimo = (i == len(ruta_nodos) - 1)
                    peso_p = PESOS[node] if not es_ultimo else 0
                    peso_acum += peso_p
                    resumen.append({
                        "orden": i, "lat": lat, "lon": lon,
                        "Parada": "Fin (Depot)" if es_ultimo else f"Parada {i}",
                        "Nombre": NOMBRES[node],
                        "Hora llegada": hora_actual.strftime("%H:%M"),
                        "Peso recogido (kg)": peso_p,
                        "Peso acumulado (kg)": peso_acum,
                        "Distancia tramo (km)": f"{dist_m / 1000:.2f}",
                    })
                    if not es_ultimo:
                        hora_actual += timedelta(minutes=tiempo_parada)

            camiones_res.append({
                "nombre": NOMBRES_CAM[v],
                "capacidad": CAPACIDADES[v],
                "vehiculo_idx": v,
                "ruta_nodos": ruta_nodos,
                "camino": camino,
                "dist_legs_m": dist_legs,
                "dist_total_m": sum(dist_legs),
                "resumen": resumen,
                "peso_total": peso_acum,
                "hora_fin": hora_actual.strftime("%H:%M"),
            })

        st.session_state.resultados = {
            "camiones": camiones_res,
            "uso_osrm": uso_osrm,
            "error_matriz": error_matriz,
            "errores_osrm": errores_osrm,
            "hora_inicio": hora_inicio.strftime("%H:%M"),
        }
    st.success(f"Rutas calculadas para {len(camiones_res)} camión(es). "
               "Mirá la pestaña Resultados.")

# ══════════════ TAB RESULTADOS ══════════════
with tab_resultados:
    if not st.session_state.resultados:
        st.info("Todavía no hay rutas calculadas. Cargá puntos y camiones y presioná "
                "**Calcular Rutas Óptimas**.")
    else:
        r = st.session_state.resultados
        if r["uso_osrm"]:
            st.success("Distancias reales por carretera (OSRM)")
        else:
            st.warning(f"Usando línea recta. Motivo: {r['error_matriz']}")
        if r["errores_osrm"]:
            st.info("Tramos con línea recta por fallas puntuales: " + "; ".join(r["errores_osrm"]))

        dist_total = sum(c["dist_total_m"] for c in r["camiones"]) / 1000
        peso_total = sum(c["peso_total"] for c in r["camiones"])
        hora_fin_max = max(c["hora_fin"] for c in r["camiones"])

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Camiones usados", len(r["camiones"]))
        m2.metric("Distancia total", f"{dist_total:.1f} km")
        m3.metric("Peso total", f"{peso_total:,.0f} kg")
        m4.metric("Fin estimado (último camión)", hora_fin_max)

        # Mapa combinado con un color por camión
        st.subheader("🗺️ Mapa de rutas")
        all_lats = [lat for c in r["camiones"] for lat, lon in c["camino"]]
        all_lons = [lon for c in r["camiones"] for lat, lon in c["camino"]]
        centro = (sum(all_lats) / len(all_lats), sum(all_lons) / len(all_lons))
        m = folium.Map(location=centro, zoom_start=12, tiles=None)
        folium.TileLayer("OpenStreetMap", name="Mapa estándar").add_to(m)
        folium.TileLayer(
            "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/"
            "MapServer/tile/{z}/{y}/{x}",
            attr="Esri World Imagery", name="Satélite",
        ).add_to(m)
        folium.TileLayer("CartoDB positron", name="Claro").add_to(m)
        folium.TileLayer("CartoDB dark_matter", name="Oscuro").add_to(m)

        for vi, c in enumerate(r["camiones"]):
            color = COLORES[c["vehiculo_idx"] % len(COLORES)]
            folium.PolyLine(c["camino"], color=color, weight=4, opacity=0.85,
                            tooltip=c["nombre"]).add_to(m)
            for fila in c["resumen"][:-1]:
                if fila["orden"] == 0:
                    depot_html = (
                        '<div style="font-family:Segoe UI,Arial,sans-serif;'
                        'font-size:15px;font-weight:700;white-space:nowrap;'
                        'padding:4px 8px;">DEPOT — Inicio y fin de rutas</div>'
                    )
                    folium.Marker(
                        [fila["lat"], fila["lon"]],
                        popup=folium.Popup(depot_html, max_width=280),
                        tooltip="DEPOT",
                        icon=folium.Icon(color="red", icon="home"),
                    ).add_to(m)
                else:
                    icon_html = (f'<div style="background:{color};color:white;border-radius:50%;'
                                 f'width:32px;height:32px;display:flex;align-items:center;'
                                 f'justify-content:center;font-size:14px;font-weight:bold;'
                                 f'border:2px solid white;box-shadow:2px 2px 4px rgba(0,0,0,0.4);">'
                                 f'{fila["orden"]}</div>')
                    popup_html = (
                        f'<div style="font-family:Segoe UI,Arial,sans-serif;'
                        f'font-size:14px;line-height:1.7;white-space:nowrap;'
                        f'padding:4px 6px;min-width:190px;">'
                        f'<div style="font-size:16px;font-weight:700;'
                        f'border-bottom:2.5px solid {color};'
                        f'padding-bottom:5px;margin-bottom:7px;">'
                        f'{fila["Nombre"]}</div>'
                        f'<table style="border-collapse:collapse;font-size:14px;">'
                        f'<tr><td style="color:#6B7280;padding-right:12px;">Camión</td>'
                        f'<td style="font-weight:600;">{c["nombre"]}</td></tr>'
                        f'<tr><td style="color:#6B7280;padding-right:12px;">Llegada</td>'
                        f'<td style="font-weight:600;">{fila["Hora llegada"]}</td></tr>'
                        f'<tr><td style="color:#6B7280;padding-right:12px;">Peso</td>'
                        f'<td style="font-weight:600;">{fila["Peso recogido (kg)"]:g} kg</td></tr>'
                        f'</table></div>'
                    )
                    folium.Marker(
                        [fila["lat"], fila["lon"]],
                        popup=folium.Popup(popup_html, max_width=320),
                        tooltip=f"{c['nombre']} · {fila['orden']}. {fila['Nombre']}",
                        icon=folium.DivIcon(html=icon_html, icon_size=(32, 32), icon_anchor=(16, 16)),
                    ).add_to(m)
        # Marcador del depot de llegada (si es distinto al de salida)
        ultima = r["camiones"][0]["resumen"][-1] if r["camiones"] else None
        if ultima is not None and ultima["Nombre"] == "DEPOT LLEGADA":
            llegada_html = (
                '<div style="font-family:Segoe UI,Arial,sans-serif;'
                'font-size:15px;font-weight:700;white-space:nowrap;'
                'padding:4px 8px;">DEPOT LLEGADA — Fin de rutas</div>'
            )
            folium.Marker(
                [ultima["lat"], ultima["lon"]],
                popup=folium.Popup(llegada_html, max_width=280),
                tooltip="DEPOT LLEGADA",
                icon=folium.Icon(color="green", icon="flag"),
            ).add_to(m)

        Fullscreen(position="topright", title="Pantalla completa",
                   title_cancel="Salir de pantalla completa").add_to(m)
        folium.LayerControl(position="topright", collapsed=True).add_to(m)
        st_folium(m, use_container_width=True, height=760, returned_objects=[])

        # Detalle por camión
        st.subheader("Detalle por camión")
        for vi, c in enumerate(r["camiones"]):
            color = COLORES[c["vehiculo_idx"] % len(COLORES)]
            uso_pct = c["peso_total"] / c["capacidad"] * 100 if c["capacidad"] else 0
            with st.expander(
                f"{c['nombre']}   |   {len(c['resumen']) - 2} paradas   |   "
                f"{c['dist_total_m'] / 1000:.1f} km   |   carga {uso_pct:.0f}%",
                expanded=(len(r["camiones"]) == 1),
            ):
                e1, e2, e3, e4 = st.columns(4)
                e1.metric("Paradas", len(c["resumen"]) - 2)
                e2.metric("Distancia", f"{c['dist_total_m'] / 1000:.1f} km")
                e3.metric("Carga", f"{c['peso_total']:,.0f} kg",
                          delta=f"{uso_pct:.0f}% de {c['capacidad']:,.0f} kg",
                          delta_color="off")
                e4.metric("Hora de fin", c["hora_fin"])

                df_c = pd.DataFrame(c["resumen"]).drop(columns=["lat", "lon", "orden"])
                for col_peso in ["Peso recogido (kg)", "Peso acumulado (kg)"]:
                    df_c[col_peso] = df_c[col_peso].map(lambda v: f"{float(v):,.0f}")
                st.table(df_c.style.hide(axis="index"))

# ══════════════ TAB COSTOS ══════════════
with tab_costos:
    st.subheader("💰 Comparación de modelos de ruta")
    st.caption("Compare el costo de la recolección bajo el modelo actual contra "
               "el modelo nuevo (rutas optimizadas). Cada modelo tiene sus propias "
               "toneladas y su propio precio por tonelada.")

    toneladas_ruta = None
    if st.session_state.resultados:
        toneladas_ruta = sum(
            c["peso_total"] for c in st.session_state.resultados["camiones"]
        ) / 1000

    col_actual, col_nuevo = st.columns(2)
    with col_actual:
        st.markdown("##### Modelo actual")
        ton_actual_bruta = st.number_input(
            "Toneladas recolectadas — modelo actual (total histórico)",
            min_value=0.0, step=0.5, format="%.2f",
            value=float(db.obtener_config("ton_actual", 0)),
            help="Total de toneladas que maneja hoy el modelo actual, "
                 "antes de restar lo que absorbe el proyecto nuevo.",
        )
        precio_actual = st.number_input(
            "Precio por tonelada (CRC) — modelo actual",
            min_value=0.0, step=1000.0, format="%.2f",
            value=float(db.obtener_config("precio_ton_actual", 0)),
        )
    with col_nuevo:
        st.markdown("##### Modelo nuevo (rutas optimizadas)")
        ton_nuevo = st.number_input(
            "Toneladas recolectadas — modelo nuevo",
            min_value=0.0, step=0.5, format="%.2f",
            value=float(toneladas_ruta) if toneladas_ruta is not None
                  else float(db.obtener_config("ton_nuevo", 0)),
            help="Se pre-llena con el peso total de las rutas calculadas. "
                 "Puede modificarse para evaluar otros escenarios.",
        )
        if toneladas_ruta is not None and abs(ton_nuevo - toneladas_ruta) > 0.001:
            st.caption(f"Las rutas calculadas suman {toneladas_ruta:.2f} ton.")
        precio_nuevo = st.number_input(
            "Precio por tonelada (CRC) — modelo nuevo",
            min_value=0.0, step=1000.0, format="%.2f",
            value=float(db.obtener_config("precio_ton_nuevo", 0)),
        )

    if st.button("Guardar parámetros de comparación"):
        db.guardar_configuracion_general(
            ton_actual=ton_actual_bruta, precio_ton_actual=precio_actual,
            ton_nuevo=ton_nuevo, precio_ton_nuevo=precio_nuevo,
        )
        st.success("Parámetros guardados.")

    # Las toneladas que ahora recoge el modelo nuevo dejan de estar
    # disponibles para el modelo actual — se restan del total histórico.
    ton_actual_neta = max(ton_actual_bruta - ton_nuevo, 0.0)

    st.markdown(
        f"**Toneladas netas del modelo actual:** {ton_actual_bruta:.2f} ton "
        f"(total histórico) − {ton_nuevo:.2f} ton (absorbidas por el nuevo modelo) "
        f"= **{ton_actual_neta:.2f} ton**"
    )
    st.caption(
        "El costo del modelo actual se calcula sobre las toneladas netas, "
        "ya que las toneladas que recoge el modelo nuevo ya no están "
        "disponibles para el modelo actual."
    )

    costo_modelo_actual = ton_actual_neta * precio_actual
    costo_modelo_nuevo = ton_nuevo * precio_nuevo
    diferencia = costo_modelo_actual - costo_modelo_nuevo

    k1, k2, k3 = st.columns(3)
    k1.metric("Costo modelo actual (neto)", f"CRC {costo_modelo_actual:,.2f}",
              help=f"{ton_actual_neta:.2f} ton x CRC {precio_actual:,.2f}")
    k2.metric("Costo modelo nuevo", f"CRC {costo_modelo_nuevo:,.2f}",
              help=f"{ton_nuevo:.2f} ton x CRC {precio_nuevo:,.2f}")
    k3.metric(
        "Diferencia (ahorro)", f"CRC {diferencia:,.2f}",
        delta=(f"{(diferencia / costo_modelo_actual * 100):.1f}%"
               if costo_modelo_actual > 0 else None),
        delta_color="normal" if diferencia >= 0 else "inverse",
    )

    st.divider()
    st.subheader("⛽ Combustible y operación (informativo)")
    st.caption("Sección de referencia: estos valores NO se suman a la comparación "
               "de arriba. Sirven para estimar el costo operativo de las rutas "
               "calculadas.")

    col_f1, col_f2 = st.columns(2)
    with col_f1:
        rendimiento = st.number_input(
            "Rendimiento del camión (km por litro)", min_value=0.1, step=0.5,
            format="%.2f", value=float(db.obtener_config("rendimiento", 5.0)),
        )
        precio_litro = st.number_input(
            "Precio del combustible (CRC por litro)", min_value=0.0, step=10.0,
            format="%.2f", value=float(db.obtener_config("precio_litro", 0)),
        )
    with col_f2:
        costo_km_extra = st.number_input(
            "Otros costos por km (CRC) — mantenimiento, llantas",
            min_value=0.0, step=10.0, format="%.2f",
            value=float(db.obtener_config("costo_km_extra", 0)),
        )
        costo_fijo_dia = st.number_input(
            "Costos fijos por día (CRC) — salarios, seguros",
            min_value=0.0, step=1000.0, format="%.2f",
            value=float(db.obtener_config("costo_fijo_dia", 0)),
        )

    if st.button("Guardar parámetros de combustible"):
        db.guardar_configuracion_general(
            rendimiento=rendimiento, precio_litro=precio_litro,
            costo_km_extra=costo_km_extra, costo_fijo_dia=costo_fijo_dia,
        )
        st.success("Parámetros guardados.")

    if not st.session_state.resultados:
        st.info("Calcule las rutas para estimar el consumo (se necesitan los km recorridos).")
    else:
        r = st.session_state.resultados
        km_total = sum(c["dist_total_m"] for c in r["camiones"]) / 1000
        litros = km_total / rendimiento if rendimiento > 0 else 0
        costo_combustible = litros * precio_litro
        costo_variable = km_total * costo_km_extra
        costo_operativo = costo_combustible + costo_variable + costo_fijo_dia

        st.markdown(f"**Base del cálculo:** {km_total:.1f} km recorridos · "
                    f"{litros:.1f} litros estimados")

        f1, f2, f3, f4 = st.columns(4)
        f1.metric("Combustible", f"CRC {costo_combustible:,.2f}")
        f2.metric("Variables por km", f"CRC {costo_variable:,.2f}")
        f3.metric("Fijos del día", f"CRC {costo_fijo_dia:,.2f}")
        f4.metric("Costo operativo total", f"CRC {costo_operativo:,.2f}")

        with st.expander("Ver desglose"):
            desglose = pd.DataFrame([
                {"Concepto": "Combustible", "Monto (CRC)": f"{costo_combustible:,.0f}",
                 "Detalle": f"{litros:.1f} L x CRC {precio_litro:,.0f}"},
                {"Concepto": "Costos variables por km", "Monto (CRC)": f"{costo_variable:,.0f}",
                 "Detalle": f"{km_total:.1f} km x CRC {costo_km_extra:,.0f}"},
                {"Concepto": "Costos fijos del día", "Monto (CRC)": f"{costo_fijo_dia:,.0f}",
                 "Detalle": "Salarios, seguros, etc."},
                {"Concepto": "TOTAL operativo", "Monto (CRC)": f"{costo_operativo:,.0f}",
                 "Detalle": ""},
                {"Concepto": "Costo operativo por tonelada",
                 "Monto (CRC)": (f"{(costo_operativo / ton_nuevo):,.0f}"
                                 if ton_nuevo > 0 else "-"),
                 "Detalle": f"Sobre {ton_nuevo:.2f} ton del modelo nuevo"},
            ])
            st.dataframe(desglose, use_container_width=True, hide_index=True)

# ══════════════ TAB EXPORTAR ══════════════
with tab_exportar:
    if not st.session_state.resultados:
        st.info("Calculá las rutas primero.")
    else:
        r = st.session_state.resultados

        st.subheader("📤 Exportar resultados")
        st.markdown("##### Datos y SIG")
        # CSV combinado con columna Camión
        filas_csv = []
        for c in r["camiones"]:
            for fila in c["resumen"]:
                f2 = {"Camión": c["nombre"], **{k: v for k, v in fila.items()
                                                if k not in ("lat", "lon", "orden")}}
                filas_csv.append(f2)
        df_export = pd.DataFrame(filas_csv)

        col_e1, col_e2 = st.columns(2)
        with col_e1:
            st.download_button("CSV (todas las rutas)",
                               df_export.to_csv(index=False).encode("utf-8"),
                               "rutas_optimas.csv", "text/csv", use_container_width=True)
        with col_e2:
            geojson_bytes = exportar_geojson(r)
            st.download_button("GeoJSON (QGIS / ArcGIS / web)", geojson_bytes,
                               "rutas_optimas.geojson", "application/geo+json",
                               use_container_width=True)

        col_e3, col_e4 = st.columns(2)
        with col_e3:
            shp_bytes = exportar_shapefile(r)
            st.download_button("Shapefile (.zip)", shp_bytes, "rutas_optimas_shp.zip",
                               "application/zip", use_container_width=True)
            st.caption("Capas: rutas_lineas + rutas_puntos, con atributo de camión.")
        with col_e4:
            gpx_bytes = exportar_gpx(r)
            st.download_button("GPX (OsmAnd / Garmin)", gpx_bytes, "rutas_optimas.gpx",
                               "application/gpx+xml", use_container_width=True)
            st.caption("Un track por camión.")

        kml_bytes = exportar_kml(r)
        st.download_button("KML (Google Earth / My Maps)", kml_bytes, "rutas_optimas.kml",
                           "application/vnd.google-earth.kml+xml", use_container_width=True)

        st.divider()
        st.markdown("##### Navegación")
        st.markdown("**Google Maps por camión** (segmentos de máx. 10 paradas):")
        for c in r["camiones"]:
            stops = [(fila["lat"], fila["lon"]) for fila in c["resumen"]]
            links = generar_links_google_maps(stops)
            st.markdown(f"**{c['nombre']}:**")
            cols = st.columns(min(len(links), 4))
            for j, (label, url) in enumerate(links):
                cols[j % len(cols)].link_button(label, url, use_container_width=True)

        with st.expander("Cómo usar con Waze"):
            st.markdown(
                "Waze no tiene API de multi-paradas. Opciones: **(A)** links parada por "
                "parada abajo, o **(B)** importar el GPX en [OsmAnd](https://osmand.net/) "
                "(gratis), que navega la ruta completa con voz."
            )
            for c in r["camiones"]:
                st.markdown(f"**{c['nombre']}:**")
                for fila in c["resumen"][1:-1]:
                    waze_url = f"https://waze.com/ul?ll={fila['lat']},{fila['lon']}&navigate=yes"
                    st.markdown(f"- Parada {fila['orden']} — {fila['Nombre']} "
                                f"({fila['Hora llegada']}): [Abrir en Waze]({waze_url})")
