from __future__ import annotations

import math
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


SIGNAL_REFS: list[dict[str, Any]] = [
    {
        "name": "FM broadcast",
        "min_mhz": 88,
        "max_mhz": 108,
        "bandwidth_hz": (120_000, 250_000),
        "traits": ("wide_fm", "continuous"),
        "why": "FM broadcast is a wide, steady WFM signal in the 88-108 MHz band.",
    },
    {
        "name": "Airband AM voice/navigation",
        "min_mhz": 108,
        "max_mhz": 137,
        "bandwidth_hz": (6_000, 30_000),
        "traits": ("narrow", "am_audio"),
        "why": "Airband channels are narrow AM signals around 108-137 MHz.",
    },
    {
        "name": "NOAA weather radio",
        "min_mhz": 162.4,
        "max_mhz": 162.55,
        "bandwidth_hz": (8_000, 40_000),
        "traits": ("narrow", "fm_audio", "continuous"),
        "why": "NOAA weather channels are continuous narrow FM signals near 162.4-162.55 MHz.",
    },
    {
        "name": "FT8 / weak-signal amateur digital candidate",
        "bands_mhz": [(50.313, 50.318), (144.174, 144.176)],
        "bandwidth_hz": (20, 3_000),
        "traits": ("very_narrow", "structured_digital"),
        "why": "FT8 is a very narrow amateur digital mode; frequency context and narrow tone structure matter.",
    },
    {
        "name": "LoRa / Meshtastic-style CSS candidate",
        "bands_mhz": [(433.05, 434.79), (902, 928)],
        "bandwidth_hz": (60_000, 600_000),
        "traits": ("chirp_like", "bursty"),
        "why": "LoRa/Meshtastic uses chirp spread spectrum, usually bursty, often in 433 MHz or 902-928 MHz ISM ranges.",
    },
    {
        "name": "Wi-Fi / Bluetooth / Zigbee ISM activity",
        "bands_mhz": [(2400, 2483.5), (5150, 5895)],
        "bandwidth_hz": (1_000_000, 80_000_000),
        "traits": ("wide", "bursty"),
        "why": "2.4/5 GHz ISM activity is often wide and bursty, with many overlapping local devices.",
    },
    {
        "name": "ADS-B / Mode S aircraft transponder",
        "min_mhz": 1088,
        "max_mhz": 1092,
        "bandwidth_hz": (500_000, 3_000_000),
        "traits": ("pulse", "bursty"),
        "why": "ADS-B/Mode S lives around 1090 MHz and appears as short pulses/bursts.",
    },
    {
        "name": "Radar / FMCW sweep candidate",
        "min_mhz": 30,
        "max_mhz": 6000,
        "bandwidth_hz": (500_000, 80_000_000),
        "traits": ("chirp_like", "wide"),
        "why": "Radar-like signals often show sweeping/chirp energy or repeated wideband structure.",
    },
    {
        "name": "Narrowband FM / FSK telemetry candidate",
        "min_mhz": 30,
        "max_mhz": 1000,
        "bandwidth_hz": (4_000, 50_000),
        "traits": ("narrow", "structured_digital"),
        "why": "Many pagers, telemetry links, and land-mobile data signals are narrow FSK/NFM-like emissions.",
    },
]


def band_matches(frequency_mhz: float) -> list[dict[str, Any]]:
    return [band for band in BAND_REFS if band["min"] <= frequency_mhz <= band["max"]]


def _ref_frequency_match(ref: dict[str, Any], frequency_mhz: float) -> bool:
    if "bands_mhz" in ref:
        return any(low <= frequency_mhz <= high for low, high in ref["bands_mhz"])
    return ref["min_mhz"] <= frequency_mhz <= ref["max_mhz"]


def _estimate_occupied_bandwidth(freqs: Any, spectrum_db: Any, threshold_db: float = 12.0) -> tuple[float, float, float]:
    import numpy as np

    peak = float(np.max(spectrum_db))
    floor = float(np.percentile(spectrum_db, 20))
    threshold = max(floor + 6.0, peak - threshold_db)
    mask = spectrum_db >= threshold
    if not np.any(mask):
        peak_idx = int(np.argmax(spectrum_db))
        peak_freq = float(freqs[peak_idx])
        return 0.0, peak_freq, peak_freq
    idx = np.where(mask)[0]
    low = float(freqs[int(idx[0])])
    high = float(freqs[int(idx[-1])])
    return max(0.0, high - low), low, high


def extract_iq_features(iq: Any, sample_rate_hz: int, center_frequency_hz: int) -> dict[str, Any]:
    import numpy as np

    usable = iq[: min(iq.size, sample_rate_hz * 5)]
    if usable.size < 2048:
        raise ValueError("Capture is too short for feature extraction")

    nfft = 4096 if usable.size >= 4096 else 2048
    window = np.hanning(nfft).astype(np.float32)
    hop = max(nfft // 4, min(nfft, usable.size // 256))
    starts = np.arange(0, usable.size - nfft + 1, hop, dtype=np.int64)
    spectra = np.empty((nfft, starts.size), dtype=np.float32)
    for col, start in enumerate(starts):
        chunk = usable[start : start + nfft] * window
        spectra[:, col] = np.abs(np.fft.fftshift(np.fft.fft(chunk))).astype(np.float32)

    freqs = np.fft.fftshift(np.fft.fftfreq(nfft, d=1.0 / sample_rate_hz))
    power = np.maximum(np.mean(spectra**2, axis=1), 1e-18)
    spectrum_db = 10 * np.log10(power)
    peak_idx = int(np.argmax(spectrum_db))
    peak_offset_hz = float(freqs[peak_idx])
    occupied_hz, low_offset_hz, high_offset_hz = _estimate_occupied_bandwidth(freqs, spectrum_db)

    median = float(np.median(spectrum_db))
    peak_db = float(np.max(spectrum_db))
    peak_mask = spectrum_db > median + 10.0
    peak_count = 0
    in_peak = False
    for active in peak_mask:
        if bool(active) and not in_peak:
            peak_count += 1
        in_peak = bool(active)

    spectral_flatness = float(math.exp(float(np.mean(np.log(power)))) / (float(np.mean(power)) + 1e-18))
    inst_phase = np.unwrap(np.angle(usable))
    inst_freq = np.diff(inst_phase) * sample_rate_hz / (2 * np.pi)
    inst_freq_std_hz = float(np.std(inst_freq)) if inst_freq.size else 0.0
    envelope = np.abs(usable)
    env_threshold = float(np.percentile(envelope, 75))
    duty_cycle = float(np.mean(envelope >= env_threshold))
    frame_power = np.mean(np.abs(usable[: (usable.size // 1024) * 1024].reshape(-1, 1024)) ** 2, axis=1)
    burstiness = float(np.std(frame_power) / (np.mean(frame_power) + 1e-12)) if frame_power.size else 0.0

    traits: list[str] = []
    if occupied_hz < 5_000:
        traits.append("very_narrow")
    elif occupied_hz < 60_000:
        traits.append("narrow")
    elif occupied_hz > 1_000_000:
        traits.append("wide")
    if 100_000 <= occupied_hz <= 300_000 and inst_freq_std_hz > 20_000:
        traits.append("wide_fm")
    if burstiness > 0.55:
        traits.append("bursty")
    else:
        traits.append("continuous")
    if spectral_flatness > 0.55:
        traits.append("noise_like_or_spread")
    if inst_freq_std_hz > max(50_000, occupied_hz * 0.2):
        traits.append("chirp_like")
    if peak_count >= 3 and occupied_hz < 500_000:
        traits.append("structured_digital")
    if occupied_hz < 35_000 and burstiness < 0.45:
        traits.append("am_audio")
        traits.append("fm_audio")

    return {
        "peak_frequency_hz": int(center_frequency_hz + peak_offset_hz),
        "peak_frequency_mhz": round((center_frequency_hz + peak_offset_hz) / 1_000_000, 6),
        "occupied_bandwidth_hz": int(round(occupied_hz)),
        "occupied_low_hz": int(center_frequency_hz + low_offset_hz),
        "occupied_high_hz": int(center_frequency_hz + high_offset_hz),
        "peak_count": int(peak_count),
        "spectral_flatness": round(spectral_flatness, 4),
        "instantaneous_frequency_std_hz": int(round(inst_freq_std_hz)),
        "duty_cycle": round(duty_cycle, 3),
        "burstiness": round(burstiness, 3),
        "traits": sorted(set(traits)),
        "mean_power_dbfs": round(float(20 * np.log10(np.sqrt(np.mean(np.abs(usable) ** 2)) + 1e-12)), 2),
    }


def match_signal_candidates(frequency_hz: int, features: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    frequency_mhz = frequency_hz / 1_000_000
    features = features or {}
    has_measurements = bool(features)
    bandwidth = features.get("occupied_bandwidth_hz")
    traits = set(features.get("traits") or [])
    candidates: list[dict[str, Any]] = []

    for ref in SIGNAL_REFS:
        score = 0
        evidence = []
        frequency_match = _ref_frequency_match(ref, frequency_mhz)
        broad_generic = ref.get("min_mhz") == 30 and ref.get("max_mhz", 0) >= 1000
        if frequency_match and not broad_generic:
            score += 40
            evidence.append("frequency range matches")
        elif ref["name"] == "Radar / FMCW sweep candidate" and has_measurements:
            score += 10
        else:
            continue

        bw_range = ref.get("bandwidth_hz")
        if bandwidth is not None and bw_range:
            low, high = bw_range
            if low <= float(bandwidth) <= high:
                score += 25
                evidence.append(f"occupied bandwidth is about {float(bandwidth) / 1000:.1f} kHz")
            elif float(bandwidth) > 0:
                distance = min(abs(float(bandwidth) - low), abs(float(bandwidth) - high))
                if distance < high:
                    score += 8
                    evidence.append("bandwidth is near the expected range")

        matching_traits = [trait for trait in ref.get("traits", ()) if trait in traits]
        if matching_traits:
            score += min(25, len(matching_traits) * 9)
            evidence.append("observed traits: " + ", ".join(matching_traits))

        if broad_generic and not matching_traits:
            continue

        if score >= 35:
            candidates.append(
                {
                    "name": ref["name"],
                    "score": max(0, min(100, score)),
                    "why": ref["why"],
                    "evidence": evidence,
                }
            )

    candidates.sort(key=lambda item: item["score"], reverse=True)
    return candidates[:5]


def classify_signal(frequency_hz: int, context: dict[str, Any] | None = None) -> dict[str, Any]:
    context = context or {}
    frequency_mhz = frequency_hz / 1_000_000
    matches = band_matches(frequency_mhz)
    anomaly = context.get("anomaly") or {}
    features = context.get("features") or {}
    delta_db = anomaly.get("delta_db")
    rssi_db = anomaly.get("rssi_db")
    baseline_db = anomaly.get("baseline_rssi_db")
    candidates = match_signal_candidates(frequency_hz, features)

    likely = "Unknown RF energy"
    confidence = "low"
    if candidates:
        likely = candidates[0]["name"]
        confidence = "medium" if candidates[0]["score"] < 70 else "high"
    if matches:
        primary = matches[-1]
        if not candidates:
            likely = primary["kind"]
            confidence = "medium"
    if any("FM broadcast" == band["name"] for band in matches) and (not candidates or candidates[0]["score"] < 70):
        likely = "FM broadcast carrier or adjacent broadcast energy"
        confidence = "high"
    elif any("Wi-Fi" in band["name"] or "ISM" in band["name"] for band in matches) and not candidates:
        likely = "unlicensed ISM/Wi-Fi/Bluetooth/Zigbee-style activity"
        confidence = "medium"
    elif any("Airband" == band["name"] for band in matches) and not candidates:
        likely = "aviation band signal, likely AM voice/navigation neighborhood"
        confidence = "medium"
    elif any("amateur" in band["name"].lower() for band in matches) and not candidates:
        likely = "amateur radio band activity"
        confidence = "medium"
    elif any("GPS" in band["name"] or "GNSS" in band["name"] for band in matches) and not candidates:
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
    if features:
        if features.get("occupied_bandwidth_hz") is not None:
            reasons.append(f"Measured occupied bandwidth is about {features['occupied_bandwidth_hz'] / 1000:.1f} kHz.")
        if features.get("traits"):
            reasons.append("Measured traits: " + ", ".join(features["traits"]) + ".")
    if candidates:
        interest += min(20, candidates[0]["score"] / 4)

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
        "features": features,
        "candidates": candidates,
        "reasons": reasons,
        "next_actions": next_actions,
        "note": "This is explainable signal triage, not a confirmed protocol decode. Treat candidates as leads.",
    }
