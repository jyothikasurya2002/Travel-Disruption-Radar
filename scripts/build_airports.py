"""One-off: build data/airports_us.csv from the OurAirports public dataset.

Keeps only U.S. large/medium airports with scheduled passenger service and an
IATA code -- i.e. airports a business traveller could actually fly into.

Run:  .venv/bin/python scripts/build_airports.py
"""
import csv
import io
import urllib.request
from pathlib import Path

SOURCE_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
OUT = Path(__file__).resolve().parent.parent / "data" / "airports_us.csv"
KEEP_TYPES = {"large_airport", "medium_airport"}


def main() -> None:
    with urllib.request.urlopen(SOURCE_URL, timeout=60) as resp:
        text = resp.read().decode("utf-8")

    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        if (
            r["iso_country"] == "US"
            and r["type"] in KEEP_TYPES
            and r["scheduled_service"] == "yes"
            and r["iata_code"]
        ):
            rows.append({
                "iata": r["iata_code"],
                "icao": r.get("icao_code") or r["gps_code"] or r["ident"],
                "name": r["name"],
                "city": r["municipality"],
                "state": r["iso_region"].replace("US-", ""),
                "type": r["type"],
                "lat": r["latitude_deg"],
                "lon": r["longitude_deg"],
            })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(sorted(rows, key=lambda x: x["iata"]))
    print(f"Wrote {len(rows)} airports to {OUT}")


if __name__ == "__main__":
    main()
