import argparse
import time

from radiacode import RadiaCode
from radiacode.transports.bluetooth import DeviceNotFound as DeviceNotFoundBT
from radiacode.transports.usb import DeviceNotFound as DeviceNotFoundUSB


def main():
    parser = argparse.ArgumentParser(
        description='Read real-time data from a RadiaCode device.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Connection options (choose one):
  USB (all platforms, requires sudo on Linux):
    python -m radiacode.examples.basic

  Bluetooth — macOS / Windows (via bleak, no sudo required):
    python -m radiacode.examples.basic --bluetooth-name RadiaCode
    python -m radiacode.examples.basic --bluetooth-address <CoreBluetooth-UUID>

  Bluetooth — Linux (via bluepy, requires sudo):
    python -m radiacode.examples.basic --bluetooth-mac AA:BB:CC:DD:EE:FF
""",
    )
    parser.add_argument(
        '--bluetooth-mac',
        type=str,
        required=False,
        metavar='MAC',
        help='Bluetooth MAC address — Linux only (via bluepy). '
             'On macOS/Windows use --bluetooth-address or --bluetooth-name.',
    )
    parser.add_argument(
        '--bluetooth-address',
        type=str,
        required=False,
        metavar='ADDRESS',
        help='Bluetooth device address / CoreBluetooth UUID for bleak '
             '(macOS, Windows, Linux-bleak). Takes precedence over --bluetooth-name.',
    )
    parser.add_argument(
        '--bluetooth-name',
        type=str,
        required=False,
        metavar='PREFIX',
        default=None,
        help='Scan for a BLE device whose name starts with PREFIX '
             '(default auto-scan if no address given). E.g. "RadiaCode".',
    )
    parser.add_argument(
        '--serial',
        type=str,
        required=False,
        metavar='SN',
        help='USB serial number (e.g. "RC-10x-xxxxxx") — useful with multiple USB devices.',
    )

    args = parser.parse_args()

    ble_mac = args.bluetooth_mac
    ble_addr = args.bluetooth_address
    ble_name = args.bluetooth_name

    if ble_addr or ble_name or ble_mac:
        import platform
        if platform.system() == 'Linux' and ble_mac and not ble_addr and not ble_name:
            print(f'Connecting via Bluetooth (bluepy) to MAC {ble_mac}')
        else:
            label = ble_addr or f'name prefix "{ble_name}"' if ble_name else 'auto-scan'
            print(f'Connecting via Bluetooth (bleak) — {label}')
        try:
            rc = RadiaCode(
                bluetooth_mac=ble_mac,
                bluetooth_address=ble_addr,
                bluetooth_name=ble_name,
            )
        except DeviceNotFoundBT as e:
            print(e)
            return
        except ValueError as e:
            print(e)
            return
    else:
        print('Connecting via USB' + (f' (serial: {args.serial})' if args.serial else ''))
        try:
            rc = RadiaCode(serial_number=args.serial)
        except DeviceNotFoundUSB:
            print('Device not found — check USB connection')
            return

    print(f'Serial:   {rc.serial_number()}')
    print(f'Firmware: {rc.fw_version()}')
    print(f'Spectrum: {rc.spectrum()}')
    print('--- DataBuf (Ctrl-C to stop) ---')
    while True:
        for v in rc.data_buf():
            print(v.dt.isoformat(), v)
        time.sleep(2)


if __name__ == '__main__':
    main()
