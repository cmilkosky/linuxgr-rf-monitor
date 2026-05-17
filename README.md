# LinuxGR RF Monitor

HackRF-based RF monitoring for `linuxGR`: sweep the local RF environment, store one RSSI point per frequency bin in InfluxDB, render a browser-based spectrum heatmap, and publish health/anomaly status into Home Assistant.

This repo is the working implementation for a home RF baseline/anomaly project. It is designed to answer:

- What RF energy is present across a wide frequency range right now?
- What changed compared with the recent baseline?
- Which frequency/time block should I drill into next?
- How can Home Assistant alert me without becoming the main RF analysis UI?

![RF monitor heatmap](docs/screenshots/rf-monitor-console.png)

![RF monitor drilldown](docs/screenshots/rf-monitor-drilldown.png)

## Current Architecture

```mermaid
flowchart LR
    HackRF["HackRF One"] --> Sweep["hackrf_sweep<br/>400-6000 MHz<br/>1 MHz bins"]
    Sweep --> Collector["hackrf_influx_collector.py"]
    Collector --> Influx["InfluxDB OSS v2<br/>bucket: rf_monitoring"]
    Influx --> Console["RF Monitor Console<br/>FastAPI + Canvas UI<br/>port 8099"]
    Influx --> Detector["RF Anomaly Detector<br/>rolling baseline"]
    Detector --> Influx
    Detector --> Status["status.json"]
    Status --> HAPub["HA MQTT publisher"]
    HAPub --> HA["Home Assistant<br/>RF Monitor sidebar dashboard"]
```

## Data Flow

```mermaid
sequenceDiagram
    participant SDR as HackRF
    participant Sweep as hackrf_sweep
    participant Collector as Collector service
    participant DB as InfluxDB v2
    participant UI as RF Console
    participant Anom as Anomaly Detector
    participant HA as Home Assistant

    Collector->>Sweep: run one sweep per minute
    Sweep->>SDR: sample wide spectrum
    Sweep-->>Collector: CSV rows with RSSI bins
    Collector->>DB: write rf_sweep points
    UI->>DB: query heatmap/top/drilldown windows
    Anom->>DB: query baseline and recent peaks
    Anom->>DB: write rf_anomaly events
    Anom->>HA: publish MQTT status
```

## Components

### Collector

`hackrf-monitor/hackrf_influx_collector.py`

Runs `hackrf_sweep`, parses each CSV row, and writes InfluxDB line protocol. The important parser behavior is that bin count comes from the actual RSSI values in each `hackrf_sweep` row, not from the whole requested sweep span.

Example service config:

- `hackrf-monitor/hackrf-influx.service`
- `hackrf-monitor/hackrf-influx.env.example`

### RF Console

`rf-monitor/rf_monitor_console.py`

FastAPI app on port `8099`.

Main endpoints:

- `/` - browser UI
- `/api/heatmap` - frequency/time heatmap matrix
- `/api/frequency/{frequency_hz}` - neighboring-bin drilldown
- `/api/top` - strongest recent bins
- `/api/anomalies` - current anomaly list
- `/api/health` - Influx/service health

### Anomaly Detector

`rf-monitor/rf_anomaly_detector.py`

Runs once per minute. It computes a rolling median baseline per frequency and compares the recent peak against that baseline.

Default thresholds:

- Baseline window: `6h`
- Recent window: `3m`
- Minimum rise above baseline: `18 dB`
- Minimum RSSI: `-55 dB`

Anomaly events are written back to InfluxDB as `rf_anomaly`, and the latest status is written to `/home/cmilkosk/rf-monitor/status.json`.

### Home Assistant Publisher

`rf-monitor/publish_rf_ha_status.py`

Publishes MQTT discovery and status sensors for Home Assistant. HA is intentionally a status/alert surface, while the dedicated RF console remains the primary analysis UI.

## Installed Services On linuxGR

The live deployment uses:

```text
influxdb
hackrf-influx
rf-monitor-console
rf-anomaly-detector
rf-ha-status.timer
```

Useful checks:

```bash
systemctl status influxdb hackrf-influx rf-monitor-console rf-anomaly-detector
systemctl list-timers rf-ha-status.timer
journalctl -u hackrf-influx -f
journalctl -u rf-anomaly-detector -f
curl http://127.0.0.1:8099/api/health
```

## InfluxDB Model

InfluxDB OSS v2 is used deliberately. This repo expects the v2 bucket/org/token API, not InfluxDB v1 databases or InfluxDB 3 Core.

Primary bucket:

```text
org: home
bucket: rf_monitoring
retention: 30d
```

Measurements:

- `rf_sweep`
- `rf_anomaly`

`rf_sweep` tags:

- `source`
- `frequency_hz`
- `bin_width_hz`

`rf_sweep` fields:

- `rssi_db`
- `start_hz`
- `stop_hz`
- `samples`

## Home Assistant

The deployment creates a sidebar dashboard named `RF Monitor`, placed with the existing `Security` and `AI` dashboards.

The HA dashboard includes:

- Link to the RF console
- RF monitor status entities
- Embedded iframe of the RF console

MQTT status entities currently include:

```text
sensor.linuxgr_rf_monitor_linuxgr_rf_monitor_status
sensor.linuxgr_rf_monitor_linuxgr_rf_active_anomalies
sensor.linuxgr_rf_monitor_linuxgr_rf_last_update
sensor.linuxgr_rf_monitor_linuxgr_rf_delta_threshold
sensor.linuxgr_rf_monitor_linuxgr_rf_min_rssi
sensor.linuxgr_rf_monitor_linuxgr_rf_overview
```

## Why A Custom Console?

Grafana and Home Assistant are both useful, but neither is ideal as the primary RF analysis surface.

This UI needs to be RF-native:

- Dense frequency-vs-time heatmap
- Hover a heatmap block to read frequency, time, and RSSI
- Click a hot block to zoom into that frequency range
- Use the selected-frequency panel to drill into adjacent bins
- Show anomaly overlays/events
- Eventually trigger IQ capture around a selected frequency

Home Assistant remains the home-automation surface for status and alerts.

## Roadmap

Near-term:

- Add visible anomaly overlays to the heatmap
- Add presets for common bands
- Add CSV/PNG export for a selected frequency window
- Tune anomaly thresholds after a few days of baseline data

Next phase:

- Capture IQ around clicked/anomalous frequencies
- Generate waterfall/spectrogram artifacts
- Add signal investigation records
- Connect known signal references such as Artemis/SigID

Later:

- Automatic modulation/signal classification
- Voice vs digital heuristics
- FT8 and other mode-specific detectors
- Model-assisted RF signal identification
