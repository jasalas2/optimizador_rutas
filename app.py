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


def _migrar_columnas(conn, tabla, columnas_esperadas):
    """Agrega columnas nuevas a una tabla ya existente (creada en una versión
    anterior de la app), sin borrar los datos que ya tenía. CREATE TABLE IF
    NOT EXISTS no modifica una tabla que ya existe, así que las columnas
    agregadas en versiones nuevas necesitan esta migración explícita."""
    existentes = {row[1] for row in conn.execute(f"PRAGMA table_info({tabla})")}
    for nombre_col, tipo_sql in columnas_esperadas:
        if nombre_col not in existentes:
            conn.execute(f"ALTER TABLE {tabla} ADD COLUMN {nombre_col} {tipo_sql}")


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

        # Migraciones: agregan columnas nuevas si la base de datos viene de
        # una versión anterior de la app (creada antes de que existieran).
        _migrar_columnas(conn, "puntos", [
            ("camion_asignado", "TEXT"),
        ])
        _migrar_columnas(conn, "camiones", [
            ("personas", "INTEGER DEFAULT 1"),
            ("viajes_max", "INTEGER DEFAULT 1"),
            ("plantel_lat", "REAL"),
            ("plantel_lon", "REAL"),
            ("salida_lat", "REAL"),
            ("salida_lon", "REAL"),
        ])

        # Estructura de costos: Inversión (monto + vida útil) y Mantenimiento /
        # Administrativa (monto + frecuencia), cada una como tabla de renglones
        # libres que el usuario puede agregar/quitar.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS costos_inversion (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                concepto TEXT NOT NULL,
                monto REAL DEFAULT 0,
                vida_util_anios REAL DEFAULT 1
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS costos_mantenimiento (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                concepto TEXT NOT NULL,
                monto REAL DEFAULT 0,
                frecuencia TEXT DEFAULT 'Mes'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS costos_administrativa (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                concepto TEXT NOT NULL,
                monto REAL DEFAULT 0,
                frecuencia TEXT DEFAULT 'Mes'
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


CAMIONES_DB_TO_UI = {
    "nombre": "Nombre", "capacidad_kg": "Capacidad (kg)",
    "personas": "Personas", "viajes_max": "Viajes máx.",
    "salida_lat": "Salida Lat", "salida_lon": "Salida Lon",
    "plantel_lat": "Plantel Lat", "plantel_lon": "Plantel Lon",
}
CAMIONES_UI_TO_DB = {v: k for k, v in CAMIONES_DB_TO_UI.items()}
CAMIONES_COLS_DB = list(CAMIONES_DB_TO_UI.keys())


def cargar_camiones():
    with get_conn() as conn:
        df = pd.read_sql_query(
            f"SELECT {', '.join(CAMIONES_COLS_DB)} FROM camiones ORDER BY id", conn
        )
    df = df.rename(columns=CAMIONES_DB_TO_UI)
    df["Personas"] = df["Personas"].fillna(1).astype(int)
    df["Viajes máx."] = df["Viajes máx."].fillna(1).astype(int)
    return df


def guardar_camiones(df_ui):
    df = df_ui.rename(columns=CAMIONES_UI_TO_DB).copy()
    for col in CAMIONES_COLS_DB:
        if col not in df.columns:
            df[col] = None
    df = df.dropna(subset=["nombre"])
    with get_conn() as conn:
        conn.execute("DELETE FROM camiones")
        if len(df) > 0:
            df[CAMIONES_COLS_DB].to_sql("camiones", conn, if_exists="append", index=False)


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


# ── Estructura de costos (Inversión / Mantenimiento / Administrativa) ──────
FRECUENCIAS_DIAS = {"Día": 1, "Semana": 7, "Mes": 30, "Año": 365}

COSTOS_INVERSION_DB_TO_UI = {"concepto": "Concepto", "monto": "Monto total (CRC)",
                             "vida_util_anios": "Vida útil (años)"}
COSTOS_RECURRENTE_DB_TO_UI = {"concepto": "Concepto", "monto": "Monto (CRC)",
                              "frecuencia": "Frecuencia"}


def _cargar_costos(tabla, mapeo_db_to_ui):
    with get_conn() as conn:
        df = pd.read_sql_query(
            f"SELECT {', '.join(mapeo_db_to_ui.keys())} FROM {tabla} ORDER BY id", conn
        )
    return df.rename(columns=mapeo_db_to_ui)


def _guardar_costos(tabla, df_ui, mapeo_db_to_ui):
    mapeo_ui_to_db = {v: k for k, v in mapeo_db_to_ui.items()}
    df = df_ui.rename(columns=mapeo_ui_to_db).copy()
    df = df.dropna(subset=["concepto"])
    with get_conn() as conn:
        conn.execute(f"DELETE FROM {tabla}")
        if len(df) > 0:
            df[list(mapeo_db_to_ui.keys())].to_sql(tabla, conn, if_exists="append", index=False)


def hay_costos_inversion_guardados():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM costos_inversion").fetchone()[0] > 0


def cargar_costos_inversion():
    return _cargar_costos("costos_inversion", COSTOS_INVERSION_DB_TO_UI)


def guardar_costos_inversion(df_ui):
    _guardar_costos("costos_inversion", df_ui, COSTOS_INVERSION_DB_TO_UI)


def hay_costos_mantenimiento_guardados():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM costos_mantenimiento").fetchone()[0] > 0


def cargar_costos_mantenimiento():
    return _cargar_costos("costos_mantenimiento", COSTOS_RECURRENTE_DB_TO_UI)


def guardar_costos_mantenimiento(df_ui):
    _guardar_costos("costos_mantenimiento", df_ui, COSTOS_RECURRENTE_DB_TO_UI)


def hay_costos_administrativa_guardados():
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM costos_administrativa").fetchone()[0] > 0


def cargar_costos_administrativa():
    return _cargar_costos("costos_administrativa", COSTOS_RECURRENTE_DB_TO_UI)


def guardar_costos_administrativa(df_ui):
    _guardar_costos("costos_administrativa", df_ui, COSTOS_RECURRENTE_DB_TO_UI)


def costo_diario_inversion(df_ui):
    """Suma de monto / (vida_util_años × 365) — prorrateo diario de compras grandes."""
    total = 0.0
    for _, fila in df_ui.iterrows():
        monto = fila.get("Monto total (CRC)")
        vida = fila.get("Vida útil (años)")
        if pd.notna(monto) and pd.notna(vida) and vida > 0:
            total += float(monto) / (float(vida) * 365)
    return total


def costo_diario_recurrente(df_ui):
    """Suma de monto / días-de-la-frecuencia — prorrateo diario de gastos periódicos."""
    total = 0.0
    for _, fila in df_ui.iterrows():
        monto = fila.get("Monto (CRC)")
        frecuencia = fila.get("Frecuencia")
        if pd.notna(monto) and frecuencia in FRECUENCIAS_DIAS:
            total += float(monto) / FRECUENCIAS_DIAS[frecuencia]
    return total


class _DB:
    """Espacio de nombres para mantener las llamadas db.xxx() del resto del código."""
    pass


db = _DB()
for _f in (init_db, hay_puntos_guardados, cargar_puntos, guardar_puntos,
           hay_camiones_guardados, cargar_camiones, guardar_camiones,
           obtener_config, guardar_config, guardar_configuracion_general,
           hay_costos_inversion_guardados, cargar_costos_inversion, guardar_costos_inversion,
           hay_costos_mantenimiento_guardados, cargar_costos_mantenimiento, guardar_costos_mantenimiento,
           hay_costos_administrativa_guardados, cargar_costos_administrativa, guardar_costos_administrativa,
           costo_diario_inversion, costo_diario_recurrente):
    setattr(db, _f.__name__, _f)


st.set_page_config(page_title="Optimizador de Rutas", layout="wide")
st.markdown("""
<style>
footer {visibility: hidden;}

/* Tipografía base más grande y legible */
html, body, [data-testid="stAppViewContainer"] {font-size: 17px;}
h1 {font-size: 1.6rem; font-weight: 700; letter-spacing: -0.01em; margin-bottom: 0.2rem;}
h2 {font-size: 1.5rem; font-weight: 650;}
h3 {font-size: 1.25rem; font-weight: 600;}
p, li, label {font-size: 1.02rem;}
[data-testid="stCaptionContainer"] {font-size: 0.95rem;}

/* Pestañas grandes, arriba de la página */
/* Selectores redundantes a propósito: cubren TODAS las variantes del DOM
   que Streamlit ha usado en distintas versiones (p, div, span, markdown) */
.stTabs [data-baseweb="tab-list"] button [data-testid="stMarkdownContainer"] p,
.stTabs [data-baseweb="tab-list"] button p,
.stTabs [data-baseweb="tab-list"] button div,
.stTabs [data-baseweb="tab-list"] button span,
.stTabs [data-baseweb="tab"] p,
.stTabs [data-baseweb="tab"] div,
.stTabs [data-baseweb="tab"] {
    font-size: 1.5rem !important;
    font-weight: 700 !important;
    line-height: 1.3 !important;
}
.stTabs [data-baseweb="tab-list"] button,
button[data-baseweb="tab"] {
    padding: 1.0rem 1.9rem !important;
    height: auto !important;
    min-height: 3.4rem !important;
}
.stTabs [data-baseweb="tab-list"] {
    gap: 0.5rem;
    position: sticky; top: 0; z-index: 999;
    background: white;
    border-bottom: 2px solid #E5EAF2;
    padding-top: 0.3rem;
}
.stTabs [data-baseweb="tab-highlight"] {
    background-color: #2563EB; height: 4px;
}

/* Ocultar el header vacío de Streamlit para que las pestañas queden
   pegadas ARRIBA de la página y el sticky funcione desde el borde superior */
header[data-testid="stHeader"] {
    height: 0 !important; min-height: 0 !important; visibility: hidden;
}
[data-testid="stAppViewContainer"] > .main .block-container {
    padding-top: 1.2rem !important;
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


def resolver_vrp(distancias, demandas, capacidades, start_nodes, end_node,
                 asignaciones=None, balancear=False, viajes_max=1):
    """
    VRP multi-vehículo con restricción de capacidad POR CAMIÓN, SALIDA
    PROPIA POR CAMIÓN, y soporte de VIAJES MÚLTIPLES: un camión puede
    llenarse, ir a descargar al depot de llegada (siempre el mismo, un
    único vertedero/relleno para toda la flota), y volver a salir a
    recolectar más, hasta su propio máximo de viajes.

    Se modela internamente con "pseudo-vehículos": cada camión real se
    representa como N vehículos de OR-Tools encadenados (N = su propio
    viajes_max) — el primero sale del punto de salida DE ESE CAMIÓN
    (start_nodes[i]), los siguientes "salen" directamente del depot de
    llegada (porque ahí es donde el camión real queda parqueado tras
    descargar). Todos terminan en el depot de llegada. Al final se
    agrupan de vuelta por camión real.

    - distancias: matriz NxN en metros
    - demandas: peso en kg de cada nodo
    - capacidades: lista con la capacidad en kg de cada camión real
    - start_nodes: lista con el nodo de salida de CADA camión (uno por
      camión, pueden repetirse si dos camiones salen del mismo lugar)
    - end_node: nodo único del depot de llegada/descarga (compartido por
      todos los camiones y todos los viajes)
    - asignaciones: dict {nodo: índice_camión_real} para fijar manualmente
      (el punto puede caer en cualquiera de los viajes de ESE camión)
    - balancear: penaliza que un camión recorra mucho más que otro
    - viajes_max: máximo de viajes por camión. Puede ser:
        · un entero → se aplica igual a todos los camiones
        · una lista del mismo largo que `capacidades` → un valor por camión

    Devuelve una lista por camión real, y cada elemento es a su vez una
    lista de "viajes" (sub-rutas) EFECTIVAMENTE USADOS, en orden:
        [
          [[s0, 3, 1, end], [end, 5, 2, end]],   # Camión 0: usó 2 viajes
          [[s1, 4, end]],                          # Camión 1: usó 1 viaje
          ...
        ]
    Un camión sin ningún punto asignado devuelve [] (lista vacía de viajes).
    """
    n_camiones = len(capacidades)
    assert len(start_nodes) == n_camiones, "start_nodes debe tener un valor por camión"
    if isinstance(viajes_max, (list, tuple)):
        vm_list = [max(1, int(v)) for v in viajes_max]
        assert len(vm_list) == n_camiones, "viajes_max debe tener un valor por camión"
    else:
        vm_list = [max(1, int(viajes_max))] * n_camiones

    real_end = end_node
    n_pseudo = sum(vm_list)

    # Offsets: en qué índice de pseudo-vehículo empieza cada camión real
    offsets = [0]
    for vm in vm_list:
        offsets.append(offsets[-1] + vm)

    starts, ends = [], []
    for i in range(n_camiones):
        for trip in range(vm_list[i]):
            starts.append(start_nodes[i] if trip == 0 else real_end)
            ends.append(real_end)

    manager = pywrapcp.RoutingIndexManager(len(distancias), n_pseudo, starts, ends)
    routing = pywrapcp.RoutingModel(manager)

    def cb_dist(from_idx, to_idx):
        return distancias[manager.IndexToNode(from_idx)][manager.IndexToNode(to_idx)]

    t = routing.RegisterTransitCallback(cb_dist)
    routing.SetArcCostEvaluatorOfAllVehicles(t)

    # ── Restricción de capacidad (por viaje, no por camión completo) ──
    def cb_demanda(from_idx):
        return int(demandas[manager.IndexToNode(from_idx)])

    d = routing.RegisterUnaryTransitCallback(cb_demanda)
    pseudo_capacidades = [
        int(capacidades[i]) for i in range(n_camiones) for _ in range(vm_list[i])
    ]
    routing.AddDimensionWithVehicleCapacity(d, 0, pseudo_capacidades, True, "Capacidad")

    # ── Asignación manual: el punto puede ir en CUALQUIER viaje de ese camión ──
    if asignaciones:
        for nodo, camion_idx in asignaciones.items():
            index = manager.NodeToIndex(nodo)
            permitidos = list(range(offsets[camion_idx], offsets[camion_idx + 1]))
            routing.VehicleVar(index).SetValues(permitidos)

    # ── Balanceo opcional ──
    if balancear:
        routing.AddDimension(t, 0, 3_000_000, True, "Distancia")
        dist_dim = routing.GetDimensionOrDie("Distancia")
        dist_dim.SetGlobalSpanCostCoefficient(100)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    n_nodos = len(distancias)
    params.time_limit.seconds = max(10, min(90, n_nodos * max(vm_list) * 2))

    sol = routing.SolveWithParameters(params)
    if not sol:
        return None

    # ── Extraer la ruta cruda de cada pseudo-vehículo ──
    rutas_pseudo = []
    for v in range(n_pseudo):
        idx = routing.Start(v)
        ruta = []
        while not routing.IsEnd(idx):
            ruta.append(manager.IndexToNode(idx))
            idx = sol.Value(routing.NextVar(idx))
        ruta.append(manager.IndexToNode(idx))
        rutas_pseudo.append(ruta)

    # ── Agrupar de vuelta por camión real, quedándonos solo con los
    #    viajes que efectivamente recogieron algo (más de 2 nodos) ──
    resultado = []
    for i in range(n_camiones):
        viajes_camion = rutas_pseudo[offsets[i]: offsets[i + 1]]
        usados = [v for v in viajes_camion if len(v) > 2]

        # Si se usó algún viaje posterior al primero, pero el primero
        # (el único que realmente sale del depot de salida) quedó vacío,
        # igual hay que representar ese trayecto inicial obligatorio
        # (el camión tiene que llegar físicamente hasta el depot de
        # llegada antes de poder volver a salir).
        if usados and len(viajes_camion[0]) <= 2 and viajes_camion[0] not in usados:
            resultado.append([viajes_camion[0]] + usados)
        else:
            resultado.append(usados)
    return resultado


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
# RED PROPIA (BETA) — motor de rutas sobre un shapefile de líneas
# Completamente independiente del optimizador principal (que usa OSRM).
# No comparte estado ni variables con el resto de la app.
# ─────────────────────────────────────────────
def _haversine_m_red(a, b):
    """a, b en formato (lon, lat)."""
    R = 6_371_000
    lat1, lon1 = math.radians(a[1]), math.radians(a[0])
    lat2, lon2 = math.radians(b[1]), math.radians(b[0])
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(h))


def construir_grafo_red(gdf_lineas, tolerancia_m=5.0):
    """
    Arma un grafo (networkx) a partir de las líneas de un GeoDataFrame.

    Dos pasadas de robustez, pensadas para shapefiles reales (que casi
    nunca vienen topológicamente perfectos):

    1. `unary_union` sobre todas las líneas: parte automáticamente cada
       línea en cada punto donde CRUZA a otra, aunque no compartan un
       vértice explícito ahí (dos calles que se cruzan en la mitad, no
       solo en sus extremos).
    2. Tolerancia de `tolerancia_m` metros entre extremos: dos puntos que
       deberían ser el mismo cruce, pero quedaron a unos centímetros/metros
       de distancia por error de digitalización, se tratan como un único
       nodo.

    Devuelve (grafo, lista_de_coordenadas_de_cada_nodo).
    Si el shapefile tiene líneas sueltas (no conectadas), el grafo queda
    con varios "componentes" separados — se reporta aparte, no es un error.
    """
    import networkx as nx
    from shapely.ops import unary_union

    geometrias = [g for g in gdf_lineas.geometry if g is not None and not g.is_empty]
    if not geometrias:
        return nx.Graph(), []

    union = unary_union(geometrias)
    if union.geom_type == "LineString":
        partes = [union]
    elif union.geom_type == "MultiLineString":
        partes = list(union.geoms)
    else:
        # GeometryCollection u otro tipo mixto: quedarnos solo con las líneas
        partes = [g for g in getattr(union, "geoms", [union]) if g.geom_type == "LineString"]

    G = nx.Graph()
    nodos = []

    def nodo_id(coord):
        for i, existente in enumerate(nodos):
            if _haversine_m_red(coord, existente) <= tolerancia_m:
                return i
        nodos.append(coord)
        return len(nodos) - 1

    for parte in partes:
        coords = list(parte.coords)
        for i in range(len(coords) - 1):
            a, b = coords[i], coords[i + 1]
            na, nb = nodo_id(a), nodo_id(b)
            if na == nb:
                continue
            dist = _haversine_m_red(a, b)
            if G.has_edge(na, nb):
                if dist < G[na][nb]["weight"]:
                    G[na][nb]["weight"] = dist
            else:
                G.add_edge(na, nb, weight=dist)
    return G, nodos


def enganchar_a_red(punto_lonlat, nodos):
    """Nodo más cercano de la red a un punto dado. Devuelve (nodo_id, distancia_m)."""
    mejor_id, mejor_dist = None, float("inf")
    for i, n in enumerate(nodos):
        d = _haversine_m_red(punto_lonlat, n)
        if d < mejor_dist:
            mejor_id, mejor_dist = i, d
    return mejor_id, mejor_dist


def matriz_distancias_red(puntos_lonlat, G, nodos):
    """
    Distancias por la red (Dijkstra) entre todos los pares de puntos.
    Si dos puntos no están en el mismo componente conectado (red
    fragmentada / líneas sueltas), cae a línea recta para ESE par y lo
    reporta en `pares_sin_red` para poder avisarle al usuario.
    """
    import networkx as nx
    n = len(puntos_lonlat)
    enganches = [enganchar_a_red(p, nodos) for p in puntos_lonlat]
    nodos_enganchados = [e[0] for e in enganches]

    matriz = [[0.0] * n for _ in range(n)]
    pares_sin_red = []

    for i in range(n):
        try:
            dist_desde_i = nx.single_source_dijkstra_path_length(
                G, nodos_enganchados[i], weight="weight")
        except nx.NodeNotFound:
            dist_desde_i = {}
        for j in range(n):
            if i == j:
                continue
            nodo_j = nodos_enganchados[j]
            if nodo_j in dist_desde_i:
                matriz[i][j] = dist_desde_i[nodo_j]
            else:
                matriz[i][j] = _haversine_m_red(puntos_lonlat[i], puntos_lonlat[j])
                if (j, i) not in pares_sin_red:
                    pares_sin_red.append((i, j))
    return matriz, nodos_enganchados, enganches, pares_sin_red


def contar_componentes_red(G):
    """Lista de componentes conectados del grafo (cada uno, un set de nodos)."""
    import networkx as nx
    return list(nx.connected_components(G))


def camino_geometria_red(G, nodos, nodo_a, nodo_b):
    """Coordenadas (lon, lat) del camino más corto entre dos nodos de la red."""
    import networkx as nx
    try:
        ruta = nx.shortest_path(G, nodo_a, nodo_b, weight="weight")
        return [nodos[n] for n in ruta]
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return [nodos[nodo_a], nodos[nodo_b]]


def _normalizar_gdf_lineas(gdf):
    """Reproyecta a EPSG:4326 y filtra solo geometrías de línea."""
    if gdf.crs is not None and str(gdf.crs) != "EPSG:4326":
        gdf = gdf.to_crs("EPSG:4326")
    tipos_validos = {"LineString", "MultiLineString"}
    gdf = gdf[gdf.geometry.geom_type.isin(tipos_validos)]
    if len(gdf) == 0:
        return None, ("El archivo no contiene geometrías de línea "
                      "(¿es una capa de puntos o polígonos?).")
    return gdf, None


def leer_capa_lineas(archivos_subidos):
    """
    Lee una capa de líneas desde archivos subidos, en cualquiera de estos
    formatos:
      A) Un .zip conteniendo el shapefile (aunque los archivos estén dentro
         de una subcarpeta, como pasa al comprimir con clic derecho en Windows)
      B) Un .geojson / .json
      C) Un .gpkg (GeoPackage)
      D) Los archivos del shapefile SUELTOS sin comprimir: .shp + .shx + .dbf
         (y .prj si existe), subidos juntos en la misma carga

    Devuelve (GeoDataFrame en EPSG:4326, None) o (None, mensaje_error).
    """
    import zipfile
    import tempfile
    import os
    import geopandas as gpd

    if not archivos_subidos:
        return None, "No se subió ningún archivo."

    nombres = [a.name.lower() for a in archivos_subidos]

    # ── B) GeoJSON directo ──
    for archivo, nombre in zip(archivos_subidos, nombres):
        if nombre.endswith((".geojson", ".json")):
            try:
                gdf = gpd.read_file(archivo)
            except Exception as e:
                return None, f"No se pudo leer el GeoJSON: {e}"
            return _normalizar_gdf_lineas(gdf)

    # ── C) GeoPackage directo ──
    for archivo, nombre in zip(archivos_subidos, nombres):
        if nombre.endswith(".gpkg"):
            with tempfile.TemporaryDirectory() as tmpdir:
                ruta = os.path.join(tmpdir, "capa.gpkg")
                with open(ruta, "wb") as f:
                    f.write(archivo.getbuffer())
                try:
                    gdf = gpd.read_file(ruta)
                except Exception as e:
                    return None, f"No se pudo leer el GeoPackage: {e}"
            return _normalizar_gdf_lineas(gdf)

    # ── A) Zip con shapefile (búsqueda RECURSIVA del .shp, tolera subcarpetas) ──
    for archivo, nombre in zip(archivos_subidos, nombres):
        if nombre.endswith(".zip"):
            with tempfile.TemporaryDirectory() as tmpdir:
                try:
                    with zipfile.ZipFile(archivo) as zf:
                        zf.extractall(tmpdir)
                except zipfile.BadZipFile:
                    return None, "El archivo no es un .zip válido."

                shp_path = None
                for raiz, _, archivos_dir in os.walk(tmpdir):
                    for fname in archivos_dir:
                        if fname.lower().endswith(".shp"):
                            shp_path = os.path.join(raiz, fname)
                            break
                    if shp_path:
                        break
                if shp_path is None:
                    return None, ("No se encontró ningún archivo .shp dentro del .zip "
                                  "(ni en subcarpetas). Verificá el contenido del zip.")
                try:
                    gdf = gpd.read_file(shp_path)
                except Exception as e:
                    return None, f"No se pudo leer el shapefile: {e}"
            return _normalizar_gdf_lineas(gdf)

    # ── D) Archivos del shapefile sueltos (.shp + .shx + .dbf juntos) ──
    if any(n.endswith(".shp") for n in nombres):
        requeridos = {".shp", ".shx", ".dbf"}
        extensiones = {os.path.splitext(n)[1] for n in nombres}
        faltantes = requeridos - extensiones
        if faltantes:
            return None, (f"Faltan archivos del shapefile: {', '.join(sorted(faltantes))}. "
                          "Subí juntos el .shp, .shx y .dbf (y el .prj si lo tenés).")
        with tempfile.TemporaryDirectory() as tmpdir:
            base = None
            for archivo, nombre in zip(archivos_subidos, nombres):
                ruta = os.path.join(tmpdir, os.path.basename(nombre))
                with open(ruta, "wb") as f:
                    f.write(archivo.getbuffer())
                if nombre.endswith(".shp"):
                    base = ruta
            try:
                gdf = gpd.read_file(base)
            except Exception as e:
                return None, f"No se pudo leer el shapefile: {e}"
        return _normalizar_gdf_lineas(gdf)

    return None, ("Formato no reconocido. Subí un .zip con el shapefile, un "
                  ".geojson, un .gpkg, o los archivos .shp + .shx + .dbf juntos.")


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
                    "tipo": fila["tipo"],
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
                           "nombre": fila["Nombre"], "tipo": fila["tipo"],
                           "hora": fila["Hora llegada"],
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
    "Personas": [1],
    "Viajes máx.": [1],
    "Salida Lat": [9.964356],
    "Salida Lon": [-84.161528],
    "Plantel Lat": [9.964356],
    "Plantel Lon": [-84.161528],
})

if db.hay_puntos_guardados():
    datos_puntos = db.cargar_puntos()
else:
    datos_puntos = datos_default_puntos
    db.guardar_puntos(datos_default_puntos)

if db.hay_camiones_guardados():
    datos_camiones = db.cargar_camiones()
    # Migración de datos: si algún camión no tiene su propio punto de Salida
    # (porque se guardó antes de este cambio, cuando la salida era una sola
    # configuración global), se rellena con el viejo "depot_lat"/"depot_lon"
    # si existía, para no dejar a nadie con el campo vacío.
    faltan_salida = datos_camiones["Salida Lat"].isna() | datos_camiones["Salida Lon"].isna()
    if faltan_salida.any():
        fallback_lat = float(db.obtener_config("depot_lat", 9.964356))
        fallback_lon = float(db.obtener_config("depot_lon", -84.161528))
        datos_camiones.loc[faltan_salida, "Salida Lat"] = fallback_lat
        datos_camiones.loc[faltan_salida, "Salida Lon"] = fallback_lon
        db.guardar_camiones(datos_camiones)
else:
    datos_camiones = datos_default_camiones
    db.guardar_camiones(datos_default_camiones)

# ─────────────────────────────────────────────
# SIDEBAR — configuración general
# ─────────────────────────────────────────────
with st.sidebar:
    st.header("Configuración")
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
    st.header("Planta San Antonio")
    st.caption(
        "Punto único donde TODOS los camiones descargan — siempre es el "
        "mismo, no cambia entre rutas. La salida de cada camión se configura "
        "por separado, en la pestaña Camiones."
    )
    depot2_lat = st.number_input(
        "Latitud", value=float(db.obtener_config("depot2_lat", 9.964356)), format="%.6f")
    depot2_lon = st.number_input(
        "Longitud", value=float(db.obtener_config("depot2_lon", -84.161528)), format="%.6f")

    if st.button("Guardar configuración", use_container_width=True):
        db.guardar_configuracion_general(
            hora_inicio=hora_inicio_str, velocidad_kmh=velocidad_kmh,
            tiempo_parada=tiempo_parada, balancear="1" if balancear else "0",
            depot2_lat=depot2_lat, depot2_lon=depot2_lon,
        )
        st.success("Guardada")

# ─────────────────────────────────────────────
# PESTAÑAS
# ─────────────────────────────────────────────
tab_puntos, tab_camiones, tab_resultados, tab_costos, tab_exportar, tab_red_propia = st.tabs(
    ["Puntos", "Camiones", "Resultados", "Costos", "Exportar",
     "Red propia (Beta)"]
)

# ══════════════ TAB CAMIONES ══════════════
with tab_camiones:
    st.subheader("Flota de camiones")
    st.caption(
        "Agregá una fila por camión. **Salida** es de dónde arranca ese camión "
        "cada día (suele ser distinta por camión). **Viajes máx.** es cuántas "
        "veces puede llenarse, ir a descargar al depot de llegada y volver a "
        "salir en el mismo día (1 = un solo viaje). **Plantel** son las "
        "coordenadas donde se guarda al terminar la jornada."
    )
    tabla_camiones = st.data_editor(
        datos_camiones, num_rows="dynamic", use_container_width=True,
        column_config={
            "Capacidad (kg)": st.column_config.NumberColumn(min_value=1, format="%.0f"),
            "Personas": st.column_config.NumberColumn(
                min_value=1, format="%d",
                help="Cantidad de personas que trabajan en ese camión (chofer + ayudantes).",
            ),
            "Viajes máx.": st.column_config.NumberColumn(
                min_value=1, format="%d",
                help="Máximo de veces que puede volver a salir tras descargar, en el mismo día.",
            ),
            "Salida Lat": st.column_config.NumberColumn(format="%.6f"),
            "Salida Lon": st.column_config.NumberColumn(format="%.6f"),
            "Plantel Lat": st.column_config.NumberColumn(format="%.6f"),
            "Plantel Lon": st.column_config.NumberColumn(format="%.6f"),
        },
        key="editor_camiones",
    )
    if st.button("Guardar camiones"):
        db.guardar_camiones(tabla_camiones)
        st.success("Camiones guardados (recargá para ver los nombres en la tabla de puntos)")

    cams_validos = tabla_camiones.dropna(subset=["Nombre", "Capacidad (kg)"])
    if len(cams_validos) > 0:
        cc1, cc2, cc3 = st.columns(3)
        cc1.metric("Capacidad total de la flota", f"{cams_validos['Capacidad (kg)'].sum():,.0f} kg")
        cc2.metric("Personas totales", f"{int(cams_validos['Personas'].fillna(1).sum())}")
        cc3.metric("Capacidad total considerando viajes",
                   f"{(cams_validos['Capacidad (kg)'] * cams_validos['Viajes máx.'].fillna(1)).sum():,.0f} kg",
                   help="Capacidad × viajes máx. de cada camión, sumado — el tope real "
                        "de recolección diaria de toda la flota.")

nombres_camiones = tabla_camiones.dropna(subset=["Nombre"])["Nombre"].tolist()

# ══════════════ TAB PUNTOS ══════════════
with tab_puntos:
    st.subheader("Puntos de Recolección")
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
if st.button("Calcular Rutas Óptimas", type="primary", use_container_width=True):
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

    CAPACIDADES = cams["Capacidad (kg)"].tolist()
    NOMBRES_CAM = cams["Nombre"].tolist()
    PERSONAS_CAM = cams["Personas"].fillna(1).astype(int).tolist()
    VIAJES_MAX_CAM = cams["Viajes máx."].fillna(1).astype(int).tolist()
    SALIDA_LAT_CAM = cams["Salida Lat"].tolist()
    SALIDA_LON_CAM = cams["Salida Lon"].tolist()
    PLANTEL_LAT_CAM = cams["Plantel Lat"].tolist()
    PLANTEL_LON_CAM = cams["Plantel Lon"].tolist()

    if any(pd.isna(SALIDA_LAT_CAM[i]) or pd.isna(SALIDA_LON_CAM[i])
           for i in range(len(NOMBRES_CAM))):
        st.error("Todos los camiones necesitan su punto de Salida (Lat/Lon) "
                 "completo en la pestaña Camiones.")
        st.stop()

    n_camiones_flota = len(NOMBRES_CAM)
    # Nodos: [salida de cada camión] + [puntos de recolección] + [depot de llegada, único]
    LOCATIONS = (
        [(SALIDA_LAT_CAM[i], SALIDA_LON_CAM[i]) for i in range(n_camiones_flota)]
        + list(zip(puntos["Latitud"], puntos["Longitud"]))
        + [(depot2_lat, depot2_lon)]
    )
    NOMBRES = (
        [f"DEPOT SALIDA — {NOMBRES_CAM[i]}" for i in range(n_camiones_flota)]
        + puntos["Nombre"].tolist()
        + ["DEPOT LLEGADA"]
    )
    PESOS = [0] * n_camiones_flota + puntos["Peso (kg)"].fillna(0).tolist() + [0]

    start_nodes = list(range(n_camiones_flota))   # cada camión, su propio nodo
    end_node = len(LOCATIONS) - 1                 # el vertedero, compartido
    real_end_coords = LOCATIONS[end_node]

    # Si no se definió plantel para un camión, se asume que vuelve al depot de llegada
    PLANTEL_CAM = [
        (PLANTEL_LAT_CAM[i], PLANTEL_LON_CAM[i])
        if pd.notna(PLANTEL_LAT_CAM[i]) and pd.notna(PLANTEL_LON_CAM[i])
        else real_end_coords
        for i in range(n_camiones_flota)
    ]

    capacidad_efectiva_flota = sum(
        CAPACIDADES[i] * VIAJES_MAX_CAM[i] for i in range(len(CAPACIDADES))
    )
    if sum(PESOS) > capacidad_efectiva_flota:
        st.error(f"El peso total ({sum(PESOS):,.0f} kg) excede la capacidad efectiva "
                 f"de la flota considerando viajes ({capacidad_efectiva_flota:,.0f} kg). "
                 "Agregá camiones, capacidad, o viajes máximos.")
        st.stop()

    # Asignaciones manuales: nodo → índice de camión
    asignaciones = {}
    camion_col = puntos["Camión"].fillna("Auto").tolist()
    for i, cam_nombre in enumerate(camion_col):
        if cam_nombre != "Auto" and cam_nombre in NOMBRES_CAM:
            # +n_camiones_flota porque los primeros nodos son las salidas
            asignaciones[i + n_camiones_flota] = NOMBRES_CAM.index(cam_nombre)

    with st.spinner("Consultando OSRM y optimizando rutas..."):
        distancias, uso_osrm, error_matriz = obtener_matriz_osrm(LOCATIONS)
        rutas = resolver_vrp(distancias, PESOS, CAPACIDADES, start_nodes, end_node,
                             asignaciones=asignaciones or None, balancear=balancear,
                             viajes_max=VIAJES_MAX_CAM)

        if rutas is None:
            st.error("No se encontró solución. Posibles causas: asignaciones manuales "
                     "imposibles de cumplir con las capacidades, o capacidad insuficiente "
                     "incluso considerando los viajes máximos configurados.")
            st.stop()

        camiones_res = []
        errores_osrm = []
        for v, viajes_nodos in enumerate(rutas):
            if not viajes_nodos:
                continue  # camión sin ningún viaje usado

            hora_actual = datetime.combine(datetime.today(), hora_inicio)
            peso_dia = 0
            orden_counter = 0
            resumen = []
            camino_total = []
            tramos = []  # un tramo dibujable por viaje, con su propio color/selección
            dist_recoleccion_m = 0.0

            for trip_idx, ruta_nodos in enumerate(viajes_nodos):
                stops = [LOCATIONS[n] for n in ruta_nodos]
                camino_tramo, dist_legs, err = obtener_ruta_completa_osrm(stops)
                if err:
                    errores_osrm.append(f"{NOMBRES_CAM[v]} (viaje {trip_idx + 1}): {err}")
                dist_recoleccion_m += sum(dist_legs)
                tramos.append({
                    "trip_idx": trip_idx,
                    "etiqueta": f"{NOMBRES_CAM[v]} — Viaje {trip_idx + 1}",
                    "camino": camino_tramo,
                    "dist_m": sum(dist_legs),
                })

                # Evitar duplicar el punto de unión entre el final de un viaje
                # y el inicio del siguiente (ambos son el mismo punto físico).
                camino_total.extend(camino_tramo if trip_idx == 0 else camino_tramo[1:])

                for i, node in enumerate(ruta_nodos):
                    lat, lon = LOCATIONS[node]
                    if i == 0:
                        # El nodo inicial de un viaje POSTERIOR al primero es el
                        # mismo punto físico y el mismo instante que la fila de
                        # "Descarga (fin viaje anterior)" ya agregada — no se
                        # duplica una fila nueva para eso, solo se registra el
                        # inicio real (primer viaje, sale del depot de salida).
                        if trip_idx != 0:
                            continue
                        resumen.append({
                            "orden": orden_counter, "lat": lat, "lon": lon, "tipo": "inicio",
                            "trip_idx": trip_idx,
                            "Parada": "Inicio (Depot Salida)", "Nombre": NOMBRES[node],
                            "Hora llegada": hora_actual.strftime("%H:%M"),
                            "Peso recogido (kg)": 0, "Peso acumulado (kg)": peso_dia,
                            "Distancia tramo (km)": "-",
                        })
                        orden_counter += 1
                    else:
                        dist_m = dist_legs[i - 1]
                        hora_actual += timedelta(hours=(dist_m / 1000) / velocidad_kmh)
                        es_fin_viaje = (i == len(ruta_nodos) - 1)
                        peso_p = PESOS[node] if not es_fin_viaje else 0
                        peso_dia += peso_p
                        tipo = "descarga" if es_fin_viaje else "parada"
                        label = (f"Descarga (fin viaje {trip_idx + 1})" if es_fin_viaje
                                else f"Parada {orden_counter}")
                        resumen.append({
                            "orden": orden_counter, "lat": lat, "lon": lon, "tipo": tipo,
                            "trip_idx": trip_idx,
                            "Parada": label, "Nombre": NOMBRES[node],
                            "Hora llegada": hora_actual.strftime("%H:%M"),
                            "Peso recogido (kg)": peso_p, "Peso acumulado (kg)": peso_dia,
                            "Distancia tramo (km)": f"{dist_m / 1000:.2f}",
                        })
                        orden_counter += 1
                        hora_actual += timedelta(minutes=tiempo_parada)

            # ── Tramo final: del depot de llegada al plantel de ESE camión ──
            plantel_coords = PLANTEL_CAM[v]
            camino_plantel, dist_legs_plantel, err_plantel = obtener_ruta_completa_osrm(
                [real_end_coords, plantel_coords]
            )
            if err_plantel:
                errores_osrm.append(f"{NOMBRES_CAM[v]} (a plantel): {err_plantel}")
            dist_plantel_m = sum(dist_legs_plantel) if dist_legs_plantel else 0.0
            camino_total.extend(camino_plantel[1:] if camino_plantel else [])
            tramos.append({
                "trip_idx": len(viajes_nodos),
                "etiqueta": f"{NOMBRES_CAM[v]} — A plantel",
                "camino": camino_plantel,
                "dist_m": dist_plantel_m,
            })
            if dist_plantel_m > 0:
                hora_actual += timedelta(hours=(dist_plantel_m / 1000) / velocidad_kmh)
            resumen.append({
                "orden": orden_counter, "lat": plantel_coords[0], "lon": plantel_coords[1],
                "tipo": "fin_jornada", "trip_idx": len(viajes_nodos),
                "Parada": "Fin de jornada (Plantel)", "Nombre": "PLANTEL",
                "Hora llegada": hora_actual.strftime("%H:%M"),
                "Peso recogido (kg)": 0, "Peso acumulado (kg)": peso_dia,
                "Distancia tramo (km)": f"{dist_plantel_m / 1000:.2f}",
            })

            camiones_res.append({
                "nombre": NOMBRES_CAM[v],
                "capacidad": CAPACIDADES[v],
                "personas": PERSONAS_CAM[v],
                "viajes_max": VIAJES_MAX_CAM[v],
                "n_viajes_usados": len(viajes_nodos),
                "vehiculo_idx": v,
                "camino": camino_total,
                "tramos": tramos,
                "dist_recoleccion_m": dist_recoleccion_m,
                "dist_plantel_m": dist_plantel_m,
                "dist_total_m": dist_recoleccion_m + dist_plantel_m,
                "resumen": resumen,
                "peso_total": peso_dia,
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
        st.subheader("Mapa de rutas")

        # ── Construir la lista de rutas (viajes) seleccionables, con un
        #    color propio para cada una — así un camión con 2+ viajes se ve
        #    con un color distinto por cada viaje, no todo del mismo color ──
        color_por_ruta = {}
        etiquetas_por_camion = {}
        color_i = 0
        for c in r["camiones"]:
            etiquetas = []
            for tramo in c["tramos"]:
                if tramo["trip_idx"] >= c["n_viajes_usados"]:
                    continue  # el tramo "a plantel" no es una ruta seleccionable
                color_por_ruta[(c["nombre"], tramo["trip_idx"])] = COLORES[color_i % len(COLORES)]
                etiquetas.append(tramo["etiqueta"])
                color_i += 1
            etiquetas_por_camion[c["nombre"]] = etiquetas

        todas_las_rutas = [et for ets in etiquetas_por_camion.values() for et in ets]
        rutas_seleccionadas = st.multiselect(
            "Rutas a mostrar en el mapa", options=todas_las_rutas,
            default=todas_las_rutas,
            help="Cada viaje de cada camión es una ruta independiente, con su "
                 "propio color. Deseleccioná las que no quieras ver.",
        )
        seleccionadas_set = set(rutas_seleccionadas)

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

        marcadores_dibujados = set()  # evita apilar íconos duplicados en el mismo punto

        def _clave(lat, lon):
            return (round(lat, 6), round(lon, 6))

        for vi, c in enumerate(r["camiones"]):
            etiquetas_camion = etiquetas_por_camion[c["nombre"]]
            camion_visible = any(et in seleccionadas_set for et in etiquetas_camion)

            # Líneas: una por tramo (viaje), con su propio color; el tramo
            # "a plantel" se dibuja aparte, gris y punteado, solo si el
            # camión tiene al menos un viaje visible.
            for tramo in c["tramos"]:
                if tramo["trip_idx"] >= c["n_viajes_usados"]:
                    if camion_visible and tramo["camino"]:
                        folium.PolyLine(
                            tramo["camino"], color="#6B7280", weight=3,
                            opacity=0.7, dash_array="6,8",
                            tooltip=f"{c['nombre']} · a plantel",
                        ).add_to(m)
                    continue
                if tramo["etiqueta"] not in seleccionadas_set:
                    continue
                color_ruta = color_por_ruta[(c["nombre"], tramo["trip_idx"])]
                folium.PolyLine(
                    tramo["camino"], color=color_ruta, weight=4, opacity=0.85,
                    tooltip=tramo["etiqueta"],
                ).add_to(m)

            for fila in c["resumen"]:
                clave = _clave(fila["lat"], fila["lon"])

                # Los marcadores de depot (salida/llegada) y plantel son puntos
                # de referencia FIJOS: siempre se muestran, sin importar qué
                # rutas estén (de)seleccionadas — solo la deduplicación por
                # coordenada evita apilar íconos idénticos en el mismo punto.
                if fila["tipo"] == "inicio":
                    if clave in marcadores_dibujados:
                        continue
                    marcadores_dibujados.add(clave)
                    depot_html = (
                        f'<div style="font-family:Segoe UI,Arial,sans-serif;'
                        f'font-size:15px;font-weight:700;white-space:nowrap;'
                        f'padding:4px 8px;">{fila["Nombre"]}</div>'
                    )
                    folium.Marker(
                        [fila["lat"], fila["lon"]],
                        popup=folium.Popup(depot_html, max_width=280),
                        tooltip=fila["Nombre"],
                        icon=folium.Icon(color="red", icon="home"),
                    ).add_to(m)

                elif fila["tipo"] == "descarga":
                    if clave in marcadores_dibujados:
                        continue
                    marcadores_dibujados.add(clave)
                    descarga_html = (
                        '<div style="font-family:Segoe UI,Arial,sans-serif;'
                        'font-size:15px;font-weight:700;white-space:nowrap;'
                        'padding:4px 8px;">DEPOT LLEGADA — Descarga</div>'
                    )
                    folium.Marker(
                        [fila["lat"], fila["lon"]],
                        popup=folium.Popup(descarga_html, max_width=280),
                        tooltip="DEPOT LLEGADA",
                        icon=folium.Icon(color="green", icon="arrow-down"),
                    ).add_to(m)

                elif fila["tipo"] == "fin_jornada":
                    plantel_html = (
                        f'<div style="font-family:Segoe UI,Arial,sans-serif;'
                        f'font-size:15px;font-weight:700;white-space:nowrap;'
                        f'padding:4px 8px;">Plantel — {c["nombre"]}</div>'
                    )
                    folium.Marker(
                        [fila["lat"], fila["lon"]],
                        popup=folium.Popup(plantel_html, max_width=280),
                        tooltip=f"Plantel {c['nombre']}",
                        icon=folium.Icon(color="gray", icon="flag"),
                    ).add_to(m)

                else:  # "parada" — punto de recolección numerado
                    etiqueta_fila = c["tramos"][fila["trip_idx"]]["etiqueta"]
                    if etiqueta_fila not in seleccionadas_set:
                        continue
                    color = color_por_ruta[(c["nombre"], fila["trip_idx"])]
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
                        f'<td style="font-weight:600;">{etiqueta_fila}</td></tr>'
                        f'<tr><td style="color:#6B7280;padding-right:12px;">Llegada</td>'
                        f'<td style="font-weight:600;">{fila["Hora llegada"]}</td></tr>'
                        f'<tr><td style="color:#6B7280;padding-right:12px;">Peso</td>'
                        f'<td style="font-weight:600;">{fila["Peso recogido (kg)"]:g} kg</td></tr>'
                        f'</table></div>'
                    )
                    folium.Marker(
                        [fila["lat"], fila["lon"]],
                        popup=folium.Popup(popup_html, max_width=320),
                        tooltip=f"{etiqueta_fila} · {fila['orden']}. {fila['Nombre']}",
                        icon=folium.DivIcon(html=icon_html, icon_size=(32, 32), icon_anchor=(16, 16)),
                    ).add_to(m)
        Fullscreen(position="topright", title="Pantalla completa",
                   title_cancel="Salir de pantalla completa").add_to(m)
        folium.LayerControl(position="topright", collapsed=True).add_to(m)
        st_folium(m, use_container_width=True, height=760, returned_objects=[])

        # Detalle por camión
        st.subheader("Detalle por camión")
        for vi, c in enumerate(r["camiones"]):
            color = COLORES[c["vehiculo_idx"] % len(COLORES)]
            n_paradas = sum(1 for f in c["resumen"] if f["tipo"] == "parada")
            capacidad_efectiva = c["capacidad"] * c["n_viajes_usados"]
            uso_pct = c["peso_total"] / capacidad_efectiva * 100 if capacidad_efectiva else 0
            viajes_txt = (f"{c['n_viajes_usados']} viaje" +
                         ("s" if c["n_viajes_usados"] != 1 else ""))
            with st.expander(
                f"{c['nombre']}   |   {n_paradas} paradas   |   {viajes_txt}   |   "
                f"{c['dist_total_m'] / 1000:.1f} km   |   carga {uso_pct:.0f}%",
                expanded=(len(r["camiones"]) == 1),
            ):
                e1, e2, e3, e4, e5 = st.columns(5)
                e1.metric("Paradas", n_paradas)
                e2.metric("Viajes usados", f"{c['n_viajes_usados']} de {c['viajes_max']}")
                e3.metric("Distancia total", f"{c['dist_total_m'] / 1000:.1f} km",
                          help=f"Recolección: {c['dist_recoleccion_m'] / 1000:.1f} km · "
                               f"A plantel: {c['dist_plantel_m'] / 1000:.1f} km")
                e4.metric("Carga del día", f"{c['peso_total']:,.0f} kg",
                          delta=f"{uso_pct:.0f}% de capacidad efectiva "
                                f"({capacidad_efectiva:,.0f} kg)",
                          delta_color="off")
                e5.metric("Fin de jornada", c["hora_fin"])

                df_c = pd.DataFrame(c["resumen"]).drop(columns=["lat", "lon", "orden", "tipo"])
                for col_peso in ["Peso recogido (kg)", "Peso acumulado (kg)"]:
                    df_c[col_peso] = df_c[col_peso].map(lambda v: f"{float(v):,.0f}")
                st.table(df_c.style.hide(axis="index"))

# ══════════════ TAB COSTOS ══════════════
with tab_costos:
    st.subheader("Comparación de modelos de ruta")
    st.caption("El costo del modelo nuevo se calcula a partir de la estructura "
               "de costos completa de más abajo (no se escribe a mano). El costo "
               "del modelo actual se compara sobre las toneladas netas, restando "
               "lo que ya absorbe el modelo nuevo.")

    toneladas_ruta = None
    if st.session_state.resultados:
        toneladas_ruta = sum(
            c["peso_total"] for c in st.session_state.resultados["camiones"]
        ) / 1000

    # ── Toneladas de cada modelo ──
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
        # Este espacio se llena más abajo, una vez calculado el costo por
        # tonelada a partir de toda la estructura de costos — pero se
        # muestra aquí arriba para mantener el formato lado a lado.
        precio_nuevo_slot = st.empty()

    if st.button("Guardar toneladas"):
        db.guardar_configuracion_general(ton_actual=ton_actual_bruta, ton_nuevo=ton_nuevo)
        st.success("Toneladas guardadas.")

    resultado_slot = st.container()

    st.divider()

    # ══════════ Estructura de costos (única sección con todos los rubros) ══════════
    st.subheader("Estructura de costos")
    st.caption(
        "Todo lo que aparece acá se suma para calcular el costo operativo "
        "diario, y de ahí el costo real por tonelada del modelo nuevo. Los "
        "rubros con 'Vida útil' (Inversión) se prorratean por año y día; los "
        "que tienen 'Frecuencia' (Mantenimiento, Administrativa) se prorratean "
        "según esa frecuencia."
    )

    # ── Inversión ──
    with st.expander("Inversión (camiones, garaje, otros)", expanded=True):
        st.caption(
            "Montos grandes de compra/construcción, prorrateados por su vida "
            "útil. Ej: un camión de ₡25.000.000 con 10 años de vida útil "
            "aporta ≈ ₡6.849/día al costo operativo."
        )
        datos_inv = db.cargar_costos_inversion() if db.hay_costos_inversion_guardados() else pd.DataFrame({
            "Concepto": ["Camión", "Garaje", "Otros"],
            "Monto total (CRC)": [0.0, 0.0, 0.0],
            "Vida útil (años)": [10.0, 20.0, 5.0],
        })
        tabla_inv = st.data_editor(
            datos_inv, num_rows="dynamic", use_container_width=True,
            column_config={
                "Monto total (CRC)": st.column_config.NumberColumn(min_value=0, format="%.2f"),
                "Vida útil (años)": st.column_config.NumberColumn(min_value=0.1, format="%.1f"),
            },
            key="editor_costos_inversion",
        )
        if st.button("Guardar inversión"):
            db.guardar_costos_inversion(tabla_inv)
            st.success("Inversión guardada.")
        costo_inversion_dia = db.costo_diario_inversion(tabla_inv)
        st.caption(f"Subtotal inversión: CRC {costo_inversion_dia:,.2f} / día")

    # ── Mano de obra ──
    with st.expander("Mano de obra", expanded=True):
        col_mo1, col_mo2 = st.columns(2)
        with col_mo1:
            horas_laboradas = st.number_input(
                "Horas laboradas (jornada)", min_value=0.0, step=0.5, format="%.2f",
                value=float(db.obtener_config("horas_laboradas", 8.0)),
                help="Horas que trabaja la cuadrilla/chofer en la jornada.",
            )
        with col_mo2:
            precio_hora = st.number_input(
                "Precio por hora del trabajador (CRC)",
                min_value=0.0, step=100.0, format="%.2f",
                value=float(db.obtener_config("precio_hora", 0)),
                help="Costo real por hora, incluyendo cargas sociales/CCSS.",
            )
        if st.button("Guardar mano de obra"):
            db.guardar_configuracion_general(horas_laboradas=horas_laboradas, precio_hora=precio_hora)
            st.success("Mano de obra guardada.")
        st.caption("Se multiplica por la cantidad de personas de los camiones "
                   "usados en la ruta calculada (pestaña Camiones).")

    # ── Combustible y variables por km ──
    with st.expander("Combustible y variables por km", expanded=True):
        col_c1, col_c2 = st.columns(2)
        with col_c1:
            rendimiento = st.number_input(
                "Rendimiento del camión (km por litro)", min_value=0.1, step=0.5,
                format="%.2f", value=float(db.obtener_config("rendimiento", 5.0)),
            )
            precio_litro = st.number_input(
                "Precio del combustible (CRC por litro)", min_value=0.0, step=10.0,
                format="%.2f", value=float(db.obtener_config("precio_litro", 0)),
            )
        with col_c2:
            costo_km_extra = st.number_input(
                "Otros costos por km (CRC) — llantas, desgaste",
                min_value=0.0, step=10.0, format="%.2f",
                value=float(db.obtener_config("costo_km_extra", 0)),
            )
        if st.button("Guardar combustible"):
            db.guardar_configuracion_general(
                rendimiento=rendimiento, precio_litro=precio_litro,
                costo_km_extra=costo_km_extra,
            )
            st.success("Combustible guardado.")

    # ── Mantenimiento ──
    with st.expander("Mantenimiento (lavacar, extintores, etc.)"):
        datos_mant = db.cargar_costos_mantenimiento() if db.hay_costos_mantenimiento_guardados() else pd.DataFrame({
            "Concepto": ["Lavado", "Extintores"],
            "Monto (CRC)": [0.0, 0.0],
            "Frecuencia": ["Semana", "Año"],
        })
        tabla_mant = st.data_editor(
            datos_mant, num_rows="dynamic", use_container_width=True,
            column_config={
                "Monto (CRC)": st.column_config.NumberColumn(min_value=0, format="%.2f"),
                "Frecuencia": st.column_config.SelectboxColumn(
                    options=["Día", "Semana", "Mes", "Año"], default="Mes"),
            },
            key="editor_costos_mantenimiento",
        )
        if st.button("Guardar mantenimiento"):
            db.guardar_costos_mantenimiento(tabla_mant)
            st.success("Mantenimiento guardado.")
        costo_mantenimiento_dia = db.costo_diario_recurrente(tabla_mant)
        st.caption(f"Subtotal mantenimiento: CRC {costo_mantenimiento_dia:,.2f} / día")

    # ── Administrativa ──
    with st.expander("Administrativa (contabilidad, permisos, seguros...)"):
        datos_admin = db.cargar_costos_administrativa() if db.hay_costos_administrativa_guardados() else pd.DataFrame({
            "Concepto": ["Contabilidad", "Permisos", "Seguros de oficina"],
            "Monto (CRC)": [0.0, 0.0, 0.0],
            "Frecuencia": ["Mes", "Año", "Mes"],
        })
        tabla_admin = st.data_editor(
            datos_admin, num_rows="dynamic", use_container_width=True,
            column_config={
                "Monto (CRC)": st.column_config.NumberColumn(min_value=0, format="%.2f"),
                "Frecuencia": st.column_config.SelectboxColumn(
                    options=["Día", "Semana", "Mes", "Año"], default="Mes"),
            },
            key="editor_costos_administrativa",
        )
        if st.button("Guardar administrativa"):
            db.guardar_costos_administrativa(tabla_admin)
            st.success("Administrativa guardada.")
        costo_administrativa_dia = db.costo_diario_recurrente(tabla_admin)
        st.caption(f"Subtotal administrativa: CRC {costo_administrativa_dia:,.2f} / día")

    st.divider()

    # ══════════ Cálculo final: costo operativo total y costo por tonelada ══════════
    if not st.session_state.resultados:
        with precio_nuevo_slot:
            st.metric("Precio por tonelada (CRC) — modelo nuevo, calculado", "—")
        st.info("Calculá las rutas para obtener el costo por tonelada "
                 "(se necesitan los km recorridos).")
        costo_por_tonelada = 0.0
        costo_operativo = 0.0
        km_total = 0.0
        litros = 0.0
        costo_combustible = 0.0
        costo_variable = 0.0
        costo_mano_obra = 0.0
    else:
        r = st.session_state.resultados
        km_total = sum(c["dist_total_m"] for c in r["camiones"]) / 1000
        personas_total = sum(c["personas"] for c in r["camiones"])
        litros = km_total / rendimiento if rendimiento > 0 else 0
        costo_combustible = litros * precio_litro
        costo_variable = km_total * costo_km_extra
        costo_mano_obra = horas_laboradas * precio_hora * personas_total
        costo_operativo = (costo_combustible + costo_variable + costo_mano_obra
                           + costo_inversion_dia + costo_mantenimiento_dia
                           + costo_administrativa_dia)
        costo_por_tonelada = costo_operativo / ton_nuevo if ton_nuevo > 0 else 0.0

        with precio_nuevo_slot:
            st.metric("Precio por tonelada (CRC) — modelo nuevo, calculado",
                      f"CRC {costo_por_tonelada:,.2f}",
                      help=f"CRC {costo_operativo:,.2f} costo operativo total "
                           f"÷ {ton_nuevo:.2f} ton — ver detalle más abajo")

        st.markdown("##### Costo operativo diario total")
        st.markdown(f"**Base del cálculo:** {km_total:.1f} km recorridos · "
                    f"{litros:.1f} litros estimados · {ton_nuevo:.2f} ton recolectadas")

        f1, f2, f3 = st.columns(3)
        f1.metric("Combustible + variables", f"CRC {costo_combustible + costo_variable:,.2f}")
        f2.metric("Mano de obra", f"CRC {costo_mano_obra:,.2f}",
                  help=f"{horas_laboradas:.2f} h x CRC {precio_hora:,.2f} "
                       f"x {personas_total} persona(s)")
        f3.metric("Inversión + Mant. + Admin.",
                  f"CRC {costo_inversion_dia + costo_mantenimiento_dia + costo_administrativa_dia:,.2f}")

        st.metric("Costo real por tonelada (modelo nuevo)",
                  f"CRC {costo_por_tonelada:,.2f}",
                  help=f"CRC {costo_operativo:,.2f} costo operativo total "
                       f"÷ {ton_nuevo:.2f} ton")

        with st.expander("Ver desglose completo"):
            desglose = pd.DataFrame([
                {"Concepto": "Combustible", "Monto (CRC)": f"{costo_combustible:,.2f}",
                 "Detalle": f"{litros:.1f} L x CRC {precio_litro:,.2f}"},
                {"Concepto": "Variables por km", "Monto (CRC)": f"{costo_variable:,.2f}",
                 "Detalle": f"{km_total:.1f} km x CRC {costo_km_extra:,.2f}"},
                {"Concepto": "Mano de obra", "Monto (CRC)": f"{costo_mano_obra:,.2f}",
                 "Detalle": f"{horas_laboradas:.2f} h x CRC {precio_hora:,.2f} x "
                            f"{personas_total} persona(s) (incl. cargas sociales)"},
                {"Concepto": "Inversión (prorrateada)", "Monto (CRC)": f"{costo_inversion_dia:,.2f}",
                 "Detalle": "Camiones, garaje, otros — por vida útil"},
                {"Concepto": "Mantenimiento (prorrateado)", "Monto (CRC)": f"{costo_mantenimiento_dia:,.2f}",
                 "Detalle": "Lavado, extintores, etc. — por frecuencia"},
                {"Concepto": "Administrativa (prorrateada)", "Monto (CRC)": f"{costo_administrativa_dia:,.2f}",
                 "Detalle": "Contabilidad, permisos, seguros — por frecuencia"},
                {"Concepto": "TOTAL operativo diario", "Monto (CRC)": f"{costo_operativo:,.2f}",
                 "Detalle": ""},
                {"Concepto": "Costo por tonelada", "Monto (CRC)": f"{costo_por_tonelada:,.2f}",
                 "Detalle": f"Sobre {ton_nuevo:.2f} ton del modelo nuevo"},
            ])
            st.dataframe(desglose, use_container_width=True, hide_index=True)

    # ── Comparación final: se dibuja arriba, en resultado_slot ──
    ton_actual_neta = max(ton_actual_bruta - ton_nuevo, 0.0)
    costo_modelo_actual = ton_actual_neta * precio_actual
    costo_modelo_nuevo = ton_nuevo * costo_por_tonelada
    diferencia = costo_modelo_actual - costo_modelo_nuevo

    with resultado_slot:
        st.subheader("Resultado de la comparación")
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
        k1, k2, k3 = st.columns(3)
        k1.metric("Costo modelo actual (neto)", f"CRC {costo_modelo_actual:,.2f}",
                  help=f"{ton_actual_neta:.2f} ton x CRC {precio_actual:,.2f}")
        k2.metric("Costo modelo nuevo", f"CRC {costo_modelo_nuevo:,.2f}",
                  help=f"{ton_nuevo:.2f} ton x CRC {costo_por_tonelada:,.2f} (calculado)")
        k3.metric(
            "Diferencia (ahorro)", f"CRC {diferencia:,.2f}",
            delta=(f"{(diferencia / costo_modelo_actual * 100):.1f}%"
                   if costo_modelo_actual > 0 else None),
            delta_color="normal" if diferencia >= 0 else "inverse",
        )
        st.divider()

# ══════════════ TAB EXPORTAR ══════════════
with tab_exportar:
    if not st.session_state.resultados:
        st.info("Calculá las rutas primero.")
    else:
        r = st.session_state.resultados

        st.subheader("Exportar resultados")
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
                for fila in c["resumen"]:
                    if fila["tipo"] != "parada":
                        continue
                    waze_url = f"https://waze.com/ul?ll={fila['lat']},{fila['lon']}&navigate=yes"
                    st.markdown(f"- Parada {fila['orden']} — {fila['Nombre']} "
                                f"({fila['Hora llegada']}): [Abrir en Waze]({waze_url})")


# ══════════════ TAB RED PROPIA (BETA) ══════════════
# Sección 100% independiente: NO lee ni escribe los datos de las pestañas
# Puntos/Camiones/Resultados/Costos/Exportar, ni toca session_state.resultados.
# Usa su propia clave de sesión y sus propios inputs.
with tab_red_propia:
    st.subheader("Red propia (Beta) — rutear sobre tu propio shapefile de líneas")
    st.caption(
        "Esta sección es independiente del optimizador principal (que usa OSRM "
        "y las pestañas Puntos/Camiones/Resultados). Acá subís tu propia red de "
        "calles como shapefile de líneas, y la app calcula la mejor ruta "
        "recorriendo esa red — no afecta nada de lo demás."
    )

    if "red_propia_resultado" not in st.session_state:
        st.session_state.red_propia_resultado = None
    if "red_propia_grafo" not in st.session_state:
        st.session_state.red_propia_grafo = None

    st.markdown("##### 1. Subí tu capa de líneas")
    st.caption(
        "Formatos aceptados: **(A)** un .zip con el shapefile adentro (aunque "
        "esté en una subcarpeta), **(B)** los archivos .shp + .shx + .dbf "
        "sueltos, subidos juntos sin comprimir, **(C)** un .geojson, o "
        "**(D)** un .gpkg. Pueden ser calles conectadas o líneas sueltas — "
        "la app avisa si algún punto queda fuera de la red."
    )
    archivos_capa = st.file_uploader(
        "Capa de líneas (zip / shp+shx+dbf / geojson / gpkg)",
        type=["zip", "shp", "shx", "dbf", "prj", "geojson", "json", "gpkg"],
        accept_multiple_files=True,
        key="uploader_red_propia",
    )

    tolerancia_m = st.number_input(
        "Tolerancia de conexión entre líneas (metros)", min_value=0.5, max_value=100.0,
        value=5.0, step=0.5,
        help="Dos extremos de línea a menos de esta distancia se tratan como "
             "el mismo cruce/intersección. Subilo si tu shapefile tiene calles "
             "que deberían tocarse pero quedan separadas por pequeños errores "
             "de digitalización.",
    )

    if archivos_capa:
        if st.button("Cargar red"):
            with st.spinner("Leyendo la capa y armando la red..."):
                gdf_lineas, error_lectura = leer_capa_lineas(archivos_capa)
                if error_lectura:
                    st.error(error_lectura)
                else:
                    G, nodos = construir_grafo_red(gdf_lineas, tolerancia_m=tolerancia_m)
                    componentes = contar_componentes_red(G)
                    st.session_state.red_propia_grafo = {
                        "G": G, "nodos": nodos,
                        "n_lineas": len(gdf_lineas),
                        "n_componentes": len(componentes),
                        "tamano_componentes": sorted((len(c) for c in componentes), reverse=True),
                    }
                    st.session_state.red_propia_resultado = None  # invalida un cálculo previo

    if st.session_state.red_propia_grafo:
        info = st.session_state.red_propia_grafo
        g1, g2, g3 = st.columns(3)
        g1.metric("Líneas leídas", info["n_lineas"])
        g2.metric("Nodos de la red", info["G"].number_of_nodes())
        g3.metric("Componentes conectados", info["n_componentes"])
        if info["n_componentes"] > 1:
            top3 = info["tamano_componentes"][:3]
            st.warning(
                f"La red tiene {info['n_componentes']} partes NO conectadas entre sí "
                f"(tamaños: {top3}{'...' if info['n_componentes'] > 3 else ''} nodos). "
                "Si dos de tus puntos caen en partes distintas, la app va a usar "
                "línea recta entre ellos y te lo va a avisar en el resultado."
            )
        else:
            st.success("La red quedó como un solo componente conectado.")

        st.divider()
        st.markdown("##### 2. Puntos a recorrer")
        st.caption("Cargá acá los puntos de esta ruta (independiente de la pestaña Puntos).")

        if "editor_red_propia_puntos" not in st.session_state:
            datos_puntos_red = pd.DataFrame({
                "Nombre": ["Depot", "Punto 1", "Punto 2"],
                "Latitud": [9.964356, 9.934804, 9.936133],
                "Longitud": [-84.161528, -84.081784, -84.082634],
            })
        else:
            datos_puntos_red = None  # el data_editor recuerda su propio estado por key

        tabla_puntos_red = st.data_editor(
            datos_puntos_red if datos_puntos_red is not None else pd.DataFrame(
                {"Nombre": [], "Latitud": [], "Longitud": []}),
            num_rows="dynamic", use_container_width=True,
            column_config={
                "Latitud": st.column_config.NumberColumn(format="%.6f"),
                "Longitud": st.column_config.NumberColumn(format="%.6f"),
            },
            key="editor_red_propia_puntos",
        )
        st.caption(
            "El **primer punto de la lista** se usa como salida y llegada "
            "(recorrido circular). Se calcula un solo recorrido — esta sección "
            "beta todavía no reparte entre varios camiones."
        )

        if st.button("Calcular ruta sobre esta red", type="primary"):
            puntos_validos = tabla_puntos_red.dropna(subset=["Latitud", "Longitud"])
            if len(puntos_validos) < 2:
                st.error("Necesitás al menos 2 puntos con coordenadas.")
            else:
                G = info["G"]
                nodos = info["nodos"]
                puntos_lonlat = list(zip(puntos_validos["Longitud"], puntos_validos["Latitud"]))
                nombres_red = puntos_validos["Nombre"].tolist()

                with st.spinner("Calculando distancias sobre la red y optimizando..."):
                    matriz, nodos_enganchados, enganches, pares_sin_red = matriz_distancias_red(
                        puntos_lonlat, G, nodos)

                    demandas_red = [0] * len(puntos_lonlat)
                    rutas_red = resolver_vrp(
                        matriz, demandas_red, [10**9], start_nodes=[0], end_node=0,
                    )

                    if rutas_red is None or not rutas_red[0]:
                        st.error("No se pudo calcular una ruta con estos puntos.")
                    else:
                        orden_nodos = rutas_red[0][0]  # único camión, único viaje
                        camino_completo = []
                        dist_total_m = 0.0
                        for a, b in zip(orden_nodos, orden_nodos[1:]):
                            tramo = camino_geometria_red(
                                G, nodos, nodos_enganchados[a], nodos_enganchados[b])
                            camino_completo.extend(tramo if not camino_completo else tramo[1:])
                            dist_total_m += matriz[a][b]

                        distancias_enganche = [enganches[i][1] for i in range(len(enganches))]

                        st.session_state.red_propia_resultado = {
                            "orden_nodos": orden_nodos,
                            "nombres": [nombres_red[i] for i in orden_nodos],
                            "puntos_lonlat": puntos_lonlat,
                            "camino": camino_completo,
                            "dist_total_m": dist_total_m,
                            "pares_sin_red": [(nombres_red[i], nombres_red[j])
                                             for i, j in pares_sin_red],
                            "distancias_enganche": distancias_enganche,
                            "nombres_todos": nombres_red,
                        }

    if st.session_state.red_propia_resultado:
        res = st.session_state.red_propia_resultado
        st.divider()
        st.markdown("##### 3. Resultado")

        if res["pares_sin_red"]:
            pares_txt = "; ".join(f"{a} ↔ {b}" for a, b in res["pares_sin_red"][:5])
            extra = "..." if len(res["pares_sin_red"]) > 5 else ""
            st.warning(
                f"Estos pares de puntos NO tienen camino por tu red (se usó línea "
                f"recta entre ellos): {pares_txt}{extra}. Revisá si tu shapefile "
                "los conecta, o si hace falta subir la tolerancia de conexión."
            )

        enganches_grandes = [
            (res["nombres_todos"][i], d) for i, d in enumerate(res["distancias_enganche"])
            if d > 50
        ]
        if enganches_grandes:
            txt = "; ".join(f"{n} ({d:.0f} m)" for n, d in enganches_grandes[:5])
            st.info(
                f"Estos puntos quedan lejos de cualquier línea de la red (más de "
                f"50 m de distancia al segmento más cercano): {txt}. Puede ser "
                "normal (el punto está a mitad de cuadra), o indicar que falta "
                "esa calle en el shapefile."
            )

        st.metric("Distancia total de la ruta", f"{res['dist_total_m'] / 1000:.2f} km")
        st.markdown("**Orden de la ruta:** " + " → ".join(res["nombres"]))

        m_red = folium.Map(
            location=(res["puntos_lonlat"][0][1], res["puntos_lonlat"][0][0]),
            zoom_start=13,
        )
        folium.PolyLine(
            [(lat, lon) for lon, lat in res["camino"]],
            color="#2563EB", weight=4, opacity=0.85,
        ).add_to(m_red)
        for i, node_idx in enumerate(res["orden_nodos"]):
            lon, lat = res["puntos_lonlat"][node_idx]
            folium.Marker(
                [lat, lon],
                popup=f"{i}. {res['nombres_todos'][node_idx]}",
                tooltip=f"{i}. {res['nombres_todos'][node_idx]}",
                icon=folium.Icon(color="blue" if i > 0 else "red",
                                 icon="play" if i == 0 else "info-sign"),
            ).add_to(m_red)
        st_folium(m_red, use_container_width=True, height=500, returned_objects=[])
