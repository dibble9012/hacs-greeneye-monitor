# 2026.11.0

Requires Home Assistant 2026.8 or newer. Existing config entries, entities and
history carry over unchanged (same domain, same stored data format, same unique IDs).

## Fixes

- **Configure button works again** (#45, #47, #50, #52, #54, #57). The options flow
  set `config_entry` itself, which Home Assistant 2025.12 made impossible, and its
  menu step ID didn't match the step that showed it.
- **Download diagnostics works again.** It read entity internals that Home
  Assistant removed (`use_device_name`) and private `hass.data` structures. It now
  uses only public registries and states, and also works while the entry is unloaded.
- The new-monitor forms now use selectors, which adds typed units and a device class
  dropdown limited to sensible values. (The old validators did still render.)
- **No more "multiple config entries with same unique ID" repair** (#48). Each
  discovered monitor now gets its own unique ID, so ignoring a monitor no longer
  creates a second entry with the hub's ID. Leftover ignored entries from older
  versions are removed automatically.
- Replaced the deprecated `via_device` device link (removed in HA 2027.8) with
  `via_device_id`.
- Setup no longer leaks a shutdown listener and monitor listeners on every reload,
  awaits platform setup, and retries (instead of failing) when the port is busy.
- A pulse counter without configuration is skipped with a warning instead of
  crashing entity setup with an assertion.

## New

- **Edit monitor settings after setup** (#53): channels with net metering,
  temperature unit, and ECM-1240 Aux 5 mode.
- **Edit pulse counters after setup** (#30, #54): unit counted, amount per pulse,
  device class and rate interval. Units are checked against the device class.
- **Configurable energy update interval** (#56), default 5 minutes (was a fixed
  30 minutes, which made hourly Energy dashboard figures lumpy). 0 records every packet.
- The initial setup form checks that the port is free.
- The options menu only shows sections that apply (#24).

## Efficiency

- Monitors are routed to platforms through one dispatcher instead of each platform
  listening separately; discovery uses `discovery_flow`, so a busy flow manager no
  longer stalls packet processing.
- Configuration number entities (CT type/range, packet interval) only write state
  when their value changes, instead of on every packet for every channel.
- Removed the dependency on `logbook` and the check that peeked at its internals
  to warn about recorder exclusions.

## Not fixed here (library or device issues)

- `Exception while attempting to parse a packet ... fromhex()` / `UnicodeDecodeError`
  (#55): the `greeneye_monitor` library mistakes packet bytes for a settings
  response. Energy data keeps flowing, but settings are only requested once per
  connection, so if that request fails the CT type/range and packet interval entities
  aren't created until the monitor reconnects and HA reloads. Needs a library fix.
- Large negative kWh after outages (#49): residual data in the GEM's polarized
  counters. Brultech COM firmware 5.48 adds `^^^SYSRSTB` to reset them.

## Tests

The previous tests mocked the YAML-era code and 26 of 27 failed against current
Home Assistant. They are replaced with tests that run the real library and send
captured GEM and ECM-1240 packets over TCP, driving every flow through the
frontend's HTTP API.
