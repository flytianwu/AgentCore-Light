from types import SimpleNamespace
import unittest
from unittest.mock import patch

from light_daemon import Device


class HandshakeTests(unittest.TestCase):
    def exercise(self, replies):
        device = Device('test')
        now = 0
        self.pings = []
        pending = bytearray()

        class Port:
            def write(port, data):
                self.assertEqual(data, b'PING\n')
                pending.extend(replies[len(self.pings)])
                self.pings.append(now)
                return len(data)

            def read(port, size):
                nonlocal now
                now += 0.1
                chunk = bytes(pending[:size])
                del pending[:size]
                return chunk

        device.connection = Port()
        with patch('light_daemon.time.monotonic', side_effect=lambda: now):
            device.handshake()

    def test_delayed_fragmented_pong_on_same_port(self):
        self.exercise([b'ESP-ROM:esp32c3\nPONG:AGENT', b'CORE-LIGHT-V3\r\n'])
        self.assertEqual(len(self.pings), 2)

    def test_failure_categories_and_bounded_attempts(self):
        for reply, reason in [(b'', 'no bytes'), (b'ESP-ROM:esp32c3\n', 'boot output'),
                              (b'PONG:OTHER\n', 'unexpected reply'), (b'PON', 'partial reply')]:
            with self.subTest(reason=reason), self.assertRaisesRegex(TimeoutError, reason):
                self.exercise([reply] * 3)
            self.assertEqual(len(self.pings), 3)

    def test_recovery_requires_consecutive_silent_connections_and_cooldown(self):
        from light_daemon import SilentHandshake
        device = Device('only-this-serial')
        port = SimpleNamespace(vid=0x303a, pid=0x1001, serial_number=device.serial_number, device='test')
        now = 1000
        with patch('light_daemon.list_ports.comports', return_value=[port]), \
             patch('light_daemon.serial.Serial'), patch('light_daemon.time.sleep'), \
             patch('light_daemon.time.monotonic', side_effect=lambda: now), \
             patch.object(device, 'handshake') as handshake, \
             patch('light_daemon.subprocess.run') as reset:
            def fail(error):
                handshake.side_effect = error
                with self.assertRaises((TimeoutError, ConnectionError)):
                    device.connect()
                self.assertIsNone(device.connection)

            for error in (SilentHandshake(), SilentHandshake(), TimeoutError('boot output'),
                          SilentHandshake(), SilentHandshake(), SilentHandshake()):
                fail(error)
                reset.assert_not_called()
            fail(SilentHandshake())
            self.assertEqual(reset.call_count, 1)
            self.assertEqual(reset.call_args.args[0][-1], 'only-this-serial')
            for _ in range(5):
                now += 5
                fail(SilentHandshake())
            self.assertEqual(reset.call_count, 1)
            now += 600
            fail(SilentHandshake())
            self.assertEqual(reset.call_count, 2)
