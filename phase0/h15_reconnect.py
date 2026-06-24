#!/usr/bin/env python3
"""H15 — поведение RadiaCode при reconnect (Фаза 0 валидации).

Снимает RareData (накопленная доза / duration) ДО и ПОСЛЕ разрыва соединения, чтобы понять:
прибор ПРОДОЛЖАЕТ свою сессию (монотонные счётчики) или ОБНУЛЯЕТ её. От этого зависит,
нужен ли baseline-shift при маппинге в AMDosimeter (Фаза 3) — как у Atom Fast после
power-cycle.

На USB «разрыв» = usb.util.dispose_resources(device) + повторное открытие. Это прокси к
BLE-дисконнекту: проверяется поведение FIRMWARE-сессии прибора, а не транспорт.
Окончательное BLE-подтверждение (H5/H15) — на iPhone (nRF Connect), USB-проксей не покрыто.

Полностью автоматический — БЕЗ ручного Ctrl+C.

Запуск (USB на macOS требует root):
  sudo /Users/vadimkz/Projects/radiacode/.venv/bin/python \
       /Users/vadimkz/atomapp-ios/radiacode_stuff/phase0/h15_reconnect.py --gap 20 --cycles 1 --rare-timeout 300

Результаты заносить в docs/radiacode-ble-protocol.md §14.4 и docs/radiacode-ble-integration-plan.md.
"""
import argparse
import time

import usb.util

from radiacode import RadiaCode
from radiacode.types import RareData, RealTimeData


def grab_rare(rc, timeout=300):
    """Дождаться периодической записи RareData и вернуть последнюю в окне.

    ВАЖНО: DB-группы (RareData/DoseRateDB) пишутся прибором на медленной каденции
    ~DBLag_ms=180000 (180 c), поэтому timeout должен быть заметно больше 180 c.
    Печатает heartbeat (видно, что коннект жив и записи идут), даже пока RareData нет.
    """
    t0 = time.time()
    last_print = t0
    seen = 0
    rts = 0
    latest = None
    while time.time() - t0 < timeout:
        for r in rc.data_buf():
            seen += 1
            if isinstance(r, RealTimeData):
                rts += 1
            if isinstance(r, RareData):
                latest = r
        if latest is not None:
            return latest
        if time.time() - last_print >= 30:
            last_print = time.time()
            print(f"       ... ждём RareData {int(time.time() - t0)}/{timeout}s "
                  f"(records={seen}, RealTimeData={rts})", flush=True)
        time.sleep(1)
    return None


def disconnect(rc):
    """Разорвать соединение: USB → dispose_resources; BLE(Linux) → close()."""
    conn = getattr(rc, "_connection", None)
    if conn is None:
        return
    dev = getattr(conn, "_device", None)  # USB (pyusb)
    if dev is not None:
        try:
            usb.util.dispose_resources(dev)
        except Exception as e:
            print(f"       (dispose_resources: {e})")
    close = getattr(conn, "close", None)  # BLE (bluepy) — на всякий случай
    if callable(close):
        try:
            close()
        except Exception:
            pass


def show(tag, r):
    if r is None:
        print(f"  {tag}: RareData не получена (timeout)")
        return
    print(f"  {tag}: dose={r.dose * 1e6:.1f} uR ({r.dose * 1e7:.0f} nSv)  "
          f"duration={r.duration}s  T={r.temperature:.1f}C  chg={r.charge_level * 100:.0f}%")


def main():
    ap = argparse.ArgumentParser(description="H15 — reconnect-поведение RadiaCode")
    ap.add_argument("--gap", type=int, default=20,
                    help="пауза между disconnect и reconnect, с (default 20)")
    ap.add_argument("--cycles", type=int, default=1, help="число циклов (default 1)")
    ap.add_argument("--rare-timeout", type=int, default=300,
                    help="макс. ожидание RareData, с (default 300; должно быть > DBLag 180 c)")
    ap.add_argument("--serial", default=None, help="serial номер прибора")
    args = ap.parse_args()

    print("[H15] connect ...")
    rc = RadiaCode(serial_number=args.serial)
    print(f"[H15] device={rc.serial_number()} fw_target={rc.fw_version()[1]}")

    print(f"[H15] baseline RareData (медленная каденция ~180 c, ждём до {args.rare_timeout}c) ...")
    before = grab_rare(rc, args.rare_timeout)
    show("before", before)

    for i in range(1, args.cycles + 1):
        print(f"\n[H15] cycle {i}/{args.cycles}: disconnect → wait {args.gap}s → reconnect")
        disconnect(rc)
        del rc
        time.sleep(args.gap)
        rc = RadiaCode(serial_number=args.serial)
        after = grab_rare(rc, args.rare_timeout)
        show(f"after#{i}", after)
        if before and after:
            d_dose = after.dose - before.dose
            d_dur = after.duration - before.duration
            if after.duration >= before.duration and after.dose >= before.dose:
                verdict = ("ПРОДОЛЖАЕТ сессию (монотонно) → baseline-shift для "
                           "RareData.dose НЕ нужен")
            elif after.duration < before.duration * 0.5:
                verdict = "ОБНУЛЯЕТ сессию → нужен baseline-shift (как у Atom Fast)"
            else:
                verdict = "неоднозначно — смотреть числа вручную"
            print(f"    Δdose={d_dose * 1e6:+.1f} uR  Δduration={d_dur:+d}s  → {verdict}")
        before = after

    print("\n[H15] вывод → radiacode-ble-protocol.md §14.4 + план §«История валидации».")
    print("[H15] окончательный BLE-reconnect (H5/H15) подтвердить на iPhone (nRF Connect).")


if __name__ == "__main__":
    main()
