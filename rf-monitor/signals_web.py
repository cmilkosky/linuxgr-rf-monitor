"""RF Monitor v2 - Signals views.

The main page (``/``) is a signals dashboard: one row per signal episode, grouped
into Strongest / per-type / Latest sections. ``/signals/table`` is the expandable,
filterable table behind every section header. ``/signals/<id>`` is the detail view
with the captured sample. The spectrum heatmap keeps living at ``/spectrum``.
"""

from __future__ import annotations

import csv
import html
import io
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

import signals_store as store
import rf_watchlist

router = APIRouter()
LOCAL_TZ = ZoneInfo("America/New_York")

# ADS-B aircraft have their own page. Left in the signal list they are 1,203 episodes a
# day - 39% of every row - which buried the signals this app exists to show.
EXCLUDED_SOURCE = "adsb-lane"

TYPE_SECTIONS: list[tuple[str, str, tuple[str, ...]]] = [
    ("voice", "VOICE", ("voice",)),
    ("music", "MUSIC", ("music",)),
    ("ism", "ISM SENSORS / RTL_433", ("ism",)),
    ("text", "AIRCRAFT / APRS / PAGER TEXT", ("aircraft", "aprs", "pager")),
    ("satellite", "SATELLITE IMAGES", ("satellite",)),
]

SECTION_CHOICES = [
    ("strongest", "Strongest (today)"),
    ("latest", "Latest"),
    ("all", "Everything"),
    ("voice", "Voice"),
    ("music", "Music"),
    ("ism", "ISM sensors"),
    ("text", "Aircraft / APRS / pager text"),
    ("satellite", "Satellite images"),
]

CSS = """
:root { color-scheme: dark; --bg:#101418; --panel:#171d22; --line:#2b353d; --text:#e7edf2;
        --muted:#9caab5; --hot:#ffca62; --accent:#63d2ff; --bad:#ff6f91; --good:#7fd88f; }
* { box-sizing:border-box; }
body { margin:0; font-family:Inter,ui-sans-serif,system-ui,-apple-system,Segoe UI,sans-serif;
       background:var(--bg); color:var(--text); }
header { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:10px 22px;
         border-bottom:1px solid var(--line); background:#12181d; position:sticky; top:0; z-index:5; }
header h1 { margin:0; font-size:19px; font-weight:650; letter-spacing:.2px; }
nav { display:flex; gap:6px; }
nav a { padding:7px 11px; font-size:13px; color:var(--text); text-decoration:none;
        border:1px solid #35424c; border-radius:6px; background:#172027; }
nav a:hover { border-color:var(--accent); }
nav a.active { color:var(--hot); border-color:#77613c; background:#20231f; }
main { padding:18px 22px 60px; max-width:1500px; }
.strip { display:flex; gap:10px; flex-wrap:wrap; margin-bottom:18px; }
.stat { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:9px 14px; min-width:120px; }
.stat strong { display:block; font-size:20px; }
.stat span { color:var(--muted); font-size:12px; }
section.block { margin-bottom:22px; }
.blockHead { display:flex; align-items:baseline; justify-content:space-between; gap:12px;
             border-bottom:1px solid var(--line); padding-bottom:6px; margin-bottom:10px; }
.blockHead h2 { margin:0; font-size:14px; letter-spacing:.9px; color:var(--accent); font-weight:700; }
.blockHead .meta { color:var(--muted); font-size:12px; }
.blockHead a { color:var(--hot); font-size:12px; text-decoration:none; border:1px solid #77613c;
               border-radius:6px; padding:4px 9px; background:#20231f; }
.blockHead a:hover { border-color:var(--hot); }
table { width:100%; border-collapse:collapse; font-size:13px; }
th { text-align:left; font-weight:600; color:var(--muted); font-size:11.5px; letter-spacing:.6px;
     text-transform:uppercase; padding:6px 9px; border-bottom:1px solid var(--line); white-space:nowrap; }
th a { color:var(--muted); text-decoration:none; }
th a:hover { color:var(--accent); }
td { padding:7px 9px; border-bottom:1px solid #1e262c; vertical-align:top; }
tr.row { cursor:pointer; }
tr.row:hover td { background:#1b232a; }
td.num { font-variant-numeric:tabular-nums; white-space:nowrap; }
.freq { color:var(--hot); font-weight:600; font-variant-numeric:tabular-nums; }
.snr { color:var(--good); font-variant-numeric:tabular-nums; }
.pill { display:inline-block; border:1px solid #35424c; border-radius:999px; padding:1px 7px;
        font-size:11px; color:var(--text); background:#202a31; margin-left:6px; }
.pill.new { border-color:#77613c; color:var(--hot); background:#20231f; }
.pill.known { border-color:#3a4a58; color:var(--muted); }
.pill.dec { border-color:#2f6b4a; color:var(--good); }
.empty { color:var(--muted); font-size:13px; padding:10px 2px; font-style:italic; }
form.filters { display:flex; flex-wrap:wrap; gap:10px; align-items:flex-end; background:var(--panel);
               border:1px solid var(--line); border-radius:8px; padding:12px; margin-bottom:16px; }
form.filters label { display:flex; flex-direction:column; gap:4px; font-size:11px; color:var(--muted);
                     text-transform:uppercase; letter-spacing:.5px; }
select,input,button { background:#202a31; color:var(--text); border:1px solid #35424c; border-radius:6px;
                      padding:6px 9px; font:inherit; font-size:13px; }
input[type=number] { width:104px; }
button { cursor:pointer; }
button:hover { border-color:var(--accent); }
.btn { display:inline-block; padding:6px 11px; border:1px solid #39758f; border-radius:6px;
       background:#1d4050; color:var(--text); text-decoration:none; font-size:13px; }
.pager { display:flex; gap:10px; align-items:center; margin-top:14px; color:var(--muted); font-size:12px; }
.detailGrid { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:14px 0 20px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px 12px; }
.card span { display:block; color:var(--muted); font-size:11px; text-transform:uppercase; letter-spacing:.5px; }
.card strong { display:block; font-size:16px; margin-top:3px; font-variant-numeric:tabular-nums; }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; margin-bottom:16px; }
.panel h3 { margin:0 0 10px; font-size:13px; letter-spacing:.7px; color:var(--accent); text-transform:uppercase; }
.evidence { margin:0; padding-left:18px; line-height:1.6; font-size:13.5px; }
.evidence li { margin-bottom:3px; }
img.spec { width:100%; max-width:1000px; border:1px solid var(--line); border-radius:8px; background:#0c1013; }
audio { width:100%; max-width:520px; display:block; margin:6px 0 12px; }
pre.text { background:#0c1013; border:1px solid var(--line); border-radius:8px; padding:10px;
           white-space:pre-wrap; word-break:break-word; font-size:13px; }
.kv { display:grid; grid-template-columns:220px 1fr; gap:4px 12px; font-size:13px; }
.kv div:nth-child(odd) { color:var(--muted); }
.muted { color:var(--muted); }
.labelRow { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-top:10px; }
"""


# --------------------------------------------------------------------------- #
# formatting helpers
# --------------------------------------------------------------------------- #
def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def mhz(hz: Any) -> str:
    try:
        value = float(hz) / 1e6
    except (TypeError, ValueError):
        return "-"
    return f"{value:,.3f}" if value < 1000 else f"{value:,.1f}"


def bw_text(hz: Any) -> str:
    try:
        value = float(hz)
    except (TypeError, ValueError):
        return "-"
    if value >= 1e6:
        return f"{value/1e6:.1f} MHz"
    return f"{value/1e3:.0f} kHz"


def snr_text(value: Any) -> str:
    try:
        return f"{float(value):+.1f} dB"
    except (TypeError, ValueError):
        return "-"


def when_text(row: dict[str, Any]) -> str:
    stamp = row.get("last_seen") or row.get("first_seen") or ""
    try:
        dt = store.parse_iso(stamp).astimezone(LOCAL_TZ)
    except Exception:
        return esc(stamp)
    today = datetime.now(LOCAL_TZ).date()
    if dt.date() == today:
        return dt.strftime("%H:%M")
    return dt.strftime("%b %d %H:%M")


def duration_text(row: dict[str, Any]) -> str:
    try:
        first = store.parse_iso(row["first_seen"])
        last = store.parse_iso(row["last_seen"])
    except Exception:
        return "-"
    seconds = max(0, int((last - first).total_seconds()))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds//60}m"
    return f"{seconds/3600:.1f}h"


def decode_text(row: dict[str, Any]) -> str:
    if row.get("decode_status") == "decoded" and row.get("decoded_summary"):
        return f'<span class="pill dec">decoded</span> {esc(row["decoded_summary"])[:110]}'
    status = row.get("decode_status") or "none"
    if status == "captured":
        return '<span class="muted">sample captured · decoder pending</span>'
    if status == "failed":
        return '<span class="muted">capture failed</span>'
    return '<span class="muted">—</span>'


def shell(title: str, body: str, *, active: str = "signals", refresh: int = 0) -> str:
    meta_refresh = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""

    def nav_item(key: str, href: str, label: str) -> str:
        cls = "active" if key == active else ""
        return f'<a class="{cls}" href="{href}">{label}</a>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
{meta_refresh}<title>{esc(title)}</title><style>{CSS}</style></head>
<body>
<header>
  <h1>RF Monitor · Signals</h1>
  <nav>
    {nav_item("signals", "/", "Signals")}
    {nav_item("spectrum", "/spectrum", "Spectrum")}
    {nav_item("watchlist", "/watchlist", "Watchlist")}
    {nav_item("adsb", "/adsb", "ADS-B")}
    {nav_item("health", "/ha-radio-health", "Radio Health")}
  </nav>
</header>
<main>{body}</main>
</body></html>"""


# --------------------------------------------------------------------------- #
# shared table rendering
# --------------------------------------------------------------------------- #
def rows_table(rows: list[dict[str, Any]], *, sort: str | None = None, direction: str | None = None,
               params: dict[str, Any] | None = None, limit: int = 0, offset: int = 0,
               total: int | None = None) -> str:
    if not rows:
        return '<div class="empty">No signals match yet.</div>'

    params = dict(params or {})

    def head(label: str, key: str) -> str:
        if sort is None:
            return f"<th>{label}</th>"
        nxt = "asc" if (sort == key and direction != "asc") else "desc"
        arrow = " ▾" if sort == key and direction != "asc" else (" ▴" if sort == key else "")
        q = dict(params, sort=key, direction=nxt, offset=0)
        return f'<th><a href="/signals/table?{urlencode(q)}">{label}{arrow}</a></th>'

    out = [
        "<table><thead><tr>",
        head("Seen", "time"),
        head("Frequency", "freq"),
        head("Strength", "snr"),
        "<th>Bandwidth</th>",
        "<th>What I think it is</th>",
        "<th>Decode</th>",
        head("Duration", "duration"),
        "</tr></thead><tbody>",
    ]
    for row in rows:
        pills = ""
        if row.get("is_new"):
            pills += '<span class="pill new">NEW</span>'
        if row.get("class") == "known":
            pills += f'<span class="pill known">{esc(row.get("known_emitter") or "known band")}</span>'
        conf = row.get("confidence")
        conf_pill = f'<span class="pill">{esc(conf)}</span>' if conf else ""
        out.append(
            f'<tr class="row" onclick="location.href=\'/signals/{int(row["id"])}\'">'
            f'<td class="num">{when_text(row)}</td>'
            f'<td class="num freq">{mhz(row["freq_hz"])} MHz</td>'
            f'<td class="num snr">{snr_text(row.get("peak_snr_db"))}</td>'
            f'<td class="num">{bw_text(row.get("bandwidth_hz"))}</td>'
            f'<td>{esc(row.get("likely") or "unclassified")}{conf_pill}{pills}</td>'
            f"<td>{decode_text(row)}</td>"
            f'<td class="num">{duration_text(row)} <span class="muted">×{int(row.get("sightings") or 1)}</span></td>'
            "</tr>"
        )
    out.append("</tbody></table>")

    if limit and total is not None:
        shown_from = offset + 1 if rows else 0
        shown_to = offset + len(rows)
        prev_q = dict(params, offset=max(0, offset - limit), limit=limit)
        next_q = dict(params, offset=offset + limit, limit=limit)
        prev_link = (f'<a class="btn" href="/signals/table?{urlencode(prev_q)}">← previous</a>'
                     if offset > 0 else "")
        next_link = (f'<a class="btn" href="/signals/table?{urlencode(next_q)}">next →</a>'
                     if shown_to < total else "")
        out.append(
            f'<div class="pager"><span>{shown_from}–{shown_to} of {total}</span>{prev_link}{next_link}</div>'
        )
    return "".join(out)


def section_block(title: str, rows: list[dict[str, Any]], *, section_key: str, empty_note: str) -> str:
    body = rows_table(rows) if rows else f'<div class="empty">{esc(empty_note)}</div>'
    return (
        f'<section class="block"><div class="blockHead">'
        f"<h2>{esc(title)}</h2>"
        f'<div><span class="meta">{len(rows)} shown</span> '
        f'<a href="/signals/table?section={section_key}">View all →</a></div>'
        f"</div>{body}</section>"
    )


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #
def storage_text() -> str:
    used = store.artifact_bytes()
    if used >= 1024 ** 3:
        return f"{used/1024**3:,.2f} GB"
    return f"{used/1e6:,.1f} MB"


def render_dashboard() -> str:
    counts = store.dashboard_counts(exclude_source=EXCLUDED_SOURCE)
    strongest = store.query_episodes(section="strongest", limit=10, sort="snr", direction="desc",
                                     exclude_source=EXCLUDED_SOURCE)
    latest = store.query_episodes(section="latest", limit=50, sort="time", direction="desc",
                                  exclude_source=EXCLUDED_SOURCE)

    blocks = [section_block("STRONGEST SIGNALS (ACTIVE TODAY, BY SNR)", strongest,
                            section_key="strongest",
                            empty_note="Nothing above the noise floor yet today.")]
    for key, title, types in TYPE_SECTIONS:
        rows = store.query_episodes(decode_types=list(types), limit=20, sort="time", direction="desc",
                                    exclude_source=EXCLUDED_SOURCE)
        blocks.append(section_block(title, rows, section_key=key,
                                    empty_note=f"No {title.lower()} decoded yet — decoder lands in the next phase."))
    blocks.append(section_block("LATEST (50)", latest, section_key="latest",
                                empty_note="No signals recorded yet."))

    last_scan = store.local_iso(counts["last_seen"]) if counts.get("last_seen") else "never"
    strip = f"""
<div class="strip">
  <div class="stat"><strong>{counts['today_total']}</strong><span>signals today</span></div>
  <div class="stat"><strong>{counts['today_new']}</strong><span>new vs last 7 days</span></div>
  <div class="stat"><strong>{counts['today_decoded']}</strong><span>decoded today</span></div>
  <div class="stat"><strong>{storage_text()}</strong><span>sample storage (cap {store.CAP_BYTES/1024**3:,.1f} GB)</span></div>
  <div class="stat"><strong style="font-size:13px">{esc(last_scan)}</strong><span>last scan (ET)</span></div>
</div>
<div class="muted" style="margin:-4px 0 14px;font-size:12px">
  ADS-B aircraft are not in this list — they live on the <a href="/adsb" style="color:var(--hot);text-decoration:none">ADS-B page</a>.
  Duration counts from when a signal was first heard, so a station active all week shows its lifetime, not today's time.
</div>"""
    return shell("RF Monitor · Signals", strip + "".join(blocks), active="signals", refresh=60)


def _parse_dt(value: str | None) -> str | None:
    if not value:
        return None
    try:
        naive = datetime.fromisoformat(value)
    except ValueError:
        return None
    if naive.tzinfo is None:
        naive = naive.replace(tzinfo=LOCAL_TZ)
    return store.to_iso(naive)


def _filter_args(
    section: str, decode_type: str, klass: str, band: str, fmin: str, fmax: str,
    dt_from: str, dt_to: str, min_snr: str, decoded: str, search: str,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"section": section if section in dict(SECTION_CHOICES) else "all"}
    if section in ("voice", "music", "ism", "satellite"):
        kwargs["decode_type"] = section
    elif section == "text":
        kwargs["decode_types"] = ["aircraft", "aprs", "pager"]
    if decode_type:
        kwargs["decode_type"] = decode_type
    if klass:
        kwargs["klass"] = klass
    if band:
        kwargs["band"] = band
    try:
        if fmin:
            kwargs["freq_min_hz"] = int(float(fmin) * 1e6)
        if fmax:
            kwargs["freq_max_hz"] = int(float(fmax) * 1e6)
    except ValueError:
        pass
    seen_after = _parse_dt(dt_from)
    seen_before = _parse_dt(dt_to)
    if seen_after:
        kwargs["seen_after"] = seen_after
    if seen_before:
        kwargs["seen_before"] = seen_before
    if min_snr:
        try:
            kwargs["min_snr"] = float(min_snr)
        except ValueError:
            pass
    if decoded in ("1", "true", "on", "yes"):
        kwargs["decoded_only"] = True
    if search:
        kwargs["search"] = search
    return kwargs


def render_table(
    section: str, decode_type: str, klass: str, band: str, fmin: str, fmax: str,
    dt_from: str, dt_to: str, min_snr: str, decoded: str, search: str,
    sort: str, direction: str, limit: int, offset: int,
) -> str:
    kwargs = _filter_args(section, decode_type, klass, band, fmin, fmax, dt_from, dt_to, min_snr, decoded, search)
    kwargs["exclude_source"] = EXCLUDED_SOURCE
    rows = store.query_episodes(**kwargs, sort=sort, direction=direction, limit=limit, offset=offset)
    total = store.count_episodes(**kwargs)

    params = {
        "section": section, "decode_type": decode_type, "class": klass, "band": band,
        "fmin": fmin, "fmax": fmax, "from": dt_from, "to": dt_to, "min_snr": min_snr,
        "decoded": decoded, "search": search, "sort": sort, "direction": direction, "limit": limit,
    }
    params = {k: v for k, v in params.items() if v not in (None, "")}

    def option_list(choices: list[tuple[str, str]], current: str) -> str:
        return "".join(
            f'<option value="{esc(v)}"{" selected" if v == current else ""}>{esc(l)}</option>'
            for v, l in choices
        )

    form = f"""
<form class="filters" method="get" action="/signals/table">
  <label>Section<select name="section">{option_list(SECTION_CHOICES, section)}</select></label>
  <label>Type<select name="decode_type">
    <option value=""{" selected" if not decode_type else ""}>any</option>
    {option_list([("voice","voice"),("music","music"),("ism","ism"),("aircraft","aircraft"),
                  ("aprs","aprs"),("pager","pager"),("satellite","satellite")], decode_type)}
  </select></label>
  <label>Class<select name="class">
    <option value=""{" selected" if not klass else ""}>any</option>
    {option_list([("unexplained","unexplained"),("known","known band")], klass)}
  </select></label>
  <label>Band contains<input name="band" value="{esc(band)}" placeholder="FM, Wi-Fi…" size="12" /></label>
  <label>Freq min (MHz)<input type="number" step="0.001" name="fmin" value="{esc(fmin)}" /></label>
  <label>Freq max (MHz)<input type="number" step="0.001" name="fmax" value="{esc(fmax)}" /></label>
  <label>Seen from<input type="datetime-local" name="from" value="{esc(dt_from)}" /></label>
  <label>Seen to<input type="datetime-local" name="to" value="{esc(dt_to)}" /></label>
  <label>Min SNR (dB)<input type="number" step="0.5" name="min_snr" value="{esc(min_snr)}" /></label>
  <label>Search<input name="search" value="{esc(search)}" placeholder="text…" size="14" /></label>
  <label>Rows<select name="limit">{option_list([("25","25"),("50","50"),("100","100"),("250","250")], str(limit))}</select></label>
  <label>&nbsp;<span><input type="checkbox" name="decoded" value="1"{" checked" if decoded in ("1","true","on","yes") else ""} /> decoded only</span></label>
  <input type="hidden" name="sort" value="{esc(sort)}" />
  <input type="hidden" name="direction" value="{esc(direction)}" />
  <button type="submit">Apply</button>
  <a class="btn" href="/signals/export.csv?{urlencode(params)}">Export CSV</a>
</form>"""

    body = f"""
<div class="blockHead"><h2>SIGNALS TABLE</h2>
  <div class="meta">{total} matching · showing {len(rows)}</div></div>
{form}
{rows_table(rows, sort=sort, direction=direction, params=params, limit=limit, offset=offset, total=total)}
"""
    return shell("RF Monitor · Signals table", body, active="signals")


def render_detail(episode: dict[str, Any]) -> str:
    artifacts = {a["kind"]: a for a in episode.get("artifacts", [])}
    features = {}
    if episode.get("features_json"):
        try:
            features = json.loads(episode["features_json"])
        except Exception:
            features = {}

    # peak_snr_db is measured against the spectrum-wide floor; recover that floor
    # exactly from the absolute level so the sentence cannot misstate the baseline.
    global_floor = None
    try:
        global_floor = float(episode["peak_rssi_db"]) - float(episode["peak_snr_db"])
    except (TypeError, ValueError, KeyError):
        pass
    evidence = [
        (f"Averages {snr_text(episode.get('peak_snr_db'))} above the spectrum noise floor "
         f"({global_floor:.1f} dB) across {int(episode.get('n_bins') or 1)} × 500 kHz bins; "
         f"this bin's own 6 h median is {episode.get('floor_db'):.1f} dB.")
        if global_floor is not None and episode.get("floor_db") is not None else "",
        f"Occupied roughly {bw_text(episode.get('bandwidth_hz'))} across {int(episode.get('n_bins') or 1)} × 1 MHz sweep bin(s).",
        f"On-air {float(episode.get('duty') or 0)*100:.0f}% of the observation window "
        f"({'continuous carrier' if float(episode.get('duty') or 0) >= 0.5 else 'intermittent bursts'}).",
        f"Seen {int(episode.get('sightings') or 1)} sweep passes between "
        f"{store.local_iso(episode['first_seen'])} and {store.local_iso(episode['last_seen'])} ET.",
        "Not matched to a known broadcast / Wi-Fi / cellular block." if episode.get("class") == "unexplained"
        else f"Inside a known block: {episode.get('known_emitter') or episode.get('band')}.",
        "Not heard at this frequency in the previous 7 days." if episode.get("is_new")
        else "Heard at this frequency before (not new).",
    ]
    if features.get("traits"):
        evidence.append("Measured traits: " + ", ".join(features["traits"]) + ".")
    if features.get("spectral_flatness") is not None:
        evidence.append(
            f"Spectral flatness {features['spectral_flatness']} "
            f"(higher = more noise/spread, lower = cleaner carrier)."
        )
    evidence_html = "".join(f"<li>{esc(e)}</li>" for e in evidence if e)

    media: list[str] = []
    if "png" in artifacts:
        media.append(
            f'<div class="panel"><h3>Spectrogram sample</h3>'
            f'<img class="spec" src="/signals/artifact/{int(episode["id"])}/spectrum.png" alt="spectrogram" /></div>'
        )
    audio_bits = []
    for mode, label in (("audio-am", "AM"), ("audio-nfm", "NFM"), ("audio-wfm", "WFM")):
        if mode in artifacts:
            name = Path(artifacts[mode]["path"]).name
            audio_bits.append(
                f'<div><strong>{label}</strong> demodulated audio'
                f'<audio controls preload="none" src="/signals/artifact/{int(episode["id"])}/{esc(name)}"></audio></div>'
            )
    if audio_bits:
        media.append('<div class="panel"><h3>Audio sample</h3>' + "".join(audio_bits) +
                     '<div class="muted" style="font-size:12px">Static or noise instead of voice means the signal is '
                     'digital, encrypted, or not an audio modulation.</div></div>')
    if "iq" in artifacts:
        name = Path(artifacts["iq"]["path"]).name
        media.append(f'<div class="panel"><h3>Raw IQ</h3><a class="btn" href="/signals/artifact/{int(episode["id"])}/{esc(name)}">'
                     f'download {esc(name)}</a></div>')
    if not media:
        media.append('<div class="panel"><h3>Sample</h3><div class="empty">No capture stored for this signal yet — '
                     'it has not been picked up by the characterizer.</div></div>')

    if episode.get("decode_status") == "decoded":
        media.append(f'<div class="panel"><h3>Decoded</h3><pre class="text">{esc(episode.get("decoded_summary"))}</pre></div>')

    attempts = episode.get("attempts") or []
    attempt_rows = "".join(
        f"<tr><td>{esc(a.get('decoder'))}</td><td>{esc(a.get('status'))}</td>"
        f"<td>{esc(a.get('detail'))}</td><td class='num'>{esc(store.local_iso(a.get('started_at')))}</td></tr>"
        for a in attempts
    ) or "<tr><td colspan='4' class='muted'>no decode attempts yet</td></tr>"

    labels = episode.get("labels") or []
    label_html = "".join(
        f'<div class="muted">labeled <b>{esc(l["label"])}</b> {esc(l.get("note") or "")} · {esc(store.local_iso(l["created_at"]))}</div>'
        for l in labels
    ) or '<div class="muted">not labeled yet</div>'

    grid = f"""
<div class="detailGrid">
  <div class="card"><span>Frequency</span><strong>{mhz(episode['freq_hz'])} MHz</strong></div>
  <div class="card"><span>Strength (in-band avg)</span><strong>{snr_text(episode.get('peak_snr_db'))}</strong></div>
  <div class="card"><span>Bandwidth</span><strong>{bw_text(episode.get('bandwidth_hz'))}</strong></div>
  <div class="card"><span>Score</span><strong>{float(episode.get('score') or 0):.0f}/100</strong></div>
  <div class="card"><span>On-air</span><strong>{float(episode.get('duty') or 0)*100:.0f}%</strong></div>
  <div class="card"><span>Sightings</span><strong>{int(episode.get('sightings') or 1)}</strong></div>
  <div class="card"><span>Class</span><strong>{esc(episode.get('class'))}</strong></div>
  <div class="card"><span>First seen</span><strong style="font-size:12px">{esc(store.local_iso(episode['first_seen']))}</strong></div>
  <div class="card"><span>Last seen</span><strong style="font-size:12px">{esc(store.local_iso(episode['last_seen']))}</strong></div>
</div>"""

    verdict = f"""
<div class="panel"><h3>What I think it is</h3>
  <div style="font-size:17px;margin-bottom:6px">{esc(episode.get('likely') or 'unclassified RF energy')}
    <span class="pill">{esc(episode.get('confidence') or 'low')}</span>
    {f'<span class="pill new">NEW</span>' if episode.get('is_new') else ''}
  </div>
  <div class="muted" style="font-size:12.5px">Band: {esc(episode.get('band') or 'unmatched')} · 
    {esc(episode.get('known_emitter') or 'not a known emitter')}</div>
</div>
<div class="panel"><h3>Why this was flagged</h3><ul class="evidence">{evidence_html}</ul></div>"""

    label_form = f"""
<div class="panel"><h3>Label this signal</h3>
  <form method="post" action="/signals/{int(episode['id'])}/label">
    <div class="labelRow">
      <select name="label">
        <option value="known">known emitter — stop reporting it</option>
        <option value="notable">notable — keep, worth watching</option>
        <option value="ignore">ignore — suppress this frequency</option>
      </select>
      <input name="note" placeholder="note (e.g. neighbour's weather station)" size="34" />
      <button type="submit">Save label</button>
    </div>
  </form>{label_html}
</div>"""

    already = rf_watchlist.entry_for_freq(int(episode["freq_hz"]))
    if already:
        already_html = (f'<span class="muted" style="font-size:12.5px">already on the watchlist · '
                        f'radio {esc(already.get("radio") or "unassigned")} · '
                        f'{"enabled" if already.get("enabled") else "disabled"}</span>')
    else:
        already_html = ""
    promote_panel = f"""
<div class="panel"><h3>Watch this frequency?</h3>
  <div class="muted" style="font-size:12.5px">Add {esc(mhz(episode.get('freq_hz')))} to the watchlist and give it a parked radio if one is free. A frequency with no free radio is still recorded, so nothing is lost.</div>
  <div class="labelRow"><a class="btn" href="/watchlist/promote?episode_id={int(episode['id'])}">Add to watchlist</a>{already_html}</div>
</div>"""

    body = f"""
<div class="blockHead"><h2>SIGNAL #{int(episode['id'])}</h2>
  <div class="meta"><a href="/signals/table?section=all" style="color:var(--hot);text-decoration:none">← back to all signals</a></div></div>
{grid}{verdict}{''.join(media)}
<div class="panel"><h3>Decode attempts</h3>
  <table><thead><tr><th>Decoder</th><th>Status</th><th>Detail</th><th>When</th></tr></thead>
  <tbody>{attempt_rows}</tbody></table>
</div>
{promote_panel}
{label_form}"""
    return shell(f"RF Monitor · signal #{episode['id']}", body, active="signals")


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@router.get("/", response_class=HTMLResponse)
def dashboard() -> str:
    return render_dashboard()


@router.get("/signals", response_class=HTMLResponse)
def signals_root() -> RedirectResponse:
    return RedirectResponse("/", status_code=307)


@router.get("/signals/table", response_class=HTMLResponse)
def signals_table(
    section: str = Query("all"),
    decode_type: str = Query(""),
    klass: str = Query("", alias="class"),
    band: str = Query(""),
    fmin: str = Query(""),
    fmax: str = Query(""),
    dt_from: str = Query("", alias="from"),
    dt_to: str = Query("", alias="to"),
    min_snr: str = Query(""),
    decoded: str = Query(""),
    search: str = Query(""),
    sort: str = Query("time"),
    direction: str = Query("desc"),
    limit: int = Query(50, ge=5, le=250),
    offset: int = Query(0, ge=0),
) -> str:
    return render_table(section, decode_type, klass, band, fmin, fmax, dt_from, dt_to,
                        min_snr, decoded, search, sort, direction, limit, offset)


@router.get("/signals/export.csv")
def signals_export(
    section: str = Query("all"),
    decode_type: str = Query(""),
    klass: str = Query("", alias="class"),
    band: str = Query(""),
    fmin: str = Query(""),
    fmax: str = Query(""),
    dt_from: str = Query("", alias="from"),
    dt_to: str = Query("", alias="to"),
    min_snr: str = Query(""),
    decoded: str = Query(""),
    search: str = Query(""),
    sort: str = Query("time"),
    direction: str = Query("desc"),
) -> Response:
    kwargs = _filter_args(section, decode_type, klass, band, fmin, fmax, dt_from, dt_to, min_snr, decoded, search)
    kwargs["exclude_source"] = EXCLUDED_SOURCE
    rows = store.query_episodes(**kwargs, sort=sort, direction=direction, limit=250, offset=0)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "first_seen_utc", "last_seen_utc", "freq_mhz", "bandwidth_hz",
                     "peak_snr_db", "floor_db", "duty", "class", "likely", "confidence",
                     "decode_type", "decode_status", "decoded_summary", "sightings", "score", "is_new"])
    for row in rows:
        writer.writerow([
            row["id"], row["first_seen"], row["last_seen"], f"{row['freq_hz']/1e6:.6f}",
            row["bandwidth_hz"], row["peak_snr_db"], row["floor_db"], row["duty"], row["class"],
            row["likely"], row["confidence"], row["decode_type"], row["decode_status"],
            row["decoded_summary"], row["sightings"], row["score"], row["is_new"],
        ])
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="rf-signals.csv"'},
    )


@router.get("/signals/artifact/{episode_id}/{name}")
def signals_artifact(episode_id: int, name: str) -> FileResponse:
    base = (store.ARTIFACT_DIR / str(int(episode_id))).resolve()
    target = (base / Path(name).name).resolve()
    if base not in target.parents or not target.is_file():
        raise HTTPException(404, "artifact not found")
    return FileResponse(target)


@router.get("/signals/{episode_id}", response_class=HTMLResponse)
def signals_detail(episode_id: int) -> HTMLResponse:
    episode = store.get_episode(int(episode_id))
    if episode is None:
        raise HTTPException(404, "signal not found")
    return HTMLResponse(render_detail(episode))


@router.post("/signals/{episode_id}/label")
def signals_label(episode_id: int, request: Request, label: str = Query("known"), note: str = Query("")) -> RedirectResponse:
    if label not in ("known", "notable", "ignore"):
        raise HTTPException(400, "unknown label")
    store.add_label(int(episode_id), label, note or None)
    return RedirectResponse(f"/signals/{int(episode_id)}", status_code=303)


# --------------------------------------------------------------------------- #
# watchlist
# --------------------------------------------------------------------------- #
def render_watchlist(message: str = "") -> str:
    rf_watchlist.seed_defaults()
    entries = rf_watchlist.list_entries()
    radios = rf_watchlist.radio_status()

    radio_rows = "".join(
        f"<tr><td>{esc(r['name'])}</td><td>{esc(r['kind'])}</td><td>{esc(r['duty'])}</td>"
        f"<td>{esc(((r.get('assigned') or {}).get('label')) or '—')}</td>"
        f"<td>{'free' if r['free'] else ('in use' if r.get('assigned') else 'not a parked slot')}</td></tr>"
        for r in radios
    )
    if entries:
        entry_rows = "".join(
            f"<tr><td>{e['id']}</td><td>{esc(mhz(e['freq_hz']))}</td><td>{esc(e['label'] or '')}</td>"
            f"<td>{esc(e['decoder'] or '')}</td><td>{esc(e['mode'])}</td>"
            f"<td>{esc(e['radio'] or 'unassigned')}</td><td>{'yes' if e['enabled'] else 'no'}</td>"
            f"<td class=\"muted\">{esc((e['notes'] or '')[:80])}</td>"
            f"<td><a class=\"btn\" href=\"/watchlist/{e['id']}/toggle\">{'disable' if e['enabled'] else 'enable'}</a> "
            f"<a class=\"btn\" href=\"/watchlist/{e['id']}/remove\">remove</a></td></tr>"
            for e in entries
        )
        entry_table = (
            "<table><thead><tr><th>#</th><th>Frequency</th><th>Label</th><th>Decoder</th><th>Mode</th>"
            "<th>Radio</th><th>On</th><th>Notes</th><th></th></tr></thead>"
            f"<tbody>{entry_rows}</tbody></table>"
        )
    else:
        entry_table = ('<div class="muted">Nothing on the watchlist yet — open any signal and use '
                       '“Add to watchlist”.</div>')

    message_html = f'<div class="panel"><b>{esc(message)}</b></div>' if message else ""
    body = f"""
<div class="blockHead"><h2>WATCHLIST</h2><div class="meta">{len(entries)} entries</div></div>
{message_html}
<div class="panel"><h3>Frequencies to listen to</h3>{entry_table}</div>
<div class="panel"><h3>Add a frequency by hand</h3>
  <form method="get" action="/watchlist/add" class="labelRow">
    <input name="freq_mhz" placeholder="frequency in MHz, e.g. 144.390" size="26" />
    <input name="label" placeholder="label (optional)" size="30" />
    <button type="submit">Add</button>
  </form>
</div>
<div class="panel"><h3>Radios</h3>
  <table><thead><tr><th>Radio</th><th>Kind</th><th>Duty</th><th>Assigned to</th><th>State</th></tr></thead>
  <tbody>{radio_rows}</tbody></table>
  <div class="muted" style="font-size:12.5px">A parked entry needs a radio with duty <i>parked</i>. When none is free the entry is still recorded, with no radio — intent kept, lane not pretended.</div>
</div>"""
    return shell("RF Monitor · watchlist", body, active="watchlist")


@router.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(message: str = Query("")) -> str:
    return render_watchlist(message)


@router.get("/watchlist/promote")
def watchlist_promote(episode_id: int = Query(...)) -> RedirectResponse:
    try:
        entry = rf_watchlist.promote_episode(int(episode_id))
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    note = entry.get("radio") or "no free radio yet"
    return RedirectResponse("/watchlist?" + urlencode({"message": f"{entry.get('label')} → {note}"}), status_code=303)


@router.get("/watchlist/add")
def watchlist_add(freq_mhz: str = Query(...), label: str = Query("")) -> RedirectResponse:
    try:
        freq_hz = int(round(float(freq_mhz) * 1_000_000))
    except ValueError:
        raise HTTPException(400, "freq_mhz must be a number")
    if not 1_000_000 <= freq_hz <= 6_000_000_000:
        raise HTTPException(400, "frequency outside the radio's range")
    entry = rf_watchlist.add_entry(freq_hz, label=label.strip() or None, origin="manual")
    note = entry.get("radio") or "no free radio yet"
    return RedirectResponse("/watchlist?" + urlencode({"message": f"{entry.get('label')} → {note}"}), status_code=303)


@router.get("/watchlist/{entry_id}/toggle")
def watchlist_toggle(entry_id: int) -> RedirectResponse:
    entry = rf_watchlist.toggle_entry(int(entry_id))
    if entry is None:
        raise HTTPException(404, "no such watchlist entry")
    return RedirectResponse("/watchlist?" + urlencode(
        {"message": f"{entry.get('label')} now {'enabled' if entry['enabled'] else 'disabled'}"}), status_code=303)


@router.get("/watchlist/{entry_id}/remove")
def watchlist_remove(entry_id: int) -> RedirectResponse:
    entry = rf_watchlist.get_entry(int(entry_id))
    if entry is None:
        raise HTTPException(404, "no such watchlist entry")
    rf_watchlist.remove_entry(int(entry_id))
    return RedirectResponse("/watchlist?" + urlencode({"message": f"removed {entry.get('label')}"}), status_code=303)


# --------------------------------------------------------------------------- #
# ADS-B page: aircraft on a radar plot, from dump1090's own JSON
# --------------------------------------------------------------------------- #
ADSB_JSON = Path(os.environ.get("RF_ADSB_JSON", "/home/cmilkosk/rf-monitor/signals/adsb/aircraft.json"))
SITE_CONFIG = Path(os.environ.get("RF_SITE_CONFIG", str(Path.home() / ".config" / "rf-site.json")))


def site_coords() -> dict[str, float] | None:
    """Receiver position, kept in a 0600 file outside the repo (never committed)."""
    try:
        data = json.loads(SITE_CONFIG.read_text())
        return {"latitude": float(data["latitude"]), "longitude": float(data["longitude"])}
    except Exception:
        return None


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlambda = math.radians(lon2 - lon1)
    y = math.sin(dlambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlambda)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0088
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def adsb_snapshot() -> dict[str, Any]:
    site = site_coords()
    try:
        payload = json.loads(ADSB_JSON.read_text())
    except Exception as exc:
        return {"ok": False, "error": f"aircraft.json unreadable: {exc}", "aircraft": []}
    aircraft = []
    for entry in payload.get("aircraft", []):
        if not isinstance(entry, dict):
            continue
        row = {
            "hex": entry.get("hex"),
            "flight": (entry.get("flight") or "").strip(),
            "altitude": entry.get("altitude") or entry.get("alt_baro"),
            # readsb emits gs/r/t, dump1090-mutability emitted speed/ground_speed
            "ground_speed": entry.get("ground_speed") or entry.get("gs"),
            "registration": entry.get("r"),
            "type": entry.get("t"),
            "track": entry.get("track"),
            "squawk": entry.get("squawk"),
            "seen": entry.get("seen"),
            "lat": entry.get("lat"),
            "lon": entry.get("lon"),
        }
        if site and isinstance(row["lat"], (int, float)) and isinstance(row["lon"], (int, float)):
            row["distance_km"] = round(_distance_km(site["latitude"], site["longitude"], row["lat"], row["lon"]), 1)
            row["bearing_deg"] = round(_bearing_deg(site["latitude"], site["longitude"], row["lat"], row["lon"]), 1)
        aircraft.append(row)
    positioned = [a for a in aircraft if a.get("distance_km") is not None]
    return {
        "ok": True,
        "site": bool(site),
        "generated": utcnow_stamp(),
        "messages": payload.get("messages"),
        "count": len(aircraft),
        "positioned": len(positioned),
        "max_range_km": max((a["distance_km"] for a in positioned), default=0.0),
        "aircraft": aircraft,
    }


def utcnow_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@router.get("/api/adsb/live")
def api_adsb_live() -> JSONResponse:
    return JSONResponse(adsb_snapshot())


def render_adsb() -> str:
    body = """
<div class="blockHead"><h2>ADS-B · 1090 MHz</h2><div class="meta" id="stamp">loading…</div></div>
<div class="panel" id="notice" style="display:none"></div>
<div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start">
  <div class="panel" style="flex:0 0 auto">
    <canvas id="radar" width="720" height="720" style="max-width:100%;height:auto"></canvas>
    <div class="muted" style="font-size:12.5px;margin-top:6px">North up · rings at 25/50/100/200 km · dot size = altitude · label = callsign</div>
  </div>
  <div class="panel" style="flex:1 1 380px;min-width:320px">
    <h3>Aircraft</h3>
    <div style="max-height:660px;overflow:auto">
      <table><thead><tr><th>Flight</th><th>Reg</th><th>Type</th><th>Alt</th><th>GS</th><th>Dist</th><th>Brg</th></tr></thead>
      <tbody id="rows"></tbody></table>
    </div>
  </div>
</div>
<script>
const RINGS = [25, 50, 100, 200];
let scaleMax = 100;

function draw(data) {
  const c = document.getElementById('radar');
  const ctx = c.getContext('2d');
  const W = c.width, H = c.height, cx = W / 2, cy = H / 2;
  const cs = getComputedStyle(document.documentElement);
  const fg = (cs.getPropertyValue('--foreground') || '#ddd').trim();
  const muted = (cs.getPropertyValue('--muted-foreground') || '#888').trim();
  const accent = (cs.getPropertyValue('--accent') || '#3fa9f5').trim();
  const border = (cs.getPropertyValue('--border') || '#444').trim();
  ctx.clearRect(0, 0, W, H);

  const positioned = data.aircraft.filter(a => a.distance_km != null);
  const maxSeen = positioned.reduce((m, a) => Math.max(m, a.distance_km), 0);
  scaleMax = Math.max(50, Math.min(200, Math.ceil(maxSeen / 50) * 50));
  const radiusPx = (W / 2) - 34;
  const pxPerKm = radiusPx / scaleMax;

  ctx.strokeStyle = border; ctx.fillStyle = muted;
  ctx.font = '12px system-ui, sans-serif'; ctx.textAlign = 'center';
  RINGS.forEach(km => {
    if (km > scaleMax) return;
    const r = km * pxPerKm;
    ctx.beginPath(); ctx.arc(cx, cy, r, 0, Math.PI * 2); ctx.stroke();
    ctx.fillText(km + ' km', cx, cy - r - 4);
  });
  ctx.beginPath(); ctx.moveTo(cx - radiusPx, cy); ctx.lineTo(cx + radiusPx, cy);
  ctx.moveTo(cx, cy - radiusPx); ctx.lineTo(cx, cy + radiusPx); ctx.stroke();
  ['N','E','S','W'].forEach((label, i) => {
    const ang = (i * Math.PI) / 2 - Math.PI / 2;
    ctx.fillText(label, cx + Math.cos(ang) * (radiusPx + 16), cy + Math.sin(ang) * (radiusPx + 16) + 4);
  });

  positioned.forEach(a => {
    const brg = a.bearing_deg * Math.PI / 180;
    const r = a.distance_km * pxPerKm;
    const x = cx + Math.sin(brg) * r;
    const y = cy - Math.cos(brg) * r;
    const alt = a.altitude || 0;
    const size = alt > 30000 ? 6 : alt > 15000 ? 5 : 4;
    ctx.fillStyle = accent;
    ctx.beginPath(); ctx.arc(x, y, size, 0, Math.PI * 2); ctx.fill();
    if (a.flight) {
      ctx.fillStyle = fg; ctx.textAlign = 'left';
      ctx.fillText(a.flight, x + size + 3, y + 4);
    }
  });

  ctx.fillStyle = fg; ctx.textAlign = 'center';
  ctx.beginPath(); ctx.arc(cx, cy, 3, 0, Math.PI * 2); ctx.fill();

  document.getElementById('stamp').textContent =
    `${data.count} aircraft · ${data.positioned} positioned · max ${data.max_range_km} km · ${data.messages} msgs · ${data.generated}`;
  const rows = positioned.slice().sort((a, b) => a.distance_km - b.distance_km).map(a =>
    `<tr><td>${a.flight || '·'}</td><td class="muted">${a.registration || ''}</td>` +
    `<td class="muted">${a.type || ''}</td>` +
    `<td>${a.altitude != null ? Math.round(a.altitude).toLocaleString() + ' ft' : '·'}</td>` +
    `<td>${a.ground_speed != null ? Math.round(a.ground_speed) + ' kt' : '·'}</td>` +
    `<td>${a.distance_km} km</td><td>${Math.round(a.bearing_deg)}°</td></tr>`).join('');
  document.getElementById('rows').innerHTML = rows || '<tr><td colspan="7" class="muted">no positioned aircraft</td></tr>';

  const notice = document.getElementById('notice');
  if (!data.site) {
    notice.style.display = 'block';
    notice.innerHTML = '<b>Site coordinates not configured</b> — range and bearing need the receiver position. Create <code>~/.config/rf-site.json</code> with latitude/longitude.';
  } else if (data.ok === false) {
    notice.style.display = 'block'; notice.textContent = data.error;
  } else { notice.style.display = 'none'; }
}

async function tick() {
  try {
    const res = await fetch('/api/adsb/live', {cache: 'no-store'});
    draw(await res.json());
  } catch (e) {
    document.getElementById('stamp').textContent = 'feed unavailable';
  }
}
tick(); setInterval(tick, 5000);
</script>"""
    return shell("RF Monitor · ADS-B", body, active="adsb")


@router.get("/adsb/radar", response_class=HTMLResponse)
def adsb_radar_page() -> str:
    return render_adsb()


def render_adsb_map() -> str:
    """tar1090 (upstream, maintained) embedded - map, clickable aircraft, photos.

    Chose to reuse tar1090 rather than grow our own map: it already does aircraft
    selection, a details panel, registration/route lookups and planespotters
    photos, and it reads dump1090-mutability's aircraft.json directly. The radar
    view stays at /adsb/radar as the offline-friendly fallback.
    """
    body = """
<div class="blockHead"><h2>ADS-B · 1090 MHz map</h2>
  <div class="meta">tar1090 · click any aircraft for details and a photo ·
    <a href="/adsb/radar" style="color:var(--hot);text-decoration:none">radar view →</a></div></div>
<iframe id="map" title="tar1090 map" style="width:100%;height:calc(100vh - 150px);min-height:520px;border:1px solid var(--border);border-radius:8px;background:#000"></iframe>
<script>
  // Same origin on purpose: this app serves tar1090 itself on this port. Port 80 is
  // not reachable from the LAN (ufw allows only 22/61208/8099), so pointing at
  // http://<host>/tar1090/ left an empty black frame on every other machine.
  document.getElementById('map').src = '/tar1090/';
</script>"""
    return shell("RF Monitor · ADS-B map", body, active="adsb")


@router.get("/adsb", response_class=HTMLResponse)
def adsb_page() -> str:
    return render_adsb_map()


# --------------------------------------------------------------------------- #
# JSON API
# --------------------------------------------------------------------------- #
@router.get("/api/signals/sections")
def api_sections() -> JSONResponse:
    counts = store.dashboard_counts()
    payload: dict[str, Any] = {
        "counts": counts,
        "storage": {"bytes": store.artifact_bytes(), "cap_bytes": store.CAP_BYTES},
        "strongest": store.query_episodes(section="strongest", limit=10, sort="snr", direction="desc"),
        "latest": store.query_episodes(section="latest", limit=50, sort="time", direction="desc"),
        "sections": {},
    }
    for key, _title, types in TYPE_SECTIONS:
        payload["sections"][key] = store.query_episodes(decode_types=list(types), limit=20,
                                                        sort="time", direction="desc")
    return JSONResponse(payload)


@router.get("/api/signals/health")
def api_health() -> JSONResponse:
    counts = store.dashboard_counts()
    last = counts.get("last_seen")
    stale = True
    if last:
        age = (datetime.now(store.LOCAL_TZ).astimezone(store.LOCAL_TZ) -
               store.parse_iso(last).astimezone(store.LOCAL_TZ)).total_seconds()
        stale = age > 300
    return JSONResponse({
        "ok": not stale,
        "db": str(store.DB_PATH),
        "episodes": counts.get("today_total"),
        "last_seen": last,
        "stale": stale,
        "artifact_bytes": store.artifact_bytes(),
        "cap_bytes": store.CAP_BYTES,
    })


@router.get("/api/signals/{episode_id}")
def api_episode(episode_id: int) -> JSONResponse:
    episode = store.get_episode(int(episode_id))
    if episode is None:
        raise HTTPException(404, "signal not found")
    return JSONResponse(episode)
