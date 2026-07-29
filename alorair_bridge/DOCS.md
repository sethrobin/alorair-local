# AlorAir Local Bridge

Local control of AlorAir / BaseAire "Sentinel" WiFi dehumidifiers from Home
Assistant, with **no vendor cloud**. The unit's WiFi module only connects out
to the vendor cloud (plaintext TCP, port 6200). This add-on replaces that
cloud. It decodes status, accepts commands, and bridges the unit to HA over
MQTT with autodiscovery.

You do two things: **configure the add-on** (below), and **redirect the
device's cloud connection** to this add-on (see *The network redirect*).

## Configuration

| Option | What it does |
| --- | --- |
| `mqtt_host` | Your broker. If you use the Mosquitto broker add-on, leave it as `core-mosquitto`. |
| `mqtt_port` | Broker port (default `1883`). |
| `mqtt_user` / `mqtt_pass` | Credentials for the broker. |
| `allowed_source_ip` | Optional hardening: set it to the dehumidifier's IP, so no other device on the network can reach port 6200. |
| `mode` | `local` (production — answer the device, bridge to HA) or `relay` (pass-through to the real cloud, for protocol capture). Leave it `local`. |

If you use the Mosquitto broker add-on, give the same user a login in **its**
config and restart it:

```yaml
logins:
  - username: <mqtt_user>
    password: <mqtt_pass>
```

Start this add-on. The log shows `MQTT connected (rc=Success)` and
`published HA discovery`.

## The network redirect (the only hard part)

The dehumidifier never accepts connections. It only opens an outbound
connection to its cloud on TCP port 6200. At boot, it requests the hostname
**`online-app.toovem.com`** from the DNS server that your router gives it. Then
it connects to the answer.

So you can give it a false answer for that name (**Method A**, easiest), or
send the connection elsewhere afterward (**Method B**). Nothing here is visible
to the internet. All the traffic stays on your LAN.

### Method A — DNS override (recommended)

Map `online-app.toovem.com` to your Home Assistant host's IP. One record, no
firewall rules. It needs only that the dehumidifier uses your DNS server. This
is the normal case, because it uses the DNS server that DHCP gives it.

- **Easiest on Home Assistant OS — the official Dnsmasq add-on:** Install
  **Dnsmasq** from the Add-on Store. In its config, map
  `online-app.toovem.com` to your HA host's IP. Then set your router's DHCP
  **DNS server** to the HA host, so the dehumidifier asks Dnsmasq. The whole
  setup stays inside Home Assistant. Dnsmasq forwards all other requests
  upstream, so it works as your normal resolver. Note that DNS then depends on
  HA running.
- **Pi-hole / AdGuard Home:** add a local DNS record or rewrite for the name.
- **Router with local-DNS support (OPNsense/pfSense, OpenWrt, some consumer
  routers):** add a host override for the name.

Then **power-cycle the dehumidifier**. It resolves the name only at boot.

### Method B — Destination NAT (DNS-independent, needs a capable router)

A DNAT rule catches the outbound connection whatever the DNS says:

> **IF** a packet comes **from** the dehumidifier's IP, is **TCP**, and goes
> **to port 6200** (any destination) → **rewrite its destination to**
> `<HA host IP>:6200`.

Match the *source* (the dehumidifier only) and *any* destination address (the
cloud IP rotates). UniFi, pfSense/OPNsense, MikroTik, and OpenWrt support this.
Then power-cycle the dehumidifier.

### Verify

Within about 30 seconds, the add-on log shows `=== device connected ===`, and a
**Dehumidifier** device appears under Settings → Devices & Services → MQTT.

For full router-by-router recipes, an ISP-router fallback, and the reverse-
engineered wire protocol, see the project's README and `PROTOCOL.md` on GitHub:
<https://github.com/sethrobin/alorair-local>

## What you get

A **Dehumidifier** device with:

- on/off control (this tracks the front panel too)
- a target slider (35–90%)
- a normal / continuous mode selector
- humidity, temperature, and grains-per-pound sensors
- coil-temperature and total-working-hours diagnostics
- pump, compressor, and fan indicators
- a purge button (a single ~30 s pump-out)
- a locate switch. It beacons the unit. The unit beeps loudly.

## Notes

- The device resolves its cloud name (and opens its connection) only at boot.
  After any redirect change, power-cycle it.
- If you switch the unit off at its front panel, HA shows it within one status
  frame (about 10 s). The two stay in sync.
