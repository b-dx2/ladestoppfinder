import requests
import json
import math
import time

# --- KONFIGURATION ---
# --- KONFIGURATION ---
# Wir "missbrauchen" die Variable SACHSEN_BOX einfach für Bayern ;)
# Koordinaten für Bayern (grob umfasst)
# SACHSEN_BOX = "47.20,8.90,50.60,13.90" 
# Koordinaten für Bayreuth und ca. 50 km Umland
# SACHSEN_BOX = "49.45,10.85,50.45,12.30" 
bbox = "49.956,11.579,49.979,11.616"


SEARCH_RADIUS_METERS = 400 

# WICHTIG: Nicht data.json überschreiben, sondern neu speichern
OUTPUT_FILENAME = "bayern.json" 


# Regex-String für Overpass (Schnelle Server-Suche)
FOOD_REGEX = "McDonald|Burger King|Lounge|World"

ALLOWED_CHARGERS = {
    "tesla":  {"name": "Tesla Supercharger", "class": "bg-tesla"},
    "ionity": {"name": "IONITY", "class": "bg-ionity"},
    "enbw":   {"name": "EnBW", "class": "bg-enbw"},
}

ALLOWED_FOOD = {
    "mcdonald":    {"name": "McDonald's", "class": "bg-mcd"},
    "burger king": {"name": "Burger King", "class": "bg-bk"},
    "lounge":      {"name": "Lounge / BK World", "class": "bg-tesla"}     
}

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

print("1. Sende optimierte Anfrage an Overpass...")

# TRICK: Wir filtern Essen DIREKT auf dem Server per Namen (RegEx).
# Das spart massiv Rechenzeit beim Server, weil er nicht alle 20.000 Restaurants laden muss.
overpass_query = f"""
    [out:json];
    (
      node["amenity"~"fast_food|restaurant|cafe|fuel|lounge|vending_machine"]({bbox});
      way["amenity"~"fast_food|restaurant|cafe|fuel|lounge|vending_machine"]({bbox});
      relation["amenity"~"fast_food|restaurant|cafe|fuel|lounge|vending_machine"]({bbox});
      
      node["shop"~"kiosk|convenience"]({bbox});
      way["shop"~"kiosk|convenience"]({bbox});
      relation["shop"~"kiosk|convenience"]({bbox});
    );
    out center;
    """

print(overpass_query)

try:
    start_time = time.time()
    # Timeout im Request höher als in der Query setzen
    response = requests.get("https://overpass.private.coffee/api/interpreter", 
                            params={'data': overpass_query}, 
                            timeout=100)
    
    if response.status_code != 200:
        print(f"❌ Server-Fehler: {response.status_code}")
        print("Meldung:", response.text)
        exit()

    data = response.json()
    
    # Check ob Overpass eine Fehlermeldung im JSON versteckt hat
    if "remark" in data:
        print(f"⚠️ Warnung vom Server: {data['remark']}")

    elements = data.get("elements", [])
    duration = time.time() - start_time
    print(f"✅ Download erfolgreich: {len(elements)} Objekte in {round(duration, 2)}s.")

except Exception as e:
    print(f"❌ Kritischer Fehler: {e}")
    exit()

if len(elements) == 0:
    print("❌ Immer noch 0 Ergebnisse. Wahrscheinlich ist die Bounding Box falsch oder der Server blockiert.")
    exit()

# --- LOKALE VERARBEITUNG ---
# --- AB HIER IM PYTHON SKRIPT ERSETZEN ---

print("2. Sortiere Daten...")
chargers = []
restaurants = []

for el in elements:
    tags = el.get("tags", {})
    name = tags.get("name", "Unbekannt")
    
    # FASTFOOD FILTER & ID ZUWEISUNG
    if tags.get("amenity") in ["fast_food", "restaurant"]:
        clean_name = name.lower()
        found_config = None
        found_id = None # WICHTIG: Die ID speichern (z.B. 'mcdonald')
        
        for key, config in ALLOWED_FOOD.items():
            if key in clean_name: 
                found_config = config
                found_id = key
                break
        
        if found_config:
            el['clean_info'] = found_config
            el['id_key'] = found_id # ID im Objekt speichern
            restaurants.append(el)

    # CHARGERS FILTER & ID ZUWEISUNG
    elif tags.get("amenity") == "charging_station":
        details = f"{name} {tags.get('operator', '')} {tags.get('brand', '')}".lower()
        
        found_config = None
        found_id = None # WICHTIG: Die ID speichern (z.B. 'tesla')

        for key, config in ALLOWED_CHARGERS.items():
            if key in details:
                found_config = config
                found_id = key
                break
        
        if found_config:
            el['clean_info'] = found_config
            el['id_key'] = found_id # ID im Objekt speichern
            if "Unbekannt" in name: 
                el['clean_info']['name'] = found_config['name']
            else:
                el['clean_info']['name'] = name
            chargers.append(el)

print(f"   -> {len(chargers)} Supercharger & {len(restaurants)} Fastfood-Filialen gefunden.")

print("3. Matching (Entfernung berechnen)...")
matches = []

for charger in chargers:
    c_lat, c_lon = get_coords(charger)
    if not c_lat: continue

    for food in restaurants:
        f_lat, f_lon = get_coords(food)
        if not f_lat: continue

        if abs(c_lat - f_lat) > 0.02 or abs(c_lon - f_lon) > 0.02:
            continue

        dist = calculate_distance(c_lat, c_lon, f_lat, f_lon)
        if dist <= SEARCH_RADIUS_METERS:
            # HIER WAR DER FEHLER: charger_id und food_id haben gefehlt!
            matches.append({
                "lat": c_lat,
                "lon": c_lon,
                "charger_id": charger['id_key'], # Das braucht JS zum Filtern
                "food_id":    food['id_key'],    # Das braucht JS zum Filtern
                "title": f"{charger['clean_info']['name']} + {food['clean_info']['name']}",
                "badge_class": charger['clean_info']['class'],
                "description": (
                    f"<b>{charger['clean_info']['name']}</b><br>"
                    f"Entfernung zu {food['clean_info']['name']}: {int(dist)} Meter"
                )
            })

print(f"✅ FERTIG! {len(matches)} Treffer gespeichert in {OUTPUT_FILENAME}.")

with open(OUTPUT_FILENAME, 'w', encoding='utf-8') as f:
    json.dump(matches, f, ensure_ascii=False, indent=2)
