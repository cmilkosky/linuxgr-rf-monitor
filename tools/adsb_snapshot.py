"""Snapshot the live ADS-B lane as one comparable row: aircraft, message rate,
range percentiles and RSSI percentiles.

Every hardware change to the 1090 chain (outdoor move, LNA, mast, second dongle)
should get a row from this, so the next change has a number to beat instead of an
impression. The range statistic matches docs/ADSB-BASELINE.md (great-circle from
the station to each positioned aircraft); it needs readsb to know the station
position, which it gets from RF_ADSB_LAT / RF_ADSB_LON in
/home/cmilkosk/.config/rf-adsb-station.env - see docs/ADSB-BASELINE.md.

    python3 tools/adsb_snapshot.py                      # one row, human readable
    python3 tools/adsb_snapshot.py --json               # same row as JSON
    python3 tools/adsb_snapshot.py --seconds 120        # message rate over 120 s
    python3 tools/adsb_snapshot.py --append baselines.csv

RSSI here is readsb's own relative scale, not dBm. It is only comparable between
runs on the SAME decoder at the SAME gain (readsb, --gain 49.6) - never against
dump1090-mutability or a different gain setting.
"""
from __future__ import annotations

import argparse
import json
import math
import time
import urllib.request


def fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as fh:
        return json.load(fh)


def percentile(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    ordered = sorted(vals)
    return ordered[min(len(ordered) - 1, int(len(ordered) * p))]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8099/tar1090/data/aircraft.json")
    ap.add_argument("--receiver", default=None,
                    help="receiver.json URL, default: alongside --url")
    ap.add_argument("--seconds", type=float, default=60.0,
                    help="window for the message-rate measurement")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--append", metavar="CSV", default=None)
    a = ap.parse_args()

    receiver_url = a.receiver or a.url.rsplit("/", 1)[0] + "/receiver.json"
    first = fetch(a.url)
    t0 = time.time()
    time.sleep(a.seconds)
    last = fetch(a.url)
    t1 = time.time()

    lat, lon = first.get("lat"), first.get("lon")
    if lat is None or lon is None:
        lat, lon = fetch(receiver_url).get("lat"), fetch(receiver_url).get("lon")

    pos = [ac for ac in last["aircraft"] if "lat" in ac]
    if lat is not None and lon is not None:
        rng = [haversine_km(lat, lon, ac["lat"], ac["lon"]) for ac in pos]
    else:
        rng = []
    rssi = [ac["rssi"] for ac in last["aircraft"] if "rssi" in ac]

    row = {
        "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t1)),
        "aircraft": len(last["aircraft"]),
        "positioned": len(pos),
        "msg_per_s": round((last["messages"] - first["messages"]) / (t1 - t0), 1),
        "range_max_km": round(max(rng), 1) if rng else None,
        "range_p90_km": round(percentile(rng, 0.90), 1) if rng else None,
        "range_median_km": round(percentile(rng, 0.50), 1) if rng else None,
        "rssi_min": round(min(rssi), 1) if rssi else None,
        "rssi_median": round(percentile(rssi, 0.50), 1) if rssi else None,
        "rssi_max": round(max(rssi), 1) if rssi else None,
        "station_known": lat is not None,
    }

    if a.json:
        print(json.dumps(row))
    else:
        print(f"{row['utc']}  aircraft {row['aircraft']} ({row['positioned']} positioned)  "
              f"{row['msg_per_s']} msg/s  "
              f"range max/p90/median {row['range_max_km']}/{row['range_p90_km']}/{row['range_median_km']} km  "
              f"rssi min/med/max {row['rssi_min']}/{row['rssi_median']}/{row['rssi_max']}")
    if a.append:
        import csv
        import os
        new = not os.path.exists(a.append)
        with open(a.append, "a", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(row))
            if new:
                w.writeheader()
            w.writerow(row)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
