import requests
import json
import math

# --- KONFIGURATION START ---
CITY_NAME = "Plauen"
SEARCH_RADIUS_METERS = 400
OUTPUT_FILENAME = "data.json"

# DER TÜRSTEHER (Mappings)
ALLOWED_CHARGERS = {
    "tesla":  {"name": "Tesla Supercharger", "class": "bg-tesla"},
    "ionity": {"name": "IONITY", "class": "bg-ionity"},
    "enbw":   {"name": "EnBW", "class": "bg-enbw"},
}

ALLOWED_FOOD = {
    "mcdonald":    {"name": "McDonald's", "class": "bg-mcd"},
    "burger king": {"name": "Burger King", "class": "bg-bk"},
}
# --- KONFIGURATION ENDE ---

def get_coords(element):
    """
    Holt Lat/Lon sicher aus einem Element.
    - Nodes haben 'lat'/'lon' direkt.
    - Ways/Relations haben sie dank 'out center' im Block 'center'.
    """
    if 'center' in element:
        return element['center']['lat'], element['center']['lon']
    elif 'lat' in element and 'lon' in element:
        return element['lat'], element['lon']
    return None, None

def calculate_distance(lat1, lon1, lat2, lon2):
    """Berechnet Distanz in Metern (Haversine-Formel)"""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi, dlambda = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def identify_brand(element_tags, whitelist):
    """Prüft, ob Operator/Marke/Name in der Whitelist ist."""
    check_string = (
        str(element_tags.get('operator', '')) + " " + 
        str(element_tags.get('brand', '')) + " " + 
        str(element_tags.get('name', ''))
    ).lower()

    for key, info in whitelist.items():
        if key in check_string:
            return info
    return None

print(f"--- Starte STRICT Suche in {CITY_NAME} ---")

# 1. API Abfrage definieren
overpass_url =  "https://overpass.private.coffee/api/interpreter"

overpass_query = f"""
[out:json][timeout:180];
area["name"="{CITY_NAME}"]["type"="boundary"]->.searchArea;
(
  nwr["amenity"="charging_station"]["operator"~"Tesla|IONITY|EnBW", i](area.searchArea);
  nwr["amenity"="fast_food"]["brand"~"McDonald|Burger King", i](area.searchArea);
  nwr["amenity"="fast_food"]["name"~"McDonald|Burger King", i](area.searchArea);
  nwr["amenity"="restaurant"]["brand"~"McDonald|Burger King", i](area.searchArea);
  nwr["amenity"="restaurant"]["name"~"McDonald|Burger King", i](area.searchArea);
);
out center;
"""

# 2. Daten laden
print("Lade Rohdaten von OpenStreetMap...")
headers = {
    'User-Agent': 'LadestoppFinder-StudentProject/1.0',
    'Referer': 'https://www.google.com'
}

try:
    response = requests.get(overpass_url, params={'data': overpass_query}, headers=headers)
    if response.status_code != 200:
        print(f"❌ Server Fehler Code: {response.status_code}")
        exit()
    data = response.json()
except Exception as e:
    print(f"❌ Fehler: {e}")
    exit()

# 3. Aussortieren
chargers = []
restaurants = [] # Achtung: hieß früher 'foods', jetzt 'restaurants' passend zum Loop unten

for element in data['elements']:
    tags = element.get('tags', {})

    if tags.get('amenity') == 'charging_station':
        clean_info = identify_brand(tags, ALLOWED_CHARGERS)
        if clean_info:
            element['clean_info'] = clean_info
            chargers.append(element)

    elif tags.get('amenity') in ['fast_food', 'restaurant']:
        clean_info = identify_brand(tags, ALLOWED_FOOD)
        if clean_info:
            element['clean_info'] = clean_info
            restaurants.append(element) # Hier füllen wir die restaurants-Liste

print(f"Gefiltert: {len(chargers)} relevante Ladesäulen und {len(restaurants)} Burger-Läden.")

# 4. Matching (Geometrie) & Fix für fehlende Koordinaten
matches = []

print(f"Prüfe Entfernungen zwischen {len(chargers)} Ladern und {len(restaurants)} Essensorten...")

for charger in chargers:
    # Fix: get_coords nutzen
    c_lat, c_lon = get_coords(charger)
    if c_lat is None: continue 

    for food in restaurants:
        # Fix: get_coords nutzen
        f_lat, f_lon = get_coords(food)
        if f_lat is None: continue 

        # Fix: Falscher Funktionsaufruf korrigiert (calculate_distance statt haversine)
        dist = calculate_distance(c_lat, c_lon, f_lat, f_lon)

        if dist <= SEARCH_RADIUS_METERS:
            matches.append({
                "lat": c_lat,
                "lon": c_lon,
                "title": f"Charge & Eat: {food['clean_info']['name']}",
                "description": (
                    f"<b>Lader:</b> {charger['clean_info']['name']} "
                    f"({charger['clean_info'].get('class', '')})<br>"
                    f"<b>Essen:</b> {food['clean_info']['name']}<br>"
                    f"<b>Entfernung:</b> {int(dist)}m"
                ),
                "raw_charger": charger['tags'].get('name', 'Unbekannt'),
                "raw_food": food['tags'].get('brand', food['tags'].get('name', 'Unbekannt'))
            })

# 5. Speichern
with open(OUTPUT_FILENAME, 'w', encoding='utf-8') as f:
    json.dump(matches, f, ensure_ascii=False, indent=2)

print(f"--- FERTIG! ---")
if len(matches) == 0:
    print("Keine Treffer gefunden.")
else:
    print(f"{len(matches)} Treffer in '{OUTPUT_FILENAME}' gespeichert.")
