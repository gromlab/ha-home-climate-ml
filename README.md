# ClimateML

[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub release](https://img.shields.io/github/release/gromlab/ha-climate-ml.svg)](https://github.com/gromlab/ha-climate-ml/releases)
[![Maintenance](https://img.shields.io/maintenance/yes/2026.svg)](https://github.com/gromlab/ha-climate-ml)

This integration corrects per-zone heat pump setpoints based on the difference between your zone's head sensor and an external reference, then applies a configurable schedule so each room gets the right temperature at the right time — automatically.

---

## Features

- Per-zone offset correction: computes head-sensor-minus-external-sensor delta and adjusts climate entity setpoints accordingly
- 5-minute decision cycle: reads current zone temperatures from the HA state machine, no cloud required
- YAML schedule loader: define time-of-day setpoints per zone; the integration applies them on schedule
- Safety thresholds: configurable min/max offset bounds (default ±5 °C) prevent runaway corrections
- Reads directly from existing HA climate and temperature entities — no new hardware needed

---

## Prerequisites

Before installing, make sure you have:

- Home Assistant 2024.1.0 or later
- At least one climate entity (your heat pump zones) already integrated into HA
- Temperature sensors for each zone — one at the head unit, one external reference per zone
- HACS installed ([hacs.xyz](https://hacs.xyz) if you haven't set it up yet)

---

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to **Integrations** → top-right menu → **Custom repositories**
3. Paste `https://github.com/gromlab/ha-climate-ml` and select category **Integration**
4. Find **ClimateML** in the list and click **Download**
5. Restart Home Assistant
6. Go to **Settings → Devices & Services → Add Integration**
7. Search for **ClimateML** and follow the setup steps

### Manual

1. Download the [latest release](https://github.com/gromlab/ha-climate-ml/releases/latest)
2. Copy `custom_components/climate_ml/` into your HA `config/custom_components/` directory
3. Restart Home Assistant
4. Go to **Settings → Devices & Services → Add Integration**

### Home Assistant Container (Docker)

If you're running HA in Docker, your config directory is mounted at the path you specified in your `docker-compose.yml` (commonly `./config` or `/opt/homeassistant/config`).

```bash
# Adjust path to your actual config mount
cp -r custom_components/climate_ml/ /path/to/ha-config/custom_components/
```

Restart the HA container after copying, then proceed with the Add Integration steps above.

---

## Configuration

After adding the integration, configure zones via **Settings → Devices & Services → ClimateML → Configure**.

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.climate_ml_<zone>_offset` | Sensor | Current computed offset for the zone (°C) |
| `sensor.climate_ml_<zone>_head_temp` | Sensor | Head unit temperature reading |
| `sensor.climate_ml_<zone>_ext_temp` | Sensor | External reference temperature |

### Options

Access via **Settings → Devices & Services → ClimateML → Configure**:

| Option | Description | Default |
|--------|-------------|---------|
| Head sensor | HA entity ID for the zone's head unit temperature sensor | — |
| External sensor | HA entity ID for the zone's reference temperature sensor | — |
| Offset min | Lower bound for correction offset (°C) | `-5.0` |
| Offset max | Upper bound for correction offset (°C) | `5.0` |
| Schedule entity | HA input_select or schedule helper driving time-of-day setpoints | — |

---

## Usage

The integration runs its decision cycle every 5 minutes. No automation needed for basic operation — it applies corrections and schedules automatically.

To react to correction events in your own automations:

```yaml
automation:
  - alias: "Alert when zone offset exceeds threshold"
    trigger:
      - platform: numeric_state
        entity_id: sensor.climate_ml_living_room_offset
        above: 3
    action:
      - service: notify.mobile_app
        data:
          message: "Living room offset correction above 3 °C — check sensors"
```

---

## Known Limitations

- Phase 1 (logic and entities) ships in a separate PR. This release scaffolds the integration structure only — the coordinator stub returns empty data.
- One integration instance per HA installation. Multi-instance support is not planned.
- Schedule loader reads YAML; dynamic schedule changes require an HA restart to reload.
- Offset correction writes setpoints to climate entities. If another automation also writes to the same entity, the last write wins.

---

## Troubleshooting

**Enable debug logging:**

Add to your `configuration.yaml`, then restart HA:

```yaml
logger:
  default: warning
  logs:
    custom_components.climate_ml: debug
```

Logs appear under **Settings → System → Logs**.

**Common issues:**

| Symptom | Cause | Fix |
|---------|-------|-----|
| Integration appears but no entities | Phase 1 logic not yet deployed | Wait for Phase 1 release or check [open PRs](https://github.com/gromlab/ha-climate-ml/pulls) |
| Offset sensor shows `unavailable` | Head or external sensor entity is unavailable | Check the sensor entity in **Developer Tools → States** |
| Setpoint not updating | Climate entity is in manual mode | Switch the climate entity back to auto/heat mode |

---

## Reporting Issues

Before opening an issue:

1. Enable debug logging (see above) and reproduce the problem
2. Check [existing issues](https://github.com/gromlab/ha-climate-ml/issues)

When filing a bug, include:
- Integration version (from **Settings → Devices & Services → ClimateML**)
- Home Assistant Core version
- Home Assistant Frontend version
- Debug logs from the time of the issue
