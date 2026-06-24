#!/usr/bin/env python3
"""H14 — глубина кольцевого DATA_BUF RadiaCode (Фаза 0 валидации).

Идл-свип: держим ОДНО USB-соединение открытым, не читаем буфер `gap` секунд (прибор
накапливает в свой кольцевой DATA_BUF), затем вычитываем всё и меряем, сколько истории
вернулось. Если recovered заметно меньше gap → буфер переполнился, его глубина ≈ recovered.
Это ровно наш v1-сценарий (foreground после простоя при живом коннекте) и задаёт реальный
предел backfill (риск 5 плана).

Полностью автоматический — БЕЗ ручного Ctrl+C; паузы с прогрессом в консоль.

Запуск (USB на macOS требует root):
  sudo /Users/vadimkz/Projects/radiacode/.venv/bin/python \
       /Users/vadimkz/atomapp-ios/radiacode_stuff/phase0/h14_buffer_depth.py --gaps 60,180,300,600

Результаты заносить в docs/radiacode-ble-protocol.md §14 и docs/radiacode-ble-integration-plan.md.
"""
import argparse
from collections import Counter
import time

from radiacode import RadiaCode
from radiacode.types import RealTimeData


def drain(rc, settle_reads=2, max_reads=20000):
    """Вычитать DATA_BUF досуха. Возвращает список всех записей.

    «Досуха» = подряд `settle_reads` пустых чтений (новых записей нет). data_buf()
    вычитывает инкрементально (курсор прибора двигается), поэтому цикл сходится.
    """
    out = []
    empties = 0
    for _ in range(max_reads):
        batch = rc.data_buf()
        if batch:
            out.extend(batch)
            empties = 0
        else:
            empties += 1
            if empties >= settle_reads:
                break
            time.sleep(0.2)
    return out


def summarize(recs):
    by_type = Counter(type(r).__name__ for r in recs)
    n_rts = sum(1 for r in recs if isinstance(r, RealTimeData))
    dts = [r.dt for r in recs if hasattr(r, "dt")]
    span = (max(dts) - min(dts)).total_seconds() if len(dts) >= 2 else 0.0
    return by_type, n_rts, span


def idle_with_progress(gap, step=15):
    t0 = time.time()
    while True:
        elapsed = time.time() - t0
        if elapsed >= gap:
            break
        time.sleep(min(step, gap - elapsed))
        print(f"       ... idled {int(time.time() - t0)}/{gap}s", flush=True)


def main():
    ap = argparse.ArgumentParser(description="H14 — глубина DATA_BUF (идл-свип)")
    ap.add_argument("--gaps", default="60,180,300,600",
                    help="список пауз в секундах через запятую (default 60,180,300,600)")
    ap.add_argument("--serial", default=None, help="serial номер прибора (если их несколько)")
    args = ap.parse_args()
    gaps = [int(x) for x in args.gaps.split(",") if x.strip()]

    print("[H14] connect ...")
    rc = RadiaCode(serial_number=args.serial)
    print(f"[H14] device={rc.serial_number()} fw_target={rc.fw_version()[1]}")
    for line in rc.configuration().split("\n"):
        if line.startswith(("DBLag_ms", "MinFrmPeriod_ms", "FrameTimeStep_us")):
            print(f"       CONFIG {line.strip()}")

    print("\n[H14] начальный дренаж буфера (выходим на live-край) ...")
    init = drain(rc)
    print(f"       сброшено {len(init)} записей\n")

    results = []
    for gap in gaps:
        print(f"[H14] idle {gap}s (не читаем; прибор копит в кольцевой буфер) ...")
        idle_with_progress(gap)
        recs = drain(rc)
        by_type, n_rts, span = summarize(recs)
        results.append((gap, len(recs), n_rts, span, dict(by_type)))
        print(f"       recovered: total={len(recs)}  RealTimeData={n_rts}  "
              f"dt_span={span:.0f}s  by_type={dict(by_type)}\n")

    print("=" * 70)
    print("[H14] СВОДКА (RealTimeData ≈ секунд истории, прибор отдаёт ~1/с):")
    print(f"{'gap,s':>8} {'RTData':>8} {'total':>8} {'dt_span,s':>10}  вывод")
    depth_hint = None
    for gap, total, n_rts, span, _ in results:
        overflow = n_rts < gap * 0.8
        verdict = "ВОЗМОЖНО переполнение" if overflow else "буфер вместил всё"
        if overflow and depth_hint is None:
            depth_hint = n_rts
        print(f"{gap:>8} {n_rts:>8} {total:>8} {span:>10.0f}  {verdict}")
    print("-" * 70)
    if depth_hint is not None:
        print(f"[H14] оценка глубины буфера ≈ {depth_hint} с истории "
              f"(RealTimeData перестал расти пропорционально gap).")
    else:
        print(f"[H14] переполнения не видно до gap={gaps[-1]}s → буфер глубже последнего "
              f"теста; повторите с бо́льшими --gaps.")
    print("[H14] числа → radiacode-ble-protocol.md §14.6 + план §«История валидации».")


if __name__ == "__main__":
    main()
