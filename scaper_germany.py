import requests
import json
import math
import time

# --- KONFIGURATION DEUTSCHLAND ---

LAT_START = 47.0
LAT_END   = 55.2
LON_START = 5.5
LON_END   = 15.5

# Step Size für das Raster (ca. 50x50km pro Kachel)
STEP_SIZE = 0.5

SEARCH_RADIUS_METERS = 300 
OUTPUT_FILENAME = "data.json" 

# Regex für Overpass angepasst um neue Restaurants
# (Case insensitive search wird später in der Query aktiviert)
FOOD_REGEX = "McDonald|Burger King|Lounge|World|Hub|Tegut|Rewe|Porsche|Audi|Seed|KFC|Kentucky|Subway|Nordsee"

# --- DEFINITIONEN ---

ALLOWED_CHARGERS = {
    "tesla":   {"name": "Tesla Supercharger", "class": "bg-tesla"},
    "ionity":  {"name": "IONITY", "class": "bg-ionity"},
    "enbw":    {"name": "EnBW", "class": "bg-enbw"},
    # NEU:
    "fastned": {"name": "Fastned", "class": "bg-fastned"},
    "allego":  {"name": "Allego", "class": "bg-allego"},
    "aral":    {"name": "Aral pulse", "class": "bg-aral"},
    "pulse":   {"name": "Aral pulse", "class": "bg-aral"}, 
}

ALLOWED_FOOD = {
    "mcdonald":    {"name": "McDonald's", "class": "bg-mcd"},
    "burger king": {"name": "Burger King", "class": "bg-bk"},
    # NEU:
    "kfc":         {"name": "KFC", "class": "bg-kfc"},
    "kentucky":    {"name": "KFC", "class": "bg-kfc"}, # Fallback für ausgeschriebenen Namen
    "subway":      {"name": "Subway", "class": "bg-subway"},
    "nordsee":     {"name": "Nordsee", "class": "bg-nordsee"},
    # LOUNGE / SONSTIGES:
    "lounge":      {"name": "Lounge / Shop", "class": "bg-purple-600"}     
}

# Spezielle Keywords, die bevorzugt als "Lounge" behandelt werden
LOUNGE_KEYWORDS = [
    "bk world", "tegut", "rewe ready", "rewe to go", 
    "audi charging hub", "porsche", "seed & greet", "seed&greet",
    "lounge", "charging hub"
]

# --- HILFSFUNKTIONEN ---

def get_coords(element):
    if 'center' in element: return element['center']['lat'], element['center']['lon']
    elif 'lat' in element and 'lon' in element: return element['lat'], element['lon']
    return None, None

def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = math.sin(math.radians(lat2 - lat1) / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lon2 - lon1) / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def process_tile(bbox_str):
    # Overpass Query
    overpass_query = f"""
    [out:json][timeout:180];
    (
      nwr["amenity"="charging_station"]({bbox_str});
      nwr["amenity"~"fast_food|restaurant|cafe|lounge|vending_machine"]["name"~"{FOOD_REGEX}",i]({bbox_str});
      nwr["shop"~"kiosk|convenience"]["name"~"{FOOD_REGEX}",i]({bbox_str});
    );
    out center;
    """
    try:
        r = requests.get("https://overpass.private.coffee/api/interpreter", params={'data': overpass_query}, timeout=190)
        if r.status_code != 200: 
            print(f" [Error {r.status_code}]", end="")
            return []
        data = r.json()
        elements = data.get("elements", [])
    except Exception as e:
        print(f" [Exception: {e}]", end="")
        return []

    chargers = []
    restaurants = []

    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name", "Unbekannt")
        # Wir suchen in Name, Brand und Operator (alles kleingeschrieben)
        full_search = (name + " " + tags.get("brand", "") + " " + tags.get("operator", "")).lower()
        
        # --- POI Logic (Essen) ---
        is_poi = False
        if tags.get("amenity") in ["fast_food", "restaurant", "cafe", "lounge", "vending_machine"]: is_poi = True
        if tags.get("shop") in ["kiosk", "convenience"]: is_poi = True

        if is_poi:
            config, fid = None, None
            
            # Erst prüfen ob es eine Lounge ist
            for kw in LOUNGE_KEYWORDS:
                if kw in full_search: 
                    config, fid = ALLOWED_FOOD["lounge"], "lounge"
                    break
            
            # Wenn keine Lounge, dann nach Marken suchen
            if not config:
                for k, c in ALLOWED_FOOD.items():
                    if k != "lounge" and k in full_search: 
                        config, fid = c, k
                        # Wir normalisieren die ID (z.B. kentucky -> kfc)
                        if k == "kentucky": fid = "kfc"
                        if k == "pulse": fid = "aral"
                        break
            
            if config:
                el['clean_info'] = config
                el['id_key'] = fid
                restaurants.append(el)

        # --- Charger Logic (Laden) ---
        elif tags.get("amenity") == "charging_station":
            config, fid = None, None
            for k, c in ALLOWED_CHARGERS.items():
                if k in full_search: 
                    config, fid = c, k
                    if k == "pulse": fid = "aral" # Normalisierung für ID
                    break
            
            if config:
                el['clean_info'] = config
                el['id_key'] = fid
                # Schönen Namen setzen falls "Unbekannt"
                el['clean_info']['name'] = config['name'] if "Unbekannt" in name else name
                chargers.append(el)

    # --- Matching Logic ---
    tile_matches = []
    for c in chargers:
        c_lat, c_lon = get_coords(c)
        if not c_lat: continue
        for r in restaurants:
            r_lat, r_lon = get_coords(r)
            if not r_lat: continue
            
            # Grober Vorab-Filter um Rechenzeit zu sparen (ca 2km Box)
            if abs(c_lat - r_lat) > 0.02 or abs(c_lon - r_lon) > 0.02: continue
            
            dist = calculate_distance(c_lat, c_lon, r_lat, r_lon)
            if dist <= SEARCH_RADIUS_METERS:
                poi_type = r['clean_info']['name']
                poi_real = r.get('tags', {}).get('name', poi_type)
                
                # Unique ID erstellen
                match_id = f"{c.get('id')}_{r.get('id')}"
                
                tile_matches.append({
                    "unique_id": match_id,
                    "lat": c_lat,
                    "lon": c_lon,
                    "charger_id": c['id_key'],
                    "food_id": r['id_key'],
                    "title": f"{c['clean_info']['name']} + {poi_real}",
                    "badge_class": c['clean_info']['class'],
                    "note": f"{int(dist)}m zu Fuß", # Kurzer Text für Popup
                    "description": (
                        f"<div style='margin-bottom:4px; font-weight:bold; font-size:1.1em; color:var(--charger-color)'>{c['clean_info']['name']}</div>"
                        f"<div style='display:flex; align-items:center; gap:5px;'>"
                        f"  <span>🍽️</span>"
                        f"  <span style='font-weight:600;'>{poi_real}</span>"
                        f"</div>"
                        f"<div style='font-size:0.85em; color:var(--muted-text); margin-top:4px;'>Entfernung: {int(dist)}m</div>"
                    )
                })
    return tile_matches

# --- HAUPTPROGRAMM ---

start_total_time = time.time()
print(f"🚀 Starte Deutschland-Scan ({LAT_START}-{LAT_END} / {LON_START}-{LON_END})...")
print(f"ℹ️  Raster-Größe: {STEP_SIZE} Grad. Suche nach: {FOOD_REGEX}")

all_matches = []
processed_ids = set() 
tile_count = 0

current_lat = LAT_START
while current_lat < LAT_END:
    current_lon = LON_START
    while current_lon < LON_END:
        tile_count += 1
        tile_start_time = time.time()
        
        lat_min = current_lat
        lon_min = current_lon
        lat_max = min(current_lat + STEP_SIZE, 90)
        lon_max = min(current_lon + STEP_SIZE, 180)
        bbox = f"{lat_min},{lon_min},{lat_max},{lon_max}"
        
        print(f"[{tile_count}] Sektor {bbox} ... ", end="", flush=True)
        
        matches = process_tile(bbox)
        
        new_matches_count = 0
        for m in matches:
            if m["unique_id"] not in processed_ids:
                processed_ids.add(m.pop("unique_id")) 
                all_matches.append(m)
                new_matches_count += 1
        
        duration = time.time() - tile_start_time
        print(f"-> {len(matches)} Treffer ({new_matches_count} neu). Zeit: {duration:.1f}s")
        
        current_lon += STEP_SIZE
        if duration < 2: time.sleep(1) 
    
    current_lat += STEP_SIZE

end_total_time = time.time()
total_duration = end_total_time - start_total_time
m = int(total_duration // 60)
s = int(total_duration % 60)

print(f"\n✅ FERTIG in {m}m {s}s!")
print(f"💾 Speichere {len(all_matches)} Orte in {OUTPUT_FILENAME}...")

# with open(OUTPUT_FILENAME, 'w', encoding='utf-8') as f:
#     json.dump(all_matches, f, ensure_ascii=False, indent=2)

# --- TEIL AM ENDE DES SKRIPTS EINFÜGEN ---

# 1. Alte Datei lesen (um Vergleich zu haben)
try:
    with open(OUTPUT_FILENAME, 'r', encoding='utf-8') as f:
        old_data = json.load(f)
        old_count = len(old_data)
except:
    old_count = 0

# 2. Neue Daten speichern (das hast du schon, hier nur der Vollständigkeit halber)
with open(OUTPUT_FILENAME, 'w', encoding='utf-8') as f:
    json.dump(merged_data, f, ensure_ascii=False, indent=2)

# 3. Statistik berechnen
new_count = len(merged_data)
diff = new_count - old_count
diff_sign = "+" if diff >= 0 else "" # Damit da "+5" steht und nicht nur "5"

print(f"Statistik: Alt={old_count}, Neu={new_count}, Diff={diff_sign}{diff}")

# 4. Werte an GitHub Actions übergeben (nur wenn wir auf dem Server laufen)
import os
if "GITHUB_OUTPUT" in os.environ:
    with open(os.environ["GITHUB_OUTPUT"], "a") as gh_out:
        gh_out.write(f"NEW_COUNT={new_count}\n")
        gh_out.write(f"DIFF={diff_sign}{diff}\n")
        gh_out.write(f"MSG=Update: {new_count} Einträge ({diff_sign}{diff})\n")
