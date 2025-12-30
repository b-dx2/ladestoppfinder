import requests
import json
import math
import time
import datetime
import os


# --- KONFIGURATION DEUTSCHLAND ---

LAT_START = 47.0
LAT_END   = 55.2
LON_START = 5.5
LON_END   = 15.5

LAT_START = 48.5
LAT_END   = 48.9
LON_START = 9
LON_END   = 10


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
                    if k == "pulse": fid = "aral" # Normalisierung
                    break
            
            if config:
                # 1. Daten vorbereiten
                el['clean_info'] = config.copy() 
                el['id_key'] = fid
                
                # 2. Namen generieren (Logik wie eben besprochen)
                display_name = name 
                if "Unbekannt" in display_name:
                    if tags.get("brand"):
                        display_name = tags.get("brand") 
                    elif tags.get("operator"):
                         display_name = tags.get("operator")
                    else:
                        display_name = config['name']
                    
                    city = tags.get("addr:city")
                    if city:
                        display_name = f"{display_name} ({city})"
                
                el['clean_info']['name'] = display_name

                # 3. DUPLIKAT-CHECK (Neu!)
                # Wir schauen in die Liste 'chargers', ob schon einer da ist.
                # 3. DUPLIKAT-CHECK (Korrigiert & Sicher)
                is_duplicate = False
                
                # Wir holen die Koordinaten sicher über deine Hilfsfunktion
                # (verhindert Absturz, falls es ein Polygon/Way ist)
                current_lat, current_lon = get_coords(el)

                if current_lat and current_lon:
                    for existing in chargers:
                        # Nur prüfen, wenn es der gleiche Anbieter ist
                        if existing['id_key'] == el['id_key']:
                            
                            # Koordinaten des bereits gespeicherten Punkts holen
                            existing_lat, existing_lon = get_coords(existing)
                            
                            if existing_lat and existing_lon:
                                # KORREKTUR: Hier deine Funktion 'calculate_distance' nutzen
                                dist = calculate_distance(current_lat, current_lon, existing_lat, existing_lon)
                                
                                if dist < 30: # 30 Meter Radius
                                    is_duplicate = True
                                    break
                
                # Nur hinzufügen, wenn kein Duplikat gefunden wurde
                if not is_duplicate:
                    chargers.append(el)

    # --- Matching Logic ---
    # --- Matching Logic ---
    tile_matches = []
    
    for c in chargers:
        c_lat, c_lon = get_coords(c)
        if not c_lat: continue
        
        # 1. Wir suchen das NÄCHSTE Restaurant zu diesem Charger
        best_food = None
        closest_dist = 999999 # Startwert hoch setzen

        for r in restaurants:
            r_lat, r_lon = get_coords(r)
            if not r_lat: continue
            
            # Grober Vorab-Filter (ca 2km Box)
            if abs(c_lat - r_lat) > 0.02 or abs(c_lon - r_lon) > 0.02: continue
            
            dist = calculate_distance(c_lat, c_lon, r_lat, r_lon)
            
            # Ist dies das nächste Restaurant innerhalb des Suchradius?
            if dist <= SEARCH_RADIUS_METERS and dist < closest_dist:
                closest_dist = dist
                best_food = r
        
        # 2. Daten für JSON vorbereiten
        
        # Basis-Info (Charger)
        entry = {
            "lat": c_lat,
            "lon": c_lon,
            "charger_id": c['id_key'], # z.B. "tesla"
            "food_id": None,           # Standard: Kein Essen
            "title": c['clean_info']['name'],
            "badge_class": c['clean_info']['class'],
            "note": "Keine Verpflegung",
            "popup_name": c['clean_info']['name']
        }

        # Wenn Essen gefunden wurde
        if best_food:
            # WICHTIG: Leerzeichen für CSS entfernen (z.B. "burger king" -> "burger-king")
            food_clean_id = best_food['id_key'].replace(" ", "-")
            food_real_name = best_food.get('tags', {}).get('name', best_food['clean_info']['name'])
            
            entry['food_id'] = food_clean_id
            entry['note'] = f"{int(closest_dist)}m zu {food_real_name}"
            
            # Schöne Beschreibung für das Popup bauen
            entry['description'] = (
                f"<div style='margin-bottom:4px; font-weight:bold; font-size:1.1em; color:var(--charger-color)'>{c['clean_info']['name']}</div>"
                f"<div style='display:flex; align-items:center; gap:5px; margin-top:5px;'>"
                f"  <span>🍽️</span>"
                f"  <span style='font-weight:600;'>{food_real_name}</span>"
                f"</div>"
                f"<div style='font-size:0.85em; color:#666; margin-top:2px;'>Entfernung: {int(closest_dist)}m</div>"
            )
            # Unique ID Kombi
            entry["unique_id"] = f"{c.get('id')}_{best_food.get('id')}"
        
        else:
            # Kein Essen: Nur Charger Info im Popup
            entry['description'] = (
                f"<div style='margin-bottom:4px; font-weight:bold; font-size:1.1em; color:var(--charger-color)'>{c['clean_info']['name']}</div>"
                f"<div style='font-size:0.85em; color:#999; margin-top:5px;'>Kein Fastfood in direkter Nähe ({SEARCH_RADIUS_METERS}m)</div>"
            )
            # Unique ID nur vom Charger
            entry["unique_id"] = f"{c.get('id')}_nofood"

        tile_matches.append(entry)

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

# --- ZEITMESSUNG & ABSCHLUSS ---
end_total_time = time.time()
total_duration = end_total_time - start_total_time
m = int(total_duration // 60)
s = int(total_duration % 60)

print(f"\n✅ FERTIG in {m}m {s}s!")

# WICHTIG: Wir nutzen 'all_matches' aus deinem Skript
data_to_save = all_matches 

# 1. Alte Datei prüfen (für den Vergleich: Alt vs Neu)
old_count = 0
if os.path.exists(OUTPUT_FILENAME):
    try:
        with open(OUTPUT_FILENAME, 'r', encoding='utf-8') as f:
            old_data = json.load(f)
            old_count = len(old_data)
    except:
        pass # Datei existierte wohl noch nicht oder war leer

new_count = len(data_to_save)
diff = new_count - old_count

print(f"------------------------------------------------")
print(f"📊 Statistik: Alt: {old_count} -> Neu: {new_count} (Diff: {diff:+d})")
print(f"💾 Speichere in {OUTPUT_FILENAME}...")

# 2. Speichern der JSON-Datei (Die Hauptdatenbank)
with open(OUTPUT_FILENAME, 'w', encoding='utf-8') as f:
    json.dump(data_to_save, f, ensure_ascii=False, indent=2)

# 3. meta.js erstellen (für das Datum auf der Webseite)
now = datetime.datetime.now()
monate = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]
# Format: Monat Jahr (z.B. "Mai 2024")
date_str = f"{monate[now.month-1]} {now.year}"

with open("meta.js", "w", encoding="utf-8") as f:
    f.write(f'const standDaten = "{date_str}";')

print(f"📅 'meta.js' aktualisiert: {date_str}")

# 4. GitHub Actions Integration
# Das hier wird nur ausgeführt, wenn das Skript auf dem GitHub-Server läuft
if "GITHUB_STEP_SUMMARY" in os.environ:
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
        f.write("# 🗺️ Karten-Update Report\n")
        f.write(f"Das monatliche Update war erfolgreich.\n\n")
        f.write("| Typ | Anzahl |\n|---|---|\n")
        f.write(f"| 📉 Vorher | {old_count} |\n")
        f.write(f"| 📈 Nachher | {new_count} |\n")
        f.write(f"| 📊 Differenz | **{diff:+d}** |\n")

if "GITHUB_OUTPUT" in os.environ:
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
        f.write(f"stats_msg={new_count} Einträge ({diff:+d})\n")