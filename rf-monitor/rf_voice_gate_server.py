#!/usr/bin/env python3
"""rf-voice-gate: voice-presence gate for RF recordings (Silero VAD v5, CPU only).

POST /gate  body = audio bytes (any fmt ffmpeg can read) -> JSON:
    {"ok":true,"duration_s":..,"speech_pct":..,"max_prob":..,"spans":[[s,e],..],
     "gate_pass":bool,"elapsed_ms":..}
GET  /health -> {"ok":true,"model":..,"requests":..,"busy":..}

Runs as a dedicated unprivileged service account on 127.0.0.1, so the RF lanes
never need exec rights on anyone else's home directory.
"""
from __future__ import annotations

import json, os, subprocess, tempfile, threading, time, wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

import numpy as np
import onnxruntime as ort

SR, FRAME, CTX = 16000, 512, 64
THRESH = float(os.environ.get("VOICE_VAD_THRESH", "0.5"))
MIN_SPEECH_PCT = float(os.environ.get("VOICE_GATE_MIN_PCT", "1.0"))
MAX_BODY = int(os.environ.get("VOICE_MAX_BODY_BYTES", str(64 * 1024 * 1024)))
MODEL = os.environ.get("SILERO_MODEL", "/opt/rf-voice-gate/silero_vad.onnx")

SESSION = ort.InferenceSession(MODEL, providers=["CPUExecutionProvider"])
SR_ARR = np.array([SR], np.int64)
SEM = threading.Semaphore(int(os.environ.get("VOICE_MAX_CONCURRENCY", "2")))
STATE = {"requests": 0, "errors": 0, "started": time.time()}


def decode(path: str, out: str) -> bool:
    """Decode to mono 16 kHz s16; tolerate headerless rtl_fm dumps."""
    for pre in (["-i", path], ["-f", "s16le", "-ar", "12500", "-ac", "1", "-i", path]):
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error"] + pre +
                           ["-ar", str(SR), "-ac", "1", "-c:a", "pcm_s16le", out],
                           capture_output=True, timeout=120)
        if r.returncode == 0 and os.path.getsize(out) > 1000:
            return True
    return False


def vad(audio: np.ndarray):
    state = np.zeros((2, 1, 128), np.float32)
    ctx = np.zeros((1, CTX), np.float32)
    probs = []
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        x = np.concatenate([ctx, audio[i:i + FRAME][None, :]], axis=1).astype(np.float32)
        out, state = SESSION.run(None, {"input": x, "state": state, "sr": SR_ARR})
        ctx = x[:, -CTX:]
        probs.append(float(out[0][0]))
    return np.array(probs)


def spans_of(probs: np.ndarray, dt: float, min_len: float = 0.3):
    spans, start = [], None
    for i, p in enumerate(probs):
        if p >= THRESH and start is None:
            start = i
        elif p < THRESH and start is not None:
            spans.append([round(start * dt, 2), round(i * dt, 2)])
            start = None
    if start is not None:
        spans.append([round(start * dt, 2), round(len(probs) * dt, 2)])
    return [s for s in spans if s[1] - s[0] >= min_len]


def gate(body: bytes) -> dict:
    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory() as w:
        raw = os.path.join(w, "in.bin")
        open(raw, "wb").write(body)
        wav = os.path.join(w, "a.wav")
        if not decode(raw, wav):
            return {"ok": False, "error": "undecodable audio"}
        with wave.open(wav, "rb") as f:
            audio = np.frombuffer(f.readframes(f.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    if len(audio) < FRAME:
        return {"ok": False, "error": "audio too short"}
    probs = vad(audio)
    dur = len(audio) / SR
    pct = 100.0 * float((probs >= THRESH).mean())
    return {"ok": True, "duration_s": round(dur, 2),
            "speech_pct": round(pct, 1),
            "max_prob": round(float(probs.max()), 3),
            "spans": spans_of(probs, FRAME / SR),
            "gate_pass": pct >= MIN_SPEECH_PCT,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}


class Handler(BaseHTTPRequestHandler):
    server_version = "rf-voice-gate/1.0"

    def log_message(self, *_a):
        return

    def _json(self, status: int, payload: dict):
        b = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if urlparse(self.path).path != "/health":
            return self.send_error(404)
        self._json(200, {"ok": True, "model": os.path.basename(MODEL),
                         "requests": STATE["requests"], "errors": STATE["errors"],
                         "uptime_s": round(time.time() - STATE["started"], 1),
                         "thresh": THRESH, "min_pct": MIN_SPEECH_PCT})

    def do_POST(self):
        if urlparse(self.path).path != "/gate":
            return self.send_error(404)
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n <= 0:
                return self._json(400, {"ok": False, "error": "empty body"})
            if n > MAX_BODY:
                return self._json(413, {"ok": False, "error": f"body too large ({n})"})
            body = self.rfile.read(n)
            with SEM:
                payload = gate(body)
            STATE["requests"] += 1
            self._json(200 if payload.get("ok") else 422, payload)
        except Exception as e:
            STATE["errors"] += 1
            self._json(500, {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]})


def main() -> int:
    host = os.environ.get("VOICE_HOST", "127.0.0.1")
    port = int(os.environ.get("VOICE_PORT", "10505"))
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"rf-voice-gate listening on {host}:{port}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
