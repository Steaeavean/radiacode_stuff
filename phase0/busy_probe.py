#!/usr/bin/env python3
"""busy_probe — decide how atomapp-ios should treat a RadiaCode that is BUSY
(already connected to another app, e.g. its own Android/iOS phone app).

Field bug (iOS 26.5.1): when a RadiaCode is connected to its Android app and
atomapp-ios runs a BLE scan, the RadiaCode still appears in our device list and
the app auto-connects into it. The device's single shared command processor then
returns a mismatched SET_EXCHANGE (reqtype=0x0007) echo → the app showed a
"Bluetooth settings error" modal.

We want Option 1: show the device greyed-out ("unavailable") WITHOUT connecting,
until it is free. That is cleanly possible ONLY if a busy RadiaCode can be
distinguished from a free one *at scan time*, i.e. from the advertisement itself
(without connecting). The decisive, OS-independent fact is the advertisement's
connectable flag: CoreBluetooth surfaces it as `kCBAdvDataIsConnectable`
(0 = non-connectable advert / 1 = connectable). bleak's macOS backend exposes the
raw advert dict via AdvertisementData.platform_data — this script reads it.

What it measures, per scan detection of the RadiaCode service UUID:
  • local name, address, RSSI
  • kCBAdvDataIsConnectable (0 / 1 / unknown)
and (optionally, default ON) ONE connect attempt that reproduces the app's path:
  • did the link connect at all? (didConnect vs didFailToConnect)
  • if connected: enable notify + send SET_EXCHANGE and report whether the echo
    matches (free) or mismatches/times out (busy) — the exact app failure mode.

HOW TO RUN (macOS, near the hardware — same workflow as the other phase0 probes):

  # 1) BUSY case — connect the RadiaCode to its Android app first, THEN:
  /Users/vadimkz/Projects/radiacode/.venv/bin/python \
      /Users/vadimkz/atomapp-ios/radiacode_stuff/phase0/busy_probe.py --label busy

  # 2) FREE case — fully disconnect/close the Android app (device idle), THEN:
  /Users/vadimkz/Projects/radiacode/.venv/bin/python \
      /Users/vadimkz/atomapp-ios/radiacode_stuff/phase0/busy_probe.py --label free

  # scan only (no connect attempt — safest, won't poke the Android session):
  ... busy_probe.py --label busy --no-connect

Then compare the two runs:
  • If kCBAdvDataIsConnectable == 0 while busy and == 1 while free
      → Option 1 is cleanly achievable: gate auto-connect on
        advertisementData[CBAdvertisementDataIsConnectable] in
        AMRadiacodeDeviceManager.didDiscoverPeripheral and render the cell
        greyed "unavailable". No connect, no Android disruption.
  • If the flag is 1 (or unknown) in BOTH cases
      → busy cannot be detected from the advert; fall back to the hybrid
        (connect once, on session-init failure mark the cell unavailable +
        back off auto-reconnect, modal already suppressed in 1.4.35 (38)).

Results → docs/radiacode-ble-protocol.md (H5 / new H21) +
.cursor/skills/atom-radiacode/SKILL.md (busy-device handling).
"""
import argparse
import asyncio
import datetime
import struct

from bleak import BleakClient, BleakScanner

SERVICE_UUID = 'e63215e5-7003-49d8-96b0-b024798fb901'
WRITE_UUID = 'e63215e6-7003-49d8-96b0-b024798fb901'   # host -> device (write)
NOTIFY_UUID = 'e63215e7-7003-49d8-96b0-b024798fb901'  # device -> host (notify)


def _is_connectable(adv) -> str:
    """Best-effort read of kCBAdvDataIsConnectable from bleak's macOS backend.

    bleak >= 0.21 exposes the raw CoreBluetooth advert dict as
    AdvertisementData.platform_data == (peripheral, adv_dict, rssi). The adv_dict
    holds NSString keys incl. 'kCBAdvDataIsConnectable' -> NSNumber 0/1. Other
    backends won't have it → 'unknown'.
    """
    pd = getattr(adv, 'platform_data', None)
    if not pd:
        return 'unknown'
    adv_dict = None
    for el in pd:
        if hasattr(el, 'get') or hasattr(el, 'objectForKey_'):
            adv_dict = el
            break
    if adv_dict is None:
        return 'unknown'
    for key in ('kCBAdvDataIsConnectable', 'IsConnectable'):
        try:
            val = adv_dict.get(key) if hasattr(adv_dict, 'get') else None
        except Exception:
            val = None
        if val is None and hasattr(adv_dict, 'objectForKey_'):
            try:
                val = adv_dict.objectForKey_(key)
            except Exception:
                val = None
        if val is not None:
            try:
                return '1 (connectable)' if int(val) else '0 (NON-connectable)'
            except Exception:
                return repr(val)
    return 'unknown'


async def scan(label: str, seconds: float):
    print(f"[busy_probe:{label}] scanning {seconds:.0f}s for RadiaCode "
          f"({SERVICE_UUID}) ...", flush=True)
    seen = {}  # address -> (name, rssi, connectable, count, device)

    def cb(device, adv):
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        name = adv.local_name or device.name or ''
        if SERVICE_UUID not in uuids and not name.startswith('RadiaCode'):
            return
        conn = _is_connectable(adv)
        prev = seen.get(device.address)
        count = (prev[3] + 1) if prev else 1
        seen[device.address] = (name, adv.rssi, conn, count, device)

    scanner = BleakScanner(detection_callback=cb, service_uuids=[SERVICE_UUID])
    await scanner.start()
    await asyncio.sleep(seconds)
    await scanner.stop()

    if not seen:
        print(f"[busy_probe:{label}] no RadiaCode advertised. "
              f"If it should be present, it may have STOPPED advertising while "
              f"busy (like Atom Tag/Swift/Fast) — that itself answers the "
              f"question: just don't list it.", flush=True)
        return None

    print(f"[busy_probe:{label}] detections:")
    chosen = None
    for addr, (name, rssi, conn, count, device) in seen.items():
        print(f"   name={name!r:30} addr={addr}  RSSI={rssi}  "
              f"kCBAdvDataIsConnectable={conn}  adverts_seen={count}")
        chosen = device
    return chosen


async def try_connect(label: str, device):
    print(f"[busy_probe:{label}] connect attempt → {device.address} ...", flush=True)
    t0 = datetime.datetime.now()
    try:
        async with BleakClient(device, timeout=15.0) as client:
            dt = (datetime.datetime.now() - t0).total_seconds()
            print(f"[busy_probe:{label}] LINK CONNECTED in {dt:.1f}s "
                  f"(iOS would proceed to session init here)", flush=True)

            # reassembler for one SET_EXCHANGE round-trip
            state = {'size': 0, 'buf': b'', 'fut': None}
            loop = asyncio.get_running_loop()

            def on_notify(_c, data):
                chunk = bytes(data)
                if state['size'] == 0:
                    if len(chunk) < 4:
                        return
                    state['size'] = 4 + struct.unpack('<i', chunk[:4])[0]
                    state['buf'] = chunk[4:]
                else:
                    state['buf'] += chunk
                state['size'] -= len(chunk)
                if state['size'] <= 0 and state['fut'] and not state['fut'].done():
                    state['fut'].set_result(state['buf'])

            await client.start_notify(NOTIFY_UUID, on_notify)

            # SET_EXCHANGE (reqtype 0x0007), seq 0x80, magic 01 ff 12 ff — the
            # exact first command atomapp-ios sends in session init.
            req_header = struct.pack('<HBB', 0x0007, 0, 0x80)
            request = req_header + b'\x01\xff\x12\xff'
            full = struct.pack('<I', len(request)) + request
            state['fut'] = loop.create_future()
            for pos in range(0, len(full), 18):
                await client.write_gatt_char(WRITE_UUID, full[pos:pos + 18], response=False)
            try:
                payload = await asyncio.wait_for(state['fut'], timeout=10.0)
                resp_header = payload[:4]
                if resp_header == req_header:
                    print(f"[busy_probe:{label}] SET_EXCHANGE echo OK → device is "
                          f"FREE / usable.", flush=True)
                else:
                    print(f"[busy_probe:{label}] SET_EXCHANGE echo MISMATCH "
                          f"req={req_header.hex()} resp={resp_header.hex()} → device "
                          f"is BUSY (reproduces the app's reqtype=0x0007 error).",
                          flush=True)
            except asyncio.TimeoutError:
                print(f"[busy_probe:{label}] SET_EXCHANGE TIMEOUT → device is BUSY "
                      f"/ not answering session init.", flush=True)
    except Exception as e:
        dt = (datetime.datetime.now() - t0).total_seconds()
        print(f"[busy_probe:{label}] LINK FAILED after {dt:.1f}s: "
              f"{type(e).__name__}: {e}", flush=True)
        print(f"[busy_probe:{label}] (didFailToConnect path — iOS would never "
              f"reach session init; cleanest case for Option 1.)", flush=True)


async def main_async(args):
    device = await scan(args.label, args.scan)
    if device is None:
        return
    if args.no_connect:
        print(f"[busy_probe:{args.label}] --no-connect: skipping connect attempt.")
        return
    await try_connect(args.label, device)
    print(f"\n[busy_probe:{args.label}] DONE. Run again with the opposite state "
          f"(--label {'free' if args.label == 'busy' else 'busy'}) and compare "
          f"kCBAdvDataIsConnectable.", flush=True)


def main():
    ap = argparse.ArgumentParser(description="RadiaCode busy-vs-free advertisement probe")
    ap.add_argument('--label', default='probe',
                    help="run label for the log (e.g. 'busy' or 'free')")
    ap.add_argument('--scan', type=float, default=8.0,
                    help="scan window seconds (default 8)")
    ap.add_argument('--no-connect', action='store_true',
                    help="scan only; do NOT attempt a connection (won't poke the "
                         "other app's session)")
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == '__main__':
    main()
