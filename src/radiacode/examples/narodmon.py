import argparse
import asyncio
import time

import aiohttp

from radiacode import RealTimeData, RadiaCode
from radiacode.radiacode import ProtocolError
from radiacode.transports.bluetooth import ConnectionClosed, DeviceNotFound


def sensors_data(rc_conn: RadiaCode):
    databuf = rc_conn.data_buf()

    last = None
    for v in databuf:
        if isinstance(v, RealTimeData):
            if last is None or last.dt < v.dt:
                last = v

    if last is None:
        return []

    ts = int(last.dt.timestamp())
    return [
        {
            'id': 'S1',
            'name': 'CountRate',
            'value': last.count_rate,
            'unit': 'CPS',
            'time': ts,
        },
        {
            'id': 'S2',
            'name': 'R_DoseRate',
            'value': 1000000 * last.dose_rate,
            'unit': 'μR/h',
            'time': ts,
        },
    ]


async def send_data(d):
    # use aiohttp because we already have it as dependency in webserver.py, don't want add 'requests' here
    async with aiohttp.ClientSession() as session:
        async with session.post('https://narodmon.ru/json', json=d) as resp:
            return await resp.text()


def make_connection(args) -> RadiaCode:
    if args.connection == 'usb':
        print('will use USB connection')
        return RadiaCode()
    if not (args.bluetooth_mac or args.bluetooth_address or args.bluetooth_name):
        raise ValueError('Bluetooth connection requires --bluetooth-mac, --bluetooth-address, or --bluetooth-name')
    print('will use Bluetooth connection')
    return RadiaCode(
        bluetooth_mac=args.bluetooth_mac,
        bluetooth_address=args.bluetooth_address,
        bluetooth_name=args.bluetooth_name,
    )


def main():
    parser = argparse.ArgumentParser(description='Send RadiaCode measurements to narodmon.ru')
    parser.add_argument(
        '--bluetooth-mac',
        type=str,
        required=False,
        help='Bluetooth MAC address — Linux only (via bluepy). Also used as the device identifier sent to narodmon.',
    )
    parser.add_argument(
        '--bluetooth-address',
        type=str,
        required=False,
        help='Bluetooth device address / CoreBluetooth UUID (macOS/Windows via bleak)',
    )
    parser.add_argument(
        '--bluetooth-name',
        type=str,
        required=False,
        help='Scan for BLE device with this name prefix (e.g. "RadiaCode")',
    )
    parser.add_argument('--connection', choices=['usb', 'bluetooth'], default='bluetooth', help='device connection type')
    parser.add_argument('--interval', type=int, required=False, default=600, help='send interval, seconds')
    args = parser.parse_args()

    if args.connection == 'bluetooth' and not (args.bluetooth_mac or args.bluetooth_address or args.bluetooth_name):
        parser.error('Bluetooth connection requires --bluetooth-mac, --bluetooth-address, or --bluetooth-name')

    # Use MAC as device identifier for narodmon when available, else fallback
    mac_id = args.bluetooth_mac or args.bluetooth_address or 'RC-BLE'
    device_data = {
        'mac': mac_id.replace(':', '-'),
        'name': 'RadiaCode-101',
    }

    rc_conn: RadiaCode | None = None
    try:
        while True:
            if rc_conn is None:
                try:
                    rc_conn = make_connection(args)
                except DeviceNotFound as exc:
                    print(f'Device not found, retry in 5s: {exc}')
                    time.sleep(5)
                    continue

            try:
                sensors = sensors_data(rc_conn)
            except (ConnectionClosed, ProtocolError, TimeoutError, OSError) as exc:
                print(f'Device read error, reconnecting in 5s: {exc}')
                try:
                    rc_conn.close()
                except Exception:
                    pass
                rc_conn = None
                time.sleep(5)
                continue

            d = {
                'devices': [
                    {
                        **device_data,
                        'sensors': sensors,
                    },
                ],
            }
            print(f'Sending {d}')

            try:
                r = asyncio.run(send_data(d))
                print(f'NarodMon Response: {r}')
            except Exception as ex:
                print(f'NarodMon send error: {ex}')

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print('\nStopped.')
    finally:
        if rc_conn is not None:
            try:
                rc_conn.close()
            except Exception:
                pass


if __name__ == '__main__':
    main()
