"""Exercise the real pyserial transport against a local pseudo-terminal device."""
import os
import json
import pty
import select
import tempfile
import threading
import time
import tty
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import serial

from light_daemon import Device, Service


class TransportTests(unittest.TestCase):
    def test_acknowledged_commands_complete_when_os_drain_stalls(self):
        # Simulate the observed macOS tcdrain stall at the OS boundary, while
        # retaining real pyserial I/O, firmware replies and reconnect handling.
        release_drain = threading.Event()
        errors = []

        def exercise():
            try:
                with patch('serial.serialposix.termios.tcdrain',
                           side_effect=lambda fd: release_drain.wait()):
                    self.exercise_transport('MAC2')
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=exercise, daemon=True)
        worker.start()
        try:
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive(), 'Acknowledged commands stalled waiting for OS drain')
            if errors:
                raise errors[0]
        finally:
            release_drain.set()
            worker.join(timeout=3)

    def test_missing_ack_disconnects_within_deadline_and_status_remains_available(self):
        master, slave = pty.openpty()
        tty.setraw(slave)
        device = Device('silent-device')
        try:
            device.connection = serial.Serial(os.ttyname(slave), 115200, timeout=0.1, write_timeout=1)
            # Isolate ACK timeout here; the separate regression above exercises
            # a stalled OS drain without allowing the test process to hang.
            with tempfile.TemporaryDirectory() as directory, \
                 patch('serial.serialposix.termios.tcdrain', return_value=None):
                service = Service(device, Path(directory) / 'settings.json')
                started = time.monotonic()
                service.tick()
                self.assertLess(time.monotonic() - started, 2.5)
                status = json.loads(service.request('HOST_STATUS'))
                self.assertFalse(status['connected'])
                self.assertIn('did not acknowledge BRIGHTNESS', status['error'])
                self.assertIsNone(device.connection)
                # The reconnect delay leaves the control plane responsive.
                started = time.monotonic()
                service.tick()
                self.assertLess(time.monotonic() - started, 0.5)
        finally:
            device.close()
            os.close(master)
            os.close(slave)

    def test_full_output_queue_times_out_without_waiting_for_drain(self):
        master, slave = pty.openpty()
        tty.setraw(slave)
        device = Device('blocked-device')
        try:
            device.connection = serial.Serial(os.ttyname(slave), 115200, timeout=0.1, write_timeout=0.2)
            # Nothing consumes the PTY's output, so a large write fills its queue.
            started = time.monotonic()
            with self.assertRaises(serial.SerialTimeoutException):
                device.exchange('X' * 1_000_000, lambda reply: False)
            self.assertLess(time.monotonic() - started, 1.5)
        finally:
            device.close()
            os.close(master)
            os.close(slave)

    def test_identity_ack_status_and_reconnect_replay(self):
        for version in ('MAC1', 'MAC2'):
            with self.subTest(version=version):
                self.exercise_transport(version)

    def exercise_transport(self, version):
        master, slave = pty.openpty()
        tty.setraw(slave)
        stop = threading.Event()
        hardware = {"state": "IDLE", "token": 100, "brightness": 16}

        frame = ["THREADS:00000000,0,0,0"]

        def firmware():
            buffer = b""
            while not stop.is_set():
                if not select.select([master], [], [], 0.1)[0]:
                    continue
                try:
                    buffer += os.read(master, 4096)
                except OSError:
                    continue
                while b"\n" in buffer:
                    raw, buffer = buffer.split(b"\n", 1)
                    command = raw.decode().strip()
                    if command == "PING":
                        reply = "PONG:AGENTCORE-LIGHT-V3"
                    elif command == "STATUS":
                        reply = f"STATUS:{hardware['state']},TOKEN:{hardware['token']},BRIGHTNESS:{hardware['brightness']},FW:{version},{frame[0]}"
                    elif command.startswith("THREADS:"):
                        frame[0] = command
                        reply = command
                    elif command.startswith("TOKEN:"):
                        hardware['token'] = int(command.split(':')[1])
                        reply = f"Token percent: {hardware['token']}"
                    elif command.startswith("BRIGHTNESS:"):
                        hardware['brightness'] = int(command.split(':')[1])
                        reply = command
                    elif command.startswith("STALE:"):
                        reply = command
                    else:
                        hardware['state'] = command
                        reply = 'State changed to: ' + command
                    os.write(master, (reply + '\n').encode())

        thread = threading.Thread(target=firmware, daemon=True)
        thread.start()
        device = Device('test-device')
        port = SimpleNamespace(vid=0x303A, pid=0x1001, serial_number='test-device', device=os.ttyname(slave))
        try:
            with tempfile.TemporaryDirectory() as directory, \
                 patch('light_daemon.list_ports.comports', return_value=[port]):
                service = Service(device, Path(directory) / 'settings.json')
                service.tick()
                self.assertTrue(device.extended)
                service.request('TOKEN:89')
                service.request('BRIGHTNESS:10')
                service.request('THINKING')
                self.assertEqual(device.status(), {'state': 'THINKING', 'token': 89, 'brightness': 10})
                service.request('RECONNECT')
                hardware.update(state='IDLE', token=100, brightness=16)
                service.tick()
                self.assertEqual(device.status(), {'state': 'THINKING', 'token': 89, 'brightness': 10})
                if version == 'MAC2':
                    service.request('{"session_id":"a","state":"THINKING","event":"UserPromptSubmit"}')
                    service.request('{"session_id":"b","state":"NEED_CONFIRM","event":"PermissionRequest"}')
                    service.tick(force=True)
                    device.status()
                    self.assertEqual(device.thread_frame, 'THREADS:16000000,2,1,0')
                    service.request('RECONNECT')
                    frame[0] = 'THREADS:00000000,0,0,0'
                    service.tick()
                    device.status()
                    self.assertEqual(device.thread_frame, 'THREADS:16000000,2,1,0')
                service.request('OFF')
                self.assertEqual(device.status()['state'], 'OFF')
        finally:
            device.close()
            stop.set()
            thread.join(timeout=1)
            os.close(master)
            os.close(slave)


if __name__ == '__main__':
    unittest.main()
