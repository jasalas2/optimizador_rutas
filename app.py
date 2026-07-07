"""
Panel Web - Optimizador de Rutas de Recolección
================================================
Instalación:
    pip install ortools folium requests streamlit streamlit-folium pandas

Correr localmente:
    python -m streamlit run app.py

Los datos (puntos y configuración) se guardan automáticamente en rutas.db
(SQLite) en la misma carpeta, así que no se pierden al recargar la página
ni al cerrar la terminal.
"""

import math
import time
from datetime import datetime, timedelta

import folium
import pandas as pd
import requests
import streamlit as st
from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from streamlit_folium import st_folium

import db

st.set_page_config(page_title="Optimizador de Rutas", page_icon="🚚", layout="wide")
st.title("🚚 Optimizador de Rutas de Recolección")
st.markdown("Calcula la ruta óptima con horarios estimados y peso por parada.")

db.init_db()


# ─────────────────────────────────────────────
# FUNCIONES (definidas antes de la UI que las usa)
# ─────────────────────────────────────────────
def haversine(c1, c2):
    R = 6_371_000
    lat1, lon1 = math.radians(c1[0]), math.radians(c1[1])
    lat2, lon2 = math.radians(c2[0]), math.radians(c2[1])
    a = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return int(R * 2 * math.asin(math.sqrt(a)))


def geocodificar_direccion(direccion):
    """Convierte una dirección de texto en (lat, lon) usando Nominatim (OpenStreetMap)."""
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
        resp = requests.get(url, timeout=20)
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


def obtener_ruta_osrm(origen, destino):
    url = (
        f"http://router.project-osrm.org/route/v1/driving/"
        f"{origen[1]},{origen[0]};{destino[1]},{destino[0]}"
        f"?overview=full&geometries=geojson"
    )
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == "Ok":
            coords = data["routes"][0]["geometry"]["coordinates"]
            dist_m = data["routes"][0]["legs"][0]["distance"]
            return [(lat, lon) for lon, lat in coords], dist_m, None
        return [origen, destino], haversine(origen, destino), f"OSRM código '{data.get('code')}'"
    except requests.exceptions.Timeout:
        return [origen, destino], haversine(origen, destino), "timeout"
    except requests.exceptions.ConnectionError:
        return [origen, destino], haversine(origen, destino), "sin conexión"
    except Exception as e:
        return [origen, destino], haversine(origen, destino), f"error inesperado ({e})"


def resolver_vrp_multi(distancias, pesos, num_vehicles, capacidad_por_vehiculo):
    """Reparte los puntos entre `num_vehicles` camiones respetando la capacidad
    REAL de cada uno como restricción dura: si un camión se llenaría, el/los
    puntos restantes se asignan a otro camión con espacio disponible.
    Si ni sumando toda la flota alcanza la capacidad, esos puntos se descartan
    (con una penalización alta para que el solver los deje de último recurso)
    y se devuelven aparte para poder avisarle al usuario.
    Devuelve (rutas, puntos_no_asignados)."""
    manager = pywrapcp.RoutingIndexManager(len(distancias), num_vehicles, 0)
    routing = pywrapcp.RoutingModel(manager)

    def dist_cb(from_idx, to_idx):
        return distancias[manager.IndexToNode(from_idx)][manager.IndexToNode(to_idx)]

    transit_idx = routing.RegisterTransitCallback(dist_cb)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_idx)

    def demand_cb(from_idx):
        return int(pesos[manager.IndexToNode(from_idx)])

    demand_idx = routing.RegisterUnaryTransitCallback(demand_cb)
    routing.AddDimensionWithVehicleCapacity(
        demand_idx, 0, [int(capacidad_por_vehiculo)] * num_vehicles, True, "Carga"
    )

    # Penalización alta por punto descartado: el solver solo lo hace si de
    # verdad no hay forma de que ningún camión lo cubra sin pasarse de peso.
    penalizacion = sum(sum(fila) for fila in distancias) + 1
    for node in range(1, len(distancias)):
        routing.AddDisjunction([manager.NodeToIndex(node)], penalizacion)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.seconds = 10

    sol = routing.SolveWithParameters(params)
    if not sol:
        return None, []

    rutas = []
    for v in range(num_vehicles):
        idx = routing.Start(v)
        ruta = []
        while not routing.IsEnd(idx):
            ruta.append(manager.IndexToNode(idx))
            idx = sol.Value(routing.NextVar(idx))
        ruta.append(manager.IndexToNode(idx))
        rutas.append(ruta)

    visitados = {n for ruta in rutas for n in ruta}
    no_asignados = [n for n in range(1, len(distancias)) if n not in visitados]
    return rutas, no_asignados


def construir_resultado_camion(ruta_base, LOCATIONS, NOMBRES, PESOS,
                                capacidad_max, hora_inicio_dt, velocidad_kmh, tiempo_parada):
    """Recorre la ruta de un camión en una sola pasada (sin vueltas a mitad de
    ruta) y arma distancias reales, horarios y pesos. Si el peso total asignado
    supera la capacidad del camión, se marca como advertencia (no se corta la ruta)."""
    DEPOT_IDX = 0
    advertencias = []
    ultimo_idx = len(ruta_base) - 1

    segmentos = []
    resumen = []
    hora_actual = hora_inicio_dt
    peso_acumulado = 0

    for i, node in enumerate(ruta_base):
        if i > 0:
            origen = LOCATIONS[ruta_base[i - 1]]
            destino = LOCATIONS[node]
            camino, dist_m, _ = obtener_ruta_osrm(origen, destino)
            segmentos.append({"camino": camino, "dist_m": dist_m})
            hora_actual += timedelta(hours=(dist_m / 1000) / velocidad_kmh)
            dist_tramo = f"{dist_m / 1000:.2f}"
        else:
            dist_tramo = "-"

        if node == DEPOT_IDX:
            parada_label = "🏠 Inicio (Depot)" if i == 0 else "🏠 Fin (Depot)"
            resumen.append({
                "Parada": parada_label,
                "Nombre": NOMBRES[node],
                "Hora llegada": hora_actual.strftime("%H:%M"),
                "Peso recogido (kg)": 0,
                "Peso acumulado (kg)": 0 if i == 0 else peso_acumulado,
                "Distancia tramo (km)": dist_tramo,
            })
        else:
            peso_nodo = PESOS[node]
            peso_acumulado += peso_nodo
            resumen.append({
                "Parada": f"📦 Parada {i}",
                "Nombre": NOMBRES[node],
                "Hora llegada": hora_actual.strftime("%H:%M"),
                "Peso recogido (kg)": peso_nodo,
                "Peso acumulado (kg)": peso_acumulado,
                "Distancia tramo (km)": dist_tramo,
            })
            hora_actual += timedelta(minutes=tiempo_parada)

    if peso_acumulado > capacidad_max:
        advertencias.append(
            f"⚠️ El peso total asignado a este camión ({peso_acumulado:.0f} kg) "
            f"supera su capacidad ({capacidad_max} kg)."
        )

    return {
        "ruta_nodos": ruta_base,
        "segmentos": segmentos,
        "resumen": resumen,
        "hora_fin": hora_actual.strftime("%H:%M"),
        "peso_total_dia": peso_acumulado,
        "dist_total_km": sum(s["dist_m"] for s in segmentos) / 1000,
        "advertencias": advertencias,
    }



def exportar_geojson(r):
    import json
    todas_coords = []
    for seg in r["segmentos"]:
        coords = [[lon, lat] for lat, lon in seg["camino"]]
        if todas_coords and coords:
            todas_coords.extend(coords[1:])
        else:
            todas_coords.extend(coords)

    features = [{
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": todas_coords},
        "properties": {"nombre": "Ruta optima", "tipo": "ruta"},
    }]
    for i, node in enumerate(r["ruta_nodos"]):
        lat, lon = r["LOCATIONS"][node]
        res = r["resumen"][i]
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
            "properties": {
                "orden": i,
                "nombre": r["NOMBRES"][node],
                "hora_llegada": res["Hora llegada"],
                "peso_kg": res["Peso recogido (kg)"],
                "peso_acumulado_kg": res["Peso acumulado (kg)"],
                "distancia_tramo_km": res["Distancia tramo (km)"],
                "tipo": "depot" if node == 0 else "parada",
            },
        })
    return json.dumps({"type": "FeatureCollection", "features": features},
                      ensure_ascii=False, indent=2).encode("utf-8")


def exportar_shapefile(r):
    import io, zipfile, tempfile, os
    import geopandas as gpd
    from shapely.geometry import LineString, Point

    todas_coords = []
    for seg in r["segmentos"]:
        coords = [(lon, lat) for lat, lon in seg["camino"]]
        if todas_coords and coords:
            todas_coords.extend(coords[1:])
        else:
            todas_coords.extend(coords)

    gdf_linea = gpd.GeoDataFrame(
        [{"nombre": "Ruta optima"}],
        geometry=[LineString(todas_coords)],
        crs="EPSG:4326",
    )

    rows = []
    for i, node in enumerate(r["ruta_nodos"]):
        lat, lon = r["LOCATIONS"][node]
        res = r["resumen"][i]
        peso = res["Peso recogido (kg)"]
        rows.append({
            "orden": i,
            "nombre": r["NOMBRES"][node],
            "hora": res["Hora llegada"],
            "peso_kg": float(peso) if str(peso) not in ("", "-") else 0.0,
            "tipo": "depot" if node == 0 else "parada",
            "geometry": Point(lon, lat),
        })
    gdf_puntos = gpd.GeoDataFrame(rows, crs="EPSG:4326")

    # pyogrio (backend de geopandas) no soporta escritura a BytesIO en ESRI Shapefile,
    # asi que se usa un directorio temporal y se empaqueta el resultado en un zip.
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for nombre_capa, gdf in [("ruta_linea", gdf_linea), ("ruta_puntos", gdf_puntos)]:
                capa_dir = os.path.join(tmpdir, nombre_capa)
                os.makedirs(capa_dir)
                path_shp = os.path.join(capa_dir, f"{nombre_capa}.shp")
                gdf.to_file(path_shp, driver="ESRI Shapefile")
                for fname in os.listdir(capa_dir):
                    zf.write(os.path.join(capa_dir, fname), fname)
    buf.seek(0)
    return buf.read()


def exportar_gpx(r):
    from xml.etree.ElementTree import Element, SubElement, tostring
    from xml.dom import minidom

    gpx = Element("gpx", {
        "version": "1.1", "creator": "Optimizador de Rutas",
        "xmlns": "http://www.topografix.com/GPX/1/1",
    })
    for i, node in enumerate(r["ruta_nodos"]):
        lat, lon = r["LOCATIONS"][node]
        res = r["resumen"][i]
        wpt = SubElement(gpx, "wpt", {"lat": str(lat), "lon": str(lon)})
        SubElement(wpt, "name").text = r["NOMBRES"][node]
        SubElement(wpt, "desc").text = (
            f"Orden: {i} | Hora: {res['Hora llegada']} | Peso: {res['Peso recogido (kg)']} kg"
        )
    trk = SubElement(gpx, "trk")
    SubElement(trk, "name").text = "Ruta optima"
    trkseg = SubElement(trk, "trkseg")
    for seg in r["segmentos"]:
        for lat, lon in seg["camino"]:
            SubElement(trkseg, "trkpt", {"lat": str(lat), "lon": str(lon)})
    raw = tostring(gpx, encoding="unicode")
    return minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8")


def exportar_kml(r):
    from xml.etree.ElementTree import Element, SubElement, tostring
    from xml.dom import minidom

    kml = Element("kml", {"xmlns": "http://www.opengis.net/kml/2.2"})
    doc = SubElement(kml, "Document")
    SubElement(doc, "name").text = "Ruta optima de recoleccion"
    style = SubElement(doc, "Style", {"id": "ruta"})
    ls = SubElement(style, "LineStyle")
    SubElement(ls, "color").text = "ff0000e7"
    SubElement(ls, "width").text = "4"

    for i, node in enumerate(r["ruta_nodos"]):
        lat, lon = r["LOCATIONS"][node]
        res = r["resumen"][i]
        pm = SubElement(doc, "Placemark")
        SubElement(pm, "name").text = r["NOMBRES"][node]
        SubElement(pm, "description").text = (
            f"Orden: {i} | Hora: {res['Hora llegada']} | Peso: {res['Peso recogido (kg)']} kg"
        )
        pt = SubElement(pm, "Point")
        SubElement(pt, "coordinates").text = f"{lon},{lat},0"

    pm_ruta = SubElement(doc, "Placemark")
    SubElement(pm_ruta, "name").text = "Recorrido"
    SubElement(pm_ruta, "styleUrl").text = "#ruta"
    ls2 = SubElement(pm_ruta, "LineString")
    SubElement(ls2, "tessellate").text = "1"
    coords_str = " ".join(
        f"{lon},{lat},0"
        for seg in r["segmentos"]
        for lat, lon in seg["camino"]
    )
    SubElement(ls2, "coordinates").text = coords_str
    raw = tostring(kml, encoding="unicode")
    return minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8")


def generar_link_google_maps(locations_in_order):
    """Genera un link de Google Maps con el origen, destino y paradas intermedias.
    Nota: Google Maps (sin API key) soporta hasta ~10 waypoints intermedios."""
    origin = locations_in_order[0]
    destination = locations_in_order[-1]
    waypoints = locations_in_order[1:-1]

    url = (
        "https://www.google.com/maps/dir/?api=1"
        f"&origin={origin[0]},{origin[1]}"
        f"&destination={destination[0]},{destination[1]}"
    )
    if waypoints:
        wp_str = "|".join(f"{lat},{lon}" for lat, lon in waypoints[:10])
        url += f"&waypoints={wp_str}"
    url += "&travelmode=driving"
    return url, len(waypoints) > 10


# ─────────────────────────────────────────────
# SESSION STATE
# ─────────────────────────────────────────────
if "resultados" not in st.session_state:
    st.session_state.resultados = None

DIAS_SEMANA = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]


def dias_de_frecuencia(frecuencia):
    """Convierte el texto libre de 'Días de recolección' en un set de días válidos.
    Acepta: 'Todos los días', 'Lunes a Sábado', o una lista separada por comas
    con cualquier combinación de días (ej. 'Lunes, Jueves')."""
    if frecuencia is None:
        return set(DIAS_SEMANA)
    texto = str(frecuencia).strip()
    if texto == "" or texto.lower() == "todos los días":
        return set(DIAS_SEMANA)
    if texto == "Lunes a Sábado":
        return set(DIAS_SEMANA) - {"Domingo"}
    return {p.strip() for p in texto.split(",") if p.strip()}


def punto_aplica(frecuencia, dias_seleccionados):
    """True si el punto (según su frecuencia) recolecta en alguno de los días seleccionados."""
    return bool(dias_de_frecuencia(frecuencia) & set(dias_seleccionados))


# Datos por defecto solo se usan si la base de datos está vacía (primera vez)
datos_default = pd.DataFrame({
    "Nombre":     ["Punto 1", "Punto 2", "Punto 3", "Punto 4", "Punto 5", "Punto 6"],
    "Dirección":  ["", "", "", "", "", ""],
    "Latitud":    [9.934804, 9.936133, 9.931150, 9.979572, 10.016073, 9.996015],
    "Longitud":   [-84.081784, -84.082634, -84.093640, -84.152163, -84.215665, -84.118091],
    "Peso (kg)":  [50, 80, 120, 60, 90, 110],
    "Días de recolección": ["Todos los días"] * 6,
})



if db.hay_puntos_guardados():
    datos_iniciales = db.cargar_puntos()
else:
    datos_iniciales = datos_default
    db.guardar_puntos(datos_default)

# Fallback defensivo: si la base de datos es de una versión anterior a esta
# columna y por algún motivo no se migró, se agrega acá para no romper la app.
if "Días de recolección" not in datos_iniciales.columns:
    datos_iniciales["Días de recolección"] = "Todos los días"

# ─────────────────────────────────────────────
# SIDEBAR — configuración (con valores guardados como default)
# ─────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Configuración")

    hora_inicio_str = st.text_input(
        "🕗 Hora de inicio (HH:MM)",
        value=db.obtener_config("hora_inicio", "08:00"),
    )
    try:
        hora_inicio = datetime.strptime(hora_inicio_str, "%H:%M").time()
    except ValueError:
        st.warning("Formato de hora inválido, usando 08:00.")
        hora_inicio = datetime.strptime("08:00", "%H:%M").time()

    velocidad_kmh = st.number_input(
        "🚗 Velocidad promedio (km/h)", min_value=10, max_value=120,
        value=int(db.obtener_config("velocidad_kmh", 40)),
    )
    tiempo_parada = st.number_input(
        "⏱️ Tiempo por parada (min)", min_value=1, max_value=60,
        value=int(db.obtener_config("tiempo_parada", 10)),
    )
    capacidad_max = st.number_input(
        "📦 Capacidad por camión (kg)", min_value=1,
        value=int(float(db.obtener_config("capacidad_max", 1000))),
    )

    st.divider()
    st.header("🚛 Flota")
    num_camiones = st.number_input(
        "Número de camiones disponibles", min_value=1, max_value=20,
        value=int(db.obtener_config("num_camiones", 1)),
    )
    st.caption(
        "Los puntos se reparten entre los camiones disponibles balanceando por peso."
    )

    st.divider()
    st.header("📍 Depot (Bodega)")
    depot_lat = st.number_input(
        "Latitud", value=float(db.obtener_config("depot_lat", 9.964356)), format="%.6f",
    )
    depot_lon = st.number_input(
        "Longitud", value=float(db.obtener_config("depot_lon", -84.161528)), format="%.6f",
    )

    if st.button("💾 Guardar configuración", use_container_width=True):
        db.guardar_configuracion_general(
            hora_inicio=hora_inicio_str,
            velocidad_kmh=velocidad_kmh,
            tiempo_parada=tiempo_parada,
            capacidad_max=capacidad_max,
            num_camiones=num_camiones,
            depot_lat=depot_lat,
            depot_lon=depot_lon,
        )
        st.success("Configuración guardada ✅")


# ─────────────────────────────────────────────
# TABLA DE PUNTOS
# ─────────────────────────────────────────────
st.subheader("📋 Puntos de Recolección")
st.caption(
    "💡 Podés llenar la columna **Dirección** y usar el botón de geocodificación "
    "en vez de escribir Latitud/Longitud a mano."
)

tabla = st.data_editor(
    datos_iniciales,
    num_rows="dynamic",
    use_container_width=True,
    column_config={
        "Latitud":   st.column_config.NumberColumn(format="%.6f"),
        "Longitud":  st.column_config.NumberColumn(format="%.6f"),
        "Peso (kg)": st.column_config.NumberColumn(min_value=0),
        "Dirección": st.column_config.TextColumn(width="large"),
        "Días de recolección": st.column_config.TextColumn(
            width="medium",
            help=(
                "'Todos los días', 'Lunes a Sábado', o cualquier combinación separada "
                "por comas, ej: 'Lunes, Jueves' o 'Martes, Viernes, Domingo'."
            ),
        ),
    },
    key="editor_puntos",
)

col_g1, col_g2, col_g3 = st.columns([1, 1, 2])
with col_g1:
    if st.button("📍 Completar coordenadas desde dirección"):
        pendientes = tabla[
            tabla["Dirección"].fillna("").str.strip().ne("")
            & (tabla["Latitud"].isna() | tabla["Longitud"].isna())
        ]
        if len(pendientes) == 0:
            st.info("No hay direcciones pendientes de geocodificar.")
        else:
            with st.spinner(f"Geocodificando {len(pendientes)} dirección(es)..."):
                errores_geo = []
                for idx in pendientes.index:
                    direccion = tabla.loc[idx, "Dirección"]
                    lat, lon, err = geocodificar_direccion(direccion)
                    if lat is not None:
                        tabla.loc[idx, "Latitud"] = lat
                        tabla.loc[idx, "Longitud"] = lon
                    else:
                        errores_geo.append(f"{direccion}: {err}")
                    time.sleep(1)  # respetar límite de 1 req/seg de Nominatim
                db.guardar_puntos(tabla)
                if errores_geo:
                    st.warning("No se pudieron geocodificar:\n" + "\n".join(errores_geo))
                else:
                    st.success("Coordenadas completadas ✅")
                st.rerun()

with col_g2:
    if st.button("💾 Guardar puntos"):
        db.guardar_puntos(tabla)
        st.success("Puntos guardados ✅")

# ─────────────────────────────────────────────
# DÍA(S) A CALCULAR
# ─────────────────────────────────────────────
st.subheader("📅 Día(s) a calcular")
hoy_idx = datetime.today().weekday()  # 0 = Lunes ... 6 = Domingo
dias_calculo = st.multiselect(
    "Solo se incluirán en la ruta los puntos programados para alguno de estos días",
    options=DIAS_SEMANA, default=[DIAS_SEMANA[hoy_idx]],
)

# ─────────────────────────────────────────────
# BOTÓN CALCULAR
# ─────────────────────────────────────────────
if st.button("🔍 Calcular Ruta Óptima", type="primary", use_container_width=True):
    if not dias_calculo:
        st.error("Elegí al menos un día para calcular la ruta.")
        st.stop()

    db.guardar_puntos(tabla)  # guardar antes de calcular, por si se cierra la app
    puntos_validos = tabla.dropna(subset=["Latitud", "Longitud"])
    if "Días de recolección" in puntos_validos.columns:
        col_frecuencia = puntos_validos["Días de recolección"]
    else:
        col_frecuencia = pd.Series("Todos los días", index=puntos_validos.index)
    puntos = puntos_validos[col_frecuencia.apply(lambda f: punto_aplica(f, dias_calculo))]

    dias_texto = ", ".join(dias_calculo)
    if len(puntos) < 2:
        st.error(
            f"Necesitas al menos 2 puntos programados para **{dias_texto}** con coordenadas válidas "
            f"(hay {len(puntos)}). Revisá la columna 'Días de recolección' de la tabla."
        )
        st.stop()

    DEPOT = (depot_lat, depot_lon)
    LOCATIONS = [DEPOT] + list(zip(puntos["Latitud"], puntos["Longitud"]))
    NOMBRES = ["DEPOT"] + puntos["Nombre"].tolist()
    # Peso: cualquier celda vacía/NaN se trata como 0 kg, no como "nan".
    pesos_validos = pd.to_numeric(puntos["Peso (kg)"], errors="coerce").fillna(0)
    PESOS = [0] + pesos_validos.tolist()

    with st.spinner("📡 Consultando OSRM y optimizando rutas..."):
        distancias, uso_osrm, error_matriz = obtener_matriz_osrm(LOCATIONS)

        # La capacidad real de cada camión es una restricción dura: si un
        # camión se llenaría, el/los puntos que sobran se asignan a otro
        # camión con espacio disponible.
        resultado_solver = resolver_vrp_multi(distancias, PESOS, num_camiones, capacidad_max)

        if resultado_solver is None or resultado_solver[0] is None:
            st.error(
                "No se encontró solución. Probá aumentar la capacidad por camión, "
                "sumar más camiones, o revisar los puntos cargados."
            )
            st.stop()

        rutas_base, no_asignados = resultado_solver

        hora_inicio_dt = datetime.combine(datetime.today(), hora_inicio)
        camiones_resultado = []
        for idx_camion, ruta_base in enumerate(rutas_base):
            if len(ruta_base) <= 2:
                continue  # camión sin puntos asignados
            resultado = construir_resultado_camion(
                ruta_base, LOCATIONS, NOMBRES, PESOS,
                capacidad_max, hora_inicio_dt, velocidad_kmh, tiempo_parada,
            )
            resultado["camion"] = idx_camion + 1
            camiones_resultado.append(resultado)

        if not camiones_resultado:
            st.error("Ningún camión quedó con puntos asignados. Revisá la configuración.")
            st.stop()

        st.session_state.resultados = {
            "uso_osrm": uso_osrm,
            "error_matriz": error_matriz,
            "camiones": camiones_resultado,
            "puntos_sin_asignar": [NOMBRES[n] for n in no_asignados],
            "LOCATIONS": LOCATIONS,
            "NOMBRES": NOMBRES,
            "PESOS": PESOS,
            "dia_calculo": dias_texto,
            "puntos_excluidos_hoy": len(puntos_validos) - len(puntos),
        }

# ─────────────────────────────────────────────
# MOSTRAR RESULTADOS
# ─────────────────────────────────────────────
if st.session_state.resultados:
    r = st.session_state.resultados
    camiones = r["camiones"]

    st.subheader(f"📅 Ruta para: {r['dia_calculo']}")
    if r["puntos_excluidos_hoy"] > 0:
        st.caption(
            f"ℹ️ {r['puntos_excluidos_hoy']} punto(s) no están programados para "
            f"{r['dia_calculo']} y no se incluyeron en esta ruta."
        )

    if r["uso_osrm"]:
        st.success("✅ Rutas calculadas con distancias reales por carretera (OSRM)")
    else:
        st.warning(f"⚠️ Sin distancias reales de OSRM — usando línea recta. Motivo: {r['error_matriz']}")

    todas_advertencias = [a for c in camiones for a in c["advertencias"]]
    for adv in todas_advertencias:
        st.warning(adv)

    if r["puntos_sin_asignar"]:
        nombres_sin_asignar = ", ".join(r["puntos_sin_asignar"])
        st.error(
            f"⚠️ Estos puntos no pudieron asignarse a ningún camión porque la flota no da abasto "
            f"(ni sumando la capacidad de todos los camiones): **{nombres_sin_asignar}**. "
            f"Sumá más camiones o aumentá la capacidad por camión para cubrirlos."
        )

    # ── Resumen general (todos los camiones) ──
    st.subheader("📊 Resumen general")
    dist_total_general = sum(c["dist_total_km"] for c in camiones)
    peso_total_general = sum(c["peso_total_dia"] for c in camiones)
    hora_fin_general = max(c["hora_fin"] for c in camiones)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("🚛 Camiones usados", f"{len(camiones)} / {num_camiones}")
    col2.metric("📏 Distancia total", f"{dist_total_general:.1f} km")
    col3.metric("📦 Peso total recolectado", f"{peso_total_general:.0f} kg")
    col4.metric("🕐 Hora fin (último camión)", hora_fin_general)

    # ── Mapa con todas las rutas ──
    st.subheader("🗺️ Mapa de todas las rutas")
    todos_lats = [r["LOCATIONS"][n][0] for c in camiones for n in c["ruta_nodos"]]
    todos_lons = [r["LOCATIONS"][n][1] for c in camiones for n in c["ruta_nodos"]]
    centro = (sum(todos_lats) / len(todos_lats), sum(todos_lons) / len(todos_lons))

    m = folium.Map(location=centro, zoom_start=12, tiles="OpenStreetMap", control_scale=True)

    folium.TileLayer("OpenStreetMap", name="🗺️ Calles").add_to(m)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        attr="Esri", name="🛰️ Satélite",
    ).add_to(m)
    folium.TileLayer("CartoDB dark_matter", name="🌙 Modo oscuro").add_to(m)

    from folium.plugins import Fullscreen, MiniMap, Geocoder

    Fullscreen(position="topleft", title="Pantalla completa", title_cancel="Salir").add_to(m)
    MiniMap(toggle_display=True, position="bottomleft").add_to(m)
    Geocoder(position="topright", collapsed=True).add_to(m)

    PALETA = ["#E74C3C", "#2980B9", "#27AE60", "#F39C12", "#8E44AD",
              "#16A085", "#D35400", "#2C3E50", "#C0392B", "#7F8C8D"]

    # Depot: un solo marcador, se comparte entre todos los camiones
    depot_lat_m, depot_lon_m = r["LOCATIONS"][0]
    folium.Marker(
        location=[depot_lat_m, depot_lon_m],
        popup="🏠 DEPOT — Inicio/Fin",
        tooltip="DEPOT",
        icon=folium.Icon(color="red", icon="home"),
    ).add_to(m)

    for c in camiones:
        color = PALETA[(c["camion"] - 1) % len(PALETA)]
        grupo = folium.FeatureGroup(name=f"🚛 Camión {c['camion']}")

        for seg in c["segmentos"]:
            folium.PolyLine(seg["camino"], color=color, weight=4, opacity=0.85).add_to(grupo)

        for i, node in enumerate(c["ruta_nodos"]):
            if node == 0:
                continue  # el depósito (inicio/fin/descargas) ya tiene su propio marcador
            lat, lon = r["LOCATIONS"][node]
            hora_parada = c["resumen"][i]["Hora llegada"]
            peso_p = r["PESOS"][node]
            icon_html = f"""<div style="background:{color};color:white;border-radius:50%;
                            width:30px;height:30px;display:flex;align-items:center;
                            justify-content:center;font-size:13px;font-weight:bold;
                            border:2px solid white;box-shadow:2px 2px 4px rgba(0,0,0,0.4);">{i}</div>"""
            folium.Marker(
                location=[lat, lon],
                popup=f"<b>{r['NOMBRES'][node]}</b><br>🚛 Camión {c['camion']}<br>⏰ {hora_parada}<br>📦 {peso_p} kg",
                tooltip=f"Camión {c['camion']} · {i}. {r['NOMBRES'][node]} — {hora_parada}",
                icon=folium.DivIcon(html=icon_html, icon_size=(30, 30), icon_anchor=(15, 15)),
            ).add_to(grupo)

        grupo.add_to(m)

    folium.LayerControl(position="topright", collapsed=True).add_to(m)
    st_folium(m, use_container_width=True, height=520, returned_objects=[])

    # ── Detalle por camión ──
    st.subheader("📋 Detalle por camión")
    tabs = st.tabs([f"🚛 Camión {c['camion']}" for c in camiones])
    for tab, c in zip(tabs, camiones):
        with tab:
            tcol1, tcol2, tcol3 = st.columns(3)
            tcol1.metric("📏 Distancia", f"{c['dist_total_km']:.1f} km")
            tcol2.metric("📦 Peso recolectado", f"{c['peso_total_dia']:.0f} kg")
            tcol3.metric("🕐 Hora fin", c["hora_fin"])
            st.dataframe(pd.DataFrame(c["resumen"]), use_container_width=True, hide_index=True)

    # ── Exportar ──
    st.subheader("⬇️ Exportar")

    df_todos = pd.concat(
        [pd.DataFrame(c["resumen"]).assign(Camión=c["camion"]) for c in camiones],
        ignore_index=True,
    )
    df_todos = df_todos[["Camión"] + [col for col in df_todos.columns if col != "Camión"]]
    csv_todos = df_todos.to_csv(index=False).encode("utf-8")
    st.download_button("📥 CSV combinado (todos los camiones)", csv_todos,
                       "rutas_todos_los_camiones.csv", "text/csv", use_container_width=True)

    mapa_html = m.get_root().render().encode("utf-8")
    st.download_button(
        "🗺️ Mapa interactivo (HTML, para abrir sin internet)",
        mapa_html, "mapa_rutas.html", "text/html", use_container_width=True,
    )

    st.divider()
    st.markdown("**Exportar detalle de un camión específico (GPS / SIG / navegación):**")
    camion_sel_num = st.selectbox(
        "Camión a exportar", options=[c["camion"] for c in camiones],
        format_func=lambda n: f"Camión {n}",
    )
    c_sel = next(c for c in camiones if c["camion"] == camion_sel_num)
    r_sel = {
        "ruta_nodos": c_sel["ruta_nodos"],
        "segmentos": c_sel["segmentos"],
        "resumen": c_sel["resumen"],
        "LOCATIONS": r["LOCATIONS"],
        "NOMBRES": r["NOMBRES"],
        "PESOS": r["PESOS"],
    }
    df_sel = pd.DataFrame(c_sel["resumen"])
    orden_locations = [r["LOCATIONS"][n] for n in c_sel["ruta_nodos"]]

    col_e1, col_e2 = st.columns(2)
    with col_e1:
        csv_sel = df_sel.to_csv(index=False).encode("utf-8")
        st.download_button(f"📥 CSV (Camión {camion_sel_num})", csv_sel,
                           f"ruta_camion_{camion_sel_num}.csv", "text/csv",
                           use_container_width=True)
    with col_e2:
        link_maps, demasiados_puntos = generar_link_google_maps(orden_locations)
        st.link_button("🗺️ Abrir en Google Maps", link_maps, use_container_width=True)
        if demasiados_puntos:
            st.caption("⚠️ Más de 10 paradas: Google Maps solo muestra las primeras 10.")

    col_g1, col_g2 = st.columns(2)
    with col_g1:
        geojson_bytes = exportar_geojson(r_sel)
        st.download_button("🌐 GeoJSON (QGIS / ArcGIS / web)",
                           geojson_bytes, f"ruta_camion_{camion_sel_num}.geojson",
                           "application/geo+json", use_container_width=True)
    with col_g2:
        shp_bytes = exportar_shapefile(r_sel)
        st.download_button("📦 Shapefile (.zip)",
                           shp_bytes, f"ruta_camion_{camion_sel_num}_shp.zip",
                           "application/zip", use_container_width=True)

    col_g3, col_g4 = st.columns(2)
    with col_g3:
        gpx_bytes = exportar_gpx(r_sel)
        st.download_button("📡 GPX (GPS / OsmAnd / Garmin)",
                           gpx_bytes, f"ruta_camion_{camion_sel_num}.gpx",
                           "application/gpx+xml", use_container_width=True)
    with col_g4:
        kml_bytes = exportar_kml(r_sel)
        st.download_button("🌍 KML (Google Earth)",
                           kml_bytes, f"ruta_camion_{camion_sel_num}.kml",
                           "application/vnd.google-earth.kml+xml", use_container_width=True)

    with st.expander(f"Cómo usar con Waze (Camión {camion_sel_num})"):
        st.markdown("""
Waze **no tiene API pública de multi-paradas** como Google Maps.
Tenés dos opciones:

**Opción A - Parada por parada (desde el celular):**
El chofer toca el link de la primera parada, llega, cierra Waze, toca el segundo, y así.

**Opción B - Importar GPX en OsmAnd (recomendado):**
Descargá el GPX de arriba e importalo en [OsmAnd](https://osmand.net/) (gratis, Android/iOS).
OsmAnd navega rutas GPX completas con instrucción por voz, igual que Waze.
""")
        for i, node in enumerate(c_sel["ruta_nodos"]):
            if node == 0:
                continue
            lat, lon = r["LOCATIONS"][node]
            nombre = r["NOMBRES"][node]
            hora = c_sel["resumen"][i]["Hora llegada"]
            es_ultimo = (i == len(c_sel["ruta_nodos"]) - 1)
            if not es_ultimo:
                waze_url = f"https://waze.com/ul?ll={lat},{lon}&navigate=yes"
                st.markdown(f"**Parada {i} - {nombre}** ({hora}): [Abrir en Waze]({waze_url})")
