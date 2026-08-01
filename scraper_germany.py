#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ladestoppfinder - Deutschland-Scraper (Overpass API)

Optimierte Fassung:
  - kein Error 429 mehr (Slot-Check, Backoff, Retry-After, Endpoint-Rotation)
  - Cache pro Kachel -> Wiederaufnahme nach Abbruch
  - Fail-Safe: data.json wird nur bei ausreichend erfolgreichem Lauf ersetzt
Die Auswerte- und Matching-Logik ist funktional unveraendert.
"""

import os
import json
import math
import time
import random
import hashlib
import datetime

import requests

# ============================================================
# KONFIGURATION
# ============================================================

LAT_START, LAT_END = 47.0, 55.2
LON_START, LON_END = 5.5, 15.5
STEP_SIZE = 0.4

SEARCH_RADIUS_METERS = 300
OUTPUT_FILENAME = "data.json"
CACHE_DIR = ".cache_overpass"

# Cache-Lebensdauer in Stunden. Bei monatlichem Lauf reicht ein Tag,
# damit ein abgebrochener Lauf am selben Tag fortgesetzt werden kann.
CACHE_TTL_HOURS = 24

# Mindestanteil erfolgreicher Kacheln, damit data.json ueberschrieben wird.
MIN_SUCCESS_RATIO = 0.80

# Overpass-Endpunkte in Reihenfolge der Praeferenz.
#
# Auswahl auf Basis des Diagnoselaufs vom 01.08.2026:
#   overpass-api.de        -> 200 OK, vollstaendige Daten. Primaerquelle.
#   overpass.private.coffee-> liefert nur ohne UA sofort 429, mit UA ReadTimeout.
#                             Nur als Notreserve, nicht als Primaerquelle.
#   overpass.kumi.systems  -> gleiches Verhalten. Notreserve.
#   overpass.osm.jp        -> SSL-Fehler unter LibreSSL. Entfernt.
#   overpass.osm.ch        -> antwortet 200, aber mit 0 Elementen. GEFAEHRLICH:
#                             wuerde stillschweigend eine leere Karte
#                             produzieren. Bewusst NICHT aufgenommen.
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Pflicht fuer oeffentliche Overpass-Instanzen: identifizierbarer Client.
# Ohne UA antwortet overpass-api.de mit 406 und private.coffee mit 429.
USER_AGENT = (
    "ladestoppfinder/2.0 (monatlicher Datenabgleich; "
    "+https://github.com/b-dx2/ladestoppfinder; Kontakt via GitHub Issues)"
)

QUERY_TIMEOUT = 90         # Overpass-seitiges [timeout:...]
HTTP_TIMEOUT = (15, 120)   # (connect, read)
MAX_RETRIES = 6            # Versuche pro Kachel ueber alle Endpunkte hinweg
MAX_PAUSE = 90.0

# overpass-api.de erlaubt laut /api/status genau 2 Slots gleichzeitig.
# Wir arbeiten strikt sequenziell und halten einen Mindestabstand ein.
MIN_REQUEST_INTERVAL = 4.0   # Sekunden zwischen zwei Requests, hart erzwungen
BASE_PAUSE = 2.0             # zusaetzliche Grundpause zwischen zwei Kacheln

FOOD_REGEX = (
    "McDonald|Burger King|Lounge|World|Hub|Tegut|Rewe|Porsche|Audi|"
    "Seed|KFC|Kentucky|Subway|Nordsee"
)

# ============================================================
# DEFINITIONEN (unveraendert)
# ============================================================

ALLOWED_CHARGERS = {
    "tesla":   {"name": "Tesla Supercharger", "class": "bg-tesla"},
    "ionity":  {"name": "IONITY", "class": "bg-ionity"},
    "enbw":    {"name": "EnBW", "class": "bg-enbw"},
    "fastned": {"name": "Fastned", "class": "bg-fastned"},
    "allego":  {"name": "Allego", "class": "bg-allego"},
    "aral":    {"name": "Aral pulse", "class": "bg-aral"},
    "pulse":   {"name": "Aral pulse", "class": "bg-aral"},
}

ALLOWED_FOOD = {
    "mcdonald":    {"name": "McDonald's", "class": "bg-mcd"},
    "burger king": {"name": "Burger King", "class": "bg-bk"},
    "kfc":         {"name": "KFC", "class": "bg-kfc"},
    "kentucky":    {"name": "KFC", "class": "bg-kfc"},
    "subway":      {"name": "Subway", "class": "bg-subway"},
    "nordsee":     {"name": "Nordsee", "class": "bg-nordsee"},
    "lounge":      {"name": "Lounge / Shop", "class": "bg-purple-600"},
}

LOUNGE_KEYWORDS = [
    "bk world",
    "audi charging hub", "audi charging",
    "porsche",
    "seed & greet", "seed&greet",
    "charging hub",
    "rewe ready", "rewe to go", "tegut",
]

# ============================================================
# HTTP-LAYER: der eigentliche 429-Fix
# ============================================================

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
})

_endpoint_index = 0
_last_request_ts = 0.0
_stats = {"requests": 0, "retries": 0, "rate_limited": 0, "cache_hits": 0}


def current_endpoint():
    return OVERPASS_ENDPOINTS[_endpoint_index % len(OVERPASS_ENDPOINTS)]


def rotate_endpoint():
    """Bei Problemen auf die naechste Instanz wechseln."""
    global _endpoint_index
    _endpoint_index += 1
    return current_endpoint()


def reset_endpoint():
    """Zurueck zur bevorzugten Instanz (overpass-api.de)."""
    global _endpoint_index
    _endpoint_index = 0


def throttle():
    """
    Erzwingt einen Mindestabstand zwischen zwei Requests.
    Das ist der eigentliche Schutz vor 429: nicht schneller senden,
    als der Server erlaubt, statt Fehler nachtraeglich abzufangen.
    """
    global _last_request_ts
    wait = MIN_REQUEST_INTERVAL - (time.time() - _last_request_ts)
    if wait > 0:
        time.sleep(wait)
    _last_request_ts = time.time()


def status_url(endpoint):
    return endpoint.rsplit("/api/", 1)[0] + "/api/status"


def wait_for_slot(endpoint, max_wait=90):
    """
    Fragt /api/status ab und wartet, bis ein Slot frei ist.
    Nur overpass-api.de liefert eine auswertbare Statusseite; bei allen
    anderen Instanzen wird die Abfrage stillschweigend uebersprungen.
    """
    if "overpass-api.de" not in endpoint:
        return
    try:
        r = SESSION.get(status_url(endpoint), timeout=(10, 20))
        if r.status_code != 200:
            return
        for line in r.text.splitlines():
            line = line.strip()
            if "slots available now" in line:
                return
            if line.startswith("Slot available after:") and ", in " in line:
                try:
                    secs = int(line.split(", in ")[1].split(" ")[0])
                except (ValueError, IndexError):
                    return
                secs = max(0, min(secs + 2, max_wait))
                if secs > 0:
                    print(f" [warte {secs}s auf Slot]", end="", flush=True)
                    time.sleep(secs)
                return
    except requests.RequestException:
        return


def overpass_query(query):
    """
    Fuehrt eine Overpass-Query robust aus.
    Rueckgabe: Liste der Elemente oder None bei endgueltigem Fehler.

    Wichtig: Eine leere Elementliste bei erfolgreicher Antwort gilt als
    gueltiges Ergebnis (es gibt Kacheln ohne Ladesaeulen) - aber nur von
    Instanzen, die wir als vertrauenswuerdig eingestuft haben.
    """
    delay = 8.0

    for attempt in range(1, MAX_RETRIES + 1):
        endpoint = current_endpoint()
        wait_for_slot(endpoint)
        throttle()

        try:
            _stats["requests"] += 1
            # POST mit Query im Body. Bei overpass-api.de war POST im Test
            # deutlich schneller als GET (0.6s statt 9.2s), weil die Antwort
            # nicht aus dem URL-Cache neu berechnet wird.
            r = SESSION.post(endpoint, data={"data": query}, timeout=HTTP_TIMEOUT)

            if r.status_code == 200:
                try:
                    return r.json().get("elements", [])
                except ValueError:
                    print(" [ungueltiges JSON]", end="", flush=True)

            elif r.status_code == 429:
                _stats["rate_limited"] += 1
                retry_after = r.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    # private.coffee/kumi schicken hier konsequent 60.
                    sleep_for = min(float(retry_after) + 2, MAX_PAUSE)
                else:
                    sleep_for = min(delay, MAX_PAUSE)
                sleep_for += random.uniform(0, 3)
                print(f" [429, wechsle Server, pause {sleep_for:.0f}s]",
                      end="", flush=True)
                rotate_endpoint()      # sofort wechseln, nicht erst beim 2. Mal
                time.sleep(sleep_for)
                delay *= 2
                _stats["retries"] += 1
                continue

            elif r.status_code == 406:
                # Tritt nur ohne User-Agent auf. Sollte nie passieren.
                print(" [406 - User-Agent fehlt!]", end="", flush=True)
                return None

            elif r.status_code in (502, 503, 504):
                print(f" [{r.status_code}, Serverwechsel]", end="", flush=True)
                rotate_endpoint()
                time.sleep(min(delay, MAX_PAUSE))
                delay *= 2
                _stats["retries"] += 1
                continue

            else:
                print(f" [HTTP {r.status_code}]", end="", flush=True)

        except requests.Timeout:
            # private.coffee und kumi.systems laufen mit UA regelmaessig
            # in ReadTimeouts. Sofort weiterziehen statt lange warten.
            print(" [Timeout]", end="", flush=True)
            rotate_endpoint()
        except requests.RequestException as exc:
            print(f" [Netzfehler: {type(exc).__name__}]", end="", flush=True)
            rotate_endpoint()

        _stats["retries"] += 1
        time.sleep(min(delay, MAX_PAUSE) + random.uniform(0, 2))
        delay *= 2

    return None


# ============================================================
# CACHE
# ============================================================

def cache_path(bbox_str):
    key = hashlib.md5(f"{bbox_str}|{FOOD_REGEX}".encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"{key}.json")


def cache_read(bbox_str):
    path = cache_path(bbox_str)
    if not os.path.exists(path):
        return None
    age_h = (time.time() - os.path.getmtime(path)) / 3600
    if age_h > CACHE_TTL_HOURS:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            _stats["cache_hits"] += 1
            return json.load(f)
    except (OSError, ValueError):
        return None


def cache_write(bbox_str, elements):
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        with open(cache_path(bbox_str), "w", encoding="utf-8") as f:
            json.dump(elements, f, ensure_ascii=False)
    except OSError:
        pass


# ============================================================
# HILFSFUNKTIONEN (unveraendert)
# ============================================================

def get_coords(element):
    if "center" in element:
        return element["center"]["lat"], element["center"]["lon"]
    if "lat" in element and "lon" in element:
        return element["lat"], element["lon"]
    return None, None


def calculate_distance(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2 - lat1) / 2) ** 2
         + math.cos(phi1) * math.cos(phi2)
         * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def build_query(bbox_str):
    return f"""[out:json][timeout:{QUERY_TIMEOUT}];
(
  nwr["amenity"="charging_station"]({bbox_str});
  nwr["amenity"~"fast_food|restaurant|cafe|lounge|vending_machine"]["name"~"{FOOD_REGEX}",i]({bbox_str});
  nwr["shop"~"kiosk|convenience"]["name"~"{FOOD_REGEX}",i]({bbox_str});
);
out center qt;"""


# ============================================================
# AUSWERTUNG (Logik identisch zur Vorversion)
# ============================================================

def classify(elements):
    chargers, restaurants = [], []

    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name", "Unbekannt")

        strong_search = " ".join([
            tags.get("brand", ""), tags.get("operator", ""), tags.get("network", "")
        ]).lower()
        weak_search = (name or "").lower()
        full_search = (weak_search + " " + strong_search).strip()

        is_poi = (
            tags.get("amenity") in ["fast_food", "restaurant", "cafe", "lounge", "vending_machine"]
            or tags.get("shop") in ["kiosk", "convenience"]
        )

        if is_poi:
            config, fid = None, None

            for kw in LOUNGE_KEYWORDS:
                if kw in full_search:
                    config, fid = ALLOWED_FOOD["lounge"], "lounge"
                    break

            if not config:
                for k, c in ALLOWED_FOOD.items():
                    if k != "lounge" and k in full_search:
                        config, fid = c, k
                        if k == "kentucky":
                            fid = "kfc"
                        if k == "pulse":
                            fid = "aral"
                        break

            if config:
                el["clean_info"] = config
                el["id_key"] = fid
                restaurants.append(el)

        elif tags.get("amenity") == "charging_station":
            config, fid, match_level = None, None, None

            for k, c in ALLOWED_CHARGERS.items():
                if k in strong_search:
                    config, fid, match_level = c, k, "strong"
                    if k == "pulse":
                        fid = "aral"
                    break

            if not config:
                if "supercharger" in weak_search:
                    config, fid, match_level = ALLOWED_CHARGERS["tesla"], "tesla", "likely"
                else:
                    for k, c in ALLOWED_CHARGERS.items():
                        if k in weak_search:
                            config, fid, match_level = c, k, "likely"
                            if k == "pulse":
                                fid = "aral"
                            break

            if not config:
                continue

            el["clean_info"] = config.copy()
            el["id_key"] = fid
            el["match_level"] = match_level

            display_name = name
            if "Unbekannt" in display_name:
                if tags.get("brand"):
                    display_name = tags.get("brand")
                elif tags.get("operator"):
                    display_name = tags.get("operator")
                else:
                    display_name = config["name"]
                city = tags.get("addr:city")
                if city:
                    display_name = f"{display_name} ({city})"
            el["clean_info"]["name"] = display_name

            # Duplikat-Check (30 m, gleicher Anbieter)
            cur_lat, cur_lon = get_coords(el)
            is_duplicate = False
            if cur_lat is not None and cur_lon is not None:
                for existing in chargers:
                    if existing["id_key"] != el["id_key"]:
                        continue
                    ex_lat, ex_lon = get_coords(existing)
                    if ex_lat is None:
                        continue
                    if calculate_distance(cur_lat, cur_lon, ex_lat, ex_lon) < 30:
                        is_duplicate = True
                        break

            if not is_duplicate:
                chargers.append(el)

    return chargers, restaurants


def match_pairs(chargers, restaurants):
    tile_matches = []

    for c in chargers:
        c_lat, c_lon = get_coords(c)
        if c_lat is None:
            continue

        best_food, closest_dist = None, float("inf")
        for r in restaurants:
            r_lat, r_lon = get_coords(r)
            if r_lat is None:
                continue
            if abs(c_lat - r_lat) > 0.02 or abs(c_lon - r_lon) > 0.02:
                continue
            dist = calculate_distance(c_lat, c_lon, r_lat, r_lon)
            if dist <= SEARCH_RADIUS_METERS and dist < closest_dist:
                closest_dist, best_food = dist, r

        if not best_food:
            continue

        food_clean_id = best_food["id_key"].replace(" ", "-")
        food_real_name = best_food.get("tags", {}).get(
            "name", best_food["clean_info"]["name"]
        )

        entry = {
            "lat": c_lat,
            "lon": c_lon,
            "charger_id": c["id_key"],
            "food_id": food_clean_id,
            "title": c["clean_info"]["name"],
            "badge_class": c["clean_info"]["class"],
            "note": f"{int(closest_dist)}m zu {food_real_name}",
            "popup_name": c["clean_info"]["name"],
            "description": (
                f"<div style='margin-bottom:4px; font-weight:bold; font-size:1.1em; "
                f"color:var(--charger-color)'>{c['clean_info']['name']}</div>"
                f"<div style='display:flex; align-items:center; gap:5px; margin-top:5px;'>"
                f"  <span>&#127869;</span>"
                f"  <span style='font-weight:600;'>{food_real_name}</span>"
                f"</div>"
                f"<div style='font-size:0.85em; color:#666; margin-top:2px;'>"
                f"Entfernung: {int(closest_dist)}m</div>"
            ),
            "unique_id": f"{c.get('id')}_{best_food.get('id')}",
        }
        tile_matches.append(entry)

    return tile_matches


def process_tile(bbox_str):
    """Rueckgabe: (matches, ok) - ok=False bedeutet Kachel fehlgeschlagen."""
    elements = cache_read(bbox_str)
    from_cache = elements is not None

    if not from_cache:
        elements = overpass_query(build_query(bbox_str))
        if elements is None:
            return [], False
        cache_write(bbox_str, elements)

    chargers, restaurants = classify(elements)
    return match_pairs(chargers, restaurants), True


# ============================================================
# HAUPTPROGRAMM
# ============================================================

def main():
    start_total = time.time()
    print(f"Starte Deutschland-Scan ({LAT_START}-{LAT_END} / {LON_START}-{LON_END})")
    print(f"Raster: {STEP_SIZE} Grad | Endpoint: {current_endpoint()}")

    # Kachelliste vorab bauen (klarer als verschachtelte while-Schleifen)
    tiles = []
    lat = LAT_START
    while lat < LAT_END:
        lon = LON_START
        while lon < LON_END:
            tiles.append((
                lat, lon,
                min(lat + STEP_SIZE, 90),
                min(lon + STEP_SIZE, 180),
            ))
            lon += STEP_SIZE
        lat += STEP_SIZE

    total_tiles = len(tiles)
    all_matches, processed_ids = [], set()
    failed_tiles = []
    pause = BASE_PAUSE

    for idx, (lat_min, lon_min, lat_max, lon_max) in enumerate(tiles, start=1):
        bbox = f"{lat_min},{lon_min},{lat_max},{lon_max}"
        t0 = time.time()
        print(f"[{idx}/{total_tiles}] {bbox} ... ", end="", flush=True)

        matches, ok = process_tile(bbox)

        new_count = 0
        for m in matches:
            uid = m.pop("unique_id")
            if uid not in processed_ids:
                processed_ids.add(uid)
                all_matches.append(m)
                new_count += 1

        duration = time.time() - t0
        if ok:
            print(f"-> {len(matches)} Treffer ({new_count} neu), {duration:.1f}s")
            pause = max(BASE_PAUSE, pause * 0.8)   # adaptiv wieder beschleunigen
        else:
            failed_tiles.append(bbox)
            print(f"-> FEHLGESCHLAGEN nach {duration:.1f}s")
            pause = min(pause * 2, 30)             # nach Fehlern drosseln

        if idx < total_tiles:
            time.sleep(pause + random.uniform(0, 0.5))

    # Zweiter Anlauf fuer fehlgeschlagene Kacheln
    if failed_tiles:
        print(f"\nZweiter Anlauf fuer {len(failed_tiles)} Kacheln ...")
        reset_endpoint()   # zurueck auf overpass-api.de, ruhig und langsam
        still_failed = []
        for bbox in failed_tiles:
            print(f"  retry {bbox} ... ", end="", flush=True)
            matches, ok = process_tile(bbox)
            if not ok:
                still_failed.append(bbox)
                print("weiterhin fehlgeschlagen")
                continue
            new_count = 0
            for m in matches:
                uid = m.pop("unique_id")
                if uid not in processed_ids:
                    processed_ids.add(uid)
                    all_matches.append(m)
                    new_count += 1
            print(f"ok ({new_count} neu)")
            time.sleep(BASE_PAUSE * 2)
        failed_tiles = still_failed

    total_duration = time.time() - start_total
    ok_tiles = total_tiles - len(failed_tiles)
    success_ratio = ok_tiles / total_tiles if total_tiles else 0

    print(f"\nFertig in {int(total_duration // 60)}m {int(total_duration % 60)}s")
    print(f"Kacheln ok: {ok_tiles}/{total_tiles} ({success_ratio:.1%})")
    print(f"Requests: {_stats['requests']} | Retries: {_stats['retries']} | "
          f"429/504: {_stats['rate_limited']} | Cache-Treffer: {_stats['cache_hits']}")

    # --- Alte Datei einlesen (Vergleich + Fail-Safe) ---
    old_count = 0
    if os.path.exists(OUTPUT_FILENAME):
        try:
            with open(OUTPUT_FILENAME, "r", encoding="utf-8") as f:
                old_count = len(json.load(f))
        except (OSError, ValueError):
            pass

    new_count = len(all_matches)
    diff = new_count - old_count

    print("-" * 48)
    print(f"Statistik: Alt: {old_count} -> Neu: {new_count} (Diff: {diff:+d})")

    # Fail-Safe: unvollstaendigen Lauf nicht veroeffentlichen
    if old_count > 0 and success_ratio < MIN_SUCCESS_RATIO:
        print(f"ABBRUCH: nur {success_ratio:.1%} der Kacheln erfolgreich "
              f"(Minimum {MIN_SUCCESS_RATIO:.0%}). data.json bleibt unveraendert.")
        if "GITHUB_OUTPUT" in os.environ:
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
                f.write("status=incomplete\n")
                f.write(f"stats_msg=Lauf unvollstaendig ({success_ratio:.0%})\n")
        raise SystemExit(1)

    # Atomar schreiben, damit data.json nie halb beschrieben zurueckbleibt
    tmp_name = OUTPUT_FILENAME + ".tmp"
    with open(tmp_name, "w", encoding="utf-8") as f:
        json.dump(all_matches, f, ensure_ascii=False, indent=2)
    os.replace(tmp_name, OUTPUT_FILENAME)
    print(f"Gespeichert: {OUTPUT_FILENAME}")

    # meta.js
    now = datetime.datetime.now()
    monate = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
              "August", "September", "Oktober", "November", "Dezember"]
    date_str = f"{monate[now.month - 1]} {now.year}"
    with open("meta.js", "w", encoding="utf-8") as f:
        f.write(f'const standDaten = "{date_str}";')
    print(f"meta.js aktualisiert: {date_str}")

    # GitHub Actions Report
    if "GITHUB_STEP_SUMMARY" in os.environ:
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("# Karten-Update Report\n\n")
            f.write("| Typ | Wert |\n|---|---|\n")
            f.write(f"| Vorher | {old_count} |\n")
            f.write(f"| Nachher | {new_count} |\n")
            f.write(f"| Differenz | **{diff:+d}** |\n")
            f.write(f"| Kacheln ok | {ok_tiles}/{total_tiles} ({success_ratio:.1%}) |\n")
            f.write(f"| Requests | {_stats['requests']} |\n")
            f.write(f"| Rate-Limits | {_stats['rate_limited']} |\n")
            f.write(f"| Laufzeit | {int(total_duration // 60)}m {int(total_duration % 60)}s |\n")

    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write("status=ok\n")
            f.write(f"stats_msg={new_count} Eintraege ({diff:+d})\n")


if __name__ == "__main__":
    main()
