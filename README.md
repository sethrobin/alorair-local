# AlorAir Local

Local **Home Assistant** control for **AlorAir / BaseAire "Sentinel" WiFi
dehumidifiers** (developed on a **Sentinel HDi65S**) — with **no vendor cloud**.

These units have no LAN control API; the WiFi module only phones home to the
vendor cloud (plaintext TCP, port 6200) and is driven from there. This project
redirects that connection to a small local server that speaks the
reverse-engineered protocol and bridges the unit to Home Assistant over MQTT.

> Status: **fully working in production** — setpoint, power on/off, purge,
> and full status reporting including true power state (HA tracks the front
> panel too). See `PROTOCOL.md` for the reverse-engineered wire format.

## How it works

The dehumidifier opens an *outbound* connection to its cloud (it resolves
`online-app.toovem.com`, then talks plaintext TCP on port 6200). A one-line
DNS override — or a router NAT rule — delivers that connection to this bridge
instead, which decodes status, sends commands, and publishes to MQTT with
Home Assistant autodiscovery. See **The network redirect** below for setup,
`CLAUDE.md` for the architecture diagram, and `PROTOCOL.md` for the wire
format.

## Install (Home Assistant add-on — recommended)

> **Requires Home Assistant OS or Supervised** — the installs that have the
> Add-on Store (add-ons are now called **apps** in the HA UI). If you run
> Home Assistant in a plain Docker **Container** (or Core), you can't install
> add-ons at all — run the bridge as its own container instead; see
> [Alternative: plain Docker](#alternative-plain-docker-no-ha-add-on) below.
> The same split applies to the DNS step: the Dnsmasq option under Method A
> is itself an add-on, so Container users handle DNS on their router or with
> a standalone Pi-hole/AdGuard/dnsmasq.

1. Add this repository as an add-on source: Settings -> Add-ons -> Add-on
   Store -> ⋮ -> **Repositories**, paste
   `https://github.com/sethrobin/alorair-local`, then reload the store.
   (Or use the one-click link:
   [Add repository to my Home Assistant](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fsethrobin%2Falorair-local).)
2. "AlorAir Local Bridge" appears under the **AlorAir Local** section of the
   store. Install it. Updates arrive whenever the add-on `version` is bumped
   on GitHub — no file copying.
   <details><summary>Alternative: local add-on without GitHub</summary>

   Copy the `alorair_bridge/` folder into your HA config's **`/addons`** mount
   (the top-level `addons` Samba share, or `/addons` in the SSH & Web Terminal
   add-on — a sibling of `/homeassistant`, not inside it), then Check for
   updates in the store; it appears under **Local add-ons**.
   </details>
3. In the add-on **Configuration**, set `mqtt_user` / `mqtt_pass`. Leave
   `mqtt_host` as `core-mosquitto` if you use the Mosquitto broker add-on.
4. Give that user a login in the **Mosquitto broker** add-on config:
   ```yaml
   logins:
     - username: <mqtt_user>
       password: <mqtt_pass>
   ```
   Restart Mosquitto. Start the AlorAir add-on; the log should show
   `MQTT connected (rc=Success)` and `published HA discovery`.
5. Set up **the network redirect** (next section — the only fiddly part),
   then power-cycle the dehumidifier so it reconnects through it. The add-on
   log should show `=== device connected ===`.

A **Dehumidifier** device appears under Settings -> Devices & Services -> MQTT:
on/off (tracks the front panel too), target slider (35-90%), a normal /
continuous mode selector, humidity / temperature / grains-per-pound sensors,
coil-temperature and total-working-hours diagnostics, pump / compressor / fan
indicators, a purge button (one-shot ~30s pump-out cycle), and a locate
switch (beacons the unit; shows live state and flips itself off when the
unit's ~65s auto-clear fires).

## The network redirect (the only hard part)

The dehumidifier never *accepts* connections — it only dials **out** to its
cloud on TCP port 6200. So there is nothing to "connect to": your network has
to intercept that outgoing call and deliver it to Home Assistant instead.

There are two ways to do that. **Try Method A first** — it's simpler and
works on far more routers.

At boot the unit looks up the hostname **`online-app.toovem.com`** using
whatever DNS server your router hands it, then connects to the answer on port
6200 (packet capture in `PROTOCOL.md`). So you can either lie to it about
that name (Method A) or reroute the connection afterwards (Method B).

Nothing here exposes anything to the internet — all the traffic involved
stays inside your LAN.

## Method A: DNS override (easiest — recommended)

Point `online-app.toovem.com` at your Home Assistant host. One record, no
firewall rules. This is what the maintainer's own deployment runs.

> Requirement: the dehumidifier must be using *your* DNS server — the normal
> case, since it takes whatever your router hands out over DHCP. (If you run
> a network that forces devices to an external resolver, use Method B.)

1. Add a **local DNS record / DNS rewrite** mapping
   `online-app.toovem.com` → **your HA host's IP**:
   - **Home Assistant OS — official Dnsmasq add-on** (no external tools, the
     easiest path if you already run HAOS): install **Dnsmasq** from the
     Add-on Store — *"Setup and manage a Dnsmasq DNS server. This allows you
     to manipulate DNS requests… have your Home Assistant domain resolve with
     an internal address inside your network."* In its config, add a host
     entry mapping `online-app.toovem.com` → your HA host's IP, and start it.
     Then point your router's DHCP **DNS server** at the HA host so the
     dehumidifier asks Dnsmasq. The whole setup stays inside Home Assistant —
     no Pi-hole, no router DNS feature, no NAT rule. (Dnsmasq forwards every
     other lookup upstream, so it works as your normal network resolver; just
     note that DNS then depends on HA being up.)
   - **Pi-hole**: Local DNS → DNS Records → add the pair.
   - **AdGuard Home**: Filters → DNS rewrites → add `online-app.toovem.com`
     with your HA IP.
   - **UniFi**: Settings → Routing (recent firmware exposes local DNS
     records); otherwise use Method B, which UniFi does very well.
   - **OPNsense/pfSense**: Services → Unbound DNS → Overrides → Host Override.
   - **OpenWrt**: Network → DHCP and DNS → Hostnames → add the name and IP.
   - **Consumer routers**: look for "Local DNS", "DNS host entry", "Address
     Reservation + DNS", or run Pi-hole/AdGuard on any always-on machine and
     point your router's DHCP DNS at it.
2. Make sure the dehumidifier actually uses that resolver (it does by
   default — it asks whatever DNS server DHCP gave it).
3. **Power-cycle the dehumidifier.** It only resolves the name at boot, so a
   reboot is required to pick up the new answer.
4. Confirm the add-on log shows `=== device connected ===`.

To undo it, delete the record and power-cycle the unit; it goes back to the
vendor cloud.

Two footnotes. The name resolves to an Alibaba load balancer with rotating
IPs and a 60-second TTL, which is *why* you override the name and never an
IP. And if you use this method, relay mode (protocol capture) needs
`--cloud <a real cloud IP>` — otherwise the bridge resolves the same
overridden name and dials itself. The add-on detects that case and says so.

## Method B: Destination NAT (DNS-independent, needs a capable router)

Routers call this **Destination NAT (DNAT)** — sometimes "NAT redirect" or
"policy NAT". It is not the same thing as ordinary port forwarding (which
handles traffic coming *in from* the internet; this rule bends traffic that
starts *inside* your network). It catches the connection regardless of DNS,
so it still works if a future firmware hardcodes a resolver or an IP. Both
methods are proven on real hardware; pick whichever your router makes easier.

### What the rule says, in any router's language

> **IF** a packet comes **from** the dehumidifier's IP, is **TCP**, and is
> going **to port 6200** (any destination address),
> **THEN** rewrite its destination to `<HA host IP>:6200`.

Two details matter:

- **Match the source** (the dehumidifier only). You don't want other devices'
  port-6200 traffic hijacked.
- **Match any destination address.** The cloud's IP changes over time (it's
  behind DNS), so don't pin the rule to the IP it happens to use today.

### Step by step

1. **Pin both IPs.** In your router's device/client list, give the
   dehumidifier and the Home Assistant host **fixed IPs** ("DHCP
   reservation" — usually a button or checkbox on the device's entry). If
   you're not sure which client is the dehumidifier, unplug it, watch which
   entry drops off, plug it back in. The redirect rule points at these IPs
   forever, so they must never change.
2. **Create the DNAT rule.** Where this lives on common routers:
   - **UniFi**: Settings → Routing → NAT → Create Entry → **Destination NAT**:
     interface = your LAN, protocol TCP, source = dehumidifier IP,
     destination port 6200, translated IP = HA host, translated port 6200.
   - **pfSense / OPNsense**: Firewall → NAT → Port Forward: interface LAN,
     protocol TCP, source = dehumidifier IP, destination *any*, destination
     port 6200, redirect target = HA host IP, port 6200.
   - **MikroTik**:
     `/ip firewall nat add chain=dstnat src-address=<dehumidifier> protocol=tcp dst-port=6200 action=dst-nat to-addresses=<HA> to-ports=6200`
   - **OpenWrt**: Network → Firewall → Port Forwards won't take a source
     restriction in the UI on older versions; add to `/etc/config/firewall`:
     ```
     config redirect
         option name 'alorair'
         option target 'DNAT'
         option src 'lan'
         option proto 'tcp'
         option src_ip '<dehumidifier IP>'
         option src_dport '6200'
         option dest_ip '<HA host IP>'
         option dest_port '6200'
     ```
     then `service firewall restart`.
3. **Power-cycle the dehumidifier.** It only reads its network path when it
   opens a connection, so give it a fresh start.
4. **Verify.** The add-on log shows `=== device connected from (<its IP> ...`
   within ~30 seconds, and the Dehumidifier device appears in HA. If not:
   re-check the reservation actually took (right MAC?), that the rule matches
   TCP port 6200 with *any* destination, and that HA's add-on is running
   (something must be listening on 6200 for the redirect to land).

### "My router can't do either" (some ISP-provided routers)

If your router offers only inbound port forwarding (no DNAT) *and* no way to
set local DNS records, you still have options — but try Method A's fallback
first: run **Pi-hole or AdGuard Home** on any always-on machine (including
the HA box itself as an add-on) and set your router's DHCP DNS server to it.
That gives you the DNS rewrite without touching the router's firewall, and
most ISP routers do let you change the DNS server they hand out.

If even that is locked down, in order of preference:

1. **Add a small OpenWrt router as the dehumidifier's WiFi** (~$30–40, e.g.
   a GL.iNet travel router). Plug it into your network, let the dehumidifier
   join *its* WiFi instead of your main one, and put the OpenWrt rule from
   above on it. Every packet from the unit passes through the little router,
   which bends port 6200 to Home Assistant before your ISP router ever sees
   it. No changes to your main network at all.
2. **Replace or re-flash your router** with something DNAT-capable (OpenWrt,
   OPNsense, MikroTik, UniFi...). The better long-term fix, but a bigger
   project than this integration.
3. **Run your ISP box as modem-only** ("bridge mode") behind a capable
   router — same outcome as option 2.

There's no shame in option 1: it's cheap, contained, and reversible.

## Alternative: plain Docker (no HA add-on)

See `deploy/`. `docker-compose.local.yml` runs the same script on any Docker
host; set the `MQTT_*` env vars and bind-mount the script. `docker-compose.relay.yml`
runs transparent relay mode for protocol capture.

## Development

- `relay` mode = stdlib only; `local` mode needs `paho-mqtt`.
- `python3 -m unittest discover -s tests` runs the frame-layer tests.
- Re-capture protocol behavior by running relay mode and driving the unit from
  the AlorAir-C app; read the decoded log.
- Frame experiments: publish a hex string to `alorair/<device_id>/raw/set` and
  the bridge sends those exact bytes to the device (see PROTOCOL.md).
- Optional hardening: set `allowed_source_ip` in the add-on config to the
  dehumidifier's IP so nothing else on the network can talk to port 6200.
- **Never commit MQTT credentials.** They live in the HA add-on config at
  runtime, not in this repo.

## About this project

The bulk of the work here — reverse-engineering the wire protocol, writing the
bridge and the Home Assistant add-on, and producing this documentation — was
done by **Claude** (Anthropic's AI) working under my direction. I set the
goals, supplied the hardware and network access, ran the physical tests
(pressing panel buttons, listening for the compressor, capturing packets), and
made the judgment calls; Claude did most of the analysis, coding, and writing,
iterating against live results from the actual dehumidifier.

I'm sharing it not only because it works, but as a small, concrete
demonstration of the democratization of software development that AI has
enabled: a complete, tested, local integration for an otherwise cloud-locked
device — built by someone directing the effort rather than hand-writing every
line. If you have a device you wish you controlled and a problem you can
describe clearly, that path is open to you too.

— Seth Robinson

## License

MIT (see LICENSE) — change to taste.
