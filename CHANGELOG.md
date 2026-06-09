# Changelog

## [0.4.1] — Device registry: subentry linkage + orphan cleanup
- Zone devices now appear under their subentry on the integration page (previously grouped under "Devices that don't belong to a sub-entry")
- Deleting a zone subentry now also removes its device and entities from the registry on next reload — no stale cards left behind
- Version bump to 0.4.1

## [0.4.0] — Zones as config subentries
- Zones are now HA Config Subentries — add, edit, and delete zones directly from the integration page with native HA add/edit/delete affordances (no menu navigation required)
- Schedule blocks (weekday and weekend) are edited inline per zone using the native ObjectSelector list UI — add multiple blocks in one form, no separate menu steps
- Options flow simplified to global settings only
- `async_migrate_entry` v2→v3: existing zones lifted automatically from `entry.options` into subentries — no remove/re-add needed
- `async_migrate_entry` v1→v3: legacy YAML-schedule installs also handled
- Version bump to 0.4.0 (entry version 3)

## [0.3.1] — Back navigation and block session editing
- Back navigation added throughout the options flow:
  - Zone menu and schedule menu: "← Back" as a clickable menu item
  - Manage zones list: "← Back" as the first option
  - All forms (global settings, add/edit zone, add/edit block): "← Cancel / go back" toggle at the bottom
- Schedule blocks no longer close the options flow after each add/edit/delete — changes stage in memory; "← Back (save changes)" commits and closes in one step, so entire schedules can be built in one session
- Version bump to 0.3.1

## [0.3.0] — Config flow refactor
- Full options flow: zones, schedules, and tuning constants now configured entirely via HA UI — no YAML file
- Menu-based options flow: add/edit/remove zones, manage weekday/weekend schedule blocks per zone
- Per-zone sensor entities: room temperature, offset, corrected setpoint (diagnostic)
- Master switch semantics fixed: AND gate with per-zone switches; survives options reload, resets to OFF on HA restart
- Per-zone switch gains `DeviceInfo` grouping (climate + 3 sensors + switch shown as one device)
- `async_migrate_entry` v1→v2: preserves SQLite decision history and entity registry on upgrade
- Removed YAML schedule dependency entirely
- Version bump to 0.3.0 (entry version 2)

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
