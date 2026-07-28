# AlorAir Local Bridge

Local control of AlorAir / BaseAire "Sentinel" WiFi dehumidifiers from Home
Assistant, with **no vendor cloud**. The unit's WiFi module only phones home
to the vendor cloud (plaintext TCP, port 6200); this add-on stands in for that
cloud, decodes status, accepts commands, and bridges the unit to HA over MQTT
with autodiscovery.

You'll do two things: **configure the add-on** (below), and **redirect the
device's cloud connection** to this add-on (the one fiddly part — see *The
network redirect*).

## Configuration

| Option | What it does |
| --- | --- |
| `mqtt_host` | Your broker. Leave as `core-mosquitto` if you use the Mosquitto broker add-on. |
| `mqtt_port` | Broker port (default `1883`). |
| `mqtt_user` / `mqtt_pass` | Credentials for the broker. |
| `allowed_source_ip` | Optional hardening: set to the dehumidifier's IP so nothing else on the network can talk to port 6200. |
| `mode` | `local` (production — answer the device, bridge to HA) or `relay` (transparent pass-through to the real cloud, for protocol capture). Leave `local`. |

If you use the Mosquitto broker add-on, give the same user a login in **its**
config and restart it:

```yaml
logins:
  - username: <mqtt_user>
    password: <mqtt_pass>
```

Start this add-on. The log should show `MQTT connected (rc=Success)` and
`published HA discovery`.

## The network redirect (the only hard part)

The dehumidifier never *accepts* connections — it only dials **out** to its
cloud on TCP port 6200. At boot it looks up the hostname
**`online-app.toovem.com`** using whatever DNS server your router hands it,
then connects to the answer. So you either lie to it about that name
(**Method A**, easiest) or reroute the connection afterwards (**Method B**).
Nothing here is exposed to the internet — all the traffic stays on your LAN.

### Method A — DNS override (recommended)

Point `online-app.toovem.com` at your Home Assistant host's IP. One record, no
firewall rules. Requires only that the dehumidifier uses your DNS server (the
normal case — it takes whatever DHCP hands out).

- **Easiest on Home Assistant OS — the official Dnsmasq add-on:** install
  **Dnsmasq** from the Add-on Store, and in its config map
  `online-app.toovem.com` → your HA host's IP. Then set your router's DHCP
  **DNS server** to the HA host so the dehumidifier asks Dnsmasq. The whole
  setup stays inside Home Assistant. (Dnsmasq forwards all other lookups
  upstream, so it works as your normal resolver; just note DNS then depends on
  HA being up.)
- **Pi-hole / AdGuard Home:** add a local DNS record / rewrite for the name.
- **Router with local-DNS support (OPNsense/pfSense, OpenWrt, some consumer
  routers):** add a host override for the name.

Then **power-cycle the dehumidifier** — it only resolves the name at boot.

### Method B — Destination NAT (DNS-independent, needs a capable router)

A DNAT rule that catches the outbound connection regardless of DNS:

> **IF** a packet is **from** the dehumidifier's IP, is **TCP**, to **port
> 6200** (any destination) → **rewrite its destination to** `<HA host IP>:6200`.

Match the *source* (the dehumidifier only) and *any* destination address (the
cloud IP rotates). Supported on UniFi, pfSense/OPNsense, MikroTik, OpenWrt.
Then power-cycle the dehumidifier.

### Verify

The add-on log shows `=== device connected ===` within ~30 seconds, and a
**Dehumidifier** device appears under Settings → Devices & Services → MQTT.

Full router-by-router recipes, an ISP-router fallback, and the reverse-
engineered wire protocol are in the project's README and `PROTOCOL.md` on
GitHub: <https://github.com/sethrobin/alorair-local>

## What you get

A **Dehumidifier** device with: on/off (tracks the front panel too), a target
slider (35–90%), a normal / continuous mode selector, humidity / temperature /
grains-per-pound sensors, coil-temperature and total-working-hours
diagnostics, pump / compressor / fan indicators, a purge button (one-shot
~30 s pump-out), and a locate switch (beacons the unit; it beeps loudly).

## Notes

- The device only resolves its cloud name (or opens its connection) at boot —
  after any redirect change, power-cycle it.
- A unit switched off at its front panel is reflected in HA within one status
  frame (~10 s); the two stay in sync.
