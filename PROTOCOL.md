# AlorAir / BaseAire WiFi protocol (reverse-engineered)

Device: BaseAire / AlorAir **Sentinel HDi65S** (WiFi module reports BLE name
`BK-alorair`; cloud endpoint on Alibaba Cloud, TCP port 6200, plaintext).

## Cloud endpoint / how the device finds its server

Captured at the gateway 2026-07-27 (device power-cycle):

```
192.168.1.50.28227 > 192.168.1.1.53: A? online-app.toovem.com.
192.168.1.1.53 > 192.168.1.50.28227: CNAME nlb-f2xfnhpq30h62dr13l.us-west-1
                    .nlb.aliyuncsslbintl.com., A 43.110.37.64, A 47.77.188.71
```

- Hostname: **`online-app.toovem.com`** (`toovem.com` is the module vendor's
  domain, not an AlorAir one — which is why guessing alorair.com/baseaire.com
  never found it).
- It is an Alibaba Cloud **network load balancer** returning multiple A
  records with a 60 s TTL, so the peer IP legitimately changes between
  connections. Never pin an IP.
- The device asks the **DHCP-provided DNS server** (the LAN gateway) — it
  does *not* hardcode a public resolver. A local DNS record pointing this
  name at the bridge is therefore sufficient to capture the device, with no
  NAT rule at all — confirmed in production 2026-07-27 (see README Method A).
- Timing: it resolves once at boot, then reuses that address for subsequent
  reconnects. A power cycle is needed to pick up a changed DNS answer.
All fields below were confirmed against the AlorAir-C app's own readings and a
labeled command sequence. Byte offsets are **within the frame body** (the body
starts at frame offset 18; frame_offset = body_offset + 18).

## Frame envelope

```
offset  bytes  field
0       2      magic            0x0d 0x0e
2       6      reserved         all 0x00
8       6      device MAC       e.g. a0 24 42 24 d8 ce
14      4      token            0x00000000 from device;
                                 big-endian UNIX timestamp from cloud
18      ..     body             (see below)
end-2   1      checksum         see "Checksum"
end-1   1      terminator       0x16
```

## Checksum

Additive, NOT a CRC. The frame trailer is:

    ... <class> <checksum> 0x16

where `<class>` is the final body byte and

    checksum = sum(all frame bytes BEFORE <class>) & 0xFF

i.e. **the class byte is excluded from the sum**. Verified byte-for-byte on
captured command frames (class `0x05`, e.g. the setpoint-45 worked example:
1500 − 5 = 1495 → `0xD7`) and the captured device keepalive (class `0x02`:
759 − 2 = 757 → `0xF5`).

Class bytes seen so far:

```
0x05  ALL commands (setpoint / power on+off / purge / locate), cloud
      keepalive-ack
0x02  device keepalive
0x04  status frames (read directly from a live status hex dump 2026-07-27,
      confirming what their checksums implied)
```

Historical note: this was first modeled as `checksum = sum(all prior bytes)
+ K` with per-type constants K (0xFB commands, 0xFC status, 0xFE keepalive).
Those constants are artifacts of the real rule — `K = 0x100 - <class>` — and
it took the keepalive capture (class `0x02`, "K=0xFE") to expose the pattern.

The one capture that seemed to show class `0x04` on a power-ON command was
settled by experiment (2026-07-27, unit off at panel): the `0x04` variant was
silently ignored, while the ordinary class-`0x05` power-on actuated within
~600 ms with an `evt=0x21` ack. That capture was corrupt; commands are
uniformly class `0x05`.

## Status frame (device -> cloud, 74-byte frame = 56-byte body, ~every 10s)

Body starts `00 2d 0b ..`. Confirmed offsets:

```
body off  field
1         0x2d constant (model/type marker; used to detect a status frame)
3         event flag: echoes the opcode of the command being acknowledged
          (0x23 setpoint · 0x21 power · 0x22 purge, on the next status after
          the command, ~600ms); 0x1c = baseline. Panel-initiated changes do
          NOT set it — it is strictly a command ack.
11        power/enable state — follows BOTH cloud commands and the
          front-panel button (confirmed live, 2026-07-27). This is the field
          that closes the old off-vs-idle blind spot. Values:
            0 = off
            1 = on
            2 = on, but waiting out the compressor start delay after a
                power interruption (~5.5 min observed; the unit sits at 2
                with fan and compressor off, then flips to 1 as they start)
          Treat 2 as ON — anything else misreports the unit for minutes
          after every power cycle.
12        pump / purge active (1 = pumping-out, 0 = idle)
14        compressor (1 = actively removing moisture; starts ~10s after
          enable — protection delay — and stops instantly on off)
15        fan (starts with enable, keeps running ~1-2 min AFTER off as
          run-down; was mislabeled "compressor" before the byte-diff hunt)
16        temperature, deg C
17        temperature, deg F
18        relative humidity, %
23        grains per pound (GPP)
25        moisture content, g/kg — GPP is exactly this ×7 (the g/kg → gr/lb
          conversion factor), confirmed across multiple readings
30        evaporator coil temperature, deg C (matches the app's "Coil temp";
          drives auto-defrost — cold while drying, drifts to ambient off)
31        current setpoint, % (reads 34 in continuous mode — see below)
36-37     total working hours, 16-bit big-endian (matches the app's
          "total working time")
38        locate active (1 while the locate beacon runs; the unit
          self-clears it after ~65s)
52        operating mode: 0x00 = normal humidistat, 0x02 = continuous
```

Undecoded non-zero bytes remaining: offset 2 = 0x0b (constant; possibly a
frame-subtype or protocol version), 8 = 0x10 (constant), 39 = 0x20 = 32
(constant; plausibly the auto-defrost coil threshold in °F, unverified).
Never-yet-seen state also expected somewhere in the zero bytes: defrost
active (panel light exists) and the E1..E5 error codes from the manual.
The byte-diff logger will expose them when they first occur.

## Continuous mode

Per the manual (and confirmed on the wire): a setpoint below 35% switches
the unit to continuous operation (panel shows "CO", Cont. light green).
There is no separate opcode — send setpoint (0x23) with any value < 35 and
the device clamps it to 34 and sets status offset 52 to 0x02. Any setpoint
in the normal 35-90 range returns it to humidistat operation (offset 52
back to 0x00). Both transitions ack with evt=0x23.

## Filter timer: app-side only

Confirmed by relay capture 2026-07-27: performing "reset filter timer" in
the AlorAir-C app produced ZERO frames to the device (only keepalives and
statuses crossed the wire during the whole session). The 90-day filter
reminder lives entirely in the app/vendor-cloud account. The app's other
"device detail" figures map to status fields (coil temp = offset 30,
working hours = offsets 36-37) or static model specs (Pint/Day, CFM).

Modeling in HA: on/off comes straight from offset 11 (panel-proof); action is
"off" when disabled, "drying" when the compressor (offset 14) runs, else
"idle". A fan-only run-down after off still reports action "off".

## Keepalive

- Device -> cloud: 29-byte frame, ~every 30s. Captured 2026-07:

  ```
  0d0e 000000000000 a02442abcdef 00000000  00 00 09 01 00 00 00 00 02  92 16
                                                              class ^   ^cksum
  ```

  Standard trailer with class byte `0x02`. (An earlier note here claiming
  the keepalive has no trailer was wrong — it just uses a different class
  byte, which the pre-1.1.2 bridge validator didn't accept.) It is also the
  first frame the device sends after opening a session.
- Cloud -> device: 30-byte ack, body `00 01 09 01 00 00 00 00 00 05 ..`
  (class `0x05`; `build_command(mac, 0x01, 0)` builds it).
- The bridge acks the device's keepalive and also sends one every 30s.

## Command frame (cloud -> device)

Body layout:

```
00 01 09 <op> 00 00 00 00 <val> 05     (then checksum, 0x16)
          ^op at body offset 3          ^val at body offset 8
```

Opcodes (op) and value:

```
op    action      value
0x23  setpoint    target RH %   (CONFIRMED: 0x32=50, 0x2d=45, 0x3c=60;
                                 valid range 35-90, values <35 clamp to 34
                                 and engage continuous mode)
0x21  power       1 = on, 0 = off   (CONFIRMED both ways, 2026-07-27; off has
                                     a ~100s compressor/fan run-down before
                                     status shows running=0)
0x22  purge       1 = trigger       (CONFIRMED: a one-shot ~30s pump cycle
                                     that self-terminates; val 0 cancels it
                                     early. Pump bit at status offset 12
                                     tracks the cycle)
0x27  locate      1 = on, 0 = off   (CONFIRMED 2026-07-27: acked evt=0x27,
                                     works even with the unit off; a real
                                     toggle -- off cancels it early, like
                                     the vendor app -- and the unit also
                                     self-clears it after ~65s; status
                                     offset 38 tracks it while active.
                                     Physically: fast, loud beeping -- loud
                                     enough that an Apple HomePod classified
                                     it as an audible alarm)
0x01  keepalive   0
```

Notes:
- Body offset 9 is the frame's class byte, `0x05` for all commands (see
  "Checksum" for how the one seemingly-`0x04` capture was settled).
- Every acted-on command is acknowledged on the next status frame (~600ms):
  the event flag at status body offset 3 echoes the command's opcode.
- Token (frame offset 14) is a big-endian UNIX timestamp in real cloud frames;
  the bridge stamps `int(time.time())`. The device does not appear to validate
  it strictly (it sends 0 itself), but this is unconfirmed for commands.

## Raw-frame experiments over MQTT

In local mode the bridge subscribes to `<prefix>/<dev_id>/raw/set` (e.g.
`alorair/alorair_a02442abcdef/raw/set`). Publish a hex string (spaces allowed)
and the bridge writes those exact bytes to the device — no checksum is added.
This is the intended path for byte-level command experiments like the byte-9
test above:

    mosquitto_pub -t 'alorair/alorair_<mac>/raw/set' -m '0d0e0000...16'

## Worked example (setpoint = 45)

Cloud frame (token 0x6a64f3d1; MAC anonymized to the real OUI + placeholder):

```
0d0e 000000000000 a02442abcdef 6a64f3d1  00 01 09 23 00 00 00 00 2d 05  74 16
                                                       ^op 0x23   ^0x2d=45  ^cksum
```

`build_command(mac, 0x23, 45)` reproduces this byte-for-byte (checksum 0x74).
