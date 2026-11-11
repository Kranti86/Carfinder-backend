# data/

## airports.csv

IATA code + coordinates for US airports, used to enrich `/suggest` results
(Mapbox doesn't return IATA codes). Columns: `iata,name,city,region,lat,lon`.

This seed file covers the ~55 busiest US airports — enough to cover the large
majority of rental demand immediately. Per the architecture review (§9), don't
grow this by hand; regenerate the full US set from OpenFlights when you want
complete coverage:

```bash
# OpenFlights airports.dat is public domain-ish (ODbL). Columns are positional.
curl -sL https://raw.githubusercontent.com/jpatokal/openflights/master/data/airports.dat \
  -o airports.dat

# Keep US airports that have a real IATA code, emit our column order.
python - <<'PY'
import csv
rows = []
with open("airports.dat", newline="", encoding="utf-8") as f:
    for r in csv.reader(f):
        # 0 id,1 name,2 city,3 country,4 IATA,5 ICAO,6 lat,7 lon, ...
        if len(r) < 8: continue
        name, city, country, iata, _icao, lat, lon = r[1], r[2], r[3], r[4], r[5], r[6], r[7]
        if country != "United States": continue
        if not iata or iata == "\\N" or len(iata) != 3: continue
        rows.append([iata, name, city, "", lat, lon])
with open("airports.csv", "w", newline="", encoding="utf-8") as f:
    w = csv.writer(f); w.writerow(["iata","name","city","region","lat","lon"]); w.writerows(rows)
print(f"wrote {len(rows)} airports")
PY
```

The loader (`app/providers/airports.py`) reads this file once at startup and
matches a geocoder airport hit to the nearest known airport within ~8km.
