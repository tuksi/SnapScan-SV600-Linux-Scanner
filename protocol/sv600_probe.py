#!/usr/bin/env python3
"""Read-only probe for the ScanSnap SV600 over USB.

Sends only identification/status commands (no scan, no EEPROM/RAM writes,
no firmware switch) using the Fujitsu USB framing recovered from
SV600u-x64.dll:

  OUT  31 bytes : 0x43, zeros..., CDB at offset 19
  DATA          : optional bulk IN (or OUT) phase
  IN   13 bytes : status block

Needs: pip install pyusb   (and the udev rule in 60-sv600.rules, or sudo)
"""
import sys
import usb.core
import usb.util

IDS = [(0x04C5, 0x128E, 'Fujitsu-branded SV600'),
       (0x05CA, 0x03C7, 'Ricoh-branded SV600')]

CMD_LEN, CMD_CODE, CDB_OFFSET, STATUS_LEN = 0x1F, 0x43, 0x13, 0x0D
TIMEOUT_MS = 10000


def hexdump(b, indent='    '):
    for i in range(0, len(b), 16):
        chunk = bytes(b[i:i + 16])
        txt = ''.join(chr(c) if 32 <= c < 127 else '.' for c in chunk)
        print(f'{indent}{i:04x}  {chunk.hex(" "):<47}  |{txt}|')


class SV600:
    def __init__(self):
        for vid, pid, label in IDS:
            dev = usb.core.find(idVendor=vid, idProduct=pid)
            if dev:
                break
        else:
            print('No SV600 found. Other Fujitsu/PFU/Ricoh devices on the bus:')
            for d in usb.core.find(find_all=True):
                if d.idVendor in (0x04C5, 0x05CA, 0x13BA):
                    print(f'  {d.idVendor:04x}:{d.idProduct:04x}')
            sys.exit(1)
        print(f'Found {label} {vid:04x}:{pid:04x} (bus {dev.bus} addr {dev.address})')
        self.dev = dev
        self.dump_descriptors()
        if dev.is_kernel_driver_active(0):
            dev.detach_kernel_driver(0)
        usb.util.claim_interface(dev, 0)
        intf = dev.get_active_configuration()[(0, 0)]
        self.ep_out = usb.util.find_descriptor(intf, custom_match=lambda e:
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT and
            usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
        self.ep_in = usb.util.find_descriptor(intf, custom_match=lambda e:
            usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN and
            usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
        print(f'Bulk OUT {self.ep_out.bEndpointAddress:#04x}, bulk IN {self.ep_in.bEndpointAddress:#04x}')

    def dump_descriptors(self):
        d = self.dev
        try:
            print(f'  manufacturer={usb.util.get_string(d, d.iManufacturer)!r} '
                  f'product={usb.util.get_string(d, d.iProduct)!r} '
                  f'serial={usb.util.get_string(d, d.iSerialNumber)!r}')
        except (usb.core.USBError, ValueError) as e:
            print(f'  (string descriptors unavailable: {e})')
        for cfg in d:
            for intf in cfg:
                print(f'  interface {intf.bInterfaceNumber} alt {intf.bAlternateSetting} '
                      f'class {intf.bInterfaceClass:#04x}/{intf.bInterfaceSubClass:#04x}')
                for ep in intf:
                    print(f'    ep {ep.bEndpointAddress:#04x} attr {ep.bmAttributes:#04x} '
                          f'maxpkt {ep.wMaxPacketSize}')

    def cmd(self, cdb, read_len=0, out_data=b''):
        pkt = bytearray(CMD_LEN)
        pkt[0] = CMD_CODE
        pkt[CDB_OFFSET:CDB_OFFSET + len(cdb)] = cdb
        self.ep_out.write(pkt, TIMEOUT_MS)
        data = b''
        if out_data:
            self.ep_out.write(out_data, TIMEOUT_MS)
        if read_len:
            data = bytes(self.ep_in.read(read_len, TIMEOUT_MS))
        status = bytes(self.ep_in.read(STATUS_LEN, TIMEOUT_MS))
        return data, status


def run(sv, name, cdb, read_len=0, out_data=b''):
    print(f'\n== {name}: cdb={bytes(cdb).hex(" ")}' + (f' out={out_data!r}' if out_data else ''))
    try:
        data, status = sv.cmd(bytes(cdb), read_len, out_data)
    except usb.core.USBError as e:
        print(f'   USB error: {e}')
        return None
    print(f'   status ({len(status)}): {status.hex(" ")}')
    if data:
        print(f'   data ({len(data)} bytes):')
        hexdump(data)
    return data


def main():
    sv = SV600()
    run(sv, 'TEST UNIT READY', [0x00, 0, 0, 0, 0, 0])
    run(sv, 'INQUIRY (standard)', [0x12, 0, 0, 0, 0x60, 0], 0x60)
    run(sv, 'INQUIRY VPD 0xF0', [0x12, 1, 0xF0, 0, 0xFF, 0], 0xFF)
    run(sv, 'INQUIRY VPD 0xE0', [0x12, 1, 0xE0, 0, 0xFF, 0], 0xFF)
    run(sv, 'REQUEST SENSE', [0x03, 0, 0, 0, 0x12, 0], 0x12)
    run(sv, 'GET HW STATUS', [0xC2, 0, 0, 0, 0, 0, 0, 0, 0x0C, 0], 0x0C)
    # Vendor diagnostics: SEND DIAGNOSTIC with a 16-byte "GET ..." request,
    # then RECEIVE DIAGNOSTIC to fetch the answer. Read-only on the device.
    for req in (b'GET FIRMVERSION ', b'GET DEVICE ID   ', b'READ COUNT      '):
        if run(sv, f'SEND DIAG {req.decode().strip()}', [0x1D, 0, 0, 0, len(req), 0],
               out_data=req) is None:
            continue
        run(sv, 'RECEIVE DIAG', [0x1C, 0, 0, 0, 0x40, 0], 0x40)
    usb.util.release_interface(sv.dev, 0)


if __name__ == '__main__':
    main()
