"""USB protocol for the ScanSnap SV600 (Fujitsu 04c5:128e, Ricoh 05ca:03c7).

Replays the command sequence ScanSnap Home sends for a single scan (captured in
protocol/captures/scan1.pcap, commands #0410-#0591). The scanner does its own shading
correction; geometry/colour/brightness are fixed afterwards by sv600.correct.

Progress is reported through an object with step(text), spin(text), bar(done, total),
end(note='') and detail(text); see NullProgress.
"""
import collections
import errno
import os
import struct
import time

import usb.core
import usb.util

try:                                 # bundled libusb (AppImage / pip wheel); else the system one
    import libusb_package
except ImportError:
    libusb_package = None

UDEV_RULES = ('ATTRS{idVendor}=="04c5", ATTRS{idProduct}=="128e", MODE="0664", TAG+="uaccess"',
              'ATTRS{idVendor}=="05ca", ATTRS{idProduct}=="03c7", MODE="0664", TAG+="uaccess"')
UDEV_COMMAND = ("printf '%s\\n' " + ' '.join(f"'{r}'" for r in UDEV_RULES) +
                ' | sudo tee /etc/udev/rules.d/60-sv600.rules && sudo udevadm control --reload')
UDEV_HELP = ('No permission to use the scanner over USB. Run this once in a terminal, then unplug '
             'and replug the scanner:\n\n' + UDEV_COMMAND)


class NullProgress:
    """Progress sink that ignores everything."""
    def step(self, text): pass
    def spin(self, text): pass
    def bar(self, done, total): pass
    def end(self, note=''): pass
    def detail(self, text): pass


IDS = [(0x04C5, 0x128E), (0x05CA, 0x03C7)]
CMD_LEN, CMD_CODE, CDB_OFFSET = 0x1F, 0x43, 0x13
STATUS_LEN, STATUS_OFFSET = 0x0D, 9
GOOD, CHECK_CONDITION, BUSY = 0x00, 0x02, 0x08

# --- payloads copied from the capture --------------------------------------
MS_PAGE_2C = bytes.fromhex('000000002c06000000000000')
MS_PAGE_34 = bytes.fromhex('000000003406000000000000')
MS_PAGE_3C = bytes.fromhex('000000003c06000000c00000')
LUT_HEADER = bytes.fromhex('00001000010001000000')      # + 256-byte identity table

# SET WINDOW (#0424): 8-byte header + 64-byte window descriptor, verbatim
CAPTURED_WINDOW = bytes.fromhex(
    '00000000000000400000012c012c000000000000000000005710000045340000'
    '00050800008000000000000000000000c1800100000000000000000000c00000'
    '5710000045340000')
FULL_WIDTH_1200, FULL_LENGTH_1200 = 0x5710, 0x4534   # 472 x 375 mm in 1/1200 inch
READ_CHUNK = 0x07659C                                 # 29 lines at 300 dpi colour
DPI = 300


def build_window(dpi=DPI, width=FULL_WIDTH_1200, length=FULL_LENGTH_1200):
    d = bytearray(CAPTURED_WINDOW)
    struct.pack_into('>HH', d, 0x0A, dpi, dpi)             # X/Y resolution
    struct.pack_into('>II', d, 0x0E, 0, 0)                 # upper-left X/Y
    struct.pack_into('>II', d, 0x16, width, length)        # scan width/length
    struct.pack_into('>II', d, 0x3E, width, length)        # paper width/length
    return bytes(d)


assert build_window() == CAPTURED_WINDOW, 'window builder no longer matches capture'


class ScannerError(Exception):
    pass


def find_device():
    """Return the SV600's usb.core.Device, or None."""
    backend = libusb_package.get_libusb1_backend() if libusb_package else None
    for vid, pid in IDS:
        dev = usb.core.find(idVendor=vid, idProduct=pid, backend=backend)
        if dev:
            return dev
    return None


class SV600:
    def __init__(self, ui=None, timeouts=None):
        """ui: progress object (see NullProgress).
        timeouts: the [scanner] settings (usb_timeout_s, read_timeout_s, image_ready_timeout_s)."""
        self.ui = ui or NullProgress()
        t = timeouts or {'usb_timeout_s': 15.0, 'read_timeout_s': 30.0, 'image_ready_timeout_s': 60.0}
        self.usb_ms = int(t['usb_timeout_s'] * 1000)
        self.read_ms = int(t['read_timeout_s'] * 1000)
        self.ready_s = t['image_ready_timeout_s']
        self.trace = collections.deque(maxlen=60)     # last USB commands, for the error log
        self.open()
        self.settle()

    def open(self):
        self.dev = find_device()
        if self.dev is None:
            raise ScannerError('ScanSnap SV600 not found on USB (switched on? plugged in? '
                               'still attached to a virtual machine?)')
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
            usb.util.claim_interface(self.dev, 0)
        except usb.core.USBError as e:
            if e.errno == errno.EACCES:
                raise ScannerError(UDEV_HELP) from e
            if e.errno == errno.EBUSY:
                raise ScannerError('The scanner is in use by another program '
                                   '(another scan running, or a virtual machine?)') from e
            raise
        intf = self.dev.get_active_configuration()[(0, 0)]
        eps = {usb.util.endpoint_direction(e.bEndpointAddress): e for e in intf
               if usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK}
        self.ep_out, self.ep_in = eps[usb.util.ENDPOINT_OUT], eps[usb.util.ENDPOINT_IN]
        self.packet = self.ep_in.wMaxPacketSize or 512

    def close(self):
        try:
            usb.util.release_interface(self.dev, 0)
        except usb.core.USBError:
            pass
        usb.util.dispose_resources(self.dev)

    def drain(self):
        """Discard data left in the IN pipe by an interrupted transfer; returns bytes dropped."""
        n = 0
        for _ in range(64):
            try:
                n += len(self.ep_in.read(64 * 1024, 200))
            except usb.core.USBError:
                break
        return n

    def settle(self):
        """Make sure the scanner answers: clear stalls, drop leftovers, TEST UNIT READY.
        If it does not answer, reset the USB device (= unplug/replug) once and try again.
        Measured: a scanner that stopped answering every command recovered with a reset."""
        for attempt in (1, 2):
            for ep in (self.ep_in, self.ep_out):
                try:
                    self.dev.clear_halt(ep)
                except usb.core.USBError:
                    pass
            dropped = self.drain()
            if dropped:
                self.trace.append((time.time(), 'drain', 0, dropped, f'dropped {dropped} stale bytes'))
            try:
                self.cmd(bytes(6), timeout=3000)
                return
            except (usb.core.USBError, ScannerError) as e:
                self.trace.append((time.time(), 'settle', 0, 0, f'TEST UNIT READY failed: {e}'))
                if attempt == 2:
                    break
            self.ui.step('Scanner not responding: resetting its USB connection')
            try:
                self.dev.reset()
            except usb.core.USBError:
                pass
            self.close()
            for _ in range(40):                    # wait for it to come back (~2-5 s)
                time.sleep(0.25)
                if find_device() is not None:
                    break
            time.sleep(1.0)
            self.open()
        self.dump_log('scanner does not respond after a USB reset')
        raise ScannerError('The scanner does not respond, even after a USB reset. Switch it off and on '
                           '(power button or cable) and try again.')

    def dump_log(self, error):
        """Write the recent USB commands and the error to ~/.cache/sv600-linux/last_error.log."""
        try:
            from . import paths
            os.makedirs(paths.CACHE_DIR, exist_ok=True)
            with open(os.path.join(paths.CACHE_DIR, 'last_error.log'), 'w') as f:
                f.write(f'{time.strftime("%Y-%m-%d %H:%M:%S")}  error: {error!r}\n\n')
                for t, cdb, out, inn, res in self.trace:
                    f.write(f'{time.strftime("%H:%M:%S", time.localtime(t))}.{int(t % 1 * 1000):03d}  '
                            f'{cdb:32} out {out:6} in {inn:8}  {res}\n')
        except OSError:
            pass

    def cmd(self, cdb, out=b'', read_len=0, timeout=None, _retry=True):
        """Run one command. Returns (status, data_in)."""
        timeout = timeout or self.usb_ms
        pkt = bytearray(CMD_LEN)
        pkt[0] = CMD_CODE
        pkt[CDB_OFFSET:CDB_OFFSET + len(cdb)] = cdb
        self.ep_out.write(pkt, timeout)
        if out:
            self.ep_out.write(out, timeout)
        data = b''
        name = bytes(cdb).hex(' ')
        try:
            if read_len:
                # buffer rounded up to whole USB packets: a read into a smaller buffer than the
                # packet the scanner sends fails with "Overflow"
                want = -(-read_len // self.packet) * self.packet
                data = bytes(self.ep_in.read(want, timeout))[:read_len]
            st = bytes(self.ep_in.read(self.packet, timeout))
            if len(st) != STATUS_LEN or st[0] != 0x53:
                # leftovers from an interrupted transfer: the replies are now out of step with
                # the commands. Empty the pipe and send the command again (not for image reads,
                # which cannot be repeated: those end the scan with an error instead).
                self.trace.append((time.time(), name, len(out), len(st),
                                   f'out of step: got {len(st)} bytes instead of a status block'))
                self.drain()
                if _retry and cdb[0] != 0x28:
                    return self.cmd(cdb, out, read_len, timeout, _retry=False)
                raise ScannerError(f'USB replies out of step after cdb {name}')
        except usb.core.USBError as e:
            self.trace.append((time.time(), name, len(out), len(data), f'USB error: {e}'))
            raise
        status = st[STATUS_OFFSET]
        self.trace.append((time.time(), name, len(out), len(data), f'status {status:02x}'))
        self.ui.detail(f'cdb {bytes(cdb).hex(" "):30} out {len(out):4} in {len(data):7} '
                       f'status {status:02x}')
        return status, data

    def ok(self, cdb, out=b'', read_len=0, what=''):
        status, data = self.cmd(cdb, out, read_len)
        if status != GOOD:
            sense = self.sense()
            raise ScannerError(f'{what or bytes(cdb).hex(" ")} failed: status {status:02x}, '
                               f'sense {sense.hex(" ")}')
        return data

    def sense(self):
        _, data = self.cmd(bytes([0x03, 0, 0, 0, 0x12, 0]), read_len=0x12)
        return data

    def scan_button(self):
        """True while the Scan button on the scanner is held down.

        GET HW STATUS (C2, 32 bytes): byte 4 bit 0 is the button; byte 3 = 0x80 when idle.
        Measured: the bit is set for as long as the button is pressed (100 ms polling caught
        every press). Pressing it does nothing else on the scanner side.
        """
        status, data = self.cmd(bytes([0xC2, 0, 0, 0, 0, 0, 0, 0, 0x20, 0]), read_len=0x20)
        return status == GOOD and len(data) > 4 and bool(data[4] & 0x01)

    def diag(self, text, arg=b''):
        payload = text.encode().ljust(16) + arg
        self.ok(bytes([0x1D, 0, 0, 0, len(payload), 0]), out=payload, what=f'SEND DIAG {text!r}')

    def mode_select(self, page):
        self.ok(bytes([0x15, 0x10, 0, 0, len(page), 0]), out=page, what='MODE SELECT')

    # --- the scan --------------------------------------------------------
    def scan(self):
        ui = self.ui
        ui.step('Checking the scanner is ready')
        self.ok(bytes(6), what='TEST UNIT READY')

        ui.step('Preparing the scanner (scan mode, arming)')
        self.mode_select(MS_PAGE_2C)
        self.diag('SET SCAN', b'\x00')
        self.diag('SET SCAN MODE', b'\x00\x00\x00')
        self.diag('SET SCAN', b'\x01')
        try:
            self.mode_select(MS_PAGE_34)
            self.diag('CHANGE IRLED', b'\x01')
            self.diag('CHANGE IRLED', b'\x00')

            ui.step(f'Sending scan settings ({DPI} dpi colour, full frame)')
            lut = LUT_HEADER + bytes(range(256))
            self.ok(bytes([0x2A, 0, 0x83, 0, 0, 0]) + struct.pack('>I', len(lut))[1:] + b'\0',
                    out=lut, what='SEND LUT')
            self.mode_select(MS_PAGE_3C)
            win = build_window()
            self.ok(bytes([0x24, 0, 0, 0, 0, 0, 0, 0, len(win), 0]), out=win, what='SET WINDOW')

            ui.step('Starting the scan (lamp on, head starts sweeping)')
            self.ok(bytes([0x1B, 0, 0, 0, 1, 0]), out=b'\0', what='SCAN')

            # Wait until the first image chunk is ready (status 08 = busy).
            ui.step('Waiting for the first image data')
            ln = struct.pack('>I', READ_CHUNK)[1:]
            deadline = time.time() + self.ready_s
            while True:
                status, _ = self.cmd(bytes([0xF1, 0x10, 0, 0, 0, 0]) + ln + b'\0')
                if status == GOOD:
                    break
                if status != BUSY or time.time() > deadline:
                    raise ScannerError(f'image-ready check failed: status {status:02x}, '
                                       f'sense {self.sense().hex(" ")}')
                ui.spin('scanner busy')
                time.sleep(0.05)

            ui.step('Receiving image while the head sweeps')
            px_w = FULL_WIDTH_1200 * DPI // 1200
            px_h = FULL_LENGTH_1200 * DPI // 1200
            expected = px_w * px_h * 3
            image = bytearray()
            while True:
                status, data = self.cmd(bytes([0x28, 0, 0, 0, 0, 0]) + ln + b'\0',
                                        read_len=READ_CHUNK, timeout=self.read_ms)
                image += data
                ui.bar(min(len(image), expected), expected)
                if status == GOOD:
                    continue
                if status == CHECK_CONDITION:
                    s = self.sense()
                    if s[2] & 0x40:          # end of medium: last chunk
                        break
                    raise ScannerError(f'READ failed, sense {s.hex(" ")}')
                raise ScannerError(f'READ failed: status {status:02x}')
            ui.end(f'{expected / 1e6:.1f} MB')

            ui.step('Finishing the scan (reading image size, releasing scanner)')
            size = self.ok(bytes([0x28, 0, 0x80, 0, 0, 0, 0, 0, 0x18, 0]), read_len=0x18,
                           what='READ PIXEL SIZE')
            width, height = struct.unpack_from('>II', size, 0)
        except Exception as e:
            self.dump_log(e)
            raise
        finally:
            try:
                self.diag('SET SCAN', b'\x02')
            except Exception as e:                   # keep the original error, if any
                self.trace.append((time.time(), 'SET SCAN 02', 0, 0, f'clean-up failed: {e}'))
        need = width * height * 3
        if len(image) < need:
            raise ScannerError(f'got {len(image)} bytes, expected {need} for {width}x{height}')
        ui.end(f'{width} x {height} px')
        return width, height, bytes(image[:need])
