# Changelog

## [0.6.2] — Fix head offset clamping

- **Bug fix:** head sensor offset is now clamped to `>= 0` — when the Samsung head's internal sensor reads colder than the room (normal during active cooling), the negative offset was lowering the commanded setpoint below the head's current reading, causing it to think it had achieved its target and stop cooling prematurely. Now offset only applies when the head sensor reads warmer than the room (compensating for dead-zone placement).

## [0.6.1] — Schedule day_type: add "both" option

- Schedule blocks now support `day_type: "both"` — a single block applies to weekdays and weekends, removing the need to duplicate entries

## [0.6.0] — Direct Setpoints + Predictive Idle + Starvation Logic

**Breaking changes:**
- Comfort bands removed — zones now use direct °C setpoints (`default_setpoint_c`)
- Schedule blocks use new format: single `blocks` list with `{day_type, start, end, setpoint_c, mode}` replacing separate `weekday_blocks`/`weekend_blocks` with `comfort_level`
- `select.default_comfort_level` entity removed — default mode now set via virtual thermostat preset
- Entry version bumped to 7; migration from v6 automatic on first start (maps comfort levels 1–5 → °C)
- `Platform.SELECT` removed; `_COMFORT_LEVEL_DEFAULTS` and `BAND_HYSTERESIS_DEFAULT` constants removed

**New features:**
- **Direct setpoints**: each zone has `default_setpoint_c` and `default_mode` (eco/comfort); schedules specify `setpoint_c` directly
- **Two modes**: `eco` adds `eco_tolerance_c` deadband (default 1°C) above setpoint before cooling triggers; `comfort` triggers immediately at setpoint
- **Occupancy auto-upgrade**: when occupied in eco mode, silently upgrades to comfort for that tick; logs `mode_source: "occupancy"` — ML training signal for anticipating occupancy pre-cooling
- **Predictive idle shutdown**: when `dT/dt` predicts room won't reach trigger within `idle_head_threshold_minutes` (default 60 min), head turns off; turns back on automatically; anti-cycle guard requires ≥ 2 ticks on before allowing idle
- **Virtual thermostat overhaul**: COOL/OFF + `target_temperature` + `preset_mode` (eco/comfort/override); setting temperature → 120-min comfort override; setting preset eco/comfort → cancels override and sets zone default mode
- **Per-zone Enabled switch** re-added: `RestoreEntity`, default on for new zones
- **Starvation logic** (front bedroom priority): if priority zone `demand_delta < −1°C` for 30 min and `head_thermal_delta` never reaches `−10°C`, all other zones raised to `head_current_temp + 1°C` for up to 15 min; exits on thermal target or timeout; 30-min cooldown; logged as `starvation_suppressed=True`
- **New controller NUMBER entities**: `vacation_setpoint_c`, `idle_head_threshold_minutes`, `eco_tolerance_c`
- **Decision log format**: shows `22.0°C eco → 21.3°C [schedule] ✓`, `idle [default]`, or `22.0°C eco suppress [schedule/occupancy]`
- **DB schema**: new columns `active_setpoint_c`, `setpoint_source`, `active_mode`, `mode_source`, `starvation_suppressed`

## [0.5.2] — Version bump (HACS release alignment)

- No code changes; version bumped to align with HACS release detection

## [0.5.1] — Post-launch fixes

- **Comfort level blank in reconfigure**: `ObjectSelector` select fields expect string values; stored `comfort_level` ints were not matching options, leaving the field blank when re-editing a schedule block or zone default. Fixed by converting at the display boundary (`_blocks_for_form`, `default_comfort_level` cast to `str`)
- **Sparse schedule blocks**: schedule validator required full 24h contiguous coverage from midnight; any gap caused the entire schedule to be silently discarded. Replaced with overlap-only check — blocks can now be placed at any time of day, gaps fall back to zone default comfort level
- **Stale low setpoint**: when room was in-band, ClimateML suppressed commands but left the Samsung unit holding a stale low setpoint (e.g. 20°C). Unit kept cooling to that target even when the band permitted 22°C. Fix: always command `band_max + offset` when `room ≥ band_min`; Samsung stops cooling once room drops below the setpoint naturally
- **Schedule block sensor**: shows `"default: level N"` instead of `"unscheduled"` when no block is active, so the active comfort profile is always visible
- **Next Transition sensor**: returns `"No schedule"` instead of `"Unknown"` when the zone has no schedule blocks configured

## [0.5.0] — Comfort Bands + ML Shadow Mode

**Breaking changes:**
- Schedule blocks no longer have `mode` or `setpoint_c` — replaced by `comfort_level` (integer 1–5); migration auto-converts existing blocks to level 3 (Relaxed)
- Per-zone `Enabled` switch removed — zone enable/disable is now the climate entity's AUTO/OFF mode
- Entry version bumped to 6; migration from v5 automatic on first start

**New features:**
- **Comfort bands**: 5 named levels (Sleep → Vacation) defined on the Controller with absolute min/max °C — no more per-zone setpoints
- **Band-based control (cooling only)**: room > band_max+hysteresis → cool to band_max+offset; room < band_min → suppress (Samsung's internal hysteresis already stops the compressor; forcing cool on a cold room is avoided); in-band → suppress; sensor unavailable → suppress
- **Vacation Mode switch** (controller device, `RestoreEntity`): overrides all zones to the configured vacation comfort level
- **Per-zone Default Comfort Level** SELECT entity: persists across restarts; SELECT restore triggers immediate coordinator refresh to avoid first-tick default-3 issue
- **Comfort Min / Comfort Max sensors** per zone: expose active band boundaries for graphing
- **Energy Today sensor** (controller device): kWh since midnight, midnight snapshot via `async_track_time_change`
- **ML Confidence sensor** per zone: 0.0 until a model is loaded; reflects per-tick shadow prediction confidence
- **ML shadow mode**: if `/config/climate_ml/model.pkl` exists (joblib format), loaded at startup and run every tick alongside rule-based decisions — predictions logged in DB, never affect commands
- **dT/dt logging**: rate of change of room temp (5-min and 15-min rolling) logged as sensor events each tick
- **_system sensor event fix**: sun elevation/azimuth, ODU mode, outdoor temp, and HVAC power now correctly written to `sensor_events` (bug in previous versions silently dropped them)
- **ODU mode logging fix**: logs actual state string instead of literal `0`
- **Zone processing order** (Goal 6): tightest comfort bands (Level 1) processed first each tick
- **Weather forecast persisted** to new `weather_forecast` table in SQLite store
- **Door and window entity pickers** added to zone config form
- **New DB tables**: `cycle_events`, `weather_forecast`; new `decisions` columns: `ml_predicted_action`, `ml_confidence`, `comfort_level`, `band_min`, `band_max`
- Schema migrations are idempotent (new `_migrate_schema()` in store)
- `prune()` extended to cover `cycle_events` and `weather_forecast`

## [0.4.10] — Optional entity selectors genuinely optional
- **Bug**: `eva_in_entity`, `eva_out_entity`, and `occupancy_entity` in the zone config flow used `default=""` in the voluptuous schema — HA's EntitySelector interprets an empty-string default as a required field, forcing users to pick a sensor even when none is available
- **Fix**: changed to `default=d.get("X") or vol.UNDEFINED` — `vol.UNDEFINED` tells voluptuous (and HA's frontend) the field has no default, making it genuinely skippable; the stored value remains `""` when omitted, which the integration already handles gracefully

## [0.4.9-fix] — Remove stale bare device association (final duplicate fix)
- **Root cause identified by Opus review**: the device registry only ever unions subentry IDs onto the existing set — it never retracts old entries. Devices registered before v0.4.9 have a stale bare `(entry_id, None)` association alongside the correct `(entry_id, subentry_id)`, causing both the subentry listing and "Devices without a sub-entry" listing to show
- **Fix**: after `async_forward_entry_setups` (which migrates entity registry entries from `config_subentry_id=None` to the real subentry ID), remove the bare device association via `async_update_device(remove_config_entry_id=..., remove_config_subentry_id=None)` — now safe because no entities remain on `None`, so the device-update listener cannot orphan them
- Previous attempts (#22–#25) failed because entities still had `config_subentry_id=None` at cleanup time; v0.4.9 fixed entity registration first, making this cleanup safe

## [0.4.9] — Per-subentry entity registration (duplicate device fix)
- **Devices no longer appear under "Devices without a sub-entry"**: all entity platforms now pass `config_subentry_id` to `async_add_entities` — the HA-recommended pattern (from `kitchen_sink` demo). This stamps both the device registry and entity registry entries with the correct subentry ID, so no bare `(entry_id, None)` association is ever created
- Controller entities (4 NUMBER + master switch) registered under the controller subentry
- Zone entities (climate, sensors, switches) registered under each zone's own subentry
- `HomeClimateMlCoordinator` gains `zone_subentry_map` (zone_id → subentry_id) and `controller_subentry_id` property, used by all platforms at registration time
- Version bump to 0.4.9

## [0.4.8-fix2] — Drop bare-subentry cleanup permanently
- **Remove bare-subentry cleanup entirely**: removing the bare `(entry_id, None)` device association caused HA to consider entity registry entries (which store `config_subentry_id=None`) orphaned — silently dropping all entities from both ClimateML devices. The duplicate-device appearance in "Devices without a sub-entry" is a known cosmetic limitation of how `async_forward_entry_setups` registers entities; fixing it requires per-subentry entity setup which is a future HA API concern

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
