"""
Panel Web - Optimizador de Rutas de Recolección
================================================
Instalación:
    pip install ortools folium requests streamlit streamlit-folium pandas

Correr localmente:
    python -m streamlit run app.py
"""

import streamlit as st
import folium
from streamlit_folium import st_folium
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp
import math
import requests
import pandas as pd
from datetime import datetime, timedelta

st.set_page_config(page_title="Optimizador de Rutas", page_icon="🚚", layout="wide")
st.title("🚚 Optimizador de Rutas de Recolección")
st.markdown("Calcula la ruta óptima con horarios estimados y peso por parada.")

# ─────────────────────────────────────────────
# SESSION STATE — guardar resultados entre recargas
# ─────────────────────────────────────────────
if "resultados" not in st.session_state:
    st.session_state.resultados = None

# ─────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ Configuración")
    hora_inicio     = st.time_input("🕗 Hora de inicio", value=datetime.strptime("08:00", "%H:%M").time())
    velocidad_kmh   = st.number_input("🚗 Velocidad promedio (km/h)", min_value=10, max_value=120, value=40)
    tiempo_parada   = st.number_input("⏱️ Tiempo por parada (min)", min_value=1, max_value=60, value=10)
    capacidad_max   = st.number_input("📦 Capacidad máxima (kg)", min_value=1, value=1000)
    st.divider()
    st.header("📍 Depot (Bodega)")
    depot_lat = st.number_input("Latitud",  value=9.964356,   format="%.6f")
    depot_lon = st.number_input("Longitud", value=-84.161528, format="%.6f")

# ─────────────────────────────────────────────
# TABLA DE PUNTOS
# ─────────────────────────────────────────────
st.subheader("📋 Puntos de Recolección")

datos_default = pd.DataFrame({
    "Nombre":    ["Punto 1","Punto 2","Punto 3","Punto 4","Punto 5","Punto 6"],
    "Latitud":   [9.934804, 9.936133, 9.931150, 9.979572, 10.016073, 9.996015],
    "Longitud":  [-84.081784,-84.082634,-84.093640,-84.152163,-84.215665,-84.118091],
    "Peso (kg)": [50, 80, 120, 60, 90, 110],
})

tabla = st.data_editor(
    datos_default,
    num_rows="dynamic",
    use_container_width=True,
    column_config={
        "Latitud":   st.column_config.NumberColumn(format="%.6f"),
        "Longitud":  st.column_config.NumberColumn(format="%.6f"),
        "Peso (kg)": st.column_config.NumberColumn(min_value=0),
    }
)

# ─────────────────────────────────────────────
# FUNCIONES
# ─────────────────────────────────────────────
def haversine(c1, c2):
    R = 6_371_000
    lat1, lon1 = math.radians(c1[0]), math.radians(c1[1])
    lat2, lon2 = math.radians(c2[0]), math.radians(c2[1])
    a = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return int(R * 2 * math.asin(math.sqrt(a)))

def obtener_matriz_osrm(locations):
    coords_str = ";".join(f"{lon},{lat}" for lat, lon in locations)
    url = f"http://router.project-osrm.org/table/v1/driving/{coords_str}?annotations=distance"
    try:
        resp = requests.get(url, timeout=20)
        data = resp.json()
        if data.get("code") == "Ok":
            return [[int(d) for d in row] for row in data["distances"]], True
    except:
        pass
    n = len(locations)
    return [[0 if i==j else haversine(locations[i], locations[j]) for j in range(n)] for i in range(n)], False

def obtener_ruta_osrm(origen, destino):
    url = (f"http://router.project-osrm.org/route/v1/driving/"
           f"{origen[1]},{origen[0]};{destino[1]},{destino[0]}"
           f"?overview=full&geometries=geojson")
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
        if data.get("code") == "Ok":
            coords  = data["routes"][0]["geometry"]["coordinates"]
            dist_m  = data["routes"][0]["legs"][0]["distance"]
            return [(lat, lon) for lon, lat in coords], dist_m
    except:
        pass
    return [origen, destino], haversine(origen, destino)

def resolver_vrp(distancias):
    manager = pywrapcp.RoutingIndexManager(len(distancias), 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def cb(from_idx, to_idx):
        return distancias[manager.IndexToNode(from_idx)][manager.IndexToNode(to_idx)]

    t = routing.RegisterTransitCallback(cb)
    routing.SetArcCostEvaluatorOfAllVehicles(t)

    params = pywrapcp.DefaultRoutingSearchParameters()
    params.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    params.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    params.time_limit.seconds = 10

    sol = routing.SolveWithParameters(params)
    if not sol:
        return None

    idx = routing.Start(0)
    ruta = []
    while not routing.IsEnd(idx):
        ruta.append(manager.IndexToNode(idx))
        idx = sol.Value(routing.NextVar(idx))
    ruta.append(manager.IndexToNode(idx))
    return ruta

# ─────────────────────────────────────────────
# BOTÓN CALCULAR
# ─────────────────────────────────────────────
if st.button("🔍 Calcular Ruta Óptima", type="primary", use_container_width=True):
    puntos = tabla.dropna(subset=["Latitud", "Longitud"])
    if len(puntos) < 2:
        st.error("Necesitas al menos 2 puntos de recolección.")
        st.stop()

    DEPOT     = (depot_lat, depot_lon)
    LOCATIONS = [DEPOT] + list(zip(puntos["Latitud"], puntos["Longitud"]))
    NOMBRES   = ["DEPOT"] + puntos["Nombre"].tolist()
    PESOS     = [0] + puntos["Peso (kg)"].tolist()

    with st.spinner("📡 Consultando OSRM y optimizando ruta..."):
        distancias, uso_osrm = obtener_matriz_osrm(LOCATIONS)
        ruta_nodos = resolver_vrp(distancias)

        if not ruta_nodos:
            st.error("No se encontró solución.")
            st.stop()

        # Segmentos con ruta real
        segmentos = []
        for i in range(len(ruta_nodos) - 1):
            camino, dist_m = obtener_ruta_osrm(LOCATIONS[ruta_nodos[i]], LOCATIONS[ruta_nodos[i+1]])
            segmentos.append({"camino": camino, "dist_m": dist_m})

        # Horarios
        hora_actual    = datetime.combine(datetime.today(), hora_inicio)
        peso_acumulado = 0
        resumen        = []

        for i, node in enumerate(ruta_nodos):
            if i == 0:
                resumen.append({
                    "Parada": "🏠 Inicio (Depot)",
                    "Nombre": NOMBRES[node],
                    "Hora llegada": hora_actual.strftime("%H:%M"),
                    "Peso recogido (kg)": 0,
                    "Peso acumulado (kg)": 0,
                    "Distancia tramo (km)": "-",
                })
            else:
                dist_m = segmentos[i-1]["dist_m"]
                hora_actual += timedelta(hours=(dist_m/1000) / velocidad_kmh)
                es_ultimo    = (i == len(ruta_nodos) - 1)
                peso_parada  = PESOS[node] if not es_ultimo else 0
                peso_acumulado += peso_parada

                resumen.append({
                    "Parada": "🏠 Fin (Depot)" if es_ultimo else f"📦 Parada {i}",
                    "Nombre": NOMBRES[node],
                    "Hora llegada": hora_actual.strftime("%H:%M"),
                    "Peso recogido (kg)": peso_parada,
                    "Peso acumulado (kg)": peso_acumulado,
                    "Distancia tramo (km)": f"{dist_m/1000:.2f}",
                })

                if not es_ultimo:
                    hora_actual += timedelta(minutes=tiempo_parada)

        # Guardar en session_state
        st.session_state.resultados = {
            "uso_osrm":        uso_osrm,
            "ruta_nodos":      ruta_nodos,
            "segmentos":       segmentos,
            "resumen":         resumen,
            "hora_fin":        hora_actual.strftime("%H:%M"),
            "peso_acumulado":  peso_acumulado,
            "LOCATIONS":       LOCATIONS,
            "NOMBRES":         NOMBRES,
            "PESOS":           PESOS,
        }

# ─────────────────────────────────────────────
# MOSTRAR RESULTADOS (desde session_state)
# ─────────────────────────────────────────────
if st.session_state.resultados:
    r = st.session_state.resultados

    if r["uso_osrm"]:
        st.success("✅ Ruta calculada con distancias reales por carretera (OSRM)")
    else:
        st.warning("⚠️ Sin conexión a OSRM — usando distancias en línea recta.")

    dist_total = sum(s["dist_m"] for s in r["segmentos"]) / 1000

    # Métricas
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("📏 Distancia total",  f"{dist_total:.1f} km")
    col2.metric("🕐 Hora inicio",       hora_inicio.strftime("%H:%M"))
    col3.metric("🕐 Hora fin estimada", r["hora_fin"])
    excede = r["peso_acumulado"] > capacidad_max
    col4.metric("📦 Peso total", f"{r['peso_acumulado']} kg",
                delta="⚠️ Excede capacidad" if excede else "✅ Dentro del límite",
                delta_color="inverse" if excede else "normal")

    if excede:
        st.error(f"⚠️ El peso total ({r['peso_acumulado']} kg) excede la capacidad ({capacidad_max} kg).")

    # Tabla
    st.subheader("📋 Detalle por parada")
    df = pd.DataFrame(r["resumen"])
    st.dataframe(df, use_container_width=True, hide_index=True)

    # Mapa
    st.subheader("🗺️ Mapa de ruta")
    lats   = [r["LOCATIONS"][n][0] for n in r["ruta_nodos"]]
    lons   = [r["LOCATIONS"][n][1] for n in r["ruta_nodos"]]
    centro = (sum(lats)/len(lats), sum(lons)/len(lons))

    m = folium.Map(location=centro, zoom_start=12, tiles="OpenStreetMap")

    for i, seg in enumerate(r["segmentos"]):
        folium.PolyLine(seg["camino"], color="#E74C3C", weight=4, opacity=0.85).add_to(m)

    for i, node in enumerate(r["ruta_nodos"][:-1]):
        lat, lon     = r["LOCATIONS"][node]
        hora_parada  = r["resumen"][i]["Hora llegada"]
        if node == 0:
            folium.Marker(
                location=[lat, lon],
                popup="🏠 DEPOT — Inicio/Fin",
                tooltip="DEPOT",
                icon=folium.Icon(color="red", icon="home")
            ).add_to(m)
        else:
            peso_p    = r["PESOS"][node]
            icon_html = f"""<div style="background:#2980B9;color:white;border-radius:50%;
                            width:32px;height:32px;display:flex;align-items:center;
                            justify-content:center;font-size:15px;font-weight:bold;
                            border:2px solid white;box-shadow:2px 2px 4px rgba(0,0,0,0.4);">{i}</div>"""
            folium.Marker(
                location=[lat, lon],
                popup=f"<b>{r['NOMBRES'][node]}</b><br>⏰ {hora_parada}<br>📦 {peso_p} kg",
                tooltip=f"{i}. {r['NOMBRES'][node]} — {hora_parada}",
                icon=folium.DivIcon(html=icon_html, icon_size=(32,32), icon_anchor=(16,16))
            ).add_to(m)

    st_folium(m, use_container_width=True, height=520, returned_objects=[])

    # Exportar
    st.subheader("⬇️ Exportar")
    csv = df.to_csv(index=False).encode("utf-8")
    st.download_button("📥 Descargar resumen CSV", csv, "ruta_optima.csv", "text/csv")
