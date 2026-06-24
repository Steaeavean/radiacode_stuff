# Time Synchronisation — `timesync.py`

[Русская версия](#синхронизация-времени--timesyncpy)

---

## Overview

`timesync.py` (repo root) connects to a RadiaCode device over BLE and
synchronises its real-time clock (RTC) to the macOS (or Linux/Windows) system
time.

The library already calls `set_local_time(now)` automatically on every
`RadiaCode()` connect, so **manual invocation is only needed** when:

- The device display shows a wrong time (e.g. UTC instead of local time).
- You want explicit confirmation of the sync with a drift report.
- You want to schedule periodic sync via cron.

---

## Root cause of the 3-hour drift

The official RadiaCode Android app sets the device RTC to **UTC** when it
connects.  If your timezone is UTC+3, the device display will show
`09:00` instead of `12:00` until a Python-library connection re-syncs it.

The Python library always uses `datetime.datetime.now()` (local time) for
`SET_TIME`, so every connect corrects the drift automatically.

**Important:** `DATA_BUF` timestamps in Python are always correct regardless of
the device RTC.  The library reconstructs them from an elapsed counter
(`DEVICE_TIME`) and a `base_time = datetime.now() + 128 s` anchor — completely
independent of what the device's RTC shows.  The RTC only affects the
device's own display.

---

## Usage

```bash
cd /path/to/radiacode_stuff

# Auto-scan for any nearby RadiaCode:
uv run python timesync.py

# Connect by known CoreBluetooth UUID (fastest, no scan delay):
uv run python timesync.py --bluetooth-address XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX

# Report only — do not write SET_TIME:
uv run python timesync.py --dry-run

# Custom drift threshold (default 5 s):
uv run python timesync.py --threshold 30

# Always sync regardless of estimated drift:
uv run python timesync.py --threshold 0
```

### Example output

```
System time:  2026-06-24 12:30:40
Connecting...
Device:       RC-101-005265  fw 4.14
Latest record:2026-06-24 12:30:37  (drift +3s)

Synced:       device RTC → 2026-06-24 12:30:49
```

---

## Scheduling with cron

To keep the device clock in sync automatically (e.g. every hour while the Mac
is awake):

```bash
crontab -e
```

Add a line (adjust the path and UUID):

```cron
# Sync RadiaCode RTC every hour
0 * * * * cd /Users/you/radiacode_stuff && \
  /Users/you/radiacode_stuff/.venv/bin/python timesync.py \
  --bluetooth-address 62B635D0-CFAA-1B4C-204F-D1837DEF3F68 \
  --threshold 0 >> /tmp/radiacode_timesync.log 2>&1
```

Or on every macOS login (add to `~/.zprofile` or `~/.zshrc`):

```bash
# Sync RadiaCode clock in the background on login
(cd ~/radiacode_stuff && uv run python timesync.py \
  --bluetooth-address 62B635D0-CFAA-1B4C-204F-D1837DEF3F68 \
  --threshold 0 &)
```

---

## Technical notes

- The device firmware exposes `SET_TIME` (command `0x0A04`) but **no `GET_TIME`
  command**.  Reading the RTC is not possible; the script infers drift from
  `DATA_BUF` record timestamps.
- `DEVICE_TIME` (VSFR `0x0504`) is an elapsed counter reset to 0 on every
  `device_time(0)` call (done in `RadiaCode.__init__`).  It is not the RTC.
- Drift shown in `timesync.py` reflects BLE latency and the `sleep(3)` wait,
  not actual RTC error — expect ±20 s.  The important sync is the `SET_TIME`
  write, not the drift number.

---
---

# Синхронизация времени — `timesync.py`

[English version](#time-synchronisation--timesyncpy)

---

## Обзор

`timesync.py` (корень репозитория) подключается к устройству RadiaCode по BLE
и синхронизирует его часы реального времени (RTC) с системным временем
macOS (или Linux/Windows).

Библиотека уже автоматически вызывает `set_local_time(now)` при каждом
`RadiaCode()`, поэтому **ручной запуск нужен только когда**:

- Дисплей устройства показывает неправильное время (например, UTC вместо
  местного).
- Нужно явное подтверждение синхронизации с отчётом о дрейфе.
- Вы хотите запускать синхронизацию по расписанию через cron.

---

## Причина дрейфа на 3 часа

Официальное Android-приложение RadiaCode устанавливает RTC устройства в
**UTC** при подключении. Если вы находитесь в UTC+3, дисплей будет показывать
`09:00` вместо `12:00` до тех пор, пока Python-библиотека не переподключится
и не скорректирует время.

Python-библиотека всегда использует `datetime.datetime.now()` (местное время)
для `SET_TIME`, поэтому каждое подключение автоматически корректирует дрейф.

**Важно:** метки времени `DATA_BUF` в Python всегда корректны независимо от RTC
устройства. Библиотека восстанавливает их из счётчика elapsed (`DEVICE_TIME`) и
опорной точки `base_time = datetime.now() + 128 с` — полностью независимо от
того, что показывают часы на дисплее устройства. RTC влияет только на дисплей
прибора.

---

## Использование

```bash
cd /path/to/radiacode_stuff

# Автопоиск ближайшего RadiaCode:
uv run python timesync.py

# Подключение по известному CoreBluetooth UUID (быстро, без сканирования):
uv run python timesync.py --bluetooth-address XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX

# Только отчёт, без записи SET_TIME:
uv run python timesync.py --dry-run

# Порог дрейфа вручную (по умолчанию 5 с):
uv run python timesync.py --threshold 30

# Синхронизировать всегда, независимо от оценки дрейфа:
uv run python timesync.py --threshold 0
```

### Пример вывода

```
System time:  2026-06-24 12:30:40
Connecting...
Device:       RC-101-005265  fw 4.14
Latest record:2026-06-24 12:30:37  (drift +3s)

Synced:       device RTC → 2026-06-24 12:30:49
```

---

## Запуск по расписанию через cron

Для автоматической синхронизации часов (например, каждый час пока Mac активен):

```bash
crontab -e
```

Добавьте строку (замените путь и UUID):

```cron
# Синхронизация RTC RadiaCode каждый час
0 * * * * cd /Users/you/radiacode_stuff && \
  /Users/you/radiacode_stuff/.venv/bin/python timesync.py \
  --bluetooth-address 62B635D0-CFAA-1B4C-204F-D1837DEF3F68 \
  --threshold 0 >> /tmp/radiacode_timesync.log 2>&1
```

Или при каждом входе в macOS (в `~/.zprofile` или `~/.zshrc`):

```bash
# Синхронизация часов RadiaCode в фоне при логине
(cd ~/radiacode_stuff && uv run python timesync.py \
  --bluetooth-address 62B635D0-CFAA-1B4C-204F-D1837DEF3F68 \
  --threshold 0 &)
```

---

## Технические детали

- Прошивка устройства предоставляет `SET_TIME` (команда `0x0A04`), но
  **команды `GET_TIME` нет**. Прочитать RTC напрямую невозможно; скрипт
  оценивает дрейф по меткам времени записей `DATA_BUF`.
- `DEVICE_TIME` (VSFR `0x0504`) — счётчик elapsed, сбрасываемый в 0 при
  каждом вызове `device_time(0)` (выполняется в `RadiaCode.__init__`).
  Это не RTC.
- Дрейф, показываемый `timesync.py`, отражает BLE latency и паузу `sleep(3)`,
  а не фактическую ошибку RTC — ожидайте ±20 с. Главное — сама запись
  `SET_TIME`, а не цифра дрейфа.
