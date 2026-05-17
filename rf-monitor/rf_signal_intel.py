from __future__ import annotations

from typing import Any


BAND_REFS: list[dict[str, Any]] = [
    {"name": "VHF", "min": 30, "max": 300, "kind": "broad radio band"},
    {"name": "6 m amateur", "min": 50, "max": 54, "kind": "amateur radio"},
    {"name": "FM broadcast", "min": 88, "max": 108, "kind": "broadcast FM"},
    {"name": "Airband", "min": 108, "max": 137, "kind": "aviation AM voice/navigation"},
    {"name": "2 m amateur", "min": 144, "max": 148, "kind": "amateur radio"},
    {"name": "NOAA weather", "min": 162.4, "max": 162.55, "kind": "weather radio"},
    {"name": "UHF", "min": 300, "max": 3000, "kind": "broad radio band"},
    {"name": "70 cm amateur", "min": 420, "max": 450, "kind": "amateur radio"},
    {"name": "433 MHz ISM", "min": 433.05, "max": 434.79, "kind": "short-range ISM"},
    {"name": "902-928 MHz ISM", "min": 902, "max": 928, "kind": "ISM/LoRa/telemetry"},
    {"name": "ADS-B / Mode S", "min": 1088, "max": 1092, "kind": "aircraft transponder"},
    {"name": "GPS L2 / GNSS", "min": 1220, "max": 1235, "kind": "GNSS downlink neighborhood"},
    {"name": "23 cm amateur", "min": 1240, "max": 1300, "kind": "amateur radio"},
    {"name": "GPS L1 / GNSS", "min": 1570, "max": 1580, "kind": "GNSS downlink neighborhood"},
    {"name": "L-band satcom", "min": 1525, "max": 1660, "kind": "satellite/mobile satcom"},
    {"name": "L-band weather satellites", "min": 1670, "max": 1710, "kind": "weather satellite neighborhood"},
    {"name": "13 cm amateur", "min": 2300, "max": 2450, "kind": "amateur/ISM overlap"},
    {"name": "2.4 GHz ISM / Wi-Fi", "min": 2400, "max": 2483.5, "kind": "Wi-Fi/Bluetooth/Zigbee/ISM"},
    {"name": "9 cm amateur", "min": 3300, "max": 3500, "kind": "amateur microwave"},
    {"name": "C-band sat downlink", "min": 3700, "max": 4200, "kind": "satellite downlink neighborhood"},
    {"name": "5 GHz Wi-Fi / U-NII", "min": 5150, "max": 5895, "kind": "Wi-Fi/U-NII"},
    {"name": "5.8 GHz ISM", "min": 5725, "max": 5875, "kind": "ISM/video/telemetry"},
    {"name": "5 cm amateur", "min": 5650, "max": 5925, "kind": "amateur microwave"},
]


def band_matches(frequency_mhz: float) -> list[dict[str, Any]]:
    return [band for band in BAND_REFS if band["min"] <= frequency_mhz <= band["max"]]


def classify_signal(frequency_hz: int, context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = context or {}
    frequency_mhz = frequency_hz / 1_000_000
    matches = band_matches(frequency_mhz)
    anomaly = context.get("anomaly") or {}
    delta_db = anomaly.get("delta_db")
    rssi_db = anomaly.get("rssi_db")
    baseline_db = anomaly.get("baseline_rssi_db")

    likely = "Unknown RF energy"
    confidence = "low"
    if matches:
        primary = matches[-1]
        likely = primary["kind"]
        confidence = "medium"
    if any("FM broadcast" == band["name"] for band in matches):
        likely = "FM broadcast carrier or adjacent broadcast energy"
        confidence = "high"
    elif any("Wi-Fi" in band["name"] or "ISM" in band["name"] for band in matches):
        likely = "unlicensed ISM/Wi-Fi/Bluetooth/Zigbee-style activity"
        confidence = "medium"
    elif any("Airband" == band["name"] for band in matches):
        likely = "aviation band signal, likely AM voice/navigation neighborhood"
        confidence = "medium"
    elif any("amateur" in band["name"].lower() for band in matches):
        likely = "amateur radio band activity"
        confidence = "medium"
    elif any("GPS" in band["name"] or "GNSS" in band["name"] for band in matches):
        likely = "GNSS neighborhood; local SDR may see weak/broad energy or interference"
        confidence = "medium-low"

    interest = 35
    reasons = []
    if matches:
        reasons.append("Frequency falls in: " + ", ".join(band["name"] for band in matches))
        interest += min(20, len(matches) * 5)
    else:
        reasons.append("No local reference-band match; this is more interesting than a known band hit.")
        interest += 15
    if delta_db is not None:
        interest += min(30, max(0, float(delta_db)))
        reasons.append(f"Recent level is {float(delta_db):.1f} dB above its baseline.")
    if rssi_db is not None:
        reasons.append(f"Recent RSSI is {float(rssi_db):.1f} dB.")
        if float(rssi_db) >= -45:
            interest += 10
    if baseline_db is not None:
        reasons.append(f"Baseline for this bin is {float(baseline_db):.1f} dB.")

    next_actions = [
        "Run a focused scan around this frequency to estimate bandwidth and nearby peaks.",
        "Capture IQ if the signal persists or appears narrow/structured.",
        "Try AM, NFM, and WFM audio previews; static/noise may indicate digital or non-audio modulation.",
    ]
    if matches and any("FM broadcast" == band["name"] for band in matches):
        next_actions.insert(0, "Compare against known local FM stations before treating this as unusual.")
    if matches and any("Wi-Fi" in band["name"] or "ISM" in band["name"] for band in matches):
        next_actions.insert(0, "Expect frequent local-device activity; persistence and bandwidth matter more than a single spike.")

    return {
        "frequency_hz": frequency_hz,
        "frequency_mhz": round(frequency_mhz, 6),
        "likely": likely,
        "confidence": confidence,
        "interestingness": max(0, min(100, round(interest))),
        "bands": [{"name": band["name"], "kind": band["kind"], "min_mhz": band["min"], "max_mhz": band["max"]} for band in matches],
        "reasons": reasons,
        "next_actions": next_actions,
        "note": "This is heuristic signal triage, not a confirmed modulation/classification result.",
    }
