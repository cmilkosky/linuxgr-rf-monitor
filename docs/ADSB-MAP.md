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

## Known limitation: details are thin with dump1090-mutability

tar1090 gets registration, type and route from the *decoder's* database fields, which
dump1090-mutability does not emit (upstream notes "aircraft details will be limited").
So clicking an aircraft gives hex, callsign, altitude, speed, track, and a
planespotters **photo**, but not registration/type/route. Moving the lane to **readsb**
would fill those in — a decoder swap, not a UI change.

Photos also need internet from the *browser* (tiles and planespotters both), which is
why the radar fallback exists.
