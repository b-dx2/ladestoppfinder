import json

# Die Dateien, die wir verschmelzen wollen
# 'data.json' ist deine aktuelle (Sachsen)
# 'bayern.json' ist die neue
input_files = ['data.json', 'bayern.json'] 
output_file = 'data.json' # Wir überschreiben am Ende die Hauptdatei

merged_data = []
seen_coords = set()

print("Starte Fusion...")

for filename in input_files:
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            data = json.load(f)
            print(f"Lese {filename}: {len(data)} Einträge gefunden.")
            
            for entry in data:
                # Wir erstellen einen eindeutigen Fingerabdruck aus den Koordinaten
                # (auf 4 Nachkommastellen gerundet, das sind ca. 11 Meter genau)
                coord_key = (round(entry['lat'], 4), round(entry['lon'], 4), entry['title'])
                
                if coord_key not in seen_coords:
                    seen_coords.add(coord_key)
                    merged_data.append(entry)
                else:
                    # Falls es an der Grenze Hof/Sachsen Überschneidungen gab, ignorieren wir das Duplikat
                    pass
                    
    except FileNotFoundError:
        print(f"FEHLER: Konnte {filename} nicht finden. Hast du Schritt 1 ausgeführt?")

print(f"------------------------------------------------")
print(f"✅ FERTIG! Neue Gesamtanzahl: {len(merged_data)} Matches.")
print(f"Speichere in {output_file}...")

with open(output_file, 'w', encoding='utf-8') as f:
    json.dump(merged_data, f, ensure_ascii=False, indent=2)
