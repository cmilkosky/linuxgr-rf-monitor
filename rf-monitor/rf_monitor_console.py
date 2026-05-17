#!/usr/bin/env python3
from __future__ import annotations

import csv
import io
import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse


CONFIG_PATH = Path(os.environ.get("RF_MONITOR_ENV", "/home/cmilkosk/.config/hackrf-influx.env"))
STATUS_PATH = Path(os.environ.get("RF_MONITOR_STATUS", "/home/cmilkosk/rf-monitor/status.json"))


def load_env(path: Path = CONFIG_PATH) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key] = value.strip().strip('"').strip("'")
    return env


ENV = load_env()
INFLUX_URL = ENV["INFLUX_URL"].rstrip("/")
INFLUX_ORG = ENV["INFLUX_ORG"]
INFLUX_BUCKET = ENV["INFLUX_BUCKET"]
INFLUX_TOKEN = ENV["INFLUX_TOKEN"]

app = FastAPI(title="RF Monitor Console")


def flux_query(query: str) -> list[dict[str, str]]:
    response = requests.post(
        f"{INFLUX_URL}/api/v2/query",
        params={"org": INFLUX_ORG},
        headers={
            "Authorization": f"Token {INFLUX_TOKEN}",
            "Accept": "application/csv",
            "Content-Type": "application/vnd.flux",
        },
        data=query,
        timeout=30,
    )
    if response.status_code >= 400:
        raise HTTPException(response.status_code, response.text)
    rows: list[dict[str, str]] = []
    for row in csv.DictReader(io.StringIO(response.text)):
        if row.get("result") == "_result" and row.get("_value") not in (None, ""):
            rows.append(row)
    return rows


def read_status() -> dict[str, Any]:
    if STATUS_PATH.exists():
        try:
            return json.loads(STATUS_PATH.read_text())
        except json.JSONDecodeError:
            pass
    return {
        "status": "warming",
        "updated_at": None,
        "anomalies_active": 0,
        "latest_anomalies": [],
    }


def flux_duration(value: float, unit: str) -> str:
    if float(value).is_integer():
        return f"{int(value)}{unit}"
    seconds_per_unit = {"h": 3600, "m": 60}[unit]
    seconds = max(1, round(value * seconds_per_unit))
    return f"{seconds}s"


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML


@app.get("/api/status")
def status() -> JSONResponse:
    status_doc = read_status()
    status_doc["console_time"] = datetime.now(timezone.utc).isoformat()
    return JSONResponse(status_doc)


@app.get("/api/health")
def health() -> JSONResponse:
    influx = requests.get(f"{INFLUX_URL}/health", timeout=5).json()
    return JSONResponse({"ok": influx.get("status") == "pass", "influx": influx, "status": read_status()})


@app.get("/api/heatmap")
def heatmap(
    hours: float = Query(3, ge=0.1, le=24),
    freq_step_mhz: int = Query(5, ge=1, le=100),
    time_bucket: str = Query("1m", pattern=r"^\d+[smh]$"),
    freq_min_hz: int | None = Query(None, ge=0),
    freq_max_hz: int | None = Query(None, ge=0),
) -> JSONResponse:
    step_hz = freq_step_mhz * 1_000_000
    range_duration = flux_duration(hours, "h")
    freq_filter = ""
    if freq_min_hz is not None and freq_max_hz is not None and freq_max_hz > freq_min_hz:
        freq_filter = f"\n  |> filter(fn: (r) => int(v: r.frequency_hz) >= {freq_min_hz} and int(v: r.frequency_hz) <= {freq_max_hz})"
    query = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{range_duration})
  |> filter(fn: (r) => r._measurement == "rf_sweep" and r._field == "rssi_db")
  {freq_filter}
  |> aggregateWindow(every: {time_bucket}, fn: max, createEmpty: false)
  |> map(fn: (r) => ({{ r with freq_bin: string(v: int(v: r.frequency_hz) / {step_hz} * {step_hz}) }}))
  |> group(columns: ["_time", "freq_bin"])
  |> max(column: "_value")
  |> group()
  |> keep(columns: ["_time", "freq_bin", "_value"])
'''
    rows = flux_query(query)
    times = sorted({row["_time"] for row in rows})
    freqs = sorted({int(row["freq_bin"]) for row in rows})
    time_index = {value: idx for idx, value in enumerate(times)}
    freq_index = {value: idx for idx, value in enumerate(freqs)}
    values: list[list[float | None]] = [[None for _ in times] for _ in freqs]
    min_v: float | None = None
    max_v: float | None = None
    for row in rows:
        value = float(row["_value"])
        values[freq_index[int(row["freq_bin"])]][time_index[row["_time"]]] = value
        min_v = value if min_v is None else min(min_v, value)
        max_v = value if max_v is None else max(max_v, value)
    return JSONResponse(
        {
            "times": times,
            "frequencies_hz": freqs,
            "frequencies_mhz": [round(freq / 1_000_000, 3) for freq in freqs],
            "values": values,
            "min": min_v,
            "max": max_v,
            "points": len(rows),
            "freq_min_hz": freq_min_hz,
            "freq_max_hz": freq_max_hz,
        }
    )


@app.get("/api/frequency/{frequency_hz}")
def frequency_detail(
    frequency_hz: int,
    hours: float = Query(6, ge=0.1, le=72),
    span_mhz: int = Query(2, ge=0, le=25),
) -> JSONResponse:
    low = frequency_hz - (span_mhz * 1_000_000)
    high = frequency_hz + (span_mhz * 1_000_000)
    range_duration = flux_duration(hours, "h")
    query = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{range_duration})
  |> filter(fn: (r) => r._measurement == "rf_sweep" and r._field == "rssi_db")
  |> filter(fn: (r) => int(v: r.frequency_hz) >= {low} and int(v: r.frequency_hz) <= {high})
  |> aggregateWindow(every: 1m, fn: max, createEmpty: false)
  |> group()
  |> keep(columns: ["_time", "_value", "frequency_hz"])
'''
    rows = flux_query(query)
    series: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        freq = row["frequency_hz"]
        series.setdefault(freq, []).append({"time": row["_time"], "rssi_db": float(row["_value"])})
    for points in series.values():
        points.sort(key=lambda item: item["time"])
    latest = [
        {"frequency_hz": int(freq), "frequency_mhz": int(freq) / 1_000_000, "rssi_db": points[-1]["rssi_db"]}
        for freq, points in series.items()
        if points
    ]
    latest.sort(key=lambda item: item["rssi_db"], reverse=True)
    return JSONResponse({"frequency_hz": frequency_hz, "series": series, "latest_neighbors": latest[:20]})


@app.get("/api/anomalies")
def anomalies(limit: int = Query(50, ge=1, le=200)) -> JSONResponse:
    status_doc = read_status()
    items = status_doc.get("latest_anomalies", [])[:limit]
    return JSONResponse({"items": items, "status": status_doc})


@app.get("/api/top")
def top(hours: float = Query(1, ge=0.1, le=24), limit: int = Query(25, ge=1, le=200)) -> JSONResponse:
    range_duration = flux_duration(hours, "h")
    query = f'''
from(bucket: "{INFLUX_BUCKET}")
  |> range(start: -{range_duration})
  |> filter(fn: (r) => r._measurement == "rf_sweep" and r._field == "rssi_db")
  |> group(columns: ["frequency_hz"])
  |> max(column: "_value")
  |> group()
  |> sort(columns: ["_value"], desc: true)
  |> limit(n: {limit})
  |> keep(columns: ["frequency_hz", "_value", "_time"])
'''
    rows = flux_query(query)
    return JSONResponse(
        {
            "items": [
                {
                    "frequency_hz": int(row["frequency_hz"]),
                    "frequency_mhz": int(row["frequency_hz"]) / 1_000_000,
                    "rssi_db": float(row["_value"]),
                    "time": row["_time"],
                }
                for row in rows
            ]
        }
    )


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>RF Monitor</title>
  <style>
    :root { color-scheme: dark; --bg:#101418; --panel:#171d22; --line:#2b353d; --text:#e7edf2; --muted:#9caab5; --hot:#ffca62; --accent:#63d2ff; --bad:#ff6f91; }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; background: var(--bg); color: var(--text); }
    header { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:18px 22px; border-bottom:1px solid var(--line); background:#12181d; position:sticky; top:0; z-index:3; }
    h1 { margin:0; font-size:20px; font-weight:650; }
    main { display:grid; grid-template-columns: 1fr 360px; min-height: calc(100vh - 64px); }
    section { padding:16px; }
    aside { border-left:1px solid var(--line); background:#12181d; padding:16px; overflow:auto; }
    .toolbar { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:12px; }
    select, button { background:#202a31; color:var(--text); border:1px solid #35424c; border-radius:6px; padding:7px 10px; font:inherit; }
    button { cursor:pointer; }
    button:hover { border-color:var(--accent); }
    .panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; }
    #heatmapWrap { height: calc(100vh - 150px); min-height: 520px; padding:10px; }
    canvas { display:block; width:100%; height:100%; }
    .stats { display:grid; grid-template-columns: repeat(3, 1fr); gap:8px; margin-bottom:12px; }
    .stat { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px; }
    .stat strong { display:block; font-size:20px; }
    .stat span { color:var(--muted); font-size:12px; }
    .list { display:flex; flex-direction:column; gap:8px; }
    .item { border:1px solid var(--line); background:var(--panel); border-radius:8px; padding:10px; cursor:pointer; }
    .item:hover { border-color:var(--accent); }
    .item b { color:var(--hot); }
    .muted { color:var(--muted); }
    .readout { color: var(--accent); font-weight: 600; min-width: 300px; }
    #zoomState { color: var(--hot); }
    #detailCanvas { height:220px; margin-top:10px; }
    @media (max-width: 1000px) { main { grid-template-columns: 1fr; } aside { border-left:0; border-top:1px solid var(--line); } #heatmapWrap { height: 60vh; } }
  </style>
</head>
<body>
<header>
  <h1>RF Monitor</h1>
  <div class="muted" id="statusText">Loading</div>
</header>
<main>
  <section>
    <div class="toolbar">
      <label>Range <select id="hours"><option>1</option><option selected>3</option><option>6</option><option>12</option><option>24</option></select> h</label>
      <label>Frequency bin <select id="freqStep"><option>1</option><option selected>5</option><option>10</option><option>25</option><option>50</option></select> MHz</label>
      <button id="refresh">Refresh</button>
      <button id="resetZoom">Reset Zoom</button>
      <span class="muted" id="legend">Color = RSSI strength</span>
      <span id="hoverReadout" class="readout"></span>
      <span id="zoomState"></span>
    </div>
    <div id="heatmapWrap" class="panel"><canvas id="heatmap"></canvas></div>
  </section>
  <aside>
    <div class="stats">
      <div class="stat"><strong id="sweeps">--</strong><span>points loaded</span></div>
      <div class="stat"><strong id="activeAnoms">--</strong><span>active anomalies</span></div>
      <div class="stat"><strong id="maxRssi">--</strong><span>max RSSI</span></div>
    </div>
    <h3>Selected</h3>
    <div id="selected" class="muted">Click a heatmap block.</div>
    <canvas id="detailCanvas" class="panel"></canvas>
    <h3>Anomalies</h3>
    <div id="anomalies" class="list"></div>
    <h3>Top Signals</h3>
    <div id="top" class="list"></div>
  </aside>
</main>
<script>
const heat = document.getElementById('heatmap');
const detail = document.getElementById('detailCanvas');
let heatData = null;
let hoverCell = null;
let zoomMinHz = null;
let zoomMaxHz = null;

function color(v, min, max) {
  if (v === null || Number.isNaN(v)) return '#11181d';
  const t = Math.max(0, Math.min(1, (v - min) / Math.max(1, max - min)));
  const stops = [[22,27,32],[35,84,106],[73,163,117],[255,202,98],[255,111,145]];
  const p = t * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(p));
  const f = p - i;
  const c = stops[i].map((a,j)=>Math.round(a + (stops[i+1][j]-a)*f));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}

function resizeCanvas(canvas) {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * dpr));
  canvas.height = Math.max(1, Math.floor(rect.height * dpr));
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr,0,0,dpr,0,0);
  return {ctx, width: rect.width, height: rect.height};
}

function drawHeatmap() {
  const {ctx, width, height} = resizeCanvas(heat);
  ctx.clearRect(0,0,width,height);
  if (!heatData) return;
  const left = 70, bottom = 28, top = 8, right = 10;
  const rows = heatData.frequencies_hz.length;
  const cols = heatData.times.length;
  const cellW = (width-left-right) / Math.max(1, cols);
  const cellH = (height-top-bottom) / Math.max(1, rows);
  for (let r=0; r<rows; r++) {
    for (let c=0; c<cols; c++) {
      ctx.fillStyle = color(heatData.values[r][c], heatData.min ?? -90, heatData.max ?? -20);
      ctx.fillRect(left + c*cellW, top + (rows-1-r)*cellH, Math.ceil(cellW)+0.5, Math.ceil(cellH)+0.5);
    }
  }
  ctx.fillStyle = '#9caab5';
  ctx.font = '12px system-ui';
  ctx.fillText(`${heatData.frequencies_mhz[0] ?? ''} MHz`, 8, height-bottom);
  ctx.fillText(`${heatData.frequencies_mhz[rows-1] ?? ''} MHz`, 8, top+12);
  ctx.fillText(new Date(heatData.times[0] || Date.now()).toLocaleTimeString(), left, height-8);
  ctx.fillText(new Date(heatData.times[cols-1] || Date.now()).toLocaleTimeString(), Math.max(left, width-120), height-8);
  if (hoverCell) {
    ctx.strokeStyle = '#63d2ff';
    ctx.lineWidth = 1;
    ctx.strokeRect(left + hoverCell.col*cellW, top + (rows-1-hoverCell.row)*cellH, Math.max(1, cellW), Math.max(1, cellH));
  }
}

function heatCell(event) {
  if (!heatData) return null;
  const rect = heat.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  const left = 70, bottom = 28, top = 8, right = 10;
  const rows = heatData.frequencies_hz.length;
  const cols = heatData.times.length;
  const cellW = (rect.width-left-right) / Math.max(1, cols);
  const cellH = (rect.height-top-bottom) / Math.max(1, rows);
  const col = Math.floor((x-left)/cellW);
  const rowInv = Math.floor((y-top)/cellH);
  const row = rows - 1 - rowInv;
  if (row < 0 || row >= rows || col < 0 || col >= cols) return null;
  return {row, col, freq: heatData.frequencies_hz[row], time: heatData.times[col], value: heatData.values[row][col]};
}

function zoomAround(freq) {
  const step = Number(document.getElementById('freqStep').value) * 1_000_000;
  const visibleSpan = Math.max(50_000_000, step * 24);
  zoomMinHz = Math.max(0, Math.floor((freq - visibleSpan / 2) / step) * step);
  zoomMaxHz = Math.ceil((freq + visibleSpan / 2) / step) * step;
  document.getElementById('zoomState').textContent = `Zoom ${(zoomMinHz/1e6).toFixed(0)}-${(zoomMaxHz/1e6).toFixed(0)} MHz`;
}

function resetZoom() {
  zoomMinHz = null;
  zoomMaxHz = null;
  hoverCell = null;
  document.getElementById('zoomState').textContent = '';
  document.getElementById('hoverReadout').textContent = '';
  load();
}

async function selectFrequency(freq) {
  document.getElementById('selected').innerHTML = `<b>${(freq/1e6).toFixed(3)} MHz</b><br><span class="muted">Loading detail...</span>`;
  const data = await fetch(`/api/frequency/${freq}?hours=6&span_mhz=2`).then(r=>r.json());
  document.getElementById('selected').innerHTML = `<b>${(freq/1e6).toFixed(3)} MHz</b><br><span class="muted">Nearest bins: ${Object.keys(data.series).length}</span>`;
  drawDetail(data);
}

function drawDetail(data) {
  const {ctx, width, height} = resizeCanvas(detail);
  ctx.clearRect(0,0,width,height);
  const keys = Object.keys(data.series);
  if (!keys.length) return;
  const colors = ['#63d2ff','#ffca62','#7bd88f','#ff6f91','#c792ea'];
  let min=-100, max=-20;
  keys.slice(0,5).forEach((k,idx)=>{
    const pts = data.series[k];
    ctx.strokeStyle = colors[idx % colors.length];
    ctx.beginPath();
    pts.forEach((p,i)=>{
      const x = 8 + i * (width-16) / Math.max(1, pts.length-1);
      const y = 8 + (1 - ((p.rssi_db-min)/(max-min))) * (height-16);
      if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
    });
    ctx.stroke();
  });
}

async function load() {
  document.getElementById('statusText').textContent = 'Loading heatmap';
  const hours = document.getElementById('hours').value;
  const step = document.getElementById('freqStep').value;
  const zoomParams = zoomMinHz !== null && zoomMaxHz !== null ? `&freq_min_hz=${zoomMinHz}&freq_max_hz=${zoomMaxHz}` : '';
  heatData = await fetch(`/api/heatmap?hours=${hours}&freq_step_mhz=${step}${zoomParams}`).then(r=>r.json());
  document.getElementById('sweeps').textContent = heatData.points ?? 0;
  document.getElementById('maxRssi').textContent = heatData.max == null ? '--' : `${heatData.max.toFixed(1)} dB`;
  drawHeatmap();
  const status = await fetch('/api/status').then(r=>r.json());
  document.getElementById('activeAnoms').textContent = status.anomalies_active ?? 0;
  document.getElementById('statusText').textContent = `Last update ${new Date(status.updated_at || Date.now()).toLocaleString()}`;
  const anoms = await fetch('/api/anomalies?limit=12').then(r=>r.json());
  document.getElementById('anomalies').innerHTML = (anoms.items || []).map(a =>
    `<div class="item" onclick="selectFrequency(${a.frequency_hz})"><b>${(a.frequency_hz/1e6).toFixed(3)} MHz</b><br>${a.delta_db.toFixed(1)} dB over baseline · ${a.rssi_db.toFixed(1)} dB<br><span class="muted">${a.detected_at}</span></div>`
  ).join('') || '<div class="muted">No anomalies yet. Baseline is warming up.</div>';
  const top = await fetch('/api/top?hours=1&limit=12').then(r=>r.json());
  document.getElementById('top').innerHTML = (top.items || []).map(a =>
    `<div class="item" onclick="selectFrequency(${a.frequency_hz})"><b>${a.frequency_mhz.toFixed(3)} MHz</b><br>${a.rssi_db.toFixed(1)} dB<br><span class="muted">${a.time}</span></div>`
  ).join('');
}

heat.addEventListener('click', e => {
  const cell = heatCell(e);
  if (cell) {
    zoomAround(cell.freq);
    selectFrequency(cell.freq);
    load();
  }
});
heat.addEventListener('mousemove', e => {
  hoverCell = heatCell(e);
  if (!hoverCell) {
    document.getElementById('hoverReadout').textContent = '';
    drawHeatmap();
    return;
  }
  const rssi = hoverCell.value === null ? 'no data' : `${hoverCell.value.toFixed(1)} dB`;
  document.getElementById('hoverReadout').textContent = `${(hoverCell.freq/1e6).toFixed(3)} MHz · ${rssi} · ${new Date(hoverCell.time).toLocaleTimeString()}`;
  drawHeatmap();
});
heat.addEventListener('mouseleave', () => {
  hoverCell = null;
  document.getElementById('hoverReadout').textContent = '';
  drawHeatmap();
});
window.addEventListener('resize', () => { drawHeatmap(); if (heatData) drawHeatmap(); });
document.getElementById('refresh').addEventListener('click', load);
document.getElementById('resetZoom').addEventListener('click', resetZoom);
document.getElementById('hours').addEventListener('change', load);
document.getElementById('freqStep').addEventListener('change', () => { resetZoom(); });
load();
setInterval(load, 60000);
</script>
</body>
</html>
"""
