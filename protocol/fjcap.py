#!/usr/bin/env python3
"""Decode a VirtualBox/usbmon pcapng capture of a Fujitsu-protocol scanner.

Rebuilds the command stream: 31-byte 0x43 command blocks on bulk OUT, the
optional data phase, and the 13-byte status on bulk IN.

usage:
  fjcap.py CAPTURE.pcap                 # command log
  fjcap.py CAPTURE.pcap --dump DIR      # also write every data phase to DIR/NNNN_<op>_<dir>.bin
  fjcap.py CAPTURE.pcap --raw           # raw usbmon events
"""
import struct
import sys
import os

CDB_NAMES = {
    0x00: 'TEST UNIT READY', 0x03: 'REQUEST SENSE', 0x12: 'INQUIRY', 0x15: 'MODE SELECT',
    0x16: 'RESERVE', 0x17: 'RELEASE', 0x1A: 'MODE SENSE', 0x1B: 'SCAN', 0x1C: 'RECV DIAG',
    0x1D: 'SEND DIAG', 0x24: 'SET WINDOW', 0x25: 'GET WINDOW', 0x28: 'READ', 0x2A: 'SEND',
    0x31: 'OBJECT POSITION', 0xC1: 'ENDORSER', 0xC2: 'GET HW STATUS', 0xF1: 'SCANNER CONTROL',
    0xF2: 'SCANNER CONTROL2',
}


def read_pcapng(path):
    """Yield (linktype, ts, packet_bytes)."""
    links = []
    with open(path, 'rb') as f:
        data = f.read()
    off = 0
    endian = '<'
    while off + 12 <= len(data):
        btype, blen = struct.unpack_from(endian + 'II', data, off)
        if btype == 0x0A0D0D0A:
            bom = data[off + 8:off + 12]
            endian = '<' if bom == b'\x4d\x3c\x2b\x1a' else '>'
            btype, blen = struct.unpack_from(endian + 'II', data, off)
            links = []
        elif btype == 1:  # IDB
            lt, = struct.unpack_from(endian + 'H', data, off + 8)
            links.append(lt)
        elif btype == 6:  # EPB
            ifid, tsh, tsl, cap, orig = struct.unpack_from(endian + 'IIIII', data, off + 8)
            pkt = data[off + 28:off + 28 + cap]
            yield links[ifid], ((tsh << 32) | tsl) / 1e6, pkt
        elif btype == 3:  # SPB
            orig, = struct.unpack_from(endian + 'I', data, off + 8)
            yield links[0], 0.0, data[off + 12:off + 12 + min(orig, blen - 16)]
        if blen < 12:
            break
        off += blen


def usbmon_events(path):
    """Yield dicts for each usbmon record (linktype 189 = 48-byte hdr, 220 = 64-byte hdr)."""
    for lt, ts, p in read_pcapng(path):
        if lt not in (189, 220):
            raise SystemExit(f'unsupported linktype {lt}')
        hdr = 64 if lt == 220 else 48
        (urb_id, ev, xfer, epnum, devnum, busnum, flag_setup, flag_data,
         sec, usec, status, length, len_cap) = struct.unpack_from('<QBBBBHccqiiII', p, 0)
        yield dict(ts=ts, id=urb_id, ev=chr(ev), xfer=xfer, ep=epnum, dev=devnum,
                   status=status, length=length, setup=p[40:48] if flag_setup == b'\0' else None,
                   data=p[hdr:hdr + len_cap])


def bulk_stream(path):
    """Yield (ts, 'OUT'|'IN', ep, bytes) for completed bulk transfers with data."""
    pending = {}
    for e in usbmon_events(path):
        if e['xfer'] != 3:  # bulk only
            continue
        is_in = bool(e['ep'] & 0x80)
        if e['ev'] == 'S':
            pending[e['id']] = e
            if not is_in and e['data']:
                yield e['ts'], 'OUT', e['ep'], e['data']
        elif e['ev'] == 'C':
            pending.pop(e['id'], None)
            if is_in and e['data']:
                yield e['ts'], 'IN', e['ep'], e['data']
            elif e['status'] not in (0,) and is_in:
                yield e['ts'], 'IN-ERR', e['ep'], str(e['status']).encode()


def commands(path):
    """Group bulk stream into fujitsu command transactions."""
    cur = None
    for ts, d, ep, b in bulk_stream(path):
        if d == 'OUT' and len(b) == 31 and b[0] == 0x43:
            if cur:
                yield cur
            cdb = b[0x13:0x13 + (6 if b[0x13] < 0x20 else 10 if b[0x13] < 0xA0 else 12 if b[0x13] < 0xC0 else 10)]
            cur = dict(ts=ts, cdb=b[0x13:0x1F], cdb_short=cdb, out=b'', inn=b'', status=None, raw=b)
            continue
        if cur is None:
            yield dict(ts=ts, stray=(d, b))
            continue
        if d == 'OUT':
            cur['out'] += b
        elif d == 'IN':
            if len(b) == 13 and b[0] == 0x53 and cur['status'] is None and (
                    len(cur['inn']) > 0 or True):
                # status block; but image data could also be 13 bytes — accept heuristically
                cur['status'] = b
                yield cur
                cur = None
            else:
                cur['inn'] += b
        else:
            cur['err'] = b
    if cur:
        yield cur


def fmt_data(b, n=48):
    s = b[:n]
    txt = ''.join(chr(c) if 32 <= c < 127 else '.' for c in s)
    return f'{s.hex(" ")}{" ..." if len(b) > n else ""}  |{txt}|'


def main():
    path = sys.argv[1]
    if '--raw' in sys.argv:
        for e in usbmon_events(path):
            print(f"{e['ts']:.6f} {e['ev']} xfer={e['xfer']} ep={e['ep']:#04x} dev={e['dev']} "
                  f"st={e['status']} len={e['length']} {e['data'][:32].hex(' ')}"
                  + (f" setup={e['setup'].hex(' ')}" if e['setup'] else ''))
        return
    dump = None
    if '--dump' in sys.argv:
        dump = sys.argv[sys.argv.index('--dump') + 1]
        os.makedirs(dump, exist_ok=True)
    t0 = None
    for i, c in enumerate(commands(path)):
        t0 = t0 or c['ts']
        if 'stray' in c:
            d, b = c['stray']
            print(f'{c["ts"] - t0:9.3f}  STRAY {d} {len(b)}: {fmt_data(b)}')
            continue
        op = c['cdb_short'][0]
        name = CDB_NAMES.get(op, f'op {op:02x}')
        st = c['status'].hex(' ') if c['status'] else 'NO STATUS'
        print(f'{c["ts"] - t0:9.3f}  #{i:04d} {name:16} cdb={c["cdb_short"].hex(" ")}  status={st[27:29] if c["status"] else st}')
        if c['out']:
            print(f'           OUT {len(c["out"]):7}: {fmt_data(c["out"])}')
        if c['inn']:
            print(f'           IN  {len(c["inn"]):7}: {fmt_data(c["inn"])}')
        if c.get('err'):
            print(f'           ERR {c["err"]}')
        if dump:
            for k in ('out', 'inn'):
                if c[k]:
                    with open(os.path.join(dump, f'{i:04d}_{op:02x}_{k}.bin'), 'wb') as f:
                        f.write(c[k])


if __name__ == '__main__':
    main()
