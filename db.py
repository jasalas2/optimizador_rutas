"""
db.py — Persistencia local con SQLite
======================================
Guarda los puntos de recolección y la configuración del optimizador
para que no se pierdan al recargar la página.

Nota para Fase 2 (multi-camión):
La tabla 'puntos' ya incluye la columna 'camion_asignado' (vacía por ahora).
Cuando se agregue el VRP multi-vehículo, esa columna se llenará con el
camión asignado a cada punto, sin necesidad de migrar datos.
"""

import sqlite3
import pandas as pd
from contextlib import contextmanager

DB_PATH = "rutas.db"

# Mapeo entre nombres de columna en la base de datos (snake_case)
# y los nombres que se muestran en la tabla editable de Streamlit.
DB_TO_UI = {
    "nombre": "Nombre",
    "direccion": "Dirección",
    "latitud": "Latitud",
    "longitud": "Longitud",
    "peso_kg": "Peso (kg)",
    "frecuencia": "Días de recolección",
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
    """Crea las tablas si no existen. Llamar una vez al iniciar la app."""
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS puntos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                nombre TEXT NOT NULL,
                direccion TEXT,
                latitud REAL,
                longitud REAL,
                peso_kg REAL DEFAULT 0,
                camion_asignado TEXT,
                frecuencia TEXT DEFAULT 'Todos los días'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                clave TEXT PRIMARY KEY,
                valor TEXT
            )
        """)
        # Migración: si la base ya existía de antes (sin esta columna), se agrega ahora.
        columnas = [fila[1] for fila in conn.execute("PRAGMA table_info(puntos)").fetchall()]
        if "frecuencia" not in columnas:
            conn.execute("ALTER TABLE puntos ADD COLUMN frecuencia TEXT DEFAULT 'Todos los días'")


def hay_puntos_guardados():
    with get_conn() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM puntos")
        return cur.fetchone()[0] > 0


def cargar_puntos():
    """Devuelve un DataFrame con nombres de columna listos para mostrar en la UI."""
    with get_conn() as conn:
        df = pd.read_sql_query(
            "SELECT nombre, direccion, latitud, longitud, peso_kg, frecuencia FROM puntos ORDER BY id",
            conn,
        )
    df["frecuencia"] = df["frecuencia"].fillna("Todos los días")
    return df.rename(columns=DB_TO_UI)


def guardar_puntos(df_ui):
    """Recibe el DataFrame de la tabla editable (columnas en español) y lo persiste."""
    df = df_ui.rename(columns=UI_TO_DB).copy()
    # Asegurar que existan todas las columnas esperadas, aunque el usuario
    # no haya agregado la columna Dirección.
    for col in ["nombre", "direccion", "latitud", "longitud", "peso_kg", "frecuencia"]:
        if col not in df.columns:
            df[col] = None
    df = df[["nombre", "direccion", "latitud", "longitud", "peso_kg", "frecuencia"]]
    df["frecuencia"] = df["frecuencia"].fillna("Todos los días")
    df = df.dropna(subset=["nombre"])  # filas vacías que deja el editor

    with get_conn() as conn:
        conn.execute("DELETE FROM puntos")
        if len(df) > 0:
            df.to_sql("puntos", conn, if_exists="append", index=False)


def obtener_config(clave, default=None):
    with get_conn() as conn:
        cur = conn.execute("SELECT valor FROM config WHERE clave = ?", (clave,))
        row = cur.fetchone()
        return row[0] if row is not None else default


def guardar_config(clave, valor):
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO config (clave, valor) VALUES (?, ?)
               ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor""",
            (clave, str(valor)),
        )


def guardar_configuracion_general(**kwargs):
    """Guarda varios valores de configuración de una sola vez.
    Ej: guardar_configuracion_general(hora_inicio="08:00", velocidad_kmh=40)
    """
    with get_conn() as conn:
        for clave, valor in kwargs.items():
            conn.execute(
                """INSERT INTO config (clave, valor) VALUES (?, ?)
                   ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor""",
                (clave, str(valor)),
            )
