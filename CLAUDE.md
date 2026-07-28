# CLAUDE.md — AlorAir Local

Context for Claude Code working in this repo. Read this first.

## What this is

A fully local Home Assistant integration for **AlorAir / BaseAire "Sentinel"
WiFi dehumidifiers** (developed against a **Sentinel HDi65S**, manufacturer
reports as BaseAire). These units are **cloud-only**: the built-in WiFi module
has no LAN control API — it opens an outbound plaintext TCP session to the
vendor cloud (Alibaba Cloud, port 6200) and is driven from there. There was no
existing Home Assistant solution for the WiFi path (the only prior art is a
CAN-bus mod that requires opening the unit).

This project **replaces the vendor cloud**: we redirect the device's cloud
connection to our own small server that speaks the (reverse-engineered)
protocol, decodes status, accepts commands, and bridges the whole thing to Home
Assistant over MQTT with autodiscovery. No vendor servers in the loop.

## How it works (architecture)

```
 Dehumidifier (IoT VLAN)                 Home Assistant VM
   |  outbound TCP :6200                   |
   v                                       |
 UniFi gateway  --Dest-NAT-->  alorair_bridge (HA add-on)  --MQTT-->  Mosquitto
   (rewrites the device's cloud dest       |  parses status frames,        |
    to the add-on's host:6200)             |  builds command frames,       v
                                           |  publishes discovery + state  HA humidifier
                                                                           entity + sensors
```

- The device only makes an **outbound** connection; it never listens. So we
  can't connect to it — we make it connect to us, either via a **Destination-NAT**
  rule (source = the dehumidifier, dest port 6200 -> the add-on host:6200) or
  by **DNS-overriding `online-app.toovem.com`** to the add-on host. The device
  resolves that name via the DHCP-provided DNS at boot (capture in PROTOCOL.md).
  Both are confirmed working on real hardware; **this deployment now runs the
  DNS override** (simpler, and the README's recommended path). Remember the
  device only resolves at boot — power-cycle it after changing the record.
- `alorair_cloud.py` is the whole server. Two modes:
  - `relay` — transparent MITM to the real cloud; decode+log every frame. Used
    for capture / protocol work. The unit keeps working normally.
  - `local` — answer the device ourselves and bridge to HA over MQTT. Production.
- Deployed as a **local Home Assistant add-on** (`alorair_bridge/`). Running it
  on the HA host makes MQTT trivially reachable (`core-mosquitto`) and sidesteps
  cross-host/VLAN routing problems.

The reverse-engineered wire protocol is documented in `PROTOCOL.md`. Read it
before touching frame parsing or command building.

## Repo layout

```
alorair_bridge/            The HA local add-on (production deploy)
  alorair_cloud.py         **canonical** server/decoder/bridge — single source of truth
  config.yaml              add-on manifest (ports, MQTT options schema)
  Dockerfile               FROM python:3.12-slim + paho-mqtt
  run.sh                   reads /data/options.json -> env, execs the server
deploy/                    Non-add-on deployment (generic Docker host)
  docker-compose.relay.yml relay mode (protocol capture)
  docker-compose.local.yml local mode (bind-mounts the script; set MQTT_* env)
tests/test_frames.py       frame-layer tests (checksum, builders, framing)
PROTOCOL.md                the reverse-engineered wire protocol
README.md                  human-facing overview + install
repository.yaml            lets this GitHub repo be added as a HA add-on source
```

Note: the compose files in `deploy/` bind-mount `../alorair_bridge/alorair_cloud.py`
so there is only ONE copy of the script to maintain.

## Running / testing

- No third-party deps for `relay` mode (stdlib only). `local` mode needs
  `paho-mqtt`.
- `python3 -m unittest discover -s tests` runs the frame-layer tests (no
  device/broker needed). They pin `build_command(mac, 0x23, 45)` to the real
  captured setpoint-45 frame and cover `parse_status` (56-byte status body =
  74-byte frame) plus the checksum-validated stream framing. Run them before
  and after touching anything in the framing/checksum layer.
- To (re)capture protocol behavior: run `--mode relay`, point the DNAT at the
  relay, drive the unit from the **AlorAir-C phone app**, read the decoded log.

## Current status

WORKING (all confirmed live in production, 2026-07-27):
- Full status decode: RH, setpoint, temp (F), grains/lb, enable state
  (offset 11 — follows the front panel, so HA on/off can't drift),
  compressor (14), fan (15), pump (12).
- **All commands actuate**: setpoint, power on, power off (~100s compressor
  run-down before running=0), purge on/off. Each is acked on the next status
  (~600ms) via the event flag echoing the opcode. The long-standing "power/
  purge don't actuate" issue was a phantom — an off unit's status is
  indistinguishable from an idle one in the decoded fields, so earlier tests
  misread working commands as no-ops. The checksum "K constants" were also
  artifacts (real rule: class byte excluded from sum — PROTOCOL.md).
- All commands includes locate (0x27): acked evt=0x27, works with unit off,
  self-terminates after ~65s (status offset 38 tracks it).
- Continuous mode: setpoint <35 clamps to 34 and engages it (status offset
  52 = 0x02); exposed as HA humidifier modes normal/continuous.
- MQTT autodiscovery: humidifier entity (modes, 35-90 slider) + humidity/
  temperature/GPP sensors, coil-temp + working-hours diagnostics,
  pump/compressor/fan binary_sensors, purge button (one-shot ~30s
  self-terminating pump cycle), locate switch (live state from offset 38;
  auto-flips off with the unit's ~65s clear).
- Filter timer: app/cloud-side only (relay-verified — reset sends nothing
  to the device). Recreate in HA as a helper if wanted.
  Measurements carry state_class for HA long-term statistics; compressor/
  fan/outlet-RH are entity_category diagnostic.
- HA add-on installs/updates from the GitHub repo; device connects through
  the re-pointed DNAT.

KNOWN ISSUES / NEXT STEPS (all minor):
- Never-observed state: defrost-active (panel light exists; coil temp at
  offset 30 is the trigger) and E1..E5 error codes — somewhere in the zero
  status bytes; the byte-diff logger will catch their first occurrence.
- Undecoded constants at offsets 2/0x0b, 8/0x10, 39/0x20 (32 — possibly
  the defrost threshold in °F).

## Future development

- **Passthrough mode (app/cloud + HA coexist).** A third `mode: passthrough`
  that keeps `relay`'s transparent MITM to the real cloud (so the AlorAir-C
  app and cloud keep working) while also running `local`'s MQTT bridge:
  publish parsed status to HA (nearly free — relay already decodes it) and
  inject HA commands into the device socket (reuses `build_command`/`_send`).
  Changes made in HA propagate to the app too, since both read the same
  status stream; contradictory simultaneous commands are last-writer-wins and
  self-correct from the authoritative status frame.
  - Design choice deferred: monitor-only (HA read, app controls) vs. full
    dual-control (HA can command too). Full is barely more code.
  - **Wrinkle:** under the DNS-override deployment, the bridge can't resolve
    the real cloud by name (it points at HA — the self-dial guard in
    handle_relay already trips). Passthrough needs an explicit upstream
    resolver option (e.g. 1.1.1.1) to look up online-app.toovem.com fresh.
    Cleaner under the DNAT method, where HA's own DNS still reaches the cloud.
  - **Tradeoff to document loudly:** this re-introduces the vendor cloud —
    plaintext data to Alibaba, dependence on their uptime, and firmware OTA
    becomes possible again (the protocol-break/brick risk cord-cutting
    removed). Opt-in only, never default.

## Conventions / gotchas

- **Never commit secrets.** MQTT credentials are entered in the HA add-on config
  UI (written to `/data/options.json` at runtime), not in any file here. The
  `deploy/*.yml` files use placeholders.
- Keep `alorair_bridge/alorair_cloud.py` as the single source of truth. Don't
  fork a second copy.
- The Dockerfile MUST keep an explicit `FROM` (Supervisor 2026.04 dropped the
  implicit base image).
- Frame checksums and offsets are load-bearing; change them only with a captured
  frame proving the new value, and update PROTOCOL.md in the same commit.
- Version lives in TWO places: `config.yaml` `version:` and `VERSION` in
  `alorair_cloud.py` (shown as sw_version on the HA device card). Bump both.
- Device identity: MAC is read from the first frame; MQTT object id is
  `alorair_<mac_hex>`.
