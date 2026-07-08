# Deployment Notes

These notes describe the current `linuxGR` deployment.

## Paths

```text
/home/cmilkosk/bin/hackrf_influx_collector.py
/home/cmilkosk/.config/hackrf-influx.env
/home/cmilkosk/rf-monitor/rf_monitor_console.py
/home/cmilkosk/rf-monitor/rf_anomaly_detector.py
/home/cmilkosk/rf-monitor/publish_rf_ha_status.py
/home/cmilkosk/rf-monitor/radio_device_health.py
/home/cmilkosk/rf-monitor/captures/
```

## Services

```bash
sudo systemctl enable --now influxdb
sudo systemctl enable --now hackrf-influx
sudo systemctl enable --now rf-monitor-console
sudo systemctl enable --now rf-anomaly-detector
sudo systemctl enable --now rf-ha-status.timer
sudo systemctl enable --now radio-device-health.timer
```

## Health Checks

```bash
curl http://127.0.0.1:8099/api/health
curl 'http://127.0.0.1:8099/api/heatmap?hours=0.25&freq_step_mhz=25'
curl http://127.0.0.1:8099/api/captures
systemctl is-active influxdb hackrf-influx rf-monitor-console rf-anomaly-detector rf-ha-status.timer
systemctl is-active radio-device-health.timer
```

## Radio Device Health

`radio_device_health.py` polls linuxGR's attached radio devices once per minute using USB sysfs and service state. It expects one HackRF, one SDRplay RSPdxR2, and RTL-SDR devices with serials `RTL0`, `RTL2`, and `RTL3`.

The poller writes:

```text
/home/cmilkosk/rf-monitor/radio-health.json
/home/cmilkosk/rf-monitor/radio-health-history.jsonl
```

The RF Monitor console exposes this under the `Radio Health` tab and via:

```text
http://192.168.202.112:8099/api/radio-health
http://192.168.202.112:8099/api/radio-health/history?hours=24
```

Home Assistant receives MQTT discovery/state for overall radio health plus one connectivity binary sensor per radio. The `rf-monitor` Lovelace dashboard also has a native `Radio Health` view/tab that uses these entities and embeds the detailed RF console.

## Signal Capture

The RF console can capture a selected frequency with `hackrf_transfer`. During capture it pauses `hackrf-influx`, records raw I/Q, writes metadata, generates a spectrogram PNG, and restarts the sweep service.

Capture artifacts are stored in `/home/cmilkosk/rf-monitor/captures/`.

## Home Assistant

Home Assistant receives status via MQTT discovery. The full RF console is intended to be opened at:

```text
http://192.168.202.112:8099
```
