# SV600 USB protocol material

How the scan sequence in `sv600/scanner.py` was found. Not needed to run the app; the
findings are summarised in `../NOTES.md`.

| File | What |
|---|---|
| `command_table.txt` | every vendor command id of the Windows driver (`SV600u-x64.dll`, emulated `Make_CdbCommand`) with its CDB |
| `sv600_probe.py` | read-only probe: INQUIRY, VPD pages, hardware status, diagnostic GETs (no scan, nothing written) |
| `fjcap.py` | decodes a VirtualBox/usbmon USB capture into Fujitsu command blocks, data phases and statuses |
| `captures/scan1_cmds.txt` | decoded capture of one ScanSnap Home scan (Windows 10 VM), the sequence the app replays |
| `captures/scan2_cmds.txt` | same with ScanSnap Home set to "Excellent": SET WINDOW is byte-identical, the SV600 always scans at 300 dpi |
| `sane_fujitsu_scan_hang.log` | debug log of the stock SANE `fujitsu` backend hanging the scanner on SCAN |

Not in git (large, kept locally): `captures/*.pcap` (the raw captures, ~75 MB each) and
`captures/*_data/` (every data phase dumped by `fjcap.py --dump`).

Recreate a decoded log: `python fjcap.py captures/scan1.pcap > captures/scan1_cmds.txt`.
To capture again, attach the scanner to the Windows VM with a capture file:
`VBoxManage controlvm Win10 usbattach <uuid> --capturefile scan.pcap`.
