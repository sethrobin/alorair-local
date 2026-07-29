# AlorAir Local

Local **Home Assistant** control for **AlorAir / BaseAire "Sentinel" WiFi
dehumidifiers**, with **no vendor cloud**. Developed on a **Sentinel HDi65S**.

These units have no LAN control API. The WiFi module only connects out to the
vendor cloud (plaintext TCP, port 6200), and the cloud drives it from there.
This project sends that connection to a small local server instead. The server
speaks the reverse-engineered protocol and bridges the unit to Home Assistant
over MQTT.

> Status: **works in production.** It controls the setpoint, power on/off, and
> purge. It reports full status, including the true power state (Home Assistant
> tracks the front panel too). For the reverse-engineered wire format, see
> `PROTOCOL.md`.

## How it works

The dehumidifier opens an outbound connection to its cloud. It resolves
`online-app.toovem.com`, then uses plaintext TCP on port 6200. A one-line DNS
override, or a router NAT rule, sends that connection to this bridge instead.
The bridge decodes status, sends commands, and publishes to MQTT with Home
Assistant autodiscovery.

For setup, see **The network redirect** below. For the architecture diagram,
see `CLAUDE.md`. For the wire format, see `PROTOCOL.md`.

## Install (Home Assistant add-on — recommended)

> **Needs Home Assistant OS or Supervised.** These are the installs that have
> the Add-on Store. (The HA UI now calls add-ons **apps**.) If you run Home
> Assistant as a plain Docker **Container** (or Core), you cannot install
> add-ons. Run the bridge as its own container instead. See
> [Alternative: plain Docker](#alternative-plain-docker-no-ha-add-on) below.
>
> The same split applies to the DNS step. The Dnsmasq option in Method A is
> itself an add-on. So Container users configure DNS on their router, or with
> a standalone Pi-hole, AdGuard, or dnsmasq.

1. Add this repository as an add-on source. Go to Settings -> Add-ons ->
   Add-on Store -> ⋮ -> **Repositories**. Paste
   `https://github.com/sethrobin/alorair-local`, then reload the store.
   (Or use the one-click link:
   [Add repository to my Home Assistant](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fsethrobin%2Falorair-local).)
2. Find "AlorAir Local Bridge" under the **AlorAir Local** section of the
   store. Install it. When the add-on `version` changes on GitHub, the update
   appears in the store. No file copying.
   <details><summary>Alternative: local add-on without GitHub</summary>

   Copy the `alorair_bridge/` folder into your HA config's **`/addons`** mount.
   This is the top-level `addons` Samba share, or `/addons` in the SSH & Web
   Terminal add-on. It is a sibling of `/homeassistant`, not inside it. Then
   run "Check for updates" in the store. The add-on appears under **Local
   add-ons**.
   </details>
3. In the add-on **Configuration**, set `mqtt_user` and `mqtt_pass`. If you use
   the Mosquitto broker add-on, leave `mqtt_host` as `core-mosquitto`.
4. Give that user a login in the **Mosquitto broker** add-on config:
   ```yaml
   logins:
     - username: <mqtt_user>
       password: <mqtt_pass>
   ```
   Restart Mosquitto. Start the AlorAir add-on. The log shows
   `MQTT connected (rc=Success)` and `published HA discovery`.
5. Configure **the network redirect** (the next section). Then power-cycle the
   dehumidifier so it reconnects through the redirect. The add-on log shows
   `=== device connected ===`.

A **Dehumidifier** device appears under Settings -> Devices & Services -> MQTT.
It has:

- on/off control (this tracks the front panel too)
- a target slider (35–90%)
- a normal / continuous mode selector
- humidity, temperature, and grains-per-pound sensors
- coil-temperature and total-working-hours diagnostics
- pump, compressor, and fan indicators
- a purge button (a single ~30 s pump-out cycle)
- a locate switch. It beacons the unit and shows the live state. It turns
  itself off when the unit's ~65 s auto-clear runs.

## The network redirect (the only hard part)

The dehumidifier never accepts connections. It only opens an outbound
connection to its cloud on TCP port 6200. So there is nothing to connect to.
Your network must catch that outgoing call and send it to Home Assistant
instead.

There are two ways to do this. **Try Method A first.** It is simpler and works
on more routers.

At boot, the unit requests the hostname **`online-app.toovem.com`** from the
DNS server that your router gives it. Then it connects to the answer on port
6200. (`PROTOCOL.md` has the packet capture.) So you can give it a false answer
for that name (Method A), or send the connection elsewhere afterward
(Method B).

Nothing here is visible to the internet. All the traffic stays inside your LAN.

## Method A: DNS override (easiest — recommended)

Map `online-app.toovem.com` to your Home Assistant host. One record, no
firewall rules. The maintainer's own deployment uses this method.

> Requirement: the dehumidifier must use *your* DNS server. This is the normal
> case, because it uses the DNS server that your router gives it over DHCP.
> (If your network forces devices to an external resolver, use Method B.)

1. Add a **local DNS record** (or DNS rewrite) that maps
   `online-app.toovem.com` to **your HA host's IP**:
   - **Home Assistant OS — official Dnsmasq add-on** (no external tools, the
     easiest path on HAOS): Install **Dnsmasq** from the Add-on Store. Its
     description reads: *"Setup and manage a Dnsmasq DNS server. This allows
     you to manipulate DNS requests… have your Home Assistant domain resolve
     with an internal address inside your network."* In its config, add a host
     entry that maps `online-app.toovem.com` to your HA host's IP. Start it.
     Then set your router's DHCP **DNS server** to the HA host, so the
     dehumidifier asks Dnsmasq. The whole setup stays inside Home Assistant.
     No Pi-hole, no router DNS feature, no NAT rule. Dnsmasq forwards every
     other request upstream, so it works as your normal resolver. Note that
     DNS then depends on HA running.
   - **Pi-hole**: Local DNS → DNS Records → add the pair.
   - **AdGuard Home**: Filters → DNS rewrites → add `online-app.toovem.com`
     with your HA IP.
   - **UniFi**: Settings → Routing (recent firmware has local DNS records). If
     yours does not, use Method B. UniFi does Method B very well.
   - **OPNsense/pfSense**: Services → Unbound DNS → Overrides → Host Override.
   - **OpenWrt**: Network → DHCP and DNS → Hostnames → add the name and IP.
   - **Consumer routers**: Find an option named "Local DNS", "DNS host entry",
     or "Address Reservation + DNS". Or run Pi-hole or AdGuard on any always-on
     machine, and set your router's DHCP DNS to it.
2. Make sure the dehumidifier uses that resolver. It does by default, because
   it asks the DNS server that DHCP gave it.
3. **Power-cycle the dehumidifier.** It resolves the name only at boot. A
   restart is necessary to get the new answer.
4. Confirm that the add-on log shows `=== device connected ===`.

To undo this, delete the record and power-cycle the unit. It then returns to
the vendor cloud.

Two notes. The name resolves to an Alibaba load balancer. This balancer uses
rotating IPs and a 60-second TTL. This is *why* you override the name and never
an IP.

Also, relay mode (protocol capture) needs `--cloud <a real cloud IP>` with this
method. If you omit it, the bridge resolves the same overridden name and
connects to itself. The add-on detects this case and reports it.

## Method B: Destination NAT (DNS-independent, needs a capable router)

Routers call this **Destination NAT (DNAT)**. Some call it "NAT redirect" or
"policy NAT". It is not the same as normal port forwarding. Port forwarding
handles traffic that comes *in from* the internet. This rule redirects traffic
that starts *inside* your network.

DNAT catches the connection whatever the DNS says. So it still works if a
future firmware fixes the resolver or IP in the device. Both methods work on
real hardware. Choose the one that your router makes easier.

### What the rule says, in any router's language

> **IF** a packet comes **from** the dehumidifier's IP, is **TCP**, and goes
> **to port 6200** (any destination address),
> **THEN** rewrite its destination to `<HA host IP>:6200`.

Two details matter:

- **Match the source** (the dehumidifier only). You do not want to redirect
  port-6200 traffic from other devices.
- **Match any destination address.** The cloud's IP changes over time, because
  it is behind DNS. Do not fix the rule to the IP that it uses today.

### Step by step

1. **Fix both IPs.** In your router's device list, give the dehumidifier and
   the Home Assistant host **fixed IPs**. This is a "DHCP reservation", usually
   a button or checkbox on the device's entry. If you are not sure which client
   is the dehumidifier, disconnect it. Watch which entry disappears, then
   reconnect it. The redirect rule uses these IPs permanently, so they must
   never change.
2. **Create the DNAT rule.** Here is where to find it on common routers:
   - **UniFi**: Settings → Routing → NAT → Create Entry → **Destination NAT**:
     interface = your LAN, protocol TCP, source = dehumidifier IP,
     destination port 6200, translated IP = HA host, translated port 6200.
   - **pfSense / OPNsense**: Firewall → NAT → Port Forward: interface LAN,
     protocol TCP, source = dehumidifier IP, destination *any*, destination
     port 6200, redirect target = HA host IP, port 6200.
   - **MikroTik**:
     `/ip firewall nat add chain=dstnat src-address=<dehumidifier> protocol=tcp dst-port=6200 action=dst-nat to-addresses=<HA> to-ports=6200`
   - **OpenWrt**: The OpenWrt UI on older versions does not accept a source
     restriction. Add this to `/etc/config/firewall`:
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
     Then run `service firewall restart`.
3. **Power-cycle the dehumidifier.** It reads its network path only when it
   opens a connection. Give it a fresh start.
4. **Verify.** Within about 30 seconds, the add-on log shows
   `=== device connected from (<its IP> ...`, and the Dehumidifier device
   appears in HA. If it does not, check three things. Check that the
   reservation took effect (correct MAC?). Check that the rule matches TCP port
   6200 with *any* destination. Check that the HA add-on runs, because
   something must listen on 6200 for the redirect to work.

### "My router cannot do either" (some ISP-provided routers)

Your router may offer only inbound port forwarding (no DNAT), and no way to set
local DNS records. You still have options. Try the Method A fallback first.

Run **Pi-hole or AdGuard Home** on any always-on machine (the HA box can run it
as an add-on). Set your router's DHCP DNS server to that machine. This gives
you the DNS rewrite without a change to the router's firewall. Most ISP routers
do let you change the DNS server that they give out.

If even that is not possible, here are the options, best first:

1. **Add a small OpenWrt router as the dehumidifier's WiFi** (about $30–40, for
   example a GL.iNet travel router). Connect it to your network. Let the
   dehumidifier join *its* WiFi, not your main one. Put the OpenWrt rule from
   above on it. Every packet from the unit goes through this small router. The
   router redirects port 6200 to Home Assistant before your ISP router sees it.
   Your main network needs no changes.
2. **Replace or re-flash your router** with one that supports DNAT (OpenWrt,
   OPNsense, MikroTik, UniFi...). This is the better long-term fix. It is a
   bigger project than this integration.
3. **Run your ISP box as a modem only** ("bridge mode") behind a router that
   supports DNAT. This gives the same result as option 2.

Option 1 is a good choice. It is cheap, self-contained, and reversible.

## Alternative: plain Docker (no HA add-on)

See `deploy/`. `docker-compose.local.yml` runs the same script on any Docker
host. Set the `MQTT_*` environment variables and bind-mount the script.
`docker-compose.relay.yml` runs relay mode for protocol capture.

## Development

- `relay` mode needs only the standard library. `local` mode needs `paho-mqtt`.
- `python3 -m unittest discover -s tests` runs the frame-layer tests.
- To re-capture protocol behavior, run relay mode. Control the unit from the
  AlorAir-C app, then read the decoded log.
- For frame experiments, publish a hex string to
  `alorair/<device_id>/raw/set`. The bridge sends those exact bytes to the
  device (see PROTOCOL.md).
- Optional hardening: set `allowed_source_ip` in the add-on config to the
  dehumidifier's IP. Then no other device on the network can reach port 6200.
- **Never commit MQTT credentials.** They stay in the HA add-on config at
  runtime, not in this repo.

## About this project

**Claude** (Anthropic's AI) did the bulk of the work here under my direction.
It reverse-engineered the wire protocol, wrote the bridge and the Home
Assistant add-on, and produced this documentation. I set the goals, supplied
the hardware and network access, and made the judgment calls. I also ran the
physical tests: I pressed panel buttons, listened for the compressor, and
captured packets. Claude did most of the analysis, code, and writing, and it
worked against live results from the real dehumidifier.

I share it not only because it works, but as a small, concrete demonstration of
the democratization of software development that AI has enabled: a complete,
tested, local integration for an otherwise cloud-locked device, built by
someone who directed the effort rather than hand-wrote every line. If you have
a device you wish you controlled and a problem you can describe clearly, that
path is open to you too.

— Seth Robinson

## License

MIT (see LICENSE) — change to taste.
