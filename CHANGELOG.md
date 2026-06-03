# Changelog

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
