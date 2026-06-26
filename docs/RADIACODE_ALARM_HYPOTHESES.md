# RadiaCode alarm hypotheses (H1–H16)

Live-validation checklist for RadiaCode-1xx **on-device alarm limits** before
shipping atomapp-ios threshold UI (page-1 DER strip + page-4 read-only alarms).
Distinct from the general BLE transport hypotheses H1–H15 in
`atomapp-ios/docs/radiacode-ble-protocol.md` §14.

**Scripts:** `read_alarm_limits.py` (read + fixture), `validate_alarm_limits.py`
(round-trip H5–H9), shared `alarm_probe_lib.py`.

**RC-101 fixture** (serial `RC-101-005265`, do not change on device):

| Register | Display (native) | Raw | Atom internal |
|----------|------------------|-----|---------------|
| DR L1/L2 | 0.35 / 0.6 µSv/h | 35 / 60 µR/h | 350 / 600 nSv/h |
| DS L1/L2 | 0.25 / 0.3 mSv | 25000 / 30000 µR | 250000 / 300000 nSv |
| CR L1/L2 | 30 / 60 cps | 300 / 600 cp/10s | — (info) |
| DS_UNITS | Sv (µSv/h) | 1 | — |
| CR_UNITS | cps | 0 | — |

---

## English

| ID | Hypothesis | How to verify | Script / artefact |
|----|------------|---------------|-------------------|
| **H1** | `RD_VIRT_SFR_BATCH` reads all 8 alarm registers; `valid_flags = 0xFF` | Batch read returns 8 values, flags `0x000000FF` | `read_alarm_limits.py` |
| **H2** | Threshold VSFRs (`DR_LEV*`, `DS_LEV*`, `CR_LEV*`) are **uint32**, not IEEE-754 float | Raw words are small integers; float misread → denormal ≈ 0 | `read_alarm_limits.py` float demo |
| **H3** | `DR_LEV*_uR_h` store **physical µR/h** independent of `DS_UNITS` display selector | Raw DR matches native µSv/h × 100 (Sv mode) | `read_alarm_limits.py` + fixture |
| **H4** | `DS_LEV*_uR` store **physical µR** independent of display unit | Raw DS matches native mSv × 1e5 | fixture |
| **H5** | `DS_UNITS` value lives in **low byte** (`raw & 0x1`); `1` = Sv, `0` = R | Low byte matches native app unit; lib `get_alarm_limits().dose_unit` may be wrong | `validate_alarm_limits.py` H5 |
| **H6** | `CR_UNITS` value lives in **low byte** (`raw & 0x1`); `0` = cps, `1` = cpm | Low byte matches native count display | `validate_alarm_limits.py` H6 |
| **H7** | Atom mapping: `nSv/h = DR_uR_h × 10`, `nSv = DS_uR × 10` (100 Sv/R) | Mapped values match §11.5 table | `validate_alarm_limits.py` H7 |
| **H8** | `radiacode.get_alarm_limits()` **numeric** thresholds match raw decode (unit **labels** may be wrong) | Back-calc from `AlarmLimits` equals raw integers | `validate_alarm_limits.py` H8 |
| **H9** | `set_alarm_limits()` round-trip via `WR_VIRT_SFR_BATCH` writes and reads back | Write +1 µR/h on DR L1/L2, read back, restore snapshot | `validate_alarm_limits.py --write-test` |
| **H10** | `SOUND_CTRL` mask in low **uint16** (`raw & 0xFFFF`) | Decode matches enabled sounds on device | `read_alarm_limits.py` |
| **H11** | `VIBRO_CTRL` mask in low **uint8** (`raw & 0xFF`) | Decode matches enabled vibro on device | `read_alarm_limits.py` |
| **H12** | `ALARM_MODE`: `0` = **Once** on fw 4.14 (empirical RC-101) | Read register when native app shows Once | `read_alarm_limits.py` |
| **H13** | RC-101 fixture registers unchanged (stable ground truth) | `--compare-fixture` exit 0 | `read_alarm_limits.py --compare-fixture` |
| **H14** | Float misread of uint32 threshold causes false alarm UI (value > 0 displays as 0) | Demo: int 35 → float ~4.9e-44 | `read_alarm_limits.py` |
| **H15** | BLE and USB return **identical raw** alarm words on same device | Run both transports, diff JSON | `read_alarm_limits.py --json` |
| **H16** | Partial threshold write does not clobber unspecified registers | Snapshot before/after `set_alarm_limits` two DR fields only | `validate_alarm_limits.py --write-test` restore check |

### Recommended order

1. `uv run python read_alarm_limits.py --compare-fixture` — H1–H4, H10–H14, H13  
2. `uv run python validate_alarm_limits.py` — H5–H8 (read-only)  
3. `uv run python validate_alarm_limits.py --write-test` — H9, H16  
4. Repeat step 1 over USB if BLE-only so far — H15  

### atomapp-ios gate

Do not ship threshold read UI until **H1–H9** and **H13** are ✅ on target
hardware. H10–H12 are required only for future per-alarm sound/vibro surfacing.

---

## Live validation results (RC-101-005265, 2026-06-26)

Device: `RC-101-005265`, BLE UUID `62B635D0-CFAA-1B4C-204F-D1837DEF3F68`, MCU fw 4.14.
Scripts: `read_alarm_limits.py`, `validate_alarm_limits.py`. Library **0.4.4** (H8 fix).

| ID | Result | Notes |
|----|--------|-------|
| H1 | ✅ | `valid_flags=0xFF` |
| H2 | ✅ | Raw uint32 integers |
| H3 | ✅ | DR 35/60 µR/h |
| H4 | ✅ | DS 25000/30000 µR |
| H5 | ✅ | `DS_UNITS` low byte = 1 |
| H6 | ✅ | `CR_UNITS` low byte = 0 |
| H7 | ✅ | 350/600 nSv/h, 250k/300k nSv |
| H8 | ✅ | Live 0.4.4: all 8 checks PASS incl. `dose_unit_label`/`count_unit_label` (was FAIL on 0.4.3) |
| H9 | ✅ | DR write 36/61, restore OK (`--write-test`, 0.4.3 run) |
| H10 | ✅* | `SOUND_CTRL=0x031F` |
| H11 | ✅* | `VIBRO_CTRL=0x01` |
| H12 | ✅* | `ALARM_MODE=0` (Once) |
| H13 | ✅ | Fixture compare exit 0 |
| H14 | ✅ | int 35 → float 4.905e-44 |
| H15 | ⏳ | USB connect failed on test Mac; BLE-only sufficient for iOS |
| H16 | ✅ | Post-restore snapshot match |

**atomapp-ios gate (BLE): CLOSED** — H1–H9 + H13 ✅ on RC-101-005265.

---

## Русский

| ID | Гипотеза | Как проверить | Скрипт |
|----|----------|---------------|--------|
| **H1** | Батч-чтение 8 регистров порогов; `valid_flags = 0xFF` | 8 значений, флаги `0xFF` | `read_alarm_limits.py` |
| **H2** | Пороги — **uint32**, не float | Малые целые; float-ошибка → ≈0 в UI | float demo |
| **H3** | `DR_LEV*` — физические **µR/ч**, не зависят от `DS_UNITS` | Сырые DR = µSv/ч × 100 | fixture |
| **H4** | `DS_LEV*` — физические **µR** | Сырые DS = mSv × 1e5 | fixture |
| **H5** | `DS_UNITS` в **младшем байте**; `1` = Зв | `raw & 1`; либа может ошибаться | `validate_alarm_limits.py` |
| **H6** | `CR_UNITS` в младшем байте; `0` = cps | `raw & 1` | `validate_alarm_limits.py` |
| **H7** | Atom: `нЗв/ч = µR/ч × 10`, `нЗв = µR × 10` | Сверка с §11.5 | `validate_alarm_limits.py` |
| **H8** | Числа `get_alarm_limits()` = сырой декод (метки единиц могут врать) | Обратный пересчёт | `validate_alarm_limits.py` |
| **H9** | Запись `set_alarm_limits()` круговая | DR +1, чтение, restore | `--write-test` |
| **H10** | `SOUND_CTRL` — младшие 16 бит | Маска vs звуки в нативном приложении | `read_alarm_limits.py` |
| **H11** | `VIBRO_CTRL` — младший байт | Маска vs вибро | `read_alarm_limits.py` |
| **H12** | `ALARM_MODE = 0` → **Once** (fw 4.14) | Режим в приложении RadiaCode | `read_alarm_limits.py` |
| **H13** | Фикстура RC-101 не менялась | `--compare-fixture` exit 0 | `read_alarm_limits.py` |
| **H14** | Float-ошибка → ложный аларм (значение >0, UI «0») | int 35 → denormal | demo |
| **H15** | BLE и USB — одинаковые сырые слова | JSON с двух транспортов | `--json` |
| **H16** | Частичная запись не портит остальные регистры | snapshot restore после теста | `--write-test` |

### Порядок

1. `read_alarm_limits.py --compare-fixture`  
2. `validate_alarm_limits.py` (без записи)  
3. `validate_alarm_limits.py --write-test`  
4. Повторить по USB для H15, если до этого только BLE  

### Ворота для atomapp-ios

Не выпускать UI порогов, пока **H1–H9** и **H13** не ✅ на целевом приборе.

---

## Результаты прогона (RC-101-005265, 2026-06-26)

См. таблицу в English-секции выше. H8 подтверждён на железе в **0.4.4** (все 8 sub-checks PASS).
H15 (USB) не проверен — для iOS достаточно BLE. **Ворота atomapp-ios (BLE): закрыты.**

---

## References

- `atomapp-ios/docs/radiacode-ble-protocol.md` §11 (alarm limits, byte layout, fixture §11.5)
- `atomapp-ios/.cursor/skills/atom-radiacode/SKILL.md` — Alarm-limits ground-truth fixture
- `src/radiacode/radiacode.py` — `get_alarm_limits` / `set_alarm_limits`
