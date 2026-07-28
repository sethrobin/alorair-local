"""Frame-layer invariants (checksum, build_command, parse_status, framing).

Run from the repo root:  python3 -m unittest discover -s tests

The build_command test pins the builder to a captured cloud frame (the
PROTOCOL.md worked example). If it fails, the wire format changed -- do not
"fix" the expected bytes without a new capture proving them.

MAC is a placeholder: the real OUI (a0:24:42, the WiFi module vendor) with an
anonymized device portion. Frame checksums below are recomputed for it, so
they stay internally valid while identifying no specific unit.
"""
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "alorair_bridge"))
import alorair_cloud as ac

MAC = bytes.fromhex("a02442abcdef")
# Captured cloud frame shape: setpoint=45, token 0x6a64f3d1 (PROTOCOL.md worked example)
SETPOINT_45 = bytes.fromhex(
    "0d0e000000000000a02442abcdef6a64f3d100010923000000002d057416")
# Captured device keepalive shape (class byte 0x02), 2026-07 deployment log
KEEPALIVE = bytes.fromhex(
    "0d0e000000000000a02442abcdef000000000000090100000000029216")


def dev_frame(body: bytes) -> bytes:
    """Device->cloud style frame: token 0, standard trailer."""
    f = ac.MAGIC + b"\x00" * 6 + MAC + b"\x00" * 4 + body
    return f + bytes([ac.checksum(f), 0x16])


def synthetic_status() -> bytes:
    """A status frame with the confirmed field offsets populated."""
    body = bytearray(54)
    body[1] = 0x2D    # status marker
    body[3] = 0x1C    # event: baseline
    body[11] = 1      # enabled (power state)
    body[12] = 1      # pump
    body[14] = 1      # compressor
    body[15] = 1      # fan
    body[16] = 23     # temp C
    body[17] = 73     # temp F
    body[18] = 55     # RH %
    body[23] = 62     # grains/lb
    body[30] = 18     # coil temp C
    body[31] = 50     # setpoint %
    body[37] = 55     # working hours (low byte of BE16 at 36-37)
    body[53] = 0x04   # class byte (read directly from a live status, 2026-07)
    return dev_frame(bytes(body))


class BuildCommand(unittest.TestCase):
    def test_reproduces_captured_setpoint_frame(self):
        with mock.patch.object(ac.time, "time", return_value=0x6A64F3D1):
            self.assertEqual(ac.build_command(MAC, 0x23, 45), SETPOINT_45)

    def test_checksum_of_captured_command(self):
        # class byte 0x05: excluded from the sum
        self.assertEqual(ac.checksum(SETPOINT_45[:-2]), 0x74)

    def test_checksum_of_captured_keepalive(self):
        # class byte 0x02: same rule, different class -- this is the capture
        # that killed the per-type "K constant" model
        self.assertEqual(ac.checksum(KEEPALIVE[:-2]), 0x92)


class ParseStatus(unittest.TestCase):
    def test_decodes_fields(self):
        st = ac.parse_status(synthetic_status()[18:])
        self.assertEqual(st, {"temp_f": 73, "rh": 55, "gpp": 62,
                              "setpoint": 50, "enabled": True,
                              "compressor": True, "fan": True,
                              "pump": True, "coil_c": 18,
                              "locate": False, "continuous": False,
                              "starting": False, "hours": 55,
                              "event": 0x1C})

    def test_start_delay_state_is_on(self):
        # offset 11 = 2 is "on, waiting out the post-power-loss compressor
        # delay" -- reporting it as off made HA lie for ~5 min after a
        # power cycle (observed 2026-07-27)
        body = bytearray(synthetic_status()[18:])
        body[11] = 2
        st = ac.parse_status(bytes(body))
        self.assertTrue(st["enabled"])
        self.assertTrue(st["starting"])

    def test_off_state(self):
        body = bytearray(synthetic_status()[18:])
        body[11] = 0
        st = ac.parse_status(bytes(body))
        self.assertFalse(st["enabled"])
        self.assertFalse(st["starting"])

    def test_rejects_command_body(self):
        self.assertIsNone(ac.parse_status(SETPOINT_45[18:]))


class ExtractFrames(unittest.TestCase):
    def test_single_frame_emitted_immediately(self):
        # regression: the old magic-split held every frame until the NEXT
        # frame's magic arrived, making all processing one frame (~10s) late
        buf = bytearray(synthetic_status())
        self.assertEqual(ac.extract_frames(buf), [synthetic_status()])
        self.assertEqual(buf, bytearray())

    def test_byte_dribble(self):
        frame = synthetic_status()
        buf = bytearray()
        got = []
        for b in frame:
            buf.append(b)
            got += ac.extract_frames(buf)
        self.assertEqual(got, [frame])

    def test_two_frames_in_one_chunk(self):
        f1 = synthetic_status()
        f2 = dev_frame(bytes([0x00, 0x00, 0x09, 0x01, 0x02]))
        buf = bytearray(f1 + f2)
        self.assertEqual(ac.extract_frames(buf), [f1, f2])

    def test_garbage_prefix_dropped(self):
        buf = bytearray(b"\x01\x02\x03" + synthetic_status())
        self.assertEqual(ac.extract_frames(buf), [synthetic_status()])

    def test_inner_magic_not_a_boundary(self):
        # 0d0e inside the token must not be treated as a frame start
        f = ac.build_frame(MAC, 0x0D0E0D0E,
                           bytes([0x00, 0x01, 0x09, 0x23, 0, 0, 0, 0, 45, 0x05]))
        buf = bytearray(f)
        self.assertEqual(ac.extract_frames(buf), [f])

    def test_inner_0x16_without_checksum_skipped(self):
        # a 0x16 payload byte whose preceding byte isn't a valid checksum
        # must not terminate the frame early
        f = dev_frame(bytes([0x00, 0x00, 0x09, 0x01, 0x16, 0x16, 0x02]))
        buf = bytearray(f)
        self.assertEqual(ac.extract_frames(buf), [f])

    def test_unvalidatable_data_resyncs(self):
        junk = ac.MAGIC + bytes(600)          # magic that never validates
        buf = bytearray(junk + synthetic_status())
        self.assertEqual(ac.extract_frames(buf), [synthetic_status()])

    def test_captured_keepalive_validates_alone(self):
        # the real keepalive validates under the class-byte checksum rule,
        # so it is emitted immediately -- no waiting for the next frame
        buf = bytearray(KEEPALIVE)
        self.assertEqual(ac.extract_frames(buf), [KEEPALIVE])
        self.assertEqual(buf, bytearray())

    def test_trailerless_frame_flushed_by_next_magic(self):
        # safety net: a frame type with NO recognizable trailer must be
        # flushed as soon as the next frame's magic arrives -- not stall the
        # stream until the MAX_FRAME resync guard fires (the 1.1.0 bug)
        ka = (ac.MAGIC + b"\x00" * 6 + MAC + b"\x00" * 4
              + bytes([0x00, 0x00, 0x09, 0x01, 0, 0, 0, 0, 0, 0x02]))
        buf = bytearray(ka)
        self.assertEqual(ac.extract_frames(buf), [])   # alone: wait for more
        buf.extend(synthetic_status())
        self.assertEqual(ac.extract_frames(buf), [ka, synthetic_status()])
        self.assertEqual(buf, bytearray())

    def test_trailerless_frame_then_two_statuses(self):
        ka = (ac.MAGIC + b"\x00" * 6 + MAC + b"\x00" * 4
              + bytes([0x00, 0x00, 0x09, 0x01, 0, 0, 0, 0, 0, 0x02]))
        buf = bytearray(ka + synthetic_status() + synthetic_status())
        self.assertEqual(ac.extract_frames(buf),
                         [ka, synthetic_status(), synthetic_status()])

    def test_split_magic_across_chunks_kept(self):
        frame = synthetic_status()
        buf = bytearray(frame[:1])            # just the 0x0d
        self.assertEqual(ac.extract_frames(buf), [])
        buf.extend(frame[1:])
        self.assertEqual(ac.extract_frames(buf), [frame])


if __name__ == "__main__":
    unittest.main()
