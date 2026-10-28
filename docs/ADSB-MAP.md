# ADS-B map: tar1090, embedded

The console's `/adsb` page is **tar1090** — the maintained upstream ADS-B frontend by
wiedehopf — embedded in an iframe. We did not build a map: tar1090 already does map
tiles, aircraft selection, a details panel, and planespotters.net photos, and it reads
dump1090-mutability's `aircraft.json` directly.

Our own canvas radar plot is still there at **`/adsb/radar`**, linked from the map
header. It needs no map tiles and no second web server, so it keeps working if lighttpd
or the internet is down.

## What runs where

| Piece | Where |
|---|---|
| tar1090 static app | `/usr/local/share/tar1090/html/` |
| tar1090 service | `tar1090.service` (compresses dump1090 JSON into history chunks) |
| web server | **lighttpd** (was disabled, now enabled) |
| map URL | `http://<host>/tar1090/` and `http://<host>:8504/` |
| data feed | `/tar1090/data/aircraft.json` → aliased to `/home/cmilkosk/rf-monitor/signals/adsb/` |
| history chunks | `/run/tar1090/` |
| console embed | `/adsb` (iframe), radar at `/adsb/radar` |

## Install

```
sudo bash /tmp/tar1090-install.sh /home/cmilkosk/rf-monitor/signals/adsb
```

The script creates a `tar1090` system user, apt-installs its dependencies, writes
`/etc/lighttpd/conf-available/88-tar1090.conf` and `95-tar1090-otherport.conf`, and
enables `tar1090.service`. Passing the JSON directory as `$1` is what points it at our
dump1090 output instead of the auto-detected `/run/dump1090*`.

## The two gotchas that cost the most time

**1. `403 Forbidden` on the data feed.** The lighttpd alias points into
`/home/cmilkosk`, which is mode **750**. lighttpd runs as `www-data` and the tar1090
service runs as `tar1090`; neither can traverse a 750 home, so the page loaded but every
data request returned 403. Fixed with ACLs, not by loosening the home directory:

```
sudo setfacl -m u:tar1090:x /home/cmilkosk     # tar1090.service reads the JSON
sudo setfacl -m u:www-data:x /home/cmilkosk    # lighttpd serves it
```

Reverse with `setfacl -x u:tar1090 /home/cmilkosk` (same for www-data).

**2. `/data/aircraft.json` at the port-8504 root is a 404.** With the default instance
name the data lives under the instance path — `/tar1090/data/aircraft.json`. Checking
the wrong path looks like a broken feed when nothing is broken.

## Verified 2026-10-28

| Check | Result |
|---|---|
| `/tar1090/` | 200, `<title>tar1090` |
| `/tar1090/data/aircraft.json` | 200, 27 aircraft / 13 positioned |
| `/tar1090/chunks/chunks.json` | 200, history chunks present |
| photo API | `api.planespotters.net/pub/photos/hex/a9acb8` → photo returned |
| units | `lighttpd` active, `tar1090` active |
| console | `/adsb` 200 embeds tar1090; `/adsb/radar` 200 |

## Decoder: readsb, not dump1090-mutability (swapped 2026-10-28)

The lane runs **readsb 3.16.20** (built from wiedehopf's `dev` branch), not
dump1090-mutability. Same RTL, same `--gain 49.6`, same JSON directory — the only
functional difference is `--db-file /usr/local/share/tar1090/aircraft.csv.gz`, which makes
readsb emit `r` (registration), `t` (type) and `dbFlags`, so tar1090 can show real details.

| | dump1090-mutability | readsb |
|---|---|---|
| aircraft with reg/type | **0 of 27** | **17 of 17** |
| max range | 71–80 km (baseline) | 70.9 km (same ballpark) |
| units | `dump1090-mutability.service` | `rf-adsb-readsb.service` |

**Field names changed**, which matters for anything reading the JSON directly: readsb
emits `alt_baro`, `gs` and `r`/`t` where dump1090-mutability emitted `altitude`, `speed`
and `ground_speed`. Consumers must accept both — `rf_adsb_lane.py` already did;
`signals_web.py` was patched to fall back to `gs`.

**Rollback** (dump1090's unit is installed but disabled, nothing removed):

```
sudo systemctl disable --now rf-adsb-readsb
sudo systemctl enable --now rf-adsb-dump1090
```

**Rebuild recipe** if readsb ever needs reinstalling: `git clone --depth 1 -b dev
https://github.com/wiedehopf/readsb`, `make -j$(nproc) RTLSDR=yes` (2 s on LinuxGR),
install the binary to `/usr/local/bin/readsb`. The aircraft DB is a straight download —
tar1090's own `git-db/csv.sh` fails because its shallow clone has no `csv` branch:

```
sudo curl -sL -o /usr/local/share/tar1090/aircraft.csv.gz \
  https://github.com/wiedehopf/tar1090-db/raw/csv/aircraft.csv.gz
```


Photos also need internet from the *browser* (tiles and planespotters both), which is
why the radar fallback exists.
