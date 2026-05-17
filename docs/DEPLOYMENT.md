# Deployment Notes

These notes describe the current `linuxGR` deployment.

## Paths

```text
/home/cmilkosk/bin/hackrf_influx_collector.py
/home/cmilkosk/.config/hackrf-influx.env
/home/cmilkosk/rf-monitor/rf_monitor_console.py
/home/cmilkosk/rf-monitor/rf_anomaly_detector.py
/home/cmilkosk/rf-monitor/publish_rf_ha_status.py
```

## Services

```bash
sudo systemctl enable --now influxdb
sudo systemctl enable --now hackrf-influx
sudo systemctl enable --now rf-monitor-console
sudo systemctl enable --now rf-anomaly-detector
sudo systemctl enable --now rf-ha-status.timer
```

## Health Checks

```bash
curl http://127.0.0.1:8099/api/health
curl 'http://127.0.0.1:8099/api/heatmap?hours=0.25&freq_step_mhz=25'
systemctl is-active influxdb hackrf-influx rf-monitor-console rf-anomaly-detector rf-ha-status.timer
```

## Home Assistant

Home Assistant receives status via MQTT discovery. The full RF console is intended to be opened at:

```text
http://192.168.202.112:8099
```
