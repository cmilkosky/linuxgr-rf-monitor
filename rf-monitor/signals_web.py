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
import os
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

import signals_store as store

router = APIRouter()
LOCAL_TZ = ZoneInfo("America/New_York")

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
    counts = store.dashboard_counts()
    strongest = store.query_episodes(section="strongest", limit=10, sort="snr", direction="desc")
    latest = store.query_episodes(section="latest", limit=50, sort="time", direction="desc")

    blocks = [section_block("STRONGEST SIGNALS (TODAY, BY SNR)", strongest,
                            section_key="strongest",
                            empty_note="Nothing above the noise floor yet today.")]
    for key, title, types in TYPE_SECTIONS:
        rows = store.query_episodes(decode_types=list(types), limit=20, sort="time", direction="desc")
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
    # exactly from the absolute peak so the sentence cannot misstate the baseline.
    global_floor = None
    try:
        global_floor = float(episode["peak_rssi_db"]) - float(episode["peak_snr_db"])
    except (TypeError, ValueError, KeyError):
        pass
    evidence = [
        (f"Peak {snr_text(episode.get('peak_snr_db'))} above the spectrum noise floor "
         f"({global_floor:.1f} dB); this bin's own 6 h median is {episode.get('floor_db'):.1f} dB.")
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
  <div class="card"><span>Peak SNR</span><strong>{snr_text(episode.get('peak_snr_db'))}</strong></div>
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

    body = f"""
<div class="blockHead"><h2>SIGNAL #{int(episode['id'])}</h2>
  <div class="meta"><a href="/signals/table?section=all" style="color:var(--hot);text-decoration:none">← back to all signals</a></div></div>
{grid}{verdict}{''.join(media)}
<div class="panel"><h3>Decode attempts</h3>
  <table><thead><tr><th>Decoder</th><th>Status</th><th>Detail</th><th>When</th></tr></thead>
  <tbody>{attempt_rows}</tbody></table>
</div>
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
