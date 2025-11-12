import pandas as pd
import numpy as np
import folium
from geopy.distance import geodesic
import os
import shutil

# -------------------------
# LOCALIZACION DE ARCHIVO MAS RECIENTE
# -------------------------
carpeta_base = "/home/itoroc/Database"
csvs = [f for f in os.listdir(carpeta_base) if f.endswith(".csv")]
csvs.sort(key=lambda f: os.path.getmtime(os.path.join(carpeta_base, f)), reverse=True)

if not csvs:
    raise FileNotFoundError("No se encontraron archivos CSV en la carpeta.")

archivo_reciente = os.path.join(carpeta_base, csvs[0])

# generar nombre YYYYMMDD_Actual.csv
hoy = pd.Timestamp.now()
fecha_tag = hoy.strftime("%Y%m%d")
nombre_estandar = f"{fecha_tag}_Actual.csv"
ruta_estandar = os.path.join(carpeta_base, nombre_estandar)

if archivo_reciente != ruta_estandar:
    shutil.copy(archivo_reciente, ruta_estandar)


# -------------------------
# CARGAR DATOS
# -------------------------
df_real = pd.read_csv(ruta_estandar)
df_real["Tipo"] = "Sensor"

if "D_uSv_h" in df_real.columns:
    df_real.rename(columns={"D_uSv_h": "Dosis_uSv_h"}, inplace=True)

# -------------------------
# ORDENAR POR TIEMPO GPS
# -------------------------
if set(["Ano","Mes","Dia","Hora","Minuto","Segundo"]).issubset(df_real.columns):
    df_real["ts"] = pd.to_datetime(
        df_real[["Ano","Mes","Dia","Hora","Minuto","Segundo"]]
        .rename(columns={"Ano":"year","Mes":"month","Dia":"day","Hora":"hour","Minuto":"minute","Segundo":"second"}),
        errors="coerce"
    )
    df_real.sort_values("ts", inplace=True, kind="stable")
elif set(["DATE","TIME"]).issubset(df_real.columns):
    df_real["ts"] = pd.to_datetime(df_real["DATE"].astype(str) + " " + df_real["TIME"].astype(str), errors="coerce")
    df_real.sort_values("ts", inplace=True, kind="stable")
else:
    df_real["ts"] = pd.NaT

df_real = df_real.reset_index(drop=True)

# -------------------------
# UBICAR FOCO Y PRIMERA MEDICION
# -------------------------
indice_max = df_real[df_real["CPM"] == df_real["CPM"].max()].index[0]
indice_ini = df_real.index[0]

lat_centro = float(df_real.loc[indice_max, "Latitud"])
lon_centro = float(df_real.loc[indice_max, "Longitud"])
lat_ini = float(df_real.loc[indice_ini, "Latitud"])
lon_ini = float(df_real.loc[indice_ini, "Longitud"])

# -------------------------
# COLORES Y PARAMETROS
# -------------------------
colormap_int = {
    0: "#B0B0B0",
    1: "#ADFF2F",
    2: "#FFFF00",
    3: "#FFA500",
    4: "#FF4500",
    5: "#800080",
}

grosor_por_nivel = {1:0.8, 2:1.0, 3:1.5, 4:2.5, 5:3.5}

def calcular_nivel(cpm):
    if cpm >= 5 and cpm <= 150:
        return 1
    elif cpm <= 500:
        return 2
    elif cpm <= 1500:
        return 3
    elif cpm <= 6000:
        return 4
    elif cpm <= 15000:
        return 5
    else:
        return 0

df_real["Nivel"] = df_real["CPM"].apply(calcular_nivel)

# -------------------------
# DISTANCIAS Y RADIOS
# -------------------------
df_real["X_c"] = df_real["Latitud"] - lat_centro
df_real["Y_c"] = df_real["Longitud"] - lon_centro
df_real["dist_m"] = np.sqrt(df_real["X_c"]**2 + df_real["Y_c"]**2) * 111000

nuevo_radio_exterior = {}
for nivel in range(2, 6):
    df_n = df_real[df_real["Nivel"] == nivel]
    if not df_n.empty:
        nuevo_radio_exterior[nivel] = float(df_n["dist_m"].max()) + 1.0
    else:
        nuevo_radio_exterior[nivel] = 0.0
nuevo_radio_exterior[1] = nuevo_radio_exterior[2] + 10.0 if nuevo_radio_exterior[2] else 15.0

# -------------------------
# MAPA FOLIUM
# -------------------------
m = folium.Map(location=[lat_centro, lon_centro], zoom_start=20, tiles='cartodbpositron')

# puntos
for _, row in df_real.iterrows():
    color = colormap_int.get(int(row["Nivel"]), "#B0B0B0")
    folium.CircleMarker(
        location=(float(row["Latitud"]), float(row["Longitud"])),
        radius=3,
        color='black',
        fill=True,
        fill_opacity=0.9,
        fill_color=color,
        weight=0.3,
        popup=f"Nivel {int(row['Nivel'])} ({int(row['CPM'])} CPM)"
    ).add_to(m)

# circulos de exclusion
for nivel in range(1, 6):
    radio_m = nuevo_radio_exterior[nivel]
    if radio_m > 0:
        folium.Circle(
            location=[lat_centro, lon_centro],
            radius=float(radio_m),
            color=colormap_int[nivel],
            weight=grosor_por_nivel[nivel],
            fill=False,
            popup=f"Zona Nivel {nivel}"
        ).add_to(m)

# linea primer punto a foco
dist_metros = geodesic((lat_ini, lon_ini), (lat_centro, lon_centro)).meters
texto_distancia = f"Distancia al foco: {dist_metros:.1f} m"
folium.PolyLine(
    [(lat_ini, lon_ini), (lat_centro, lon_centro)],
    color="black",
    weight=2,
    popup=texto_distancia,
    tooltip=texto_distancia
).add_to(m)

# camino recorrido por tiempo GPS
df_camino = df_real.copy()
if "ts" in df_camino.columns and df_camino["ts"].notna().any():
    df_camino = df_camino.sort_values("ts", kind="stable")
coords_camino = list(zip(df_camino["Latitud"].astype(float), df_camino["Longitud"].astype(float)))
coords_camino = [(la, lo) for la, lo in coords_camino if np.isfinite(la) and np.isfinite(lo)]
if len(coords_camino) >= 2:
    folium.PolyLine(coords_camino, color="#333333", weight=2.5, opacity=0.9, tooltip="Camino recorrido (orden GPS)").add_to(m)

# guardar
ruta_html = "/home/itoroc/zonas/mapa_zonas.html"
os.makedirs(os.path.dirname(ruta_html), exist_ok=True)
m.save(ruta_html)
print(f"OK Mapa generado: {ruta_html}")
