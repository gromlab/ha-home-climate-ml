# Changelog

## [0.4.8-fix2] — Safe bare-subentry cleanup (duplicate device fix)
- **Re-add bare-subentry cleanup with try/except**: the `async_update_device` call is wrapped so a future HA device registry API change cannot crash `async_setup_entry` — it degrades gracefully (device may appear twice) instead of breaking entity setup

## [0.4.8-fix] — Entity setup + controller configure button
- **Drop bare-subentry cleanup block**: the post-platform-setup `async_update_device` call that removed bare device associations was using an API that changed in HA 2026.5/2026.6 — it caused `async_setup_entry` to crash before entity platforms registered, leaving all devices with zero entities. Removed: the cosmetic "Devices that don't belong to a sub-entry" de-duplication is no longer attempted (devices may appear in both the subentry list and the bare list in some HA versions, which is benign)
- **Controller Configure button restored**: `async_get_supported_subentry_types` now always includes `ControllerSubentryFlowHandler` so HA exposes the reconfigure flow on the existing controller subentry. Duplicate-add protection moved into `async_step_user` (aborts with `controller_already_configured` if one already exists)

## [0.4.8] — Controller consolidation + UX polish
- **Options flow removed**: global settings (`update_interval_minutes`, `setpoint_min_c`, `setpoint_max_c`) moved into the Controller subentry — no more settings gear on the integration entry
- **Controller form expanded**: 3 new fields at the top of the Controller form (decision loop interval, min/max setpoint bounds); all 9 fields have friendly labels and one-sentence descriptions
- **Zone form expanded**: occupancy sensor picker added per zone (`binary_sensor` domain) — value logged each coordinator tick; no logic effect yet, ready for future ML
- **"ML — " prefix removed from controller entities**: Override Duration, Setpoint Tolerance, Default Setpoint, Sensor Guard Threshold, and All Zones Enabled no longer carry the redundant prefix — the Controller device name provides context
- **All form labels rewritten**: "heat pump head" → "Zone climate entity"; descriptions updated throughout to be future-proof for both heating and cooling, not heat-pump-specific
- **Migration v4 → v5**: strips `update_interval_minutes`, `setpoint_min_c`, `setpoint_max_c`, `hallway_sensor`, `outdoor_sensor` from `entry.options`; these values remain accessible via `CONTROLLER_DEFAULTS` fallback until the user reconfigures the Controller subentry
- Version bump to 0.4.8

## [0.4.7] — Controller UX fixes
- **Duplicate devices fixed**: after platform setup, bare (no-subentry) device registry associations added by entity registration are removed — devices no longer appear in both "Devices that don't belong to a sub-entry" AND their subentry
- **"Add ClimateML Controller" hides after first controller created**: `async_get_supported_subentry_types` now conditionally excludes the controller type when a controller subentry already exists
- **Master switch persists across HA restarts**: `ClimateMLMasterSwitch` now uses `RestoreEntity` — same pattern as zone switches — so the enabled/disabled state survives restarts instead of resetting to OFF
- Version bump to 0.4.7

## [0.4.6] — ClimateML Controller subentry
- New subentry type **ClimateML Controller**: system-level entity pickers for ODU mode, outdoor temp, extra indoor temps (multi-select for sensor guard), power, energy, and weather. Auto-created by v3→v4 migration; configure via integration page → ClimateML Controller → Configure
- **Master switch anchored to Controller device**: `ML — All Zones Enabled` now appears under ClimateML Controller in the device list instead of floating
- **4 new NUMBER entities** on Controller device: Override Duration (15–480 min), Setpoint Tolerance (0.1–2.0°C), Default Setpoint (16–28°C), Sensor Guard Threshold (1.0–10.0°C). Values persist across restarts via RestoreEntity; update coordinator in-place without triggering integration reload
- **Force-cool logic removed**: the `force_cool_threshold_c` / `force_cool_clear_c` safety override has been removed — it was too blunt and too complicated at this stage
- **Offset clamp replaced by sensor guard**: instead of clamping the offset to a fixed max, the coordinator now computes a peer-average across all active zone sensors + extra_temp_entities. If any zone sensor deviates more than the guard threshold from the average, its offset is zeroed and a warning is logged — catches defective sensors, not normal offsets
- **EVA in/out entity pickers added to zone form**: select `sensor.samsung_hvac_{zone}_eva_in_temperature` / `eva_out_temperature` per zone; values logged each coordinator tick for future ML
- **Sun elevation and azimuth** logged every coordinator tick from `sun.sun` (always present in HA) — solar gain data for future ML
- **Weather forecast**: hourly 12h forecast fetched each tick via `weather.get_forecasts` service (try/except wrapped so unavailability never fails the coordinator) — stored in debug log for future ML
- Entry VERSION bumped to 4; `async_migrate_entry` handles v1→v3→v4 and v2→v3→v4 chains
- **Breaking**: `force_cool_threshold_c`, `force_cool_clear_c`, `offset_clamp_c` removed from options — stripped on migration
- Version bump to 0.4.6

## [0.4.5] — Decision log improvements + schedule sensors
- Decision log state string now shows the full journey: scheduled setpoint → corrected setpoint with offset value (e.g. `cool: 21.0°C → 22.3°C (+1.3°C offset) [schedule] ✓`); when no offset applied: `cool @ 21.0°C [schedule] held`; ✓ = command sent, held = within tolerance
- Decision log keys renamed: `setpoint_c` → `scheduled_c`, `command` → `commanded` (breaking if you reference log attributes directly)
- `get_block` last-block fallback fixed: returns `("off", None)` when current time falls outside all defined schedule blocks — previously held the last block's mode indefinitely (breaking change for schedules that don't cover the full 24h)
- New sensor per zone: **Current Schedule Block** — shows active block mode, setpoint, and time range; shows `off (unscheduled)` when between blocks
- New sensor per zone: **Next Schedule Transition** — timestamp of next block boundary; uses DST-safe `start_of_local_day` for tomorrow's transitions
- Version bump to 0.4.5

## [0.4.4] — Sensor availability when zone disabled
- Numeric zone sensors (room temp, offset, corrected setpoint) now report `available = False` when the zone is disabled — shows as grey "Unavailable" instead of amber "Unknown", which is the correct HA semantic for intentionally-off data
- Decision log sensor remains available even when zone is disabled (it records the disabled state)
- Version bump to 0.4.4

## [0.4.3] — Hotfix: remove invalid device registry call
- Remove `async_update_device(remove_config_subentry_id=None)` call that caused integration setup failure — HA rejects this without a paired config_entry_id; cosmetic duplicate-device issue deferred
- Version bump to 0.4.3

## [0.4.2] — Decision log sensor + duplicate device fix
- New diagnostic sensor per zone: `ML — {Zone} Decision Log` — state shows the last decision ("cool @ 21.5°C [schedule]", "off [schedule]", etc.), attributes contain the last 30 decisions as a list (newest first) with time, mode, setpoint, corrected setpoint, source, ext temp, offset, and whether a command was sent
- Fixed duplicate device on integration page: stripped the bare (no-subentry) device association that entity platforms add automatically, so each zone device now appears only under its subentry
- Version bump to 0.4.2

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
