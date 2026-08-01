#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ladestoppfinder - Deutschland-Scraper (Overpass API)

Strategie v3 - nach Diagnoselauf vom 01.08.2026:

Das Kernproblem war nie das Rate-Limit an sich, sondern die Anzahl der
Requests: 525 Kacheln gegen einen Server mit 2 Slots. Jede Kachel kostete
zusaetzlich Overhead, obwohl die Datenmenge winzig ist.

Loesung: statt 525 kleiner Kacheln nur noch ~15 grosse Streifen. Overpass
liefert einen Breitenstreifen Deutschland in einem Rutsch, die Auswertung
passiert lokal. Das reduziert die Requests um Faktor 35 und macht das
Rate-Limit praktisch irrelevant.

Weitere Erkenntnisse aus dem Test, die hier umgesetzt sind:
  - User-Agent ist Pflicht (ohne: 406 bei overpass-api.de, 429 bei anderen)
  - POST ist deutlich schneller als GET (3.5s statt 9.5s/504)
  - private.coffee und kumi.systems liefern mit UA nur ReadTimeouts -> raus
  - osm.ch antwortet 200 mit 0 Elementen -> gefaehrlich, raus
  - overpass-api.de ist die einzige verlaessliche Quelle
"""

import os
import sys
import json
import math
import time
import random
import hashlib
import datetime

import requests

# LibreSSL-Warnung von urllib3 unter macOS/Python 3.9 unterdruecken.
# Sie ist harmlos, macht die Logs aber unlesbar.
try:
    import urllib3
    urllib3.disable_warnings()
except ImportError:
    pass

# ============================================================
# KONFIGURATION
# ============================================================

LAT_START, LAT_END = 47.0, 55.2
LON_START, LON_END = 5.5, 15.5

# Hoehe eines Breitenstreifens in Grad.
# 0.6 -> 14 Streifen ueber Deutschland, je ca. 67 km hoch und 700 km breit.
# Grosszuegiger als noetig waere schneller, riskiert aber Server-Timeouts.
STRIP_HEIGHT = 0.6

SEARCH_RADIUS_METERS = 300
OUTPUT_FILENAME = "data.json"
CACHE_DIR = ".cache_overpass"
CACHE_TTL_HOURS = 20

# Mindestanteil erfolgreicher Streifen, damit data.json ersetzt wird.
MIN_SUCCESS_RATIO = 0.90

# Nur Instanzen, die im Test verwertbare Daten geliefert haben.
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.osm.rambler.ru/cgi/interpreter",
]

# Pflichtangabe. Ohne UA: 406 (overpass-api.de) bzw. 429 (nginx-Instanzen).
USER_AGENT = (
    "ladestoppfinder/3.0 (monatlicher OSM-Datenabgleich; "
    "+https://github.com/b-dx2/ladestoppfinder)"
)

QUERY_TIMEOUT = 300         # Overpass-seitig - grosse Streifen brauchen Zeit
HTTP_TIMEOUT = (20, 360)    # (connect, read) - muss ueber QUERY_TIMEOUT liegen
MAX_RETRIES = 5
MAX_PAUSE = 120.0
PAUSE_BETWEEN_STRIPS = 8.0  # bewusst grosszuegig, es sind ja nur ~14 Requests

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
# HTTP-LAYER
# ============================================================

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Accept-Encoding": "gzip, deflate",
})

_endpoint_index = 0
_stats = {"requests": 0, "retries": 0, "rate_limited": 0,
          "timeouts": 0, "cache_hits": 0}


def current_endpoint():
    return OVERPASS_ENDPOINTS[_endpoint_index % len(OVERPASS_ENDPOINTS)]


def rotate_endpoint():
    global _endpoint_index
    _endpoint_index += 1


def reset_endpoint():
    global _endpoint_index
    _endpoint_index = 0


def wait_for_slot(max_wait=180):
    """
    Wartet, bis overpass-api.de einen freien Slot meldet.
    Bei nur ~14 Requests ist das billig und verhindert 429 zuverlaessig.
    Rueckgabe False, wenn der Status nicht ermittelbar war.
    """
    endpoint = current_endpoint()
    if "overpass-api.de" not in endpoint:
        return False
    try:
        r = SESSION.get("https://overpass-api.de/api/status", timeout=(10, 25))
        if r.status_code != 200:
            return False
        waits = []
        for line in r.text.splitlines():
            line = line.strip()
            if "slots available now" in line:
                return True
            if line.startswith("Slot available after:") and ", in " in line:
                try:
                    waits.append(int(line.split(", in ")[1].split(" ")[0]))
                except (ValueError, IndexError):
                    pass
        if waits:
            secs = max(0, min(min(waits) + 3, max_wait))
            if secs:
                print(f" [Slot in {secs}s]", end="", flush=True)
                time.sleep(secs)
            return True
    except requests.RequestException:
        return False
    return False


def overpass_query(query):
    """
    Fuehrt eine Query aus. Rueckgabe: Elementliste oder None bei Endfehler.
    """
    delay = 15.0

    for attempt in range(1, MAX_RETRIES + 1):
        wait_for_slot()
        endpoint = current_endpoint()

        try:
            _stats["requests"] += 1
            t0 = time.time()
            r = SESSION.post(endpoint, data={"data": query}, timeout=HTTP_TIMEOUT)
            elapsed = time.time() - t0

            if r.status_code == 200:
                try:
                    elements = r.json().get("elements", [])
                except ValueError:
                    print(" [kein JSON]", end="", flush=True)
                else:
                    print(f" [{elapsed:.0f}s, {len(elements)} Objekte]",
                          end="", flush=True)
                    return elements

            elif r.status_code == 406:
                # Nur ohne User-Agent moeglich - Konfigurationsfehler.
                print(" [406: User-Agent fehlt]", end="", flush=True)
                return None

            elif r.status_code == 429:
                _stats["rate_limited"] += 1
                ra = r.headers.get("Retry-After")
                sleep_for = (min(float(ra) + 3, MAX_PAUSE)
                             if ra and ra.isdigit() else min(delay, MAX_PAUSE))
                print(f" [429, warte {sleep_for:.0f}s]", end="", flush=True)
                time.sleep(sleep_for + random.uniform(0, 3))
                delay *= 2
                _stats["retries"] += 1
                continue

            elif r.status_code in (502, 503, 504):
                # 504 = Overpass hat die Query serverseitig abgebrochen.
                # Nicht sofort Server wechseln - meist hilft Geduld.
                _stats["timeouts"] += 1
                print(f" [{r.status_code}, warte {delay:.0f}s]", end="", flush=True)
                time.sleep(min(delay, MAX_PAUSE))
                delay *= 2
                if attempt >= 3:
                    rotate_endpoint()
                _stats["retries"] += 1
                continue

            else:
                print(f" [HTTP {r.status_code}]", end="", flush=True)

        except requests.Timeout:
            _stats["timeouts"] += 1
            print(" [Client-Timeout]", end="", flush=True)
        except requests.RequestException as exc:
            print(f" [{type(exc).__name__}]", end="", flush=True)

        _stats["retries"] += 1
        time.sleep(min(delay, MAX_PAUSE) + random.uniform(0, 5))
        delay *= 2

    return None


# ============================================================
# CACHE
# ============================================================

def cache_path(key_str):
    key = hashlib.md5(f"{key_str}|{FOOD_REGEX}".encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, f"{key}.json")


def cache_read(key_str):
    path = cache_path(key_str)
    if not os.path.exists(path):
        return None
    if (time.time() - os.path.getmtime(path)) / 3600 > CACHE_TTL_HOURS:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        _stats["cache_hits"] += 1
        return data
    except (OSError, ValueError):
        return None


def cache_write(key_str, elements):
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        with open(cache_path(key_str), "w", encoding="utf-8") as f:
            json.dump(elements, f, ensure_ascii=False)
    except OSError:
        pass


# ============================================================
# HILFSFUNKTIONEN
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
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def build_query(bbox_str):
    return f"""[out:json][timeout:{QUERY_TIMEOUT}];
(
  nwr["amenity"="charging_station"]({bbox_str});
  nwr["amenity"~"^(fast_food|restaurant|cafe|lounge|vending_machine)$"]["name"~"{FOOD_REGEX}",i]({bbox_str});
  nwr["shop"~"^(kiosk|convenience)$"]["name"~"{FOOD_REGEX}",i]({bbox_str});
);
out center qt;"""


# ============================================================
# AUSWERTUNG (Logik identisch zur Ursprungsversion)
# ============================================================

def classify(elements, chargers, restaurants):
    """Sortiert Elemente in die uebergebenen Listen ein."""
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name", "Unbekannt")

        strong_search = " ".join([
            tags.get("brand", ""), tags.get("operator", ""), tags.get("network", "")
        ]).lower()
        weak_search = (name or "").lower()
        full_search = (weak_search + " " + strong_search).strip()

        is_poi = (
            tags.get("amenity") in
            ["fast_food", "restaurant", "cafe", "lounge", "vending_machine"]
            or tags.get("shop") in ["kiosk", "convenience"]
        )

        if is_poi:
            config = fid = None

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
                        break

            if config:
                el["clean_info"] = config
                el["id_key"] = fid
                restaurants.append(el)

        elif tags.get("amenity") == "charging_station":
            config = fid = None

            for k, c in ALLOWED_CHARGERS.items():
                if k in strong_search:
                    config, fid = c, ("aral" if k == "pulse" else k)
                    break

            if not config:
                if "supercharger" in weak_search:
                    config, fid = ALLOWED_CHARGERS["tesla"], "tesla"
                else:
                    for k, c in ALLOWED_CHARGERS.items():
                        if k in weak_search:
                            config, fid = c, ("aral" if k == "pulse" else k)
                            break

            if not config:
                continue

            display_name = name
            if "Unbekannt" in display_name:
                display_name = (tags.get("brand") or tags.get("operator")
                                or config["name"])
                city = tags.get("addr:city")
                if city:
                    display_name = f"{display_name} ({city})"

            el["clean_info"] = dict(config, name=display_name)
            el["id_key"] = fid
            chargers.append(el)


def deduplicate(chargers):
    """
    Entfernt Ladepunkte desselben Anbieters innerhalb von 30 m.
    Gitter-basiert statt paarweise - bei bundesweiten Daten waere der
    urspruengliche O(n^2)-Vergleich sonst nicht mehr handhabbar.
    """
    seen = {}
    result = []
    # ~0.0005 Grad entsprechen rund 40 m Breitengrad-Abstand.
    for el in chargers:
        lat, lon = get_coords(el)
        if lat is None:
            continue
        cell = (el["id_key"], round(lat / 0.0005), round(lon / 0.0008))
        neighbours = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                neighbours.extend(seen.get((cell[0], cell[1] + dy, cell[2] + dx), []))

        if any(calculate_distance(lat, lon, o[0], o[1]) < 30 for o in neighbours):
            continue

        seen.setdefault(cell, []).append((lat, lon))
        result.append(el)
    return result


def match_pairs(chargers, restaurants):
    """
    Ordnet jedem Ladepunkt das naechstgelegene passende Lokal zu.
    Restaurants werden vorab in ein Raster einsortiert, damit nicht jeder
    Ladepunkt gegen alle Lokale geprueft werden muss.
    """
    grid = {}
    for r in restaurants:
        lat, lon = get_coords(r)
        if lat is None:
            continue
        grid.setdefault((round(lat / 0.01), round(lon / 0.015)), []).append((lat, lon, r))

    matches = []
    for c in chargers:
        c_lat, c_lon = get_coords(c)
        if c_lat is None:
            continue

        cy, cx = round(c_lat / 0.01), round(c_lon / 0.015)
        best_food, closest = None, float("inf")

        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                for r_lat, r_lon, r in grid.get((cy + dy, cx + dx), []):
                    dist = calculate_distance(c_lat, c_lon, r_lat, r_lon)
                    if dist <= SEARCH_RADIUS_METERS and dist < closest:
                        closest, best_food = dist, r

        if not best_food:
            continue

        food_name = best_food.get("tags", {}).get(
            "name", best_food["clean_info"]["name"])
        charger_name = c["clean_info"]["name"]

        matches.append({
            "lat": c_lat,
            "lon": c_lon,
            "charger_id": c["id_key"],
            "food_id": best_food["id_key"].replace(" ", "-"),
            "title": charger_name,
            "badge_class": c["clean_info"]["class"],
            "note": f"{int(closest)}m zu {food_name}",
            "popup_name": charger_name,
            "description": (
                f"<div style='margin-bottom:4px; font-weight:bold; "
                f"font-size:1.1em; color:var(--charger-color)'>{charger_name}</div>"
                f"<div style='display:flex; align-items:center; gap:5px; "
                f"margin-top:5px;'><span>&#127869;</span>"
                f"<span style='font-weight:600;'>{food_name}</span></div>"
                f"<div style='font-size:0.85em; color:#666; margin-top:2px;'>"
                f"Entfernung: {int(closest)}m</div>"
            ),
            "unique_id": f"{c.get('type')}{c.get('id')}_"
                         f"{best_food.get('type')}{best_food.get('id')}",
        })

    return matches


# ============================================================
# HAUPTPROGRAMM
# ============================================================

def build_strips():
    strips = []
    lat = LAT_START
    while lat < LAT_END:
        top = min(round(lat + STRIP_HEIGHT, 4), LAT_END)
        strips.append((round(lat, 4), LON_START, top, LON_END))
        lat = top
    return strips


def main():
    start = time.time()
    strips = build_strips()

    print("Ladestoppfinder - Deutschland-Scan v3")
    print(f"Gebiet: {LAT_START}-{LAT_END} N / {LON_START}-{LON_END} E")
    print(f"Streifen: {len(strips)} (je {STRIP_HEIGHT} Grad hoch)")
    print(f"Endpoint: {current_endpoint()}\n")

    all_chargers, all_restaurants = [], []
    failed = []

    for idx, (lat_min, lon_min, lat_max, lon_max) in enumerate(strips, 1):
        bbox = f"{lat_min},{lon_min},{lat_max},{lon_max}"
        print(f"[{idx}/{len(strips)}] Breite {lat_min}-{lat_max} ...",
              end="", flush=True)

        elements = cache_read(bbox)
        if elements is None:
            elements = overpass_query(build_query(bbox))
            if elements is None:
                failed.append(bbox)
                print(" -> FEHLGESCHLAGEN")
                continue
            cache_write(bbox, elements)
        else:
            print(" [Cache]", end="")

        before_c, before_r = len(all_chargers), len(all_restaurants)
        classify(elements, all_chargers, all_restaurants)
        print(f" -> +{len(all_chargers) - before_c} Ladepunkte, "
              f"+{len(all_restaurants) - before_r} Lokale")

        if idx < len(strips):
            time.sleep(PAUSE_BETWEEN_STRIPS)

    # Zweiter Anlauf
    if failed:
        print(f"\nZweiter Anlauf fuer {len(failed)} Streifen ...")
        reset_endpoint()
        still_failed = []
        for bbox in failed:
            print(f"  {bbox} ...", end="", flush=True)
            time.sleep(30)
            elements = overpass_query(build_query(bbox))
            if elements is None:
                still_failed.append(bbox)
                print(" -> erneut fehlgeschlagen")
                continue
            cache_write(bbox, elements)
            classify(elements, all_chargers, all_restaurants)
            print(" -> ok")
        failed = still_failed

    print(f"\nRohdaten: {len(all_chargers)} Ladepunkte, "
          f"{len(all_restaurants)} Lokale")

    # Streifen ueberlappen an den Raendern nicht, aber ein Objekt kann
    # doppelt geliefert werden. Erst nach OSM-ID entdoppeln, dann raeumlich.
    unique = {}
    for c in all_chargers:
        unique[(c.get("type"), c.get("id"))] = c
    all_chargers = deduplicate(list(unique.values()))
    print(f"Nach Entdopplung: {len(all_chargers)} Ladepunkte")

    matches, seen_ids = [], set()
    for m in match_pairs(all_chargers, all_restaurants):
        uid = m.pop("unique_id")
        if uid not in seen_ids:
            seen_ids.add(uid)
            matches.append(m)

    duration = time.time() - start
    ok_strips = len(strips) - len(failed)
    ratio = ok_strips / len(strips) if strips else 0

    print(f"\nFertig in {int(duration // 60)}m {int(duration % 60)}s")
    print(f"Streifen ok: {ok_strips}/{len(strips)} ({ratio:.0%})")
    print(f"Requests: {_stats['requests']} | Retries: {_stats['retries']} | "
          f"429: {_stats['rate_limited']} | Timeouts: {_stats['timeouts']} | "
          f"Cache: {_stats['cache_hits']}")

    # --- Speichern ---
    old_count = 0
    if os.path.exists(OUTPUT_FILENAME):
        try:
            with open(OUTPUT_FILENAME, "r", encoding="utf-8") as f:
                old_count = len(json.load(f))
        except (OSError, ValueError):
            pass

    new_count = len(matches)
    diff = new_count - old_count
    print("-" * 50)
    print(f"Alt: {old_count} -> Neu: {new_count} (Diff: {diff:+d})")

    abort_reason = None
    if ratio < MIN_SUCCESS_RATIO:
        abort_reason = f"nur {ratio:.0%} der Streifen erfolgreich"
    elif old_count > 0 and new_count < old_count * 0.5:
        abort_reason = "Ergebnis weniger als halb so gross wie zuvor"

    if abort_reason and old_count > 0:
        print(f"ABBRUCH: {abort_reason}. data.json bleibt unveraendert.")
        if "GITHUB_OUTPUT" in os.environ:
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
                f.write("status=incomplete\n")
                f.write(f"stats_msg=Lauf abgebrochen: {abort_reason}\n")
        sys.exit(1)

    tmp = OUTPUT_FILENAME + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(matches, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUTPUT_FILENAME)
    print(f"Gespeichert: {OUTPUT_FILENAME}")

    monate = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
              "August", "September", "Oktober", "November", "Dezember"]
    now = datetime.datetime.now()
    date_str = f"{monate[now.month - 1]} {now.year}"
    with open("meta.js", "w", encoding="utf-8") as f:
        f.write(f'const standDaten = "{date_str}";')
    print(f"meta.js: {date_str}")

    if "GITHUB_STEP_SUMMARY" in os.environ:
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("# Karten-Update\n\n| Kennzahl | Wert |\n|---|---|\n")
            f.write(f"| Vorher | {old_count} |\n| Nachher | {new_count} |\n")
            f.write(f"| Differenz | **{diff:+d}** |\n")
            f.write(f"| Streifen ok | {ok_strips}/{len(strips)} |\n")
            f.write(f"| Requests | {_stats['requests']} |\n")
            f.write(f"| Laufzeit | {int(duration // 60)}m {int(duration % 60)}s |\n")

    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write("status=ok\n")
            f.write(f"stats_msg={new_count} Eintraege ({diff:+d})\n")


if __name__ == "__main__":
    main()
