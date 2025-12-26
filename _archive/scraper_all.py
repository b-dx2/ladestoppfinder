import requests
import json
import math
import time

# --- KONFIGURATION ---

# Bounding Box für Sachsen + Nordbayern (Grob: Nürnberg bis Görlitz)
# Format: Süd, West, Nord, Ost
SACHSEN_BOX = "49.956,11.579,49.979,11.616"

SEARCH_RADIUS_METERS = 300 # Etwas engerer Radius, da Lounges oft direkt am Lader sind
OUTPUT_FILENAME = "data.json" # Zieldatei

# Regex für Overpass: Wir filtern hier schon grob vor, um Datenmenge klein zu halten.
# Wir suchen nach Fastfood UND Lounge-Begriffen UND Shop-Marken
FOOD_REGEX = "McDonald|Burger King|Lounge|World|Hub|Tegut|Rewe|Porsche|Audi|Seed"

# Ladestationen Konfiguration
ALLOWED_CHARGERS = {
    "tesla":  {"name": "Tesla Supercharger", "class": "bg-tesla"},
    "ionity": {"name": "IONITY", "class": "bg-ionity"},
    "enbw":   {"name": "EnBW", "class": "bg-enbw"},
}

# Essen / Lounge Konfiguration
ALLOWED_FOOD = {
    "mcdonald":    {"name": "McDonald's", "class": "bg-mcd"},
    "burger king": {"name": "Burger King", "class": "bg-bk"},
    # Hier fassen wir alles Premium/Automatisierte zusammen:
    "lounge":      {"name": "Lounge / Shop", "class": "bg-purple-600"}     
}

# Liste der Begriffe, die als "Lounge" einsortiert werden sollen
LOUNGE_KEYWORDS = [
    "bk world",          # Der automatisierte Würfel
    "tegut",             # Automaten-Supermarkt (teo)
    "rewe ready",        # Automaten-Box
    "rewe to go",        # Tankstellen-Shop
    "audi charging hub", # Premium Lounge
    "porsche",           # Premium Lounge
    "seed & greet",      # Hilden
    "seed&greet",
    "lounge",            # Generisch
    "charging hub"       # Generisch
]

def get_coords(element):
    if 'center' in element:
        return element['center']['lat'], element['center']['lon']
    elif 'lat' in element and 'lon' in element:
        return element['lat'], element['lon']
    return None, None

def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    a = math.sin(delta_phi / 2)**2 + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

print("1. Sende optimierte Anfrage an Overpass (Sachsen + Nordbayern)...")

# Overpass Query
# Wir holen:
# 1. Ladestationen
# 2. Essen/Lounges (via amenity)
# 3. Kioske/Shops (via shop - wichtig für BK World/Tegut)
overpass_query = f"""
[out:json][timeout:180];
(
  // 1. Ladestationen
  nwr["amenity"="charging_station"]({SACHSEN_BOX});

  // 2. Restaurants / Lounges (amenity)
  nwr["amenity"~"fast_food|restaurant|cafe|lounge|vending_machine"]["name"~"{FOOD_REGEX}",i]({SACHSEN_BOX});

  // 3. Shops / Kioske (shop) - für BK World, Tegut Teo, Rewe Boxen
  nwr["shop"~"kiosk|convenience"]["name"~"{FOOD_REGEX}",i]({SACHSEN_BOX});
);
out center;
"""

try:
    start_time = time.time()
    response = requests.get("https://overpass.private.coffee/api/interpreter", 
                            params={'data': overpass_query}, 
                            timeout=200)
    
    if response.status_code != 200:
        print(f"❌ Server-Fehler: {response.status_code}")
        print("Meldung:", response.text)
        exit()
    
    data = response.json()
    elements = data.get("elements", [])
    duration = time.time() - start_time
    print(f"✅ Download erfolgreich: {len(elements)} Objekte in {round(duration, 2)}s.")

except Exception as e:
    print(f"❌ Kritischer Fehler: {e}")
    exit()

if len(elements) == 0:
    print("❌ Keine Ergebnisse. Overpass Query prüfen.")
    exit()

# --- LOKALE VERARBEITUNG ---
print("2. Sortiere und klassifiziere Daten...")
chargers = []
restaurants = []

for el in elements:
    tags = el.get("tags", {})
    name = tags.get("name", "Unbekannt")
    brand = tags.get("brand", "")
    operator = tags.get("operator", "")
    
    amenity = tags.get("amenity", "")
    shop = tags.get("shop", "")

    # Kombiniere alle Infos für die Textsuche (alles kleingeschrieben)
    full_search_text = (name + " " + brand + " " + operator).lower()

    # --- A) SHOP / ESSEN / LOUNGE CHECK ---
    # Ist es ein relevantes POI (Essen oder Shop)?
    is_poi_candidate = False
    if amenity in ["fast_food", "restaurant", "cafe", "lounge", "vending_machine"]: is_poi_candidate = True
    if shop in ["kiosk", "convenience"]: is_poi_candidate = True

    if is_poi_candidate:
        found_config = None
        found_id = None
        
        # 1. PRÜFUNG: Ist es eine Premium Lounge / Shop? (Priorität!)
        for kw in LOUNGE_KEYWORDS:
            if kw in full_search_text:
                found_config = ALLOWED_FOOD["lounge"]
                found_id = "lounge"
                break
        
        # 2. PRÜFUNG: Wenn keine Lounge, ist es klassisches Fast Food?
        if not found_config:
            for key, config in ALLOWED_FOOD.items():
                if key == "lounge": continue # Lounge schon erledigt
                if key in full_search_text:
                    found_config = config
                    found_id = key
                    break
        
        if found_config:
            el['clean_info'] = found_config
            el['id_key'] = found_id 
            restaurants.append(el)

    # --- B) LADESTATION CHECK ---
    elif amenity == "charging_station":
        found_config = None
        found_id = None 

        for key, config in ALLOWED_CHARGERS.items():
            if key in full_search_text:
                found_config = config
                found_id = key
                break
        
        if found_config:
            el['clean_info'] = found_config
            el['id_key'] = found_id 
            if "Unbekannt" in name: 
                el['clean_info']['name'] = found_config['name']
            else:
                el['clean_info']['name'] = name # Originalname behalten
            chargers.append(el)

print(f"   -> {len(chargers)} Ladestationen & {len(restaurants)} POIs (Essen/Lounge) gefunden.")

print("3. Matching (Entfernung berechnen)...")
matches = []

for charger in chargers:
    c_lat, c_lon = get_coords(charger)
    if not c_lat: continue

    for food in restaurants:
        f_lat, f_lon = get_coords(food)
        if not f_lat: continue

        # Grobfilter (ca. 2km Box um Rechenzeit zu sparen)
        if abs(c_lat - f_lat) > 0.02 or abs(c_lon - f_lon) > 0.02:
            continue

        dist = calculate_distance(c_lat, c_lon, f_lat, f_lon)
        if dist <= SEARCH_RADIUS_METERS:
            
            # Titel generieren
            poi_type = food['clean_info']['name'] # z.B. "Lounge / Shop"
            poi_real_name = food.get('tags', {}).get('name', poi_type)

            matches.append({
                "lat": c_lat,
                "lon": c_lon,
                "charger_id": charger['id_key'], 
                "food_id":    food['id_key'],    
                "title": f"{charger['clean_info']['name']} + {poi_real_name}",
                "badge_class": charger['clean_info']['class'],
                "description": (
                    f"<div class='font-bold'>{charger['clean_info']['name']}</div>"
                    f"<div>In der Nähe: <span class='font-semibold'>{poi_real_name}</span></div>"
                    f"<div class='text-xs text-gray-500 mt-1'>{int(dist)} Meter entfernt</div>"
                    f"<div class='mt-2 text-xs bg-gray-100 p-1 rounded'>Typ: {poi_type}</div>"
                )
            })

print(f"✅ FERTIG! {len(matches)} Treffer gespeichert in {OUTPUT_FILENAME}.")

with open(OUTPUT_FILENAME, 'w', encoding='utf-8') as f:
    json.dump(matches, f, ensure_ascii=False, indent=2)
