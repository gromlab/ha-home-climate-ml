# Changelog

## [0.2.3] — Rename to ClimateML
- Integration renamed from "Home Climate ML" to "ClimateML"
- Domain changed from `home_climate_ml` to `climate_ml`
- Brand icons added (256×256 and 512×512)
- `ignore: brands` removed from CI

## [0.2.2] — Zone enable switches
- Per-zone enable/disable switch (`ML — {Zone} Enabled`) — state persists across restarts
- Master switch (`ML — All Zones Enabled`) — turns all zones on or off in one toggle
- Disabled zones skip all commands; coordinator marks them `schedule_source: disabled`

## [0.2.1] — Default schedule bootstrap
- Auto-create `/config/climate_schedules.yaml` on first `async_setup_entry` if the file doesn't exist

## [0.2.0] — Phase 1 logic
- Decision loop: 5-minute offset correction per zone
- YAML schedule loader with full validation
- SQLite data store (decisions, overrides, sensor events, safety events, device commands)
- Safety thresholds: force cool at 30°C, hard alert at 35°C (with hysteresis + rate limiting)
- 2-hour per-zone user overrides, persisted and restored across HA restarts
- Tolerance-gated commands (±0.5°C) to avoid SmartThings cloud write storms
- Options flow: configure schedule YAML path with live validation

## [0.1.0] — Scaffold
- Initial HACS integration structure
- Blueprint files: CI workflows, issue templates, release config
