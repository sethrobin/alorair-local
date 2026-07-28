#!/usr/bin/env python3
"""
alorair_cloud.py  --  a stand-in for AlorAir's cloud, for the HDi65S (and its
                      BaseAire / Abestorm twins, same module).

The dehumidifier's only control channel is an OUTBOUND plaintext TCP session it
opens to the vendor cloud on port 6200. There is no LAN listener on the device,
so we don't connect to it -- we make it connect to US, either by overriding DNS
for `online-app.toovem.com` (the name it resolves at boot) to this host, or by
a Dest-NAT rule on the gateway (source = the device, dest port 6200 -> this
host:6200). Either way, the device then opens its session here.

MODES
  relay : transparent MITM to the real cloud; decode+log every frame. The unit
          keeps working normally. Use for capture / verification.
  local : answer the device ourselves (no vendor cloud) and bridge it to Home
          Assistant over MQTT with autodiscovery. This is the cord-cut mode.

PROTOCOL (reverse-engineered, all confirmed against the app's own readings)
  Envelope:  [0:2] magic 0d0e | [2:8] zeros | [8:14] device MAC |
             [14:18] token (0 from device; unix-time from cloud) | [18:] body
  Trailer:   ... <class> <checksum> 0x16   where <class> is the final body
             byte (0x05 commands, 0x02 device keepalive, 0x04 status/power-ON)
             and checksum = sum(all frame bytes BEFORE <class>) & 0xff.
  Status (device->cloud, 74-byte frame = 56-byte body), body offsets:
     11 power (0 off / 1 on / 2 on-but-in-start-delay; follows panel)
     12 pump  14 compressor  15 fan
     17 temp F  18 RH %  23 grains/lb  30 coil temp C  31 setpoint %
     36-37 working hours (BE16)  38 locate  52 mode (2=continuous)
     3 event flag (command acks only)
  Command (cloud->device): body = 00 01 09 <op> 00 00 00 00 <val> 05
     op 0x23 setpoint(val=%)  0x21 power(1/0)  0x22 purge(1/0)
     0x27 locate(1/0)  0x01 keepalive/ping(val 0)

MQTT (local mode) -- env vars:
  MQTT_HOST (default 127.0.0.1)  MQTT_PORT (1883)  MQTT_USER  MQTT_PASS
  MQTT_PREFIX (default alorair)  DISCOVERY_PREFIX (default homeassistant)
  ALLOWED_SOURCE_IP (optional: if set, only this IP may connect on :6200)

Frame experiments: publish a hex string to <prefix>/<dev_id>/raw/set and the
bridge sends those exact bytes to the device (no checksum added).
"""

import argparse
import asyncio
import datetime
import json
import os
import sys
import time

VERSION = "1.6.1"      # keep in sync with config.yaml

# The device resolves this at boot and connects to it on 6200. It is an
# Alibaba Cloud load balancer (CNAME -> nlb-*.us-west-1.nlb.aliyuncsslbintl
# .com) answering with several rotating A records, so always resolve the
# NAME -- never pin an IP. If you redirect this name by DNS, pass --cloud
# with a literal IP in relay mode or the relay will dial itself.
REAL_CLOUD_HOST = "online-app.toovem.com"
REAL_CLOUD_PORT = 6200
LISTEN_PORT = 6200

MAGIC = bytes.fromhex("0d0e")
LEGACY_KS = (0xFC, 0xFB)  # old sum+K model, kept as an rx-validation safety
                          # net for frame types not yet byte-verified
MIN_FRAME = 20         # envelope(18) + checksum + 0x16
MAX_FRAME = 512        # resync guard; real frames are 25..74 bytes
CMD = {0x01: "keepalive", 0x21: "power", 0x22: "purge",
       0x23: "setpoint", 0x27: "locate"}

_LOGF = None
_LOG_PATH = None
_LOG_MAX = 5_000_000   # rotate the logfile past this size (keeps one .1)


def emit(msg: str) -> None:
    global _LOGF
    print(msg, flush=True)
    if _LOGF is not None:
        _LOGF.write(msg + "\n")
        _LOGF.flush()
        if _LOG_PATH and _LOGF.tell() > _LOG_MAX:
            _LOGF.close()
            os.replace(_LOG_PATH, _LOG_PATH + ".1")
            _LOGF = open(_LOG_PATH, "a", buffering=1)


def ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------------------
# Framing
# ---------------------------------------------------------------------------
def checksum(frame_wo_cksum: bytes) -> int:
    """Additive checksum: sum of all bytes before the final body byte, mod 256.

    The final body byte (the frame's "class" byte: 0x05 commands, 0x02 device
    keepalive, 0x04 status/power-ON) is excluded from the sum. The per-type
    "K constants" documented earlier (0xFB/0xFC/0xFE) were artifacts of this
    rule: K = 0x100 - class byte. See PROTOCOL.md "Checksum".
    """
    return sum(frame_wo_cksum[:-1]) & 0xFF


def extract_frames(buf: bytearray):
    """Pull complete frames off the front of a stream buffer, in place.

    Primary delimiter: a frame starts at the 0d0e magic and ends at the first
    0x16 byte preceded by a valid additive checksum -- such frames are
    emitted the moment they are complete, not one frame late. A
    trailer candidate is only accepted if the frame would end flush against
    the end of the buffer or the next frame's magic (rejects payload bytes
    that happen to look like a trailer).

    A region that cannot validate (an unknown frame type with a different
    trailer rule) is flushed as-is once the next frame's magic arrives, with
    its hex logged verbatim -- unknown frame types are captured, not dropped,
    and can't stall the frames queued behind them.
    """
    frames = []
    while True:
        j = buf.find(MAGIC)
        if j == -1:
            # keep a trailing 0x0d in case it's the first half of a split magic
            keep = 1 if buf.endswith(b"\x0d") else 0
            del buf[:len(buf) - keep]
            return frames
        if j:
            del buf[:j]
        end = None
        for i in range(MIN_FRAME - 1, min(len(buf), MAX_FRAME)):
            if buf[i] != 0x16:
                continue
            s = sum(buf[:i - 1])
            # real rule (class byte at i-2 excluded from the sum), with the
            # legacy sum+K model as a safety net for unverified frame types
            if buf[i - 1] != (s - buf[i - 2]) & 0xFF and \
               not any(((s + k) & 0xFF) == buf[i - 1] for k in LEGACY_KS):
                continue
            rest = buf[i + 1:]
            if not rest or rest[:2] == MAGIC or rest == b"\x0d":
                end = i + 1
                break
        if end is not None:
            frames.append(bytes(buf[:end]))
            del buf[:end]
            continue
        # no checksum-valid trailer: delimit on the next magic instead
        m = buf.find(MAGIC, 2)
        if m == -1:
            if len(buf) > MAX_FRAME:
                emit(f"{ts()} !! dropping {len(buf)}B unframeable: "
                     f"{bytes(buf[:32]).hex()}...")
                buf.clear()
            return frames        # incomplete frame; wait for more bytes
        if 14 <= m <= MAX_FRAME:
            fr = bytes(buf[:m])
            emit(f"{ts()} ?? unvalidated frame: {fr.hex()}")
            frames.append(fr)
        else:
            emit(f"{ts()} !! dropping {m}B non-frame: {bytes(buf[:min(m, 32)]).hex()}")
        del buf[:m]


def build_frame(mac: bytes, token: int, body_wo_trailer: bytes) -> bytes:
    """Assemble a full cloud->device frame, appending checksum + 0x16."""
    env = MAGIC + b"\x00" * 6 + mac + token.to_bytes(4, "big")
    frame = env + body_wo_trailer
    return frame + bytes([checksum(frame), 0x16])


def build_command(mac: bytes, op: int, val: int) -> bytes:
    """Command body: 00 01 09 <op> 00 00 00 00 <val> 05  (token = current time)."""
    body = bytes([0x00, 0x01, 0x09, op, 0, 0, 0, 0, val & 0xFF, 0x05])
    return build_frame(mac, int(time.time()) & 0xFFFFFFFF, body)


def parse_status(body: bytes):
    """Return a dict of decoded fields for a status body (56 bytes incl trailer
    on the HDi65S), else None."""
    if len(body) < 54 or body[1] != 0x2d:
        return None
    return {
        "temp_f": body[17],
        "rh": body[18],
        "gpp": body[23],
        "setpoint": body[31],
        # 0 = off, 1 = on, 2 = on but in the post-power-loss start delay
        # (~5.5 min observed). 2 is still ON -- treating it as off made HA
        # misreport the unit for minutes after every power cycle.
        "enabled": body[11] != 0x00,
        "starting": body[11] == 0x02,
        "compressor": body[14] == 0x01,  # actively drying (~10s start delay)
        "fan": body[15] == 0x01,         # incl ~1-2min run-down after off
        "pump": body[12] == 0x01,
        "coil_c": body[30],              # evaporator coil temperature, deg C
        "locate": body[38] == 0x01,      # locate beacon (auto-clears ~65s)
        "continuous": body[52] == 0x02,  # mode flag: 2 = continuous ("CO")
        "hours": (body[36] << 8) | body[37],  # total working hours
        "event": body[3],
    }


def diff_bytes(prev: bytes, cur: bytes) -> str:
    """Compact per-offset diff between two status bodies, e.g. '[15] 0x00->0x01'.
    Offsets are body offsets, matching the PROTOCOL.md tables."""
    parts = [f"[{i}] 0x{a:02x}->0x{b:02x}"
             for i, (a, b) in enumerate(zip(prev, cur)) if a != b]
    if len(prev) != len(cur):
        parts.append(f"len {len(prev)}->{len(cur)}")
    return " ".join(parts)


def decode(frame: bytes, direction: str) -> str:
    if len(frame) < 14 or frame[0:2] != MAGIC:
        return f"[{direction}] non-frame {frame.hex()}"
    mac = ":".join(f"{x:02x}" for x in frame[8:14])
    tok = frame[14:18].hex() if len(frame) >= 18 else ""
    body = frame[18:] if len(frame) >= 18 else b""
    pretty = ""
    st = parse_status(body)
    if st:
        pretty = (f"  STATUS  rh={st['rh']}% sp={st['setpoint']}% "
                  f"temp={st['temp_f']}F gpp={st['gpp']} "
                  f"en={'starting' if st['starting'] else st['enabled']} "
                  f"comp={st['compressor']} "
                  f"fan={st['fan']} pump={st['pump']} "
                  f"evt=0x{st['event']:02x}")
    elif len(body) >= 11 and body[0] == 0x00 and body[2] == 0x09:
        op = CMD.get(body[3], f"op0x{body[3]:02x}")
        val = body[8]
        if body[3] == 0x23:
            pretty = f"  CMD setpoint -> {val}%"
        elif body[3] in (0x21, 0x22, 0x27):
            pretty = f"  CMD {op} -> {'on' if val else 'off'}"
        elif body[3] == 0x01:
            pretty = "  keepalive"
        else:
            pretty = f"  CMD {op} val={val}"
    tail = ""
    if body and body[-1] == 0x16 and len(body) >= 2:
        tail = f"  cksum=0x{body[-2]:02x}"
    return (f"[{direction}] len={len(frame):3} mac={mac} tok={tok}{pretty}{tail}")


def peer_allowed(writer) -> bool:
    allowed = os.environ.get("ALLOWED_SOURCE_IP", "").strip()
    if not allowed:
        return True
    peer = writer.get_extra_info("peername")
    return bool(peer) and peer[0] == allowed


# ---------------------------------------------------------------------------
# Relay mode -- transparent MITM to the real cloud
# ---------------------------------------------------------------------------
async def pump(reader, writer, direction):
    buf = bytearray()
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            buf.extend(data)
            for fr in extract_frames(buf):
                emit(f"{ts()} {decode(fr, direction)}")
            writer.write(data)
            await writer.drain()
    except ConnectionResetError:
        pass
    finally:
        writer.close()


async def handle_relay(dev_reader, dev_writer):
    peer = dev_writer.get_extra_info("peername")
    if not peer_allowed(dev_writer):
        emit(f"{ts()} !! rejected connection from {peer} (ALLOWED_SOURCE_IP)")
        dev_writer.close()
        return
    emit(f"{ts()} === device connected from {peer}; dialing real cloud ===")
    try:
        cloud_reader, cloud_writer = await asyncio.open_connection(
            REAL_CLOUD_HOST, REAL_CLOUD_PORT)
    except OSError as e:
        emit(f"{ts()} !! could not reach real cloud: {e}")
        dev_writer.close()
        return
    # If the cloud name is DNS-redirected to us, we just dialed ourselves.
    cloud_peer = cloud_writer.get_extra_info("peername")
    if cloud_peer and cloud_peer[0] == dev_writer.get_extra_info(
            "sockname", (None,))[0]:
        emit(f"{ts()} !! '{REAL_CLOUD_HOST}' resolves to this host "
             f"({cloud_peer[0]}) -- a DNS redirect is in place. Relay mode "
             f"needs the real cloud: pass --cloud <public IP>.")
        cloud_writer.close()
        dev_writer.close()
        return
    await asyncio.gather(
        pump(dev_reader, cloud_writer, "DEV->CLD"),
        pump(cloud_reader, dev_writer, "CLD->DEV"),
    )
    emit(f"{ts()} === session closed ===")


# ---------------------------------------------------------------------------
# Local mode -- answer the device + bridge to Home Assistant over MQTT
# ---------------------------------------------------------------------------
class Bridge:
    """Owns the MQTT client and the current device connection. MQTT callbacks
    run on paho's thread; device writes are marshalled back to the asyncio loop."""

    def __init__(self, loop):
        self.loop = loop
        self.writer = None          # asyncio StreamWriter to the device
        self.mac = None             # device MAC bytes (learned from first frame)
        self.dev_id = None          # e.g. alorair_a02442abcdef
        self.enabled = True         # our tracked on/off (enable) state
        self.last = {}              # last decoded status
        self.last_body = None       # last raw status body, for byte-diff logging
        self.normal_target = 50     # last non-continuous setpoint, for mode=normal
        self.mqtt = None
        self.prefix = os.environ.get("MQTT_PREFIX", "alorair")
        self.disc = os.environ.get("DISCOVERY_PREFIX", "homeassistant")

    # ---- topics -------------------------------------------------------------
    def base(self):
        return f"{self.prefix}/{self.dev_id}"

    def t(self, leaf):
        return f"{self.base()}/{leaf}"

    # ---- MQTT setup ---------------------------------------------------------
    def connect_mqtt(self):
        import paho.mqtt.client as mqtt
        host = os.environ.get("MQTT_HOST", "127.0.0.1")
        port = int(os.environ.get("MQTT_PORT", "1883"))
        user = os.environ.get("MQTT_USER")
        pw = os.environ.get("MQTT_PASS")
        cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                          client_id="alorair-bridge")
        if user:
            cli.username_pw_set(user, pw)
        cli.on_connect = self._on_connect
        cli.on_message = self._on_message
        # LWT: entities also watch this topic (availability_mode=all), so a
        # dead bridge marks them unavailable instead of freezing stale state.
        cli.will_set(f"{self.prefix}/bridge/available", "offline", retain=True)
        cli.reconnect_delay_set(min_delay=1, max_delay=30)
        emit(f"{ts()} MQTT connecting to {host}:{port} ...")
        # connect_async + loop_start keeps retrying until the broker is up
        # (a blocking connect() would give up for good if Mosquitto boots late).
        cli.connect_async(host, port, keepalive=30)
        cli.loop_start()
        self.mqtt = cli

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        emit(f"{ts()} MQTT connected (rc={reason_code})")
        client.publish(f"{self.prefix}/bridge/available", "online", retain=True)
        # (re)publish discovery + subscribe once we know the device id
        if self.dev_id:
            self.publish_discovery()
            self.subscribe()
            if self.writer is not None:
                client.publish(self.t("available"), "online", retain=True)

    def subscribe(self):
        self.mqtt.subscribe(self.t("set"))
        self.mqtt.subscribe(self.t("target/set"))
        self.mqtt.subscribe(self.t("purge/set"))
        self.mqtt.subscribe(self.t("locate/set"))
        self.mqtt.subscribe(self.t("mode/set"))
        self.mqtt.subscribe(self.t("raw/set"))

    def publish_discovery(self):
        dev = {
            "identifiers": [self.dev_id],
            "manufacturer": "AlorAir / BaseAire",
            "model": "Sentinel HDi65S (WiFi)",
            "name": "Dehumidifier",
            "sw_version": VERSION,
            "configuration_url": "https://github.com/sethrobin/alorair-local",
        }
        # main entity: name None -> takes the device name in the UI
        self._pub_cfg("humidifier", "humidifier", {
            "name": None,
            "unique_id": f"{self.dev_id}_humidifier",
            "device_class": "dehumidifier",
            "state_topic": self.t("state"),
            "command_topic": self.t("set"),
            "payload_on": "ON", "payload_off": "OFF",
            "action_topic": self.t("action"),
            "current_humidity_topic": self.t("humidity"),
            "target_humidity_command_topic": self.t("target/set"),
            "target_humidity_state_topic": self.t("target"),
            "min_humidity": 35, "max_humidity": 90,
            "mode_command_topic": self.t("mode/set"),
            "mode_state_topic": self.t("mode"),
            "modes": ["normal", "continuous"],
            "device": dev})
        # measurements (state_class enables long-term statistics/graphs)
        self._pub_cfg("sensor", "humidity", {
            "name": "Humidity",
            "unique_id": f"{self.dev_id}_rh",
            "state_topic": self.t("humidity"),
            "unit_of_measurement": "%",
            "device_class": "humidity",
            "state_class": "measurement",
            "suggested_display_precision": 0,
            "device": dev})
        self._pub_cfg("sensor", "temperature", {
            "name": "Temperature",
            "unique_id": f"{self.dev_id}_temp",
            "state_topic": self.t("temperature"),
            "unit_of_measurement": "°F",
            "device_class": "temperature",
            "state_class": "measurement",
            "suggested_display_precision": 0,
            "device": dev})
        self._pub_cfg("sensor", "gpp", {
            "name": "Grains per pound",
            "unique_id": f"{self.dev_id}_gpp",
            "state_topic": self.t("gpp"),
            "unit_of_measurement": "gr/lb",
            "icon": "mdi:water-opacity",
            "state_class": "measurement",
            "device": dev})
        self._pub_cfg("sensor", "coil_temp", {
            "name": "Coil temperature",
            "unique_id": f"{self.dev_id}_coil_temp",
            "state_topic": self.t("coil"),
            "unit_of_measurement": "°C",
            "device_class": "temperature",
            "state_class": "measurement",
            "suggested_display_precision": 0,
            "entity_category": "diagnostic",
            "device": dev})
        self._pub_cfg("sensor", "hours", {
            "name": "Working hours",
            "unique_id": f"{self.dev_id}_hours",
            "state_topic": self.t("hours"),
            "unit_of_measurement": "h",
            "device_class": "duration",
            "state_class": "total_increasing",
            "entity_category": "diagnostic",
            "device": dev})
        # machinery state
        self._pub_cfg("binary_sensor", "pump", {
            "name": "Pump",
            "unique_id": f"{self.dev_id}_pump",
            "state_topic": self.t("pump"),
            "payload_on": "ON", "payload_off": "OFF",
            "device_class": "running",
            "device": dev})
        self._pub_cfg("binary_sensor", "compressor", {
            "name": "Compressor",
            "unique_id": f"{self.dev_id}_compressor",
            "state_topic": self.t("compressor"),
            "payload_on": "ON", "payload_off": "OFF",
            "device_class": "running",
            "entity_category": "diagnostic",
            "device": dev})
        self._pub_cfg("binary_sensor", "fan", {
            "name": "Fan",
            "unique_id": f"{self.dev_id}_fan",
            "state_topic": self.t("fan"),
            "payload_on": "ON", "payload_off": "OFF",
            "device_class": "running",
            "entity_category": "diagnostic",
            "device": dev})
        # controls -- purge is a one-shot ~30s pump cycle that self-
        # terminates (the Pump binary_sensor shows it running), so a button;
        # locate is a real toggle with live state from status offset 38, and
        # the switch flips itself off when the unit's ~65s auto-clear fires
        self._pub_cfg("button", "purge", {
            "name": "Purge",
            "unique_id": f"{self.dev_id}_purge",
            "command_topic": self.t("purge/set"),
            "payload_press": "ON",
            "icon": "mdi:water-pump",
            "device": dev})
        self._pub_cfg("switch", "locate", {
            "name": "Locate",
            "unique_id": f"{self.dev_id}_locate",
            "command_topic": self.t("locate/set"),
            "state_topic": self.t("locate"),
            "payload_on": "ON", "payload_off": "OFF",
            "icon": "mdi:map-marker-radius",
            "device": dev})
        emit(f"{ts()} published HA discovery for {self.dev_id}")

    def _pub_cfg(self, comp, obj, cfg):
        # entities are available only when the device is connected AND the
        # bridge process is alive (the LWT covers hard bridge death)
        cfg["availability"] = [{"topic": self.t("available")},
                               {"topic": f"{self.prefix}/bridge/available"}]
        cfg["availability_mode"] = "all"
        topic = f"{self.disc}/{comp}/{self.dev_id}/{obj}/config"
        self.mqtt.publish(topic, json.dumps(cfg), retain=True)

    # ---- device -> MQTT -----------------------------------------------------
    def on_status(self, st):
        self.last = st
        # status offset 11 is authoritative for on/off -- it follows the
        # front panel too, so HA state can no longer drift
        self.enabled = st["enabled"]
        if not st["continuous"] and 35 <= st["setpoint"] <= 90:
            self.normal_target = st["setpoint"]
        m = self.mqtt
        if not m:
            return
        m.publish(self.t("humidity"), st["rh"], retain=True)
        m.publish(self.t("target"), st["setpoint"], retain=True)
        m.publish(self.t("temperature"), st["temp_f"], retain=True)
        m.publish(self.t("gpp"), st["gpp"], retain=True)
        m.publish(self.t("pump"), "ON" if st["pump"] else "OFF", retain=True)
        m.publish(self.t("compressor"), "ON" if st["compressor"] else "OFF", retain=True)
        m.publish(self.t("fan"), "ON" if st["fan"] else "OFF", retain=True)
        m.publish(self.t("coil"), st["coil_c"], retain=True)
        m.publish(self.t("hours"), st["hours"], retain=True)
        m.publish(self.t("mode"), "continuous" if st["continuous"] else "normal",
                  retain=True)
        m.publish(self.t("locate"), "ON" if st["locate"] else "OFF", retain=True)
        m.publish(self.t("state"), "ON" if st["enabled"] else "OFF", retain=True)
        if not st["enabled"]:
            action = "off"
        elif st["compressor"]:
            action = "drying"
        else:
            action = "idle"
        m.publish(self.t("action"), action, retain=True)

    # ---- MQTT -> device -----------------------------------------------------
    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode(errors="ignore").strip()
        emit(f"{ts()} MQTT cmd {msg.topic} = {payload}")
        if self.mac is None or self.writer is None:
            emit(f"{ts()} !! device not connected; dropping command")
            return
        frame = None
        if msg.topic == self.t("set"):
            on = payload.upper() == "ON"
            self.enabled = on
            frame = build_command(self.mac, 0x21, 1 if on else 0)
        elif msg.topic == self.t("target/set"):
            try:
                v = max(35, min(90, int(float(payload))))
            except ValueError:
                return
            frame = build_command(self.mac, 0x23, v)
        elif msg.topic == self.t("mode/set"):
            if payload == "continuous":
                # any setpoint <35 engages continuous; device clamps to 34
                frame = build_command(self.mac, 0x23, 34)
            elif payload == "normal":
                frame = build_command(self.mac, 0x23, self.normal_target)
        elif msg.topic == self.t("purge/set"):
            frame = build_command(self.mac, 0x22, 1 if payload.upper() == "ON" else 0)
        elif msg.topic == self.t("locate/set"):
            # locate self-terminates on the unit after ~65s; ON is all HA sends
            frame = build_command(self.mac, 0x27, 1 if payload.upper() == "ON" else 0)
        elif msg.topic == self.t("raw/set"):
            try:
                frame = bytes.fromhex("".join(payload.split()))
            except ValueError:
                emit(f"{ts()} !! raw/set payload is not hex; ignored")
                return
        if frame is not None:
            self.loop.call_soon_threadsafe(self._send, frame)

    def _send(self, frame: bytes):
        if self.writer is None:
            emit(f"{ts()} !! device not connected; dropping frame")
            return
        try:
            self.writer.write(frame)
            emit(f"{ts()} -> device {decode(frame, 'US->DEV')}")
        except Exception as e:
            emit(f"{ts()} !! send failed: {e}")

    def set_device(self, writer, mac):
        self.writer = writer
        first_time = self.mac is None
        self.mac = mac
        self.dev_id = "alorair_" + mac.hex()
        if self.mqtt:
            if first_time:
                self.publish_discovery()
                self.subscribe()
            # every (re)connect: a prior session's retained offline must not stick
            self.mqtt.publish(self.t("available"), "online", retain=True)


async def handle_local(bridge, dev_reader, dev_writer):
    peer = dev_writer.get_extra_info("peername")
    if not peer_allowed(dev_writer):
        emit(f"{ts()} !! rejected connection from {peer} (ALLOWED_SOURCE_IP)")
        dev_writer.close()
        return
    emit(f"{ts()} === device connected from {peer} (LOCAL) ===")
    buf = bytearray()
    mac_set = False
    # periodic keepalive-ack so the device doesn't drop us
    async def keepalive():
        while True:
            await asyncio.sleep(30)
            if bridge.mac and bridge.writer is dev_writer:
                try:
                    dev_writer.write(build_command(bridge.mac, 0x01, 0))
                    await dev_writer.drain()
                except Exception:
                    return
    ka = asyncio.ensure_future(keepalive())
    try:
        while True:
            data = await dev_reader.read(4096)
            if not data:
                break
            buf.extend(data)
            for fr in extract_frames(buf):
                if not mac_set and len(fr) >= 14:
                    bridge.set_device(dev_writer, fr[8:14])
                    mac_set = True
                emit(f"{ts()} {decode(fr, 'DEV->US')}")
                body = fr[18:]
                st = parse_status(body)
                if st:
                    # raw-byte visibility: full hex once, then per-offset
                    # diffs -- how undecoded offsets get identified
                    if bridge.last_body is None:
                        emit(f"{ts()} ~~ status hex: {body.hex()}")
                    elif body != bridge.last_body:
                        emit(f"{ts()} ~~ status diff: "
                             f"{diff_bytes(bridge.last_body, body)}")
                    bridge.last_body = bytes(body)
                    bridge.on_status(st)
                # ack the device's own keepalive immediately
                elif len(body) >= 4 and body[2] == 0x09 and body[3] == 0x01:
                    dev_writer.write(build_command(bridge.mac, 0x01, 0))
                    await dev_writer.drain()
    except ConnectionResetError:
        pass
    finally:
        ka.cancel()
        dev_writer.close()
        # only the session that owns the current writer may mark the device
        # offline -- a stray connection (port scan) or an already-superseded
        # session must not clobber the live one's availability
        if mac_set and bridge.writer is dev_writer:
            bridge.writer = None
            if bridge.mqtt and bridge.dev_id:
                bridge.mqtt.publish(bridge.t("available"), "offline", retain=True)
        emit(f"{ts()} === session closed ===")


async def main():
    global REAL_CLOUD_HOST, _LOGF, _LOG_PATH
    ap = argparse.ArgumentParser(description="AlorAir fake-cloud relay/bridge")
    ap.add_argument("--mode", choices=["relay", "local"], default="relay")
    ap.add_argument("--port", type=int, default=LISTEN_PORT)
    ap.add_argument("--cloud", default=REAL_CLOUD_HOST)
    ap.add_argument("--logfile", default=None)
    args = ap.parse_args()
    REAL_CLOUD_HOST = args.cloud
    if args.logfile:
        _LOG_PATH = args.logfile
        _LOGF = open(args.logfile, "a", buffering=1)

    if args.mode == "relay":
        handler = handle_relay
    else:
        bridge = Bridge(asyncio.get_running_loop())
        try:
            bridge.connect_mqtt()
        except Exception as e:
            emit(f"{ts()} !! MQTT setup failed: {e} (continuing without MQTT)")

        async def handler(r, w):
            await handle_local(bridge, r, w)

    server = await asyncio.start_server(handler, "0.0.0.0", args.port)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    emit(f"{ts()} listening on {addrs}  mode={args.mode}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
