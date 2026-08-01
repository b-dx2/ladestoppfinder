#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minimal-Diagnose fuer Overpass-429.

Testet EINE winzige Kachel gegen mehrere Endpunkte, jeweils in vier Varianten:
  GET  ohne User-Agent   (= dein bisheriges Verhalten)
  GET  mit  User-Agent
  POST ohne User-Agent
  POST mit  User-Agent

Ausgabe: Statuscode, Antwortzeit, Anzahl Elemente, Rate-Limit-Header.
Damit sehen wir schwarz auf weiss, WO der 429 herkommt.

Aufruf:
    python3 test_overpass.py
Im GitHub-Workflow als eigener Step einhaengen, um die Runner-IP zu testen.
"""

import time
import requests

# Winzige Kachel bei Karlsruhe - liefert garantiert ein paar Ladesaeulen.
BBOX = "48.98,8.36,49.03,8.44"

QUERY = f"""[out:json][timeout:25];
nwr["amenity"="charging_station"]({BBOX});
out center qt;"""

ENDPOINTS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.jp/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
]

USER_AGENT = (
    "ladestoppfinder/2.0 (Diagnose; +https://github.com/b-dx2/ladestoppfinder)"
)

INTERESTING_HEADERS = [
    "Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining",
    "RateLimit-Reset", "Server", "CF-Ray",
]


def status_page(endpoint):
    """Zeigt die Slot-Situation des Servers fuer unsere IP."""
    url = endpoint.rsplit("/api/", 1)[0] + "/api/status"
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": USER_AGENT})
        if r.status_code != 200:
            return f"status HTTP {r.status_code}"
        lines = [l.strip() for l in r.text.splitlines() if l.strip()]
        keep = [l for l in lines if "slot" in l.lower() or "Rate limit" in l]
        return " | ".join(keep) if keep else lines[0]
    except requests.RequestException as exc:
        return f"nicht erreichbar ({type(exc).__name__})"


def attempt(endpoint, method, with_ua):
    headers = {"Accept": "application/json"}
    if with_ua:
        headers["User-Agent"] = USER_AGENT

    label = f"{method:4s} UA={'ja ' if with_ua else 'nein'}"
    t0 = time.time()
    try:
        if method == "GET":
            r = requests.get(endpoint, params={"data": QUERY},
                             headers=headers, timeout=(15, 60))
        else:
            r = requests.post(endpoint, data={"data": QUERY},
                              headers=headers, timeout=(15, 60))
    except requests.RequestException as exc:
        print(f"    {label} -> Netzfehler: {type(exc).__name__}")
        return

    dt = time.time() - t0

    if r.status_code == 200:
        try:
            n = len(r.json().get("elements", []))
            print(f"    {label} -> 200 OK, {n} Elemente, {dt:.1f}s")
        except ValueError:
            print(f"    {label} -> 200 aber kein JSON, {dt:.1f}s")
    else:
        extra = {h: r.headers[h] for h in INTERESTING_HEADERS if h in r.headers}
        body = " ".join(r.text.split())[:160]
        print(f"    {label} -> HTTP {r.status_code}, {dt:.1f}s")
        if extra:
            print(f"         Header: {extra}")
        if body:
            print(f"         Body:   {body}")


def main():
    print("Overpass-Diagnose")
    print(f"Testkachel: {BBOX}\n")

    # Eigene ausgehende IP anzeigen - hilft beim Erkennen von IP-Sperren.
    try:
        ip = requests.get("https://api.ipify.org", timeout=10).text
        print(f"Ausgehende IP: {ip}\n")
    except requests.RequestException:
        print("Ausgehende IP: nicht ermittelbar\n")

    for endpoint in ENDPOINTS:
        host = endpoint.split("//")[1].split("/")[0]
        print(f"=== {host} ===")
        print(f"    /api/status: {status_page(endpoint)}")
        for method in ("GET", "POST"):
            for with_ua in (False, True):
                attempt(endpoint, method, with_ua)
                time.sleep(3)   # fair bleiben zwischen den Versuchen
        print()

    print("Auswertung:")
    print("  - Ueberall 200          -> Problem war das Tempo, Backoff reicht.")
    print("  - Nur mit UA 200        -> fehlender User-Agent war die Ursache.")
    print("  - Ein Host immer 429    -> dieser Host sperrt die Runner-IP,")
    print("                             Endpoint-Rotation loest es.")
    print("  - Alle Hosts 429        -> die CI-IP ist grossflaechig gesperrt,")
    print("                             dann self-hosted Runner oder eigene")
    print("                             Overpass-Instanz noetig.")


if __name__ == "__main__":
    main()

