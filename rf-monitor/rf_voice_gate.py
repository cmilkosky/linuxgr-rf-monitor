"""Client for the rf-voice-gate service (Silero VAD v5 on loopback :10505).

The gate runs as its own unprivileged account (`rfvoice`) and takes audio bytes
over HTTP, so the RF lanes never need exec rights on another user's home - the
reason this is a client and not an import. Every call is failure-tolerant: a gate
outage must never take a recording lane down with it.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

URL = os.environ.get("RF_VOICE_GATE_URL", "http://127.0.0.1:10505/gate")
TIMEOUT_S = float(os.environ.get("RF_VOICE_GATE_TIMEOUT_S", "30"))
DISABLED = os.environ.get("RF_VOICE_GATE_DISABLE", "").lower() in ("1", "true", "yes")


def gate_file(path: Path | str, timeout_s: float | None = None) -> dict | None:
    """POST one audio file to the gate. Returns the verdict dict, or None if the
    gate is disabled, unreachable, or could not decode the audio."""
    if DISABLED:
        return None
    try:
        data = Path(path).read_bytes()
        req = urllib.request.Request(
            URL, data=data, method="POST",
            headers={"Content-Type": "application/octet-stream"})
        with urllib.request.urlopen(req, timeout=timeout_s or TIMEOUT_S) as resp:
            verdict = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError):
        return None
    return verdict if verdict.get("ok") else None


def is_voice(path: Path | str) -> bool:
    verdict = gate_file(path)
    return bool(verdict and verdict.get("gate_pass"))


def summary(verdict: dict | None) -> str:
    """One-line human form for logs/DB summaries."""
    if not verdict:
        return "vad unavailable"
    return (f"vad {verdict['speech_pct']:.1f}% speech, max {verdict['max_prob']:.2f}, "
            f"{len(verdict.get('spans') or [])} spans, {verdict.get('elapsed_ms', 0):.0f} ms")
