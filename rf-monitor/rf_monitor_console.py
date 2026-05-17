#!/usr/bin/env python3
from __future__ import annotations

import csv
import io
import json
import math
import os
import subprocess
import statistics
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse


CONFIG_PATH = Path(os.environ.get("RF_MONITOR_ENV", "/home/cmilkosk/.config/hackrf-influx.env"))
STATUS_PATH = Path(os.environ.get("RF_MONITOR_STATUS", "/home/cmilkosk/rf-monitor/status.json"))
CAPTURE_DIR = Path(os.environ.get("RF_CAPTURE_DIR", "/home/cmilkosk/rf-monitor/captures"))
DEEP_SCAN_DIR = Path(os.environ.get("RF_DEEP_SCAN_DIR", "/home/cmilkosk/rf-monitor/deep-scans"))
CAPTURE_LOCK = threading.Lock()
DEEP_SCAN_LOCK = threading.Lock()


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
    try:
        response = requests.post(
            f"{INFLUX_URL}/api/v2/query",
            params={"org": INFLUX_ORG},
            headers={
                "Authorization": f"Token {INFLUX_TOKEN}",
                "Accept": "application/csv",
                "Content-Type": "application/vnd.flux",
            },
            data=query,
            timeout=75,
        )
    except requests.Timeout as exc:
        raise HTTPException(504, "InfluxDB query timed out; try a shorter range or wider frequency bin") from exc
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


def parse_bucket_seconds(bucket: str) -> int:
    unit = bucket[-1]
    value = int(bucket[:-1])
    return value * {"s": 1, "m": 60, "h": 3600}[unit]


def bucket_time_iso(value: str, bucket_seconds: int) -> str:
    if bucket_seconds == 60:
        return f"{value[:16]}:00Z"
    clean = value[:-1] if value.endswith("Z") else value
    if "." in clean:
        head, tail = clean.split(".", 1)
        tail = tail.split("+", 1)[0].split("-", 1)[0]
        clean = f"{head}.{tail[:6].ljust(6, '0')}"
    if "+" not in clean and not clean.endswith("+00:00"):
        clean = f"{clean}+00:00"
    dt = datetime.fromisoformat(clean)
    epoch = int(dt.timestamp())
    bucketed = epoch - (epoch % bucket_seconds)
    return datetime.fromtimestamp(bucketed, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def run_command(command: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, capture_output=True, timeout=timeout, check=False)


def systemctl(action: str, service: str) -> subprocess.CompletedProcess[str]:
    return run_command(["sudo", "systemctl", action, service], timeout=90)


def service_state(service: str) -> str:
    result = run_command(["systemctl", "is-active", service], timeout=10)
    return result.stdout.strip() or "unknown"


def service_is_active(service: str) -> bool:
    return service_state(service) == "active"


def wait_for_service_state(service: str, desired: set[str], timeout_seconds: int) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if service_state(service) in desired:
            return True
        time.sleep(0.5)
    return False


def pause_sweep_service() -> bool:
    if not service_is_active("hackrf-influx.service"):
        return False
    run_command(["sudo", "systemctl", "stop", "--no-block", "hackrf-influx.service"], timeout=10)
    if wait_for_service_state("hackrf-influx.service", {"inactive", "failed", "unknown"}, 45):
        return True
    run_command(["sudo", "systemctl", "kill", "hackrf-influx.service"], timeout=10)
    wait_for_service_state("hackrf-influx.service", {"inactive", "failed", "unknown"}, 10)
    run_command(["sudo", "systemctl", "reset-failed", "hackrf-influx.service"], timeout=10)
    return True


def resume_sweep_service() -> None:
    run_command(["sudo", "systemctl", "start", "--no-block", "hackrf-influx.service"], timeout=10)
    wait_for_service_state("hackrf-influx.service", {"active"}, 30)


def parse_hackrf_sweep_row(line: str) -> dict[str, Any] | None:
    parts = [part.strip() for part in line.split(",")]
    if len(parts) < 7:
        return None
    try:
        low_hz = int(float(parts[2]))
        high_hz = int(float(parts[3]))
        bin_width_hz = int(float(parts[4]))
        values = [float(value) for value in parts[6:] if value]
    except ValueError:
        return None
    if not values or high_hz <= low_hz:
        return None
    actual_width_hz = (high_hz - low_hz) / len(values)
    width_hz = bin_width_hz if bin_width_hz > 0 else actual_width_hz
    points = []
    for idx, value in enumerate(values):
        freq_hz = low_hz + (idx + 0.5) * actual_width_hz
        points.append(
            {
                "frequency_hz": round(freq_hz),
                "frequency_mhz": round(freq_hz / 1_000_000, 6),
                "rssi_db": value,
                "bin_width_hz": width_hz,
            }
        )
    return {"low_hz": low_hz, "high_hz": high_hz, "points": points}


def parse_hackrf_sweep_line(line: str) -> list[dict[str, float]]:
    row = parse_hackrf_sweep_row(line)
    return row["points"] if row else []


def parse_hackrf_sweep_frames(output: str, low_hz: int, high_hz: int) -> list[list[dict[str, float]]]:
    frames: list[list[dict[str, float]]] = []
    current: list[dict[str, float]] = []
    last_low_hz: int | None = None
    for line in output.splitlines():
        row = parse_hackrf_sweep_row(line)
        if not row:
            continue
        if current and last_low_hz is not None and row["low_hz"] <= last_low_hz:
            frames.append(sorted(current, key=lambda item: item["frequency_hz"]))
            current = []
        current.extend(point for point in row["points"] if low_hz <= point["frequency_hz"] <= high_hz)
        last_low_hz = row["low_hz"]
    if current:
        frames.append(sorted(current, key=lambda item: item["frequency_hz"]))
    return [frame for frame in frames if frame]


def deep_scan_path(scan_id: str, suffix: str) -> Path:
    safe_id = "".join(ch for ch in scan_id if ch.isalnum() or ch in "-_")
    if safe_id != scan_id:
        raise HTTPException(400, "Invalid focused scan id")
    return DEEP_SCAN_DIR / f"{safe_id}{suffix}"


def gif_still_path(gif_path: Path) -> Path:
    return gif_path.with_suffix(".still.png")


def ensure_gif_still(gif_path: Path) -> Path:
    if not gif_path.exists():
        raise HTTPException(404, "Animation not found")
    still_path = gif_still_path(gif_path)
    if still_path.exists() and still_path.stat().st_mtime >= gif_path.stat().st_mtime:
        return still_path
    from PIL import Image

    with Image.open(gif_path) as image:
        image.seek(0)
        still_path.parent.mkdir(parents=True, exist_ok=True)
        image.convert("RGB").save(still_path)
    return still_path


def animation_viewer_html(title: str, animation_url: str, still_url: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title}</title>
  <style>
    :root {{ color-scheme: dark; --bg:#0c1013; --panel:#141b20; --line:#2b353d; --text:#e7edf2; --muted:#9caab5; --accent:#63d2ff; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; min-height:100vh; background:var(--bg); color:var(--text); font-family:Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif; display:flex; flex-direction:column; }}
    header {{ display:flex; align-items:center; justify-content:space-between; gap:12px; padding:12px 16px; background:#10161a; border-bottom:1px solid var(--line); }}
    h1 {{ margin:0; font-size:16px; }}
    .controls {{ display:flex; align-items:center; gap:8px; flex-wrap:wrap; }}
    button, a {{ background:#202a31; color:var(--text); border:1px solid #35424c; border-radius:6px; padding:7px 10px; font:inherit; text-decoration:none; cursor:pointer; }}
    button:hover, a:hover {{ border-color:var(--accent); }}
    main {{ flex:1; display:flex; align-items:center; justify-content:center; padding:14px; }}
    img {{ max-width:100%; max-height:calc(100vh - 86px); border:1px solid var(--line); border-radius:8px; background:#050708; }}
    .muted {{ color:var(--muted); font-size:12px; }}
  </style>
</head>
<body>
  <header>
    <div><h1>{title}</h1><div id="state" class="muted">Playing</div></div>
    <div class="controls">
      <button id="toggle">Pause</button>
      <a href="{animation_url}" target="_blank">Raw GIF</a>
    </div>
  </header>
  <main><img id="preview" src="{animation_url}" alt="{title}"></main>
  <script>
    const img = document.getElementById('preview');
    const toggle = document.getElementById('toggle');
    const state = document.getElementById('state');
    let playing = true;
    toggle.addEventListener('click', () => {{
      playing = !playing;
      img.src = `${{playing ? '{animation_url}' : '{still_url}'}}?t=${{Date.now()}}`;
      toggle.textContent = playing ? 'Pause' : 'Play';
      state.textContent = playing ? 'Playing' : 'Paused';
    }});
  </script>
</body>
</html>"""


def find_peak_points(points: list[dict[str, float]], limit: int = 5) -> list[dict[str, float]]:
    if not points:
        return []
    sorted_points = sorted(points, key=lambda item: item["rssi_db"], reverse=True)
    min_spacing_hz = max(100_000, int(max(point.get("bin_width_hz", 0) for point in points) * 4))
    peaks = []
    for point in sorted_points:
        if all(abs(point["frequency_hz"] - peak["frequency_hz"]) >= min_spacing_hz for peak in peaks):
            peaks.append(point)
        if len(peaks) >= limit:
            break
    return peaks


def frequency_gap_ranges(points: list[dict[str, float]]) -> list[tuple[float, float]]:
    if len(points) < 2:
        return []
    widths = [float(point.get("bin_width_hz", 0)) for point in points if point.get("bin_width_hz", 0)]
    expected_hz = statistics.median(widths) if widths else 0
    if expected_hz <= 0:
        return []
    threshold_hz = expected_hz * 2.5
    gaps = []
    for previous, current in zip(points, points[1:]):
        delta_hz = current["frequency_hz"] - previous["frequency_hz"]
        if delta_hz > threshold_hz:
            gaps.append((previous["frequency_mhz"], current["frequency_mhz"]))
    return gaps


def draw_focused_scan_frame(
    Image: Any,
    ImageDraw: Any,
    points: list[dict[str, float]],
    center_frequency_hz: int,
    span_mhz: int,
    frame_number: int,
    total_frames: int,
    low_db: float,
    high_db: float,
) -> Any:
    width, height = 900, 520
    left, top, right, bottom = 82, 42, 24, 72
    plot_w = width - left - right
    plot_h = height - top - bottom
    img = Image.new("RGB", (width, height), (16, 20, 24))
    draw = ImageDraw.Draw(img)
    draw.text((22, 16), f"{center_frequency_hz / 1_000_000:.3f} MHz focused scan", fill=(231, 237, 242))
    draw.text((width - 180, 16), f"{frame_number}/{total_frames}", fill=(99, 210, 255))
    draw.rectangle((left, top, left + plot_w, top + plot_h), outline=(43, 53, 61))
    min_mhz = points[0]["frequency_mhz"]
    max_mhz = points[-1]["frequency_mhz"]
    for i in range(5):
        x = left + round(i * plot_w / 4)
        y = top + round(i * plot_h / 4)
        draw.line((x, top, x, top + plot_h), fill=(30, 38, 44))
        draw.line((left, y, left + plot_w, y), fill=(30, 38, 44))
        mhz = min_mhz + i * (max_mhz - min_mhz) / 4
        db = high_db - i * (high_db - low_db) / 4
        draw.text((x - 28, top + plot_h + 14), f"{mhz:.3f}", fill=(156, 170, 181))
        draw.text((16, y - 7), f"{db:.0f}", fill=(156, 170, 181))
    draw.text((left + plot_w // 2 - 54, height - 28), "Frequency (MHz)", fill=(156, 170, 181))
    draw.text((16, top + plot_h // 2 + 22), "dB", fill=(156, 170, 181))
    span = max(0.001, max_mhz - min_mhz)
    db_span = max(1.0, high_db - low_db)
    gaps = frequency_gap_ranges(points)
    for gap_start, gap_end in gaps:
        x1 = left + ((gap_start - min_mhz) / span) * plot_w
        x2 = left + ((gap_end - min_mhz) / span) * plot_w
        draw.rectangle((x1, top, x2, top + plot_h), fill=(23, 29, 34), outline=(54, 66, 75))
    line = []
    segments = []
    gap_starts = {gap[1] for gap in gaps}
    for point in points:
        x = left + ((point["frequency_mhz"] - min_mhz) / span) * plot_w
        y = top + (1 - max(0, min(1, (point["rssi_db"] - low_db) / db_span))) * plot_h
        if point["frequency_mhz"] in gap_starts and line:
            segments.append(line)
            line = []
        line.append((float(x), float(y)))
    if line:
        segments.append(line)
    for segment in segments:
        if len(segment) > 1:
            draw.line(segment, fill=(255, 202, 98), width=2)
    peaks = find_peak_points(points, 4)
    for idx, peak in enumerate(peaks):
        x = left + ((peak["frequency_mhz"] - min_mhz) / span) * plot_w
        y = top + (1 - max(0, min(1, (peak["rssi_db"] - low_db) / db_span))) * plot_h
        draw.line((x, top, x, top + plot_h), fill=(255, 111, 145), width=1)
        label = f"{peak['frequency_mhz']:.6f}"
        label_x = max(left, min(width - 118, int(x) - 36))
        label_y = max(top + 4, int(y) - 18 - (idx % 2) * 16)
        draw.rectangle((label_x - 4, label_y - 2, label_x + 106, label_y + 13), fill=(16, 20, 24), outline=(255, 111, 145))
        draw.text((label_x, label_y), label, fill=(231, 237, 242))
    gap_note = f"   Gaps {len(gaps)}" if gaps else ""
    draw.text((left, height - 52), f"Span {span_mhz} MHz   Peaks labelled by MHz{gap_note}", fill=(156, 170, 181))
    return img


def write_focused_scan_gif(
    scan_id: str,
    frames: list[list[dict[str, float]]],
    center_frequency_hz: int,
    span_mhz: int,
) -> Path:
    from PIL import Image, ImageDraw

    all_values = [point["rssi_db"] for frame in frames for point in frame]
    low_db = min(all_values)
    high_db = max(all_values)
    span = max(1.0, high_db - low_db)
    low_db -= span * 0.25
    high_db += span * 0.35
    images = [
        draw_focused_scan_frame(Image, ImageDraw, frame, center_frequency_hz, span_mhz, idx + 1, len(frames), low_db, high_db)
        for idx, frame in enumerate(frames)
    ]
    path = deep_scan_path(scan_id, ".gif")
    path.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(path, save_all=True, append_images=images[1:], duration=850, loop=0)
    return path


def run_deep_scan(center_frequency_hz: int, span_mhz: int, bin_width_hz: int) -> dict[str, Any]:
    if not DEEP_SCAN_LOCK.acquire(blocking=False):
        raise HTTPException(409, "A deep scan is already running")
    half_span_hz = span_mhz * 1_000_000 // 2
    low_hz = max(1_000_000, center_frequency_hz - half_span_hz)
    high_hz = min(6_000_000_000, center_frequency_hz + half_span_hz)
    low_mhz = math.floor(low_hz / 1_000_000)
    high_mhz = math.ceil(high_hz / 1_000_000)
    scan_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{center_frequency_hz}"
    command = ["hackrf_sweep", "-N", "4", "-f", f"{low_mhz}:{high_mhz}", "-w", str(bin_width_hz)]
    sweep_was_active = service_is_active("hackrf-influx.service")
    started_at = datetime.now(timezone.utc)
    try:
        if sweep_was_active:
            pause_sweep_service()
            time.sleep(1.0)
        result = run_command(command, timeout=90)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"hackrf_sweep exited with {result.returncode}")
        frames = parse_hackrf_sweep_frames(result.stdout, low_hz, high_hz)
        points_by_freq: dict[int, dict[str, float]] = {}
        for frame in frames:
            for point in frame:
                freq = int(point["frequency_hz"])
                if freq not in points_by_freq or point["rssi_db"] > points_by_freq[freq]["rssi_db"]:
                    points_by_freq[freq] = point
        points = sorted(points_by_freq.values(), key=lambda item: item["frequency_hz"])
        points = [point for point in points if low_hz <= point["frequency_hz"] <= high_hz]
        if not points or not frames:
            raise RuntimeError("hackrf_sweep returned no usable bins")
        peak = max(points, key=lambda item: item["rssi_db"])
        peaks = find_peak_points(points, 5)
        gaps = [gap for frame in frames for gap in frequency_gap_ranges(frame)]
        write_focused_scan_gif(scan_id, frames, center_frequency_hz, span_mhz)
        return {
            "id": scan_id,
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "center_frequency_hz": center_frequency_hz,
            "center_frequency_mhz": round(center_frequency_hz / 1_000_000, 6),
            "span_mhz": span_mhz,
            "bin_width_hz": bin_width_hz,
            "bin_width_khz": round(bin_width_hz / 1000, 3),
            "frequency_min_hz": low_hz,
            "frequency_max_hz": high_hz,
            "points": points,
            "peak": peak,
            "peaks": peaks,
            "gap_count": len(gaps),
            "frequency_gaps_mhz": [{"start_mhz": start, "end_mhz": end} for start, end in gaps],
            "frame_count": len(frames),
            "animation_url": f"/api/deep-scans/{scan_id}/animation",
            "still_url": f"/api/deep-scans/{scan_id}/still",
            "viewer_url": f"/api/deep-scans/{scan_id}/viewer",
            "command": command,
            "sweep_was_active": sweep_was_active,
        }
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    finally:
        if sweep_was_active:
            try:
                resume_sweep_service()
            except Exception:
                pass
        DEEP_SCAN_LOCK.release()


def capture_path(capture_id: str, suffix: str) -> Path:
    safe_id = "".join(ch for ch in capture_id if ch.isalnum() or ch in "-_")
    if safe_id != capture_id:
        raise HTTPException(400, "Invalid capture id")
    return CAPTURE_DIR / f"{safe_id}{suffix}"


def capture_record(meta_path: Path) -> dict[str, Any]:
    data = json.loads(meta_path.read_text())
    capture_id = data["id"]
    data["meta_url"] = f"/api/captures/{capture_id}/meta"
    data["iq_url"] = f"/api/captures/{capture_id}/iq"
    data["spectrogram_url"] = f"/api/captures/{capture_id}/spectrogram"
    data["animation_url"] = f"/api/captures/{capture_id}/animation"
    data["animation_still_url"] = f"/api/captures/{capture_id}/animation-still"
    data["animation_viewer_url"] = f"/api/captures/{capture_id}/animation-viewer"
    data["audio_urls"] = {
        "am": f"/api/captures/{capture_id}/audio/am",
        "nfm": f"/api/captures/{capture_id}/audio/nfm",
        "wfm": f"/api/captures/{capture_id}/audio/wfm",
    }
    return data


def read_iq_file(iq_path: Path) -> Any:
    import numpy as np

    raw = np.fromfile(iq_path, dtype=np.uint8)
    if raw.size < 4096:
        raise RuntimeError("Capture file was too small to analyze")
    raw = raw[: raw.size - (raw.size % 2)]
    iq = (raw[0::2].astype(np.float32) - 127.5) + 1j * (raw[1::2].astype(np.float32) - 127.5)
    return iq / 128.0


def resample_audio(samples: Any, source_rate_hz: int, target_rate_hz: int = 48_000) -> Any:
    import numpy as np

    if samples.size == 0:
        return samples
    duration = samples.size / source_rate_hz
    target_count = max(1, int(duration * target_rate_hz))
    source_x = np.linspace(0, duration, samples.size, endpoint=False)
    target_x = np.linspace(0, duration, target_count, endpoint=False)
    return np.interp(target_x, source_x, samples).astype(np.float32)


def smooth_for_audio(samples: Any, source_rate_hz: int, bandwidth_hz: int) -> Any:
    import numpy as np

    window = max(1, int(source_rate_hz / max(1, bandwidth_hz * 2)))
    if window <= 1:
        return samples
    kernel = np.ones(window, dtype=np.float32) / window
    return np.convolve(samples, kernel, mode="same").astype(np.float32)


def write_wav(path: Path, samples: Any, sample_rate_hz: int = 48_000) -> dict[str, Any]:
    import numpy as np

    samples = samples.astype(np.float32)
    samples = samples - float(np.mean(samples))
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    if peak > 0:
        samples = samples / peak
    pcm = np.clip(samples * 0.85 * 32767, -32768, 32767).astype("<i2")
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate_hz)
        wav.writeframes(pcm.tobytes())
    return {"sample_rate_hz": sample_rate_hz, "samples": int(pcm.size), "duration_seconds": round(pcm.size / sample_rate_hz, 3)}


def generate_audio_preview(iq_path: Path, sample_rate_hz: int, mode: str, wav_path: Path) -> dict[str, Any]:
    import numpy as np

    iq = read_iq_file(iq_path)
    if mode == "am":
        audio = np.abs(iq)
        audio = smooth_for_audio(audio, sample_rate_hz, 8_000)
    elif mode in {"nfm", "wfm"}:
        demod = np.angle(iq[1:] * np.conj(iq[:-1])).astype(np.float32)
        bandwidth = 12_000 if mode == "nfm" else 16_000
        audio = smooth_for_audio(demod, sample_rate_hz, bandwidth)
    else:
        raise HTTPException(400, "Unsupported audio mode")
    resampled = resample_audio(audio, sample_rate_hz, 48_000)
    info = write_wav(wav_path, resampled, 48_000)
    info["mode"] = mode
    return info


def ensure_audio_preview(capture_id: str, mode: str) -> Path:
    if mode not in {"am", "nfm", "wfm"}:
        raise HTTPException(400, "Unsupported audio mode")
    wav_path = capture_path(capture_id, f".{mode}.wav")
    if wav_path.exists():
        return wav_path
    meta_path = capture_path(capture_id, ".json")
    iq_path = capture_path(capture_id, ".iq")
    if not meta_path.exists() or not iq_path.exists():
        raise HTTPException(404, "Capture not found")
    meta = json.loads(meta_path.read_text())
    audio = meta.setdefault("audio", {})
    audio[mode] = generate_audio_preview(iq_path, int(meta["sample_rate_hz"]), mode, wav_path)
    meta_path.write_text(json.dumps(meta, indent=2))
    return wav_path


def draw_spectrum_frame(
    Image: Any,
    ImageDraw: Any,
    freqs: Any,
    spectrum_db: Any,
    center_frequency_hz: int,
    sample_rate_hz: int,
    second: int,
    total_seconds: int,
    low_db: float,
    high_db: float,
) -> Any:
    import numpy as np

    width, height = 900, 520
    left, top, right, bottom = 86, 42, 24, 66
    plot_w = width - left - right
    plot_h = height - top - bottom
    img = Image.new("RGB", (width, height), (16, 20, 24))
    draw = ImageDraw.Draw(img)
    draw.text((22, 16), f"{center_frequency_hz / 1_000_000:.3f} MHz spectrum animation", fill=(231, 237, 242))
    draw.text((width - 170, 16), f"{second + 1}/{total_seconds} sec", fill=(99, 210, 255))
    draw.rectangle((left, top, left + plot_w, top + plot_h), outline=(43, 53, 61))

    min_mhz = (center_frequency_hz - sample_rate_hz / 2) / 1_000_000
    max_mhz = (center_frequency_hz + sample_rate_hz / 2) / 1_000_000
    for i in range(5):
        x = left + round(i * plot_w / 4)
        y = top + round(i * plot_h / 4)
        draw.line((x, top, x, top + plot_h), fill=(30, 38, 44))
        draw.line((left, y, left + plot_w, y), fill=(30, 38, 44))
        mhz = min_mhz + i * (max_mhz - min_mhz) / 4
        db = high_db - i * (high_db - low_db) / 4
        draw.text((x - 28, top + plot_h + 12), f"{mhz:.3f}", fill=(156, 170, 181))
        draw.text((18, y - 7), f"{db:.0f}", fill=(156, 170, 181))
    draw.text((left + plot_w // 2 - 54, height - 28), "Frequency (MHz)", fill=(156, 170, 181))
    draw.text((16, top + plot_h // 2 + 22), "dB", fill=(156, 170, 181))

    bins = min(700, spectrum_db.size)
    sample_idx = np.linspace(0, spectrum_db.size - 1, bins).astype(np.int64)
    values = spectrum_db[sample_idx]
    x_vals = left + np.linspace(0, plot_w, bins)
    y_vals = top + (1 - np.clip((values - low_db) / max(1e-6, high_db - low_db), 0, 1)) * plot_h
    points = [(float(x), float(y)) for x, y in zip(x_vals, y_vals)]
    if len(points) > 1:
        draw.line(points, fill=(255, 202, 98), width=2)
    peak_idx = int(sample_idx[int(np.argmax(values))])
    peak_freq_mhz = (center_frequency_hz + freqs[peak_idx]) / 1_000_000
    peak_x = left + (peak_idx / max(1, spectrum_db.size - 1)) * plot_w
    draw.line((peak_x, top, peak_x, top + plot_h), fill=(255, 111, 145), width=1)
    draw.text((left, height - 50), f"Peak {peak_freq_mhz:.6f} MHz   Range {low_db:.0f} to {high_db:.0f} dB", fill=(156, 170, 181))
    return img


def summarize_iq(
    iq_path: Path,
    sample_rate_hz: int,
    center_frequency_hz: int,
    png_path: Path,
    gif_path: Path,
) -> dict[str, Any]:
    import numpy as np
    from PIL import Image, ImageDraw

    iq = read_iq_file(iq_path)

    usable = iq[: min(iq.size, sample_rate_hz * 5)]
    nfft = 2048
    if usable.size < nfft:
        raise RuntimeError("Capture file was too short for an FFT")
    target_columns = 1000
    hop = max(512, (usable.size - nfft) // target_columns)
    starts = np.arange(0, usable.size - nfft + 1, hop, dtype=np.int64)
    window = np.hanning(nfft).astype(np.float32)
    spectra = np.empty((nfft, starts.size), dtype=np.float32)
    for col, start in enumerate(starts):
        chunk = usable[start : start + nfft] * window
        spectra[:, col] = np.abs(np.fft.fftshift(np.fft.fft(chunk))).astype(np.float32)
    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, d=1.0 / sample_rate_hz))
    power_db = 20 * np.log10(spectra + 1e-12)
    peak_index = np.unravel_index(np.argmax(power_db), power_db.shape)
    low = float(np.percentile(power_db, 5))
    high = float(np.percentile(power_db, 99.5))
    norm = np.clip((power_db - low) / max(1e-6, high - low), 0, 1)
    img_array = np.zeros((norm.shape[0], norm.shape[1], 3), dtype=np.uint8)
    img_array[..., 0] = np.clip(255 * np.minimum(1, norm * 2.1), 0, 255)
    img_array[..., 1] = np.clip(255 * np.maximum(0, (norm - 0.22) * 1.55), 0, 255)
    img_array[..., 2] = np.clip(255 * np.maximum(0, 1.0 - norm * 2.6), 0, 255)
    img_array = np.flipud(img_array)
    resample = getattr(getattr(Image, "Resampling", Image), "BILINEAR")
    heatmap = Image.fromarray(img_array, mode="RGB").resize((920, 420), resample)
    canvas = Image.new("RGB", (1100, 560), (16, 20, 24))
    canvas.paste(heatmap, (120, 56))
    draw = ImageDraw.Draw(canvas)
    draw.text((22, 18), f"{center_frequency_hz / 1_000_000:.3f} MHz IQ capture", fill=(231, 237, 242))
    draw.text((120, 492), "Seconds", fill=(156, 170, 181))
    draw.text((18, 246), "Frequency", fill=(156, 170, 181))
    draw.text((24, 58), f"{(center_frequency_hz + sample_rate_hz / 2) / 1_000_000:.3f} MHz", fill=(156, 170, 181))
    draw.text((24, 462), f"{(center_frequency_hz - sample_rate_hz / 2) / 1_000_000:.3f} MHz", fill=(156, 170, 181))
    draw.text((120, 502), "0.0", fill=(156, 170, 181))
    draw.text((970, 502), f"{usable.size / sample_rate_hz:.1f}s", fill=(156, 170, 181))
    draw.rectangle((119, 55, 1041, 477), outline=(43, 53, 61))
    png_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(png_path)

    duration = usable.size / sample_rate_hz
    total_seconds = max(1, int(np.ceil(duration)))
    frame_times = starts / sample_rate_hz
    frame_spectra = []
    for second in range(total_seconds):
        cols = np.where((frame_times >= second) & (frame_times < second + 1))[0]
        if cols.size == 0:
            nearest = int(np.argmin(np.abs(frame_times - min(second, duration))))
            cols = np.array([nearest])
        frame_spectra.append(np.max(power_db[:, cols], axis=1))
    frame_values = np.concatenate(frame_spectra)
    frame_low = float(np.percentile(frame_values, 5))
    frame_high = float(np.percentile(frame_values, 99.5))
    frame_span = max(1.0, frame_high - frame_low)
    frame_low -= frame_span * 0.18
    frame_high += frame_span * 0.55
    frames = []
    for second, spectrum in enumerate(frame_spectra):
        frames.append(
            draw_spectrum_frame(
                Image,
                ImageDraw,
                freqs,
                spectrum,
                center_frequency_hz,
                sample_rate_hz,
                second,
                total_seconds,
                frame_low,
                frame_high,
            )
        )
    gif_path.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(gif_path, save_all=True, append_images=frames[1:], duration=850, loop=0)

    mean_power = float(20 * np.log10(np.sqrt(np.mean(np.abs(usable) ** 2)) + 1e-12))
    return {
        "samples": int(iq.size),
        "duration_seconds": round(iq.size / sample_rate_hz, 3),
        "mean_power_dbfs": round(mean_power, 2),
        "peak_frequency_hz": int(center_frequency_hz + freqs[peak_index[0]]),
        "peak_frequency_mhz": round((center_frequency_hz + freqs[peak_index[0]]) / 1_000_000, 6),
        "animation_frame_seconds": 1,
        "animation_frames": len(frames),
    }


def capture_signal(
    frequency_hz: int,
    duration_seconds: int,
    sample_rate_hz: int,
    lna_gain_db: int,
    vga_gain_db: int,
    amp_enable: int,
) -> dict[str, Any]:
    if not CAPTURE_LOCK.acquire(blocking=False):
        raise HTTPException(409, "A capture is already running")
    sweep_was_active = service_is_active("hackrf-influx.service")
    capture_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"-{frequency_hz}"
    iq_path = capture_path(capture_id, ".iq")
    png_path = capture_path(capture_id, ".png")
    gif_path = capture_path(capture_id, ".gif")
    meta_path = capture_path(capture_id, ".json")
    sample_count = int(duration_seconds * sample_rate_hz)
    command = [
        "hackrf_transfer",
        "-r",
        str(iq_path),
        "-f",
        str(frequency_hz),
        "-s",
        str(sample_rate_hz),
        "-n",
        str(sample_count),
        "-a",
        str(amp_enable),
        "-l",
        str(lna_gain_db),
        "-g",
        str(vga_gain_db),
    ]
    started_at = datetime.now(timezone.utc)
    meta: dict[str, Any] = {
        "id": capture_id,
        "status": "running",
        "started_at": started_at.isoformat(),
        "frequency_hz": frequency_hz,
        "frequency_mhz": frequency_hz / 1_000_000,
        "duration_seconds": duration_seconds,
        "sample_rate_hz": sample_rate_hz,
        "sample_count": sample_count,
        "lna_gain_db": lna_gain_db,
        "vga_gain_db": vga_gain_db,
        "amp_enable": amp_enable,
        "sweep_was_active": sweep_was_active,
        "command": command,
    }
    try:
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta, indent=2))
        if sweep_was_active:
            pause_sweep_service()
            time.sleep(1.0)
        result = run_command(command, timeout=duration_seconds + 20)
        meta["hackrf_transfer_stdout"] = result.stdout[-4000:]
        meta["hackrf_transfer_stderr"] = result.stderr[-4000:]
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"hackrf_transfer exited with {result.returncode}")
        if not iq_path.exists() or iq_path.stat().st_size == 0:
            raise RuntimeError("hackrf_transfer did not create a capture file")
        meta["iq_bytes"] = iq_path.stat().st_size
        meta["analysis"] = summarize_iq(iq_path, sample_rate_hz, frequency_hz, png_path, gif_path)
        meta["audio"] = {
            mode: generate_audio_preview(iq_path, sample_rate_hz, mode, capture_path(capture_id, f".{mode}.wav"))
            for mode in ("am", "nfm", "wfm")
        }
        meta["status"] = "complete"
        meta["completed_at"] = datetime.now(timezone.utc).isoformat()
        meta_path.write_text(json.dumps(meta, indent=2))
        return capture_record(meta_path)
    except Exception as exc:
        meta["status"] = "failed"
        meta["error"] = str(exc)
        meta["completed_at"] = datetime.now(timezone.utc).isoformat()
        meta_path.write_text(json.dumps(meta, indent=2))
        raise HTTPException(500, str(exc)) from exc
    finally:
        if sweep_was_active:
            try:
                resume_sweep_service()
            except Exception:
                pass
        CAPTURE_LOCK.release()


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
  |> keep(columns: ["_time", "_value", "frequency_hz"])
'''
    rows = flux_query(query)
    bucket_seconds = parse_bucket_seconds(time_bucket)
    grouped: dict[tuple[str, int], float] = {}
    for row in rows:
        time_key = bucket_time_iso(row["_time"], bucket_seconds)
        freq_bin = int(row["frequency_hz"]) // step_hz * step_hz
        key = (time_key, freq_bin)
        value = float(row["_value"])
        if key not in grouped or value > grouped[key]:
            grouped[key] = value
    times = sorted({key[0] for key in grouped})
    freqs = sorted({key[1] for key in grouped})
    time_index = {value: idx for idx, value in enumerate(times)}
    freq_index = {value: idx for idx, value in enumerate(freqs)}
    values: list[list[float | None]] = [[None for _ in times] for _ in freqs]
    min_v: float | None = None
    max_v: float | None = None
    for (time_key, freq_bin), value in grouped.items():
        values[freq_index[freq_bin]][time_index[time_key]] = value
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
            "points": len(grouped),
            "raw_points": len(rows),
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


@app.get("/api/captures")
def captures(limit: int = Query(12, ge=1, le=100)) -> JSONResponse:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    records = [capture_record(path) for path in sorted(CAPTURE_DIR.glob("*.json"), reverse=True)[:limit]]
    return JSONResponse({"items": records, "capture_running": CAPTURE_LOCK.locked()})


@app.post("/api/capture")
def capture(
    payload: dict[str, Any] = Body(...),
) -> JSONResponse:
    frequency_hz = int(payload.get("frequency_hz", 0))
    if frequency_hz < 1_000_000 or frequency_hz > 6_000_000_000:
        raise HTTPException(400, "frequency_hz is outside the supported capture range")
    duration_seconds = int(payload.get("duration_seconds", 4))
    sample_rate_hz = int(payload.get("sample_rate_hz", 5_000_000))
    lna_gain_db = int(payload.get("lna_gain_db", 32))
    vga_gain_db = int(payload.get("vga_gain_db", 24))
    amp_enable = int(payload.get("amp_enable", 0))
    if duration_seconds < 1 or duration_seconds > 20:
        raise HTTPException(400, "duration_seconds must be between 1 and 20")
    if sample_rate_hz < 2_000_000 or sample_rate_hz > 20_000_000:
        raise HTTPException(400, "sample_rate_hz must be between 2 MHz and 20 MHz")
    if lna_gain_db not in range(0, 41, 8):
        raise HTTPException(400, "lna_gain_db must be 0-40 dB in 8 dB steps")
    if vga_gain_db < 0 or vga_gain_db > 62 or vga_gain_db % 2:
        raise HTTPException(400, "vga_gain_db must be 0-62 dB in 2 dB steps")
    if amp_enable not in (0, 1):
        raise HTTPException(400, "amp_enable must be 0 or 1")
    record = capture_signal(frequency_hz, duration_seconds, sample_rate_hz, lna_gain_db, vga_gain_db, amp_enable)
    return JSONResponse(record)


@app.post("/api/deep-scan")
def deep_scan(payload: dict[str, Any] = Body(...)) -> JSONResponse:
    frequency_hz = int(payload.get("frequency_hz", 0))
    span_mhz = int(payload.get("span_mhz", 20))
    bin_width_hz = int(payload.get("bin_width_hz", 100_000))
    if frequency_hz < 1_000_000 or frequency_hz > 6_000_000_000:
        raise HTTPException(400, "frequency_hz is outside the supported scan range")
    if span_mhz < 2 or span_mhz > 100:
        raise HTTPException(400, "span_mhz must be between 2 and 100")
    if bin_width_hz < 25_000 or bin_width_hz > 1_000_000:
        raise HTTPException(400, "bin_width_hz must be between 25 kHz and 1 MHz")
    return JSONResponse(run_deep_scan(frequency_hz, span_mhz, bin_width_hz))


@app.get("/api/deep-scans/{scan_id}/animation")
def deep_scan_animation(scan_id: str) -> FileResponse:
    path = deep_scan_path(scan_id, ".gif")
    if not path.exists():
        raise HTTPException(404, "Focused scan animation not found")
    return FileResponse(path, media_type="image/gif")


@app.get("/api/deep-scans/{scan_id}/still")
def deep_scan_still(scan_id: str) -> FileResponse:
    path = ensure_gif_still(deep_scan_path(scan_id, ".gif"))
    return FileResponse(path, media_type="image/png")


@app.get("/api/deep-scans/{scan_id}/viewer", response_class=HTMLResponse)
def deep_scan_viewer(scan_id: str) -> str:
    gif_path = deep_scan_path(scan_id, ".gif")
    ensure_gif_still(gif_path)
    return animation_viewer_html(
        "Focused scan animation",
        f"/api/deep-scans/{scan_id}/animation",
        f"/api/deep-scans/{scan_id}/still",
    )


@app.get("/api/captures/{capture_id}/meta")
def capture_meta(capture_id: str) -> JSONResponse:
    path = capture_path(capture_id, ".json")
    if not path.exists():
        raise HTTPException(404, "Capture not found")
    return JSONResponse(capture_record(path))


@app.get("/api/captures/{capture_id}/spectrogram")
def capture_spectrogram(capture_id: str) -> FileResponse:
    path = capture_path(capture_id, ".png")
    if not path.exists():
        raise HTTPException(404, "Spectrogram not found")
    return FileResponse(path, media_type="image/png")


@app.get("/api/captures/{capture_id}/animation")
def capture_animation(capture_id: str) -> FileResponse:
    path = capture_path(capture_id, ".gif")
    if not path.exists():
        meta_path = capture_path(capture_id, ".json")
        iq_path = capture_path(capture_id, ".iq")
        png_path = capture_path(capture_id, ".png")
        if not meta_path.exists() or not iq_path.exists():
            raise HTTPException(404, "Animation not found")
        meta = json.loads(meta_path.read_text())
        meta["analysis"] = summarize_iq(
            iq_path,
            int(meta["sample_rate_hz"]),
            int(meta["frequency_hz"]),
            png_path,
            path,
        )
        meta_path.write_text(json.dumps(meta, indent=2))
    return FileResponse(path, media_type="image/gif")


@app.get("/api/captures/{capture_id}/animation-still")
def capture_animation_still(capture_id: str) -> FileResponse:
    path = capture_path(capture_id, ".gif")
    if not path.exists():
        meta_path = capture_path(capture_id, ".json")
        iq_path = capture_path(capture_id, ".iq")
        png_path = capture_path(capture_id, ".png")
        if not meta_path.exists() or not iq_path.exists():
            raise HTTPException(404, "Animation not found")
        meta = json.loads(meta_path.read_text())
        meta["analysis"] = summarize_iq(
            iq_path,
            int(meta["sample_rate_hz"]),
            int(meta["frequency_hz"]),
            png_path,
            path,
        )
        meta_path.write_text(json.dumps(meta, indent=2))
    return FileResponse(ensure_gif_still(path), media_type="image/png")


@app.get("/api/captures/{capture_id}/animation-viewer", response_class=HTMLResponse)
def capture_animation_viewer(capture_id: str) -> str:
    path = capture_path(capture_id, ".gif")
    if not path.exists():
        meta_path = capture_path(capture_id, ".json")
        iq_path = capture_path(capture_id, ".iq")
        png_path = capture_path(capture_id, ".png")
        if not meta_path.exists() or not iq_path.exists():
            raise HTTPException(404, "Animation not found")
        meta = json.loads(meta_path.read_text())
        meta["analysis"] = summarize_iq(
            iq_path,
            int(meta["sample_rate_hz"]),
            int(meta["frequency_hz"]),
            png_path,
            path,
        )
        meta_path.write_text(json.dumps(meta, indent=2))
    ensure_gif_still(path)
    return animation_viewer_html(
        "Capture animation",
        f"/api/captures/{capture_id}/animation",
        f"/api/captures/{capture_id}/animation-still",
    )


@app.get("/api/captures/{capture_id}/audio/{mode}")
def capture_audio(capture_id: str, mode: str) -> FileResponse:
    path = ensure_audio_preview(capture_id, mode)
    return FileResponse(path, media_type="audio/wav")


@app.get("/api/captures/{capture_id}/iq")
def capture_iq(capture_id: str) -> FileResponse:
    path = capture_path(capture_id, ".iq")
    if not path.exists():
        raise HTTPException(404, "IQ capture not found")
    return FileResponse(path, media_type="application/octet-stream", filename=path.name)


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
    .toolbarGroup { display:flex; align-items:center; gap:8px; flex-wrap:wrap; padding:6px 8px; border:1px solid var(--line); border-radius:8px; background:#141b20; }
    .selectedChip { color:var(--hot); font-weight:700; min-width:94px; }
    select, button { background:#202a31; color:var(--text); border:1px solid #35424c; border-radius:6px; padding:7px 10px; font:inherit; }
    button { cursor:pointer; }
    button:hover { border-color:var(--accent); }
    button:disabled { opacity:0.55; cursor:not-allowed; }
    .primaryBtn { background:#1d4050; border-color:#39758f; }
    .smallBtn { padding:5px 8px; font-size:12px; }
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
    .hint { color:var(--muted); font-size:12px; line-height:1.35; margin-top:8px; }
    .contextBox { margin-top:12px; border:1px solid var(--line); background:var(--panel); border-radius:8px; padding:10px; }
    .contextBox h4 { margin:0 0 6px; font-size:13px; }
    .tagList { display:flex; flex-wrap:wrap; gap:6px; margin-top:7px; }
    .tag { border:1px solid #35424c; border-radius:999px; padding:3px 7px; font-size:12px; color:var(--text); background:#202a31; }
    .captureActions { display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-top:10px; }
    .captureImg { width:100%; border:1px solid var(--line); border-radius:8px; margin-top:8px; background:#0c1013; cursor:zoom-in; }
    .captureCaption { color:var(--muted); font-size:12px; line-height:1.35; margin-top:6px; }
    .captureToggle { display:flex; gap:6px; margin-top:8px; }
    .selectedPanel { position:sticky; top:0; z-index:2; padding-bottom:10px; background:#12181d; }
    .deepScanBox { margin-top:12px; border:1px solid var(--line); background:var(--panel); border-radius:8px; padding:10px; }
    .deepScanBox h4 { margin:0 0 6px; font-size:13px; }
    #deepScanCanvas { height:190px; margin-top:10px; }
    .deepScanImg { width:100%; border:1px solid var(--line); border-radius:8px; margin-top:10px; background:#0c1013; cursor:zoom-in; }
    .mediaShell { position:relative; }
    .pausePill { position:absolute; right:8px; top:18px; padding:4px 7px; border-radius:6px; background:rgba(16,20,24,0.86); color:var(--text); border:1px solid var(--line); font-size:12px; pointer-events:none; }
    .peakList { color:var(--muted); font-size:12px; line-height:1.45; margin-top:8px; }
    .audioPreview { margin-top:10px; display:flex; flex-direction:column; gap:8px; }
    .audioRow { display:grid; grid-template-columns: 42px 1fr 44px; align-items:center; gap:8px; color:var(--muted); font-size:12px; }
    .audioLink { color:var(--accent); text-decoration:none; font-size:12px; }
    audio { width:100%; height:32px; }
    #zoomState { color: var(--hot); }
    #detailCanvas { height:220px; margin-top:10px; }
    #activityOverlay { position:fixed; inset:0; z-index:20; display:none; align-items:center; justify-content:center; background:rgba(9,13,16,0.58); backdrop-filter:blur(3px); }
    #activityOverlay.active { display:flex; }
    .activityBadge { width:190px; min-height:172px; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:14px; border:1px solid #38505d; border-radius:8px; background:rgba(18,24,29,0.94); box-shadow:0 18px 54px rgba(0,0,0,0.34); }
    .radioLoader { position:relative; width:96px; height:96px; }
    .radioLoader::before { content:""; position:absolute; left:38px; top:58px; width:20px; height:20px; border-radius:50%; background:var(--hot); box-shadow:0 0 18px rgba(255,202,98,0.75); }
    .radioLoader::after { content:""; position:absolute; left:47px; top:18px; width:2px; height:54px; background:var(--accent); transform:rotate(-18deg); transform-origin:bottom; }
    .radioWave { position:absolute; left:48px; top:48px; width:18px; height:18px; border:2px solid var(--accent); border-radius:50%; opacity:0; transform:translate(-50%,-50%) scale(0.3); animation:radioPulse 1.45s infinite ease-out; }
    .radioWave:nth-child(2) { animation-delay:0.32s; }
    .radioWave:nth-child(3) { animation-delay:0.64s; }
    .activityText { font-size:15px; font-weight:700; color:var(--text); text-transform:uppercase; letter-spacing:0; }
    @keyframes radioPulse { 0% { opacity:0.9; transform:translate(-50%,-50%) scale(0.25); } 100% { opacity:0; transform:translate(-50%,-50%) scale(3.1); } }
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
      <label>Range <select id="hours"><option selected>1</option><option>3</option><option>6</option><option>12</option><option>24</option></select> h</label>
      <label>Frequency bin <select id="freqStep"><option>1</option><option selected>5</option><option>10</option><option>25</option><option>50</option></select> MHz</label>
      <button id="refresh">Refresh</button>
      <button id="resetZoom">Reset Zoom</button>
      <div class="toolbarGroup">
        <span class="selectedChip" id="toolbarSelected">No selection</span>
        <label>Span <select id="deepSpan"><option value="10">10 MHz</option><option value="20" selected>20 MHz</option><option value="50">50 MHz</option></select></label>
        <label>Bin <select id="deepBin"><option value="25000">25 kHz</option><option value="50000">50 kHz</option><option value="100000" selected>100 kHz</option><option value="250000">250 kHz</option></select></label>
        <button id="deepScanBtn" class="primaryBtn" disabled>Focused Scan</button>
      </div>
      <div class="toolbarGroup">
        <label>Seconds <select id="captureSeconds"><option>2</option><option selected>4</option><option>8</option><option>12</option></select></label>
        <label>Rate <select id="captureRate"><option value="2000000">2 Msps</option><option value="5000000" selected>5 Msps</option><option value="10000000">10 Msps</option></select></label>
        <button id="captureBtn" class="primaryBtn" disabled>Capture Signal</button>
      </div>
      <span class="muted" id="legend">Color = RSSI strength; left rail = frequency context</span>
      <span id="hoverReadout" class="readout"></span>
      <span id="zoomState"></span>
    </div>
    <div id="heatmapWrap" class="panel"><canvas id="heatmap"></canvas></div>
  </section>
  <aside>
    <div class="selectedPanel">
      <div class="stats">
        <div class="stat"><strong id="sweeps">--</strong><span>points loaded</span></div>
        <div class="stat"><strong id="activeAnoms">--</strong><span>active anomalies</span></div>
        <div class="stat"><strong id="maxRssi">--</strong><span>max RSSI</span></div>
      </div>
      <h3>Selected</h3>
      <div id="selected" class="muted">Click a heatmap block.</div>
      <canvas id="detailCanvas" class="panel"></canvas>
      <div class="hint">Detail graph: RSSI over the last 6 hours for the selected frequency and nearby bins. Each colored line is one neighboring bin; spikes mean that bin got stronger during that time.</div>
    </div>
    <h3>Activity</h3>
    <div class="deepScanBox">
      <h4>Focused Scan</h4>
      <div id="deepScanStatus" class="muted">Zoom into a frequency, then double-click a heatmap block or run a focused scan here.</div>
      <canvas id="deepScanCanvas" class="panel"></canvas>
      <div id="deepScanMedia"></div>
      <div class="hint">Focused scans briefly pause the wide sweep and take a high-resolution look around the selected frequency. They are animated snapshots, separate from the always-on 1 MHz baseline.</div>
    </div>
    <div class="contextBox">
      <h4>Frequency Context</h4>
      <div id="bandContext" class="muted">Hover the colored rail or a heatmap block.</div>
      <div class="hint">Reference ranges are approximate and meant for orientation, not as an authoritative band plan.</div>
    </div>
    <div id="captureStatus" class="hint">Select a frequency to capture raw IQ and create a spectrogram. Capture controls are in the top bar.</div>
    <div id="captures" class="list"></div>
    <h3>Anomalies</h3>
    <div id="anomalies" class="list"></div>
    <h3>Top Signals</h3>
    <div id="top" class="list"></div>
  </aside>
</main>
<div id="activityOverlay" aria-live="polite" aria-hidden="true">
  <div class="activityBadge">
    <div class="radioLoader"><span class="radioWave"></span><span class="radioWave"></span><span class="radioWave"></span></div>
    <div id="activityText" class="activityText">Working</div>
  </div>
</div>
<script>
const heat = document.getElementById('heatmap');
const detail = document.getElementById('detailCanvas');
const deepCanvas = document.getElementById('deepScanCanvas');
let heatData = null;
let hoverCell = null;
let hoverFreqHz = null;
let zoomMinHz = null;
let zoomMaxHz = null;
let selectedFreqHz = null;
let captureRunning = false;
let deepScanRunning = false;
let deepScanData = null;
let activityTokens = new Set();

function showActivity(label) {
  const token = Symbol(label);
  activityTokens.add(token);
  document.getElementById('activityText').textContent = label;
  const overlay = document.getElementById('activityOverlay');
  overlay.classList.add('active');
  overlay.setAttribute('aria-hidden', 'false');
  return {token, started: Date.now()};
}

function hideActivity(activity) {
  if (!activity) return;
  const remainingMs = Math.max(0, 450 - (Date.now() - activity.started));
  window.setTimeout(() => {
    activityTokens.delete(activity.token);
    if (!activityTokens.size) {
      const overlay = document.getElementById('activityOverlay');
      overlay.classList.remove('active');
      overlay.setAttribute('aria-hidden', 'true');
    }
  }, remainingMs);
}

const BAND_REFS = [
  {name:'VHF', min:30, max:300, info:'30-300 MHz. FM broadcast, airband, marine, weather radio, amateur 6m/2m, and other land-mobile activity can appear here.'},
  {name:'6 m amateur', min:50, max:54, info:'Amateur radio allocation in the United States.'},
  {name:'FM broadcast', min:88, max:108, info:'Commercial FM broadcast band.'},
  {name:'Airband', min:108, max:137, info:'Civil aviation navigation and AM voice communications.'},
  {name:'2 m amateur', min:144, max:148, info:'Amateur radio allocation in the United States.'},
  {name:'NOAA weather', min:162.4, max:162.55, info:'NOAA Weather Radio channels in the United States.'},
  {name:'UHF', min:300, max:3000, info:'Broad 300-3000 MHz band.'},
  {name:'L-band', min:1000, max:2000, info:'Satellite, GNSS, aircraft, and other microwave services often appear here.'},
  {name:'S-band', min:2000, max:4000, info:'Microwave band with radar, satellite, Wi-Fi/ISM, and amateur allocations.'},
  {name:'C-band', min:4000, max:8000, info:'Microwave band. Your current sweep sees the 4000-5995 MHz portion.'},
  {name:'70 cm amateur', min:420, max:450, info:'Amateur radio allocation in the United States.'},
  {name:'433 MHz ISM', min:433.05, max:434.79, info:'Short-range devices, sensors, remotes, and ISM activity.'},
  {name:'902-928 MHz ISM', min:902, max:928, info:'ISM devices, LoRa, sensors, telemetry, and other unlicensed activity.'},
  {name:'ADS-B / Mode S', min:1088, max:1092, info:'Aircraft transponder and ADS-B activity centered near 1090 MHz.'},
  {name:'GPS L2 / GNSS', min:1220, max:1235, info:'GNSS downlink neighborhood around GPS L2.'},
  {name:'23 cm amateur', min:1240, max:1300, info:'Amateur radio allocation in the United States.'},
  {name:'GPS L1 / GNSS', min:1570, max:1580, info:'GNSS downlink neighborhood around GPS L1 at 1575.42 MHz.'},
  {name:'L-band satcom', min:1525, max:1660, info:'Inmarsat and other mobile satellite downlink neighborhood.'},
  {name:'L-band weather satellites', min:1670, max:1710, info:'Some polar-orbiting weather satellite HRPT-style downlinks are in this area; 137 MHz APT is below this sweep.'},
  {name:'13 cm amateur', min:2300, max:2450, info:'Amateur allocation overlaps the 2.4 GHz ISM neighborhood.'},
  {name:'2.4 GHz ISM / Wi-Fi', min:2400, max:2483.5, info:'Wi-Fi, Bluetooth, Zigbee, microwave ovens, and many ISM devices.'},
  {name:'9 cm amateur', min:3300, max:3500, info:'Amateur microwave allocation.'},
  {name:'C-band sat downlink', min:3700, max:4200, info:'Satellite downlink neighborhood; local licensing and repacking vary.'},
  {name:'5 GHz Wi-Fi / U-NII', min:5150, max:5895, info:'Wi-Fi and U-NII activity, with exact allowed channels varying by region/device.'},
  {name:'5.8 GHz ISM', min:5725, max:5875, info:'ISM neighborhood used by some video links, telemetry, and other devices.'},
  {name:'5 cm amateur', min:5650, max:5925, info:'Amateur microwave allocation overlaps the 5 GHz Wi-Fi/ISM area.'}
];

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

function heatGeometry(width, height) {
  return {left: 104, bottom: 28, top: 8, right: 10, railLeft: 72, railRight: 90};
}

function bandColor(idx, alpha=0.82) {
  const palette = [
    [99,210,255],[255,202,98],[123,216,143],[255,111,145],[199,146,234],
    [93,176,255],[255,157,99],[128,225,196],[238,124,190]
  ];
  const c = palette[idx % palette.length];
  return `rgba(${c[0]},${c[1]},${c[2]},${alpha})`;
}

function bandMatches(freqHz) {
  const mhz = freqHz / 1e6;
  return BAND_REFS.filter(b => mhz >= b.min && mhz <= b.max);
}

function bandSummary(freqHz) {
  const matches = bandMatches(freqHz);
  return matches.length ? matches.map(b => b.name).join(', ') : 'No reference range match';
}

function updateBandContext(freqHz) {
  const el = document.getElementById('bandContext');
  const matches = bandMatches(freqHz);
  if (!matches.length) {
    el.innerHTML = `<b>${(freqHz/1e6).toFixed(3)} MHz</b><br>No reference range match in the local list.`;
    return;
  }
  el.innerHTML = `<b>${(freqHz/1e6).toFixed(3)} MHz</b><div class="tagList">` +
    matches.map(b => `<span class="tag">${b.name}</span>`).join('') +
    `</div><div class="hint">${matches.slice(0,3).map(b => b.info).join(' ')}</div>`;
}

function drawBandRail(ctx, geom, width, height, rows) {
  if (!heatData || !rows) return;
  const plotH = height - geom.top - geom.bottom;
  const minMhz = Math.min(...heatData.frequencies_mhz);
  const maxMhz = Math.max(...heatData.frequencies_mhz);
  const span = Math.max(1, maxMhz - minMhz);
  ctx.fillStyle = '#202a31';
  ctx.fillRect(geom.railLeft, geom.top, geom.railRight - geom.railLeft, plotH);
  BAND_REFS.forEach((band, idx) => {
    const lo = Math.max(minMhz, band.min);
    const hi = Math.min(maxMhz, band.max);
    if (hi < lo) return;
    const yHi = geom.top + (1 - ((hi - minMhz) / span)) * plotH;
    const yLo = geom.top + (1 - ((lo - minMhz) / span)) * plotH;
    const h = Math.max(2, yLo - yHi);
    ctx.fillStyle = bandColor(idx, band.max - band.min > 500 ? 0.36 : 0.74);
    ctx.fillRect(geom.railLeft, yHi, geom.railRight - geom.railLeft, h);
  });
  ctx.strokeStyle = '#35424c';
  ctx.strokeRect(geom.railLeft, geom.top, geom.railRight - geom.railLeft, plotH);
}

function drawHeatmap() {
  const {ctx, width, height} = resizeCanvas(heat);
  ctx.clearRect(0,0,width,height);
  if (!heatData) return;
  const geom = heatGeometry(width, height);
  const {left, bottom, top, right} = geom;
  const rows = heatData.frequencies_hz.length;
  const cols = heatData.times.length;
  const cellW = (width-left-right) / Math.max(1, cols);
  const cellH = (height-top-bottom) / Math.max(1, rows);
  drawBandRail(ctx, geom, width, height, rows);
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
  if (hoverFreqHz) {
    const idx = heatData.frequencies_hz.indexOf(hoverFreqHz);
    if (idx >= 0) {
      const y = top + (rows-1-idx)*cellH + cellH/2;
      ctx.strokeStyle = '#e7edf2';
      ctx.globalAlpha = 0.65;
      ctx.beginPath();
      ctx.moveTo(geom.railLeft - 4, y);
      ctx.lineTo(width - right, y);
      ctx.stroke();
      ctx.globalAlpha = 1;
    }
  }
}

function heatCell(event) {
  if (!heatData) return null;
  const rect = heat.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  const geom = heatGeometry(rect.width, rect.height);
  const {left, bottom, top, right} = geom;
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

function railFrequency(event) {
  if (!heatData) return null;
  const rect = heat.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  const geom = heatGeometry(rect.width, rect.height);
  if (x < geom.railLeft || x > geom.railRight || y < geom.top || y > rect.height - geom.bottom) return null;
  const rows = heatData.frequencies_hz.length;
  const cellH = (rect.height-geom.top-geom.bottom) / Math.max(1, rows);
  const rowInv = Math.floor((y-geom.top)/cellH);
  const row = rows - 1 - rowInv;
  if (row < 0 || row >= rows) return null;
  return heatData.frequencies_hz[row];
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
  hoverFreqHz = null;
  document.getElementById('zoomState').textContent = '';
  document.getElementById('hoverReadout').textContent = '';
  load(false, 'Resetting View');
}

async function selectFrequency(freq) {
  selectedFreqHz = freq;
  document.getElementById('captureBtn').disabled = captureRunning ? true : false;
  document.getElementById('deepScanBtn').disabled = deepScanRunning ? true : false;
  document.getElementById('toolbarSelected').textContent = `${(freq/1e6).toFixed(3)} MHz`;
  document.getElementById('selected').innerHTML = `<b>${(freq/1e6).toFixed(3)} MHz</b><br><span class="muted">Loading detail...</span>`;
  updateBandContext(freq);
  const data = await fetch(`/api/frequency/${freq}?hours=6&span_mhz=2`).then(r=>r.json());
  document.getElementById('selected').innerHTML = `<b>${(freq/1e6).toFixed(3)} MHz</b><br><span class="muted">Nearest bins: ${Object.keys(data.series).length}<br>Context: ${bandSummary(freq)}</span>`;
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

function drawDeepScan() {
  const {ctx, width, height} = resizeCanvas(deepCanvas);
  ctx.clearRect(0,0,width,height);
  if (!deepScanData || !deepScanData.points?.length) {
    ctx.fillStyle = '#9caab5';
    ctx.font = '12px system-ui';
    ctx.fillText('Focused scan results will appear here.', 12, 24);
    return;
  }
  const pts = deepScanData.points;
  const minFreq = pts[0].frequency_mhz;
  const maxFreq = pts[pts.length - 1].frequency_mhz;
  const values = pts.map(p => p.rssi_db);
  const minDb = Math.min(...values);
  const maxDb = Math.max(...values);
  const left = 42, right = 10, top = 12, bottom = 34;
  const plotW = width - left - right;
  const plotH = height - top - bottom;
  ctx.strokeStyle = '#2b353d';
  ctx.strokeRect(left, top, plotW, plotH);
  ctx.strokeStyle = '#24313a';
  for (let i=1; i<4; i++) {
    const y = top + i * plotH / 4;
    ctx.beginPath();
    ctx.moveTo(left, y);
    ctx.lineTo(left + plotW, y);
    ctx.stroke();
  }
  const widths = pts.map(p => Number(p.bin_width_hz || 0)).filter(Boolean).sort((a,b)=>a-b);
  const expectedHz = widths.length ? widths[Math.floor(widths.length / 2)] : 0;
  const gapStarts = new Set();
  for (let i=1; i<pts.length; i++) {
    const deltaHz = pts[i].frequency_hz - pts[i-1].frequency_hz;
    if (expectedHz && deltaHz > expectedHz * 2.5) {
      gapStarts.add(i);
      const x1 = left + ((pts[i-1].frequency_mhz - minFreq) / Math.max(0.0001, maxFreq - minFreq)) * plotW;
      const x2 = left + ((pts[i].frequency_mhz - minFreq) / Math.max(0.0001, maxFreq - minFreq)) * plotW;
      ctx.fillStyle = 'rgba(35,45,52,0.72)';
      ctx.fillRect(x1, top, Math.max(2, x2 - x1), plotH);
      ctx.strokeStyle = '#36424b';
      ctx.strokeRect(x1, top, Math.max(2, x2 - x1), plotH);
    }
  }
  ctx.strokeStyle = '#ffca62';
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  pts.forEach((p, i) => {
    const x = left + ((p.frequency_mhz - minFreq) / Math.max(0.0001, maxFreq - minFreq)) * plotW;
    const y = top + (1 - ((p.rssi_db - minDb) / Math.max(1, maxDb - minDb))) * plotH;
    if (i === 0 || gapStarts.has(i)) ctx.moveTo(x,y); else ctx.lineTo(x,y);
  });
  ctx.stroke();
  const peak = deepScanData.peak;
  if (peak) {
    const x = left + ((peak.frequency_mhz - minFreq) / Math.max(0.0001, maxFreq - minFreq)) * plotW;
    ctx.strokeStyle = '#ff6f91';
    ctx.beginPath();
    ctx.moveTo(x, top);
    ctx.lineTo(x, top + plotH);
    ctx.stroke();
  }
  ctx.fillStyle = '#9caab5';
  ctx.font = '12px system-ui';
  ctx.fillText(`${minFreq.toFixed(3)} MHz`, left, height - 10);
  ctx.fillText(`${maxFreq.toFixed(3)} MHz`, Math.max(left, width - 112), height - 10);
  ctx.fillText(`${maxDb.toFixed(1)} dB`, 4, top + 10);
  ctx.fillText(`${minDb.toFixed(1)} dB`, 4, top + plotH);
}

function focusedPeakHtml(data) {
  const peaks = data.peaks || (data.peak ? [data.peak] : []);
  if (!peaks.length) return '';
  return `<div class="peakList"><b>Labelled peaks</b><br>` +
    peaks.map((p, idx) => `${idx + 1}. ${p.frequency_mhz.toFixed(6)} MHz · ${p.rssi_db.toFixed(1)} dB`).join('<br>') +
    `</div>`;
}

function toggleGifPreview(imgId) {
  const img = document.getElementById(imgId);
  if (!img) return;
  const playing = img.dataset.playing !== 'false';
  const nextUrl = playing ? img.dataset.stillUrl : img.dataset.animationUrl;
  if (!nextUrl) return;
  img.dataset.playing = playing ? 'false' : 'true';
  img.src = `${nextUrl}?t=${Date.now()}`;
  const pill = document.getElementById(`${imgId}-pill`);
  if (pill) pill.textContent = playing ? 'Paused' : 'Playing';
}

async function runFocusedScan(freq=selectedFreqHz) {
  if (!freq || deepScanRunning) return;
  const activity = showActivity('Focused Scan');
  deepScanRunning = true;
  document.getElementById('deepScanBtn').disabled = true;
  document.getElementById('deepScanStatus').innerHTML = `<b>${(freq/1e6).toFixed(3)} MHz</b><br>Running focused scan...`;
  document.getElementById('deepScanMedia').innerHTML = '';
  const span = Number(document.getElementById('deepSpan').value);
  const bin = Number(document.getElementById('deepBin').value);
  try {
    const response = await fetch('/api/deep-scan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({frequency_hz: freq, span_mhz: span, bin_width_hz: bin})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || data.error || 'Focused scan failed');
    deepScanData = data;
    const peak = data.peak;
    const gapNote = data.gap_count ? ` · ${data.gap_count} gap${data.gap_count === 1 ? '' : 's'} marked` : '';
    document.getElementById('deepScanStatus').innerHTML =
      `<b>${data.center_frequency_mhz.toFixed(3)} MHz focused scan</b><br>` +
      `<span class="muted">${data.points.length} bins · ${data.bin_width_khz.toFixed(0)} kHz · ${data.frame_count || 1} frames${gapNote} · peak ${peak.frequency_mhz.toFixed(6)} MHz at ${peak.rssi_db.toFixed(1)} dB</span>`;
    const deepImgId = `deep-scan-img-${data.id}`;
    document.getElementById('deepScanMedia').innerHTML =
      `<div class="mediaShell"><img id="${deepImgId}" class="deepScanImg" data-playing="true" data-animation-url="${data.animation_url}" data-still-url="${data.still_url}" onclick="toggleGifPreview('${deepImgId}')" ondblclick="window.open('${data.viewer_url}', '_blank')" src="${data.animation_url}?t=${encodeURIComponent(data.completed_at)}" alt="Animated focused scan around ${data.center_frequency_mhz.toFixed(3)} MHz"><span id="${deepImgId}-pill" class="pausePill">Playing</span></div>` +
      `<div class="captureActions"><button class="smallBtn" onclick="toggleGifPreview('${deepImgId}')">Play / Pause</button><button class="smallBtn" onclick="window.open('${data.viewer_url}', '_blank')">View Larger</button></div>` +
      focusedPeakHtml(data);
    drawDeepScan();
  } catch (err) {
    document.getElementById('deepScanStatus').innerHTML = `<b>Focused scan failed</b><br><span class="muted">${err.message}</span>`;
  } finally {
    deepScanRunning = false;
    document.getElementById('deepScanBtn').disabled = selectedFreqHz === null;
    hideActivity(activity);
  }
}

async function load(quick=false, activityLabel=null) {
  const activity = activityLabel ? showActivity(activityLabel) : null;
  document.getElementById('statusText').textContent = quick ? 'Loading quick heatmap' : 'Loading heatmap';
  const selectedHours = document.getElementById('hours').value;
  const hours = quick && zoomMinHz === null && zoomMaxHz === null ? '0.25' : selectedHours;
  const step = document.getElementById('freqStep').value;
  const bucket = Number(hours) <= 1 ? '1m' : Number(hours) <= 6 ? '3m' : '5m';
  const zoomParams = zoomMinHz !== null && zoomMaxHz !== null ? `&freq_min_hz=${zoomMinHz}&freq_max_hz=${zoomMaxHz}` : '';
  try {
    const response = await fetch(`/api/heatmap?hours=${hours}&freq_step_mhz=${step}&time_bucket=${bucket}${zoomParams}`);
    if (!response.ok) {
      const detail = await response.text();
      throw new Error(detail || `Heatmap request failed: ${response.status}`);
    }
    heatData = await response.json();
    document.getElementById('sweeps').textContent = heatData.points ?? 0;
    document.getElementById('maxRssi').textContent = heatData.max == null ? '--' : `${heatData.max.toFixed(1)} dB`;
    drawHeatmap();
    document.getElementById('statusText').textContent = `${quick ? 'Quick heatmap' : 'Heatmap'} updated ${new Date().toLocaleTimeString()}`;
  } catch (err) {
    document.getElementById('statusText').textContent = heatData ? `Heatmap refresh failed; showing previous data` : 'Heatmap load failed';
    if (!heatData) {
      const {ctx, width, height} = resizeCanvas(heat);
      ctx.clearRect(0,0,width,height);
      ctx.fillStyle = '#9caab5';
      ctx.font = '14px system-ui';
      ctx.fillText('Heatmap query is still warming up or timed out. Try 1 h / 10-25 MHz, then Refresh.', 24, 34);
    }
  }
  loadStatusPanels();
  loadCaptures();
  hideActivity(activity);
}

async function initialLoad() {
  await load(true);
  load(false);
}

async function loadStatusPanels() {
  try {
    const status = await fetch('/api/status').then(r=>r.json());
    document.getElementById('activeAnoms').textContent = status.anomalies_active ?? 0;
    if (status.updated_at) document.getElementById('statusText').textContent = `Last update ${new Date(status.updated_at).toLocaleString()}`;
  } catch (err) {}
  try {
    const anoms = await fetch('/api/anomalies?limit=12').then(r=>r.json());
    document.getElementById('anomalies').innerHTML = (anoms.items || []).map(a =>
      `<div class="item" onclick="selectFrequency(${a.frequency_hz})"><b>${(a.frequency_hz/1e6).toFixed(3)} MHz</b><br>${a.delta_db.toFixed(1)} dB over baseline · ${a.rssi_db.toFixed(1)} dB<br><span class="muted">${a.detected_at}</span></div>`
    ).join('') || '<div class="muted">No anomalies yet. Baseline is warming up.</div>';
  } catch (err) {}
  try {
    const top = await fetch('/api/top?hours=1&limit=12').then(r=>r.json());
    document.getElementById('top').innerHTML = (top.items || []).map(a =>
      `<div class="item" onclick="selectFrequency(${a.frequency_hz})"><b>${a.frequency_mhz.toFixed(3)} MHz</b><br>${a.rssi_db.toFixed(1)} dB<br><span class="muted">${a.time}</span></div>`
    ).join('');
  } catch (err) {}
}

function captureItemHtml(capture) {
  const analysis = capture.analysis || {};
  const audio = capture.audio_urls || {};
  const audioStamp = encodeURIComponent(capture.completed_at || capture.started_at || Date.now());
  const audioSrc = mode => audio[mode] ? `${audio[mode]}?t=${audioStamp}` : '';
  const status = capture.status === 'complete' ? 'Complete' : capture.status;
  const img = capture.status === 'complete' ? `
    <div class="captureToggle">
      <button class="smallBtn" onclick="showCaptureArtifact('${capture.id}', '${capture.animation_url}', 'animation')">Animation</button>
      <button class="smallBtn" onclick="showCaptureArtifact('${capture.id}', '${capture.spectrogram_url}', 'spectrogram')">Spectrogram</button>
    </div>
    <div class="mediaShell"><img id="capture-img-${capture.id}" class="captureImg" onclick="toggleGifPreview('capture-img-${capture.id}')" ondblclick="window.open('${capture.animation_viewer_url}', '_blank')" data-playing="true" data-animation-url="${capture.animation_url}" data-still-url="${capture.animation_still_url}" data-current-url="${capture.animation_url}" src="${capture.animation_url}?t=${encodeURIComponent(capture.completed_at || capture.started_at)}" alt="Animated spectrum for ${capture.frequency_mhz.toFixed(3)} MHz capture"><span id="capture-img-${capture.id}-pill" class="pausePill">Playing</span></div>
    <div id="capture-caption-${capture.id}" class="captureCaption">Animated spectrum: each frame is about 1 second. X axis is frequency, Y axis is relative strength. Download IQ is the raw radio sample file for later demodulation/classification.</div>
    <div class="audioPreview">
      <div class="audioRow"><span>AM</span><audio controls preload="metadata"><source src="${audioSrc('am')}" type="audio/wav"></audio><a class="audioLink" href="${audioSrc('am')}" target="_blank">Open</a></div>
      <div class="audioRow"><span>NFM</span><audio controls preload="metadata"><source src="${audioSrc('nfm')}" type="audio/wav"></audio><a class="audioLink" href="${audioSrc('nfm')}" target="_blank">Open</a></div>
      <div class="audioRow"><span>WFM</span><audio controls preload="metadata"><source src="${audioSrc('wfm')}" type="audio/wav"></audio><a class="audioLink" href="${audioSrc('wfm')}" target="_blank">Open</a></div>
    </div>
    <div class="captureCaption">Try AM, NFM, and WFM. If all three are static/noise, the signal is probably digital, too wide/narrow for this preset, offset from center, or not audio-bearing.</div>` : '';
  const peak = analysis.peak_frequency_mhz ? `<br>Peak ${analysis.peak_frequency_mhz.toFixed(6)} MHz` : '';
  return `<div class="item">
    <b>${capture.frequency_mhz.toFixed(3)} MHz</b><br>
    ${status} · ${capture.duration_seconds}s @ ${(capture.sample_rate_hz/1e6).toFixed(0)} Msps${peak}<br>
    <span class="muted">${capture.started_at}</span>
    <div class="captureActions">
      <button class="smallBtn" onclick="selectFrequency(${capture.frequency_hz})">Select</button>
      <button class="smallBtn" onclick="toggleGifPreview('capture-img-${capture.id}')">Play / Pause</button>
      <button class="smallBtn" onclick="window.open('${capture.animation_viewer_url}', '_blank')">View Animation</button>
      <button class="smallBtn" onclick="window.open('${capture.spectrogram_url}', '_blank')">View Spectrogram</button>
      <button class="smallBtn" onclick="window.open('${capture.meta_url}', '_blank')">Details</button>
      <button class="smallBtn" onclick="window.open('${capture.iq_url}', '_blank')">Download IQ</button>
    </div>
    ${img}
  </div>`;
}

function showCaptureArtifact(captureId, url, kind) {
  const img = document.getElementById(`capture-img-${captureId}`);
  const caption = document.getElementById(`capture-caption-${captureId}`);
  if (!img || !caption) return;
  img.dataset.currentUrl = url;
  img.dataset.playing = kind === 'animation' ? 'true' : 'false';
  img.src = `${url}?t=${Date.now()}`;
  const pill = document.getElementById(`capture-img-${captureId}-pill`);
  if (kind === 'animation') {
    img.alt = 'Animated spectrum view';
    if (pill) pill.textContent = 'Playing';
    caption.textContent = 'Animated spectrum: each frame is about 1 second. X axis is frequency, Y axis is relative strength. Download IQ is the raw radio sample file for later demodulation/classification.';
  } else {
    img.alt = 'Spectrogram view';
    if (pill) pill.textContent = 'Still';
    caption.textContent = 'Spectrogram: time and frequency are shown together, with color indicating strength. Download IQ is the raw radio sample file for later demodulation/classification.';
  }
}

function openCaptureArtifact(captureId) {
  const img = document.getElementById(`capture-img-${captureId}`);
  if (img?.dataset.currentUrl) window.open(img.dataset.currentUrl, '_blank');
}

async function loadCaptures() {
  try {
    const data = await fetch('/api/captures?limit=4').then(r=>r.json());
    captureRunning = data.capture_running;
    document.getElementById('captureBtn').disabled = captureRunning || selectedFreqHz === null;
    document.getElementById('captures').innerHTML = (data.items || []).map(captureItemHtml).join('') || '<div class="muted">No captures yet.</div>';
  } catch (err) {
    document.getElementById('captures').innerHTML = '<div class="muted">Capture list unavailable.</div>';
  }
}

async function captureSelected() {
  if (!selectedFreqHz || captureRunning) return;
  const activity = showActivity('Capturing');
  const seconds = Number(document.getElementById('captureSeconds').value);
  const sampleRate = Number(document.getElementById('captureRate').value);
  captureRunning = true;
  document.getElementById('captureBtn').disabled = true;
  document.getElementById('captureStatus').innerHTML = `<b>${(selectedFreqHz/1e6).toFixed(3)} MHz</b><br>Capturing ${seconds}s of IQ and building a spectrogram...`;
  try {
    const response = await fetch('/api/capture', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({frequency_hz: selectedFreqHz, duration_seconds: seconds, sample_rate_hz: sampleRate})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || data.error || 'Capture failed');
    const analysis = data.analysis || {};
    document.getElementById('captureStatus').innerHTML =
      `<b>Capture complete</b><br>${data.frequency_mhz.toFixed(3)} MHz · ${data.duration_seconds}s · peak ${analysis.peak_frequency_mhz?.toFixed(6) || '--'} MHz`;
    loadCaptures();
    load();
  } catch (err) {
    document.getElementById('captureStatus').innerHTML = `<b>Capture failed</b><br><span class="muted">${err.message}</span>`;
  } finally {
    captureRunning = false;
    document.getElementById('captureBtn').disabled = selectedFreqHz === null;
    hideActivity(activity);
  }
}

heat.addEventListener('click', e => {
  const cell = heatCell(e);
  const railFreq = railFrequency(e);
  const freq = cell ? cell.freq : railFreq;
  if (freq) {
    zoomAround(freq);
    selectFrequency(freq);
    load(false, 'Zooming In');
  }
});
heat.addEventListener('dblclick', e => {
  const cell = heatCell(e);
  const railFreq = railFrequency(e);
  const freq = cell ? cell.freq : railFreq;
  if (freq) {
    zoomAround(freq);
    selectFrequency(freq);
    load(false, 'Zooming In');
    runFocusedScan(freq);
  }
});
heat.addEventListener('mousemove', e => {
  hoverCell = heatCell(e);
  const railFreq = railFrequency(e);
  hoverFreqHz = hoverCell ? hoverCell.freq : railFreq;
  if (!hoverCell && !railFreq) {
    document.getElementById('hoverReadout').textContent = '';
    drawHeatmap();
    return;
  }
  updateBandContext(hoverFreqHz);
  if (hoverCell) {
    const rssi = hoverCell.value === null ? 'no data' : `${hoverCell.value.toFixed(1)} dB`;
    document.getElementById('hoverReadout').textContent = `${(hoverCell.freq/1e6).toFixed(3)} MHz · ${rssi} · ${new Date(hoverCell.time).toLocaleTimeString()} · ${bandSummary(hoverCell.freq)}`;
  } else {
    document.getElementById('hoverReadout').textContent = `${(railFreq/1e6).toFixed(3)} MHz · ${bandSummary(railFreq)}`;
  }
  drawHeatmap();
});
heat.addEventListener('mouseleave', () => {
  hoverCell = null;
  hoverFreqHz = null;
  document.getElementById('hoverReadout').textContent = '';
  drawHeatmap();
});
window.addEventListener('resize', () => { drawHeatmap(); drawDeepScan(); });
document.getElementById('refresh').addEventListener('click', () => load(false, 'Refreshing'));
document.getElementById('resetZoom').addEventListener('click', resetZoom);
document.getElementById('captureBtn').addEventListener('click', captureSelected);
document.getElementById('deepScanBtn').addEventListener('click', () => runFocusedScan());
document.getElementById('hours').addEventListener('change', load);
document.getElementById('freqStep').addEventListener('change', () => { resetZoom(); });
initialLoad();
drawDeepScan();
loadCaptures();
setInterval(load, 60000);
setInterval(loadCaptures, 60000);
</script>
</body>
</html>
"""
