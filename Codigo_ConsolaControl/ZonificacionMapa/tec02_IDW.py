#!/usr/bin/env python3
# generar_mapa_idw.py
# Base igual a tu script. Solo agrega overlay IDW y el perimetro exterior.
# Sin scipy ni matplotlib.

import pandas as pd
import numpy as np
import folium
import os
import shutil
import math

# intento de usar geopy; si no esta, usa aproximacion plana
try:
    from geopy.distance import geodesic
    def dist_m(a, b):
        return geodesic(a, b).meters
except Exception:
    def dist_m(a, b):
        lat1, lon1 = a; lat2, lon2 = b
        dlat = (lat2 - lat1) * 111320.0
        dlon = (lon2 - lon1) * 111320.0 * math.cos(math.radians((lat1 + lat2) / 2.0))
        return (dlat * dlat + dlon * dlon) ** 0.5

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
# FOCO (max CPM) Y COLORES
# -------------------------
indice_max = df_real[df_real["CPM"] == df_real["CPM"].max()].index[0]
lat_centro = float(df_real.loc[indice_max, "Latitud"])
lon_centro = float(df_real.loc[indice_max, "Longitud"])

colormap_int = {
    0: "#B0B0B0",
    1: "#ADFF2F",
    2: "#FFFF00",
    3: "#FFA500",
    4: "#FF4500",
    5: "#800080",
}
nivel_alpha = {0: 0.15, 1: 0.20, 2: 0.30, 3: 0.45, 4: 0.60, 5: 0.75}

def calcular_nivel(cpm):
    if cpm >= 5 and cpm <= 150: return 1
    elif cpm <= 500: return 2
    elif cpm <= 1500: return 3
    elif cpm <= 6000: return 4
    elif cpm <= 15000: return 5
    else: return 0

df_real["Nivel"] = df_real["CPM"].apply(calcular_nivel)


# -------------------------
# INTERPOLACION IDW (numpy) -> OVERLAY RGBA
# -------------------------
lats = df_real["Latitud"].astype(float).to_numpy()
lons = df_real["Longitud"].astype(float).to_numpy()
vals = df_real["CPM"].astype(float).to_numpy()

lat_min, lat_max = float(lats.min()), float(lats.max())
lon_min, lon_max = float(lons.min()), float(lons.max())
pad_lat = max((lat_max - lat_min) * 0.05, 1e-5)
pad_lon = max((lon_max - lon_min) * 0.05, 1e-5)
lat_min -= pad_lat; lat_max += pad_lat
lon_min -= pad_lon; lon_max += pad_lon

H, W = 320, 320
grid_lat = np.linspace(lat_max, lat_min, H)  # descendente para alinear con folium
grid_lon = np.linspace(lon_min, lon_max, W)
Lon, Lat = np.meshgrid(grid_lon, grid_lat)

p = 2.0
eps = 1e-12
Z = np.empty((H, W), dtype=float)
q_lat = Lat.ravel()
q_lon = Lon.ravel()
M = q_lat.shape[0]
chunk = 2000

for start in range(0, M, chunk):
    end = min(start + chunk, M)
    qa = q_lat[start:end][:, None]
    qo = q_lon[start:end][:, None]
    dlat = qa - lats[None, :]
    dlon = qo - lons[None, :]
    dist = np.sqrt(dlat*dlat + dlon*dlon) + eps
    w = 1.0 / np.power(dist, p)
    num = (w * vals[None, :]).sum(axis=1)
    den = w.sum(axis=1) + eps
    Z.ravel()[start:end] = num / den

zmin, zmax = np.percentile(vals, 1), np.percentile(vals, 99)
Z = np.clip(Z, zmin, zmax)

def nivel_from_cpm(c):
    if c <= 4: return 0
    if c <= 150: return 1
    if c <= 500: return 2
    if c <= 1500: return 3
    if c <= 6000: return 4
    return 5

Zlvl = np.vectorize(nivel_from_cpm)(Z).astype(int)

def hex_to_rgb255(h):
    h = h.lstrip("#")
    return int(h[0:2],16), int(h[2:4],16), int(h[4:6],16)

img = np.zeros((H, W, 4), dtype=np.uint8)
for lvl in range(0, 6):
    mask = (Zlvl == lvl)
    r, g, b = hex_to_rgb255(colormap_int[lvl])
    a = int(nivel_alpha[lvl] * 255)
    img[mask, 0] = r; img[mask, 1] = g; img[mask, 2] = b; img[mask, 3] = a

# -------------------------
# MAPA: OVERLAY + PUNTOS + PERIMETRO EXTERIOR
# -------------------------
m = folium.Map(location=[lat_centro, lon_centro], zoom_start=20, tiles='cartodbpositron')

bounds = [[lat_min, lon_min], [lat_max, lon_max]]
folium.raster_layers.ImageOverlay(
    image=img, bounds=bounds, opacity=1.0, interactive=False, cross_origin=False, zindex=1, name="IDW"
).add_to(m)

for _, row in df_real.iterrows():
    color = colormap_int.get(int(row["Nivel"]), "#B0B0B0")
    folium.CircleMarker(
        location=(float(row["Latitud"]), float(row["Longitud"])),
        radius=3, color="black", fill=True, fill_opacity=0.9,
        fill_color=color, weight=0.3,
        popup=f"Nivel {int(row['Nivel'])} ({float(row['CPM']):.1f} CPM)"
    ).add_to(m)

# perimetro exterior (nivel 1) usando max distancia de niveles 2..5
radios = {}
for nivel in range(2, 6):
    df_n = df_real[df_real["Nivel"] == nivel]
    if not df_n.empty:
        radios[nivel] = max(
            dist_m((lat_centro, lon_centro), (float(r["Latitud"]), float(r["Longitud"])))
            for _, r in df_n.iterrows()
        ) + 1.0
    else:
        radios[nivel] = 0.0
radio_exterior = (radios[2] + 10.0) if radios[2] else 15.0

folium.Circle(
    location=[lat_centro, lon_centro],
    radius=radio_exterior,
    color=colormap_int[1],
    weight=0.8,
    fill=False,
    popup="Perimetro exterior (Nivel 1)"
).add_to(m)

folium.LayerControl(collapsed=False).add_to(m)

# -------------------------
# GUARDAR MAPA
# -------------------------
ruta_html = "/home/itoroc/zonas/mapa_zonas.html"
os.makedirs(os.path.dirname(ruta_html), exist_ok=True)
m.save(ruta_html)
print(f"OK Mapa generado: {ruta_html}")
