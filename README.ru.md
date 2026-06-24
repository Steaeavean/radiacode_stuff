# Библиотека RadiaCode для Python

[![PyPI version](https://img.shields.io/pypi/v/radiacode)](https://pypi.org/project/radiacode)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

[English README](README.md)

Python-библиотека для управления дозиметрами-спектрометрами
[RadiaCode-10x](https://www.radiacode.com/): считывание измерений в реальном
времени, получение гамма-спектра, настройка устройства по USB или Bluetooth.

## Возможности

- Измерения мощности дозы и счётной скорости в реальном времени
- Получение и анализ гамма-спектра
- Подключение по USB и Bluetooth (macOS, Linux, Windows)
- Встроенный веб-интерфейс с графиком в реальном времени
- Управление настройками устройства

## Демо

Интерактивный веб-интерфейс ([бэкенд](src/radiacode/examples/webserver.py) | [фронтенд](src/radiacode/examples/webserver.html)):

![radiacode-webserver-example](./screenshot.png)

## Быстрый старт

### Установка

```bash
pip install --upgrade radiacode
# или вместе с зависимостями для примеров:
pip install --upgrade 'radiacode[examples]'
```

### Запуск примеров

```bash
# USB (все платформы)
python3 -m radiacode.examples.basic

# Bluetooth — macOS / Windows (через bleak, без sudo)
python3 -m radiacode.examples.basic --bluetooth-name RadiaCode

# Bluetooth — Linux (через bluepy, укажите MAC)
python3 -m radiacode.examples.basic --bluetooth-mac 52:43:01:02:03:04

# Веб-интерфейс по USB
python3 -m radiacode.examples.webserver
# Веб-интерфейс по Bluetooth (macOS)
python3 -m radiacode.examples.webserver --bluetooth-name RadiaCode
```

### Использование библиотеки

```python
from radiacode import RadiaCode, RealTimeData

# Подключение (по умолчанию — USB)
device = RadiaCode()

# Считывание текущих измерений
data = device.data_buf()
for record in data:
    if isinstance(record, RealTimeData):
        print(f"Мощность дозы: {record.dose_rate}")

# Получение спектра
spectrum = device.spectrum()
print(f"Время накопления: {spectrum.duration}с")
print(f"Суммарных отсчётов: {sum(spectrum.counts)}")

# Настройка устройства
device.set_display_brightness(5)  # яркость 0–9
device.set_language('ru')         # 'ru' или 'en'
```

#### Bluetooth-подключение

```python
# macOS / Windows — автопоиск ближайшего RadiaCode
device = RadiaCode(bluetooth_name='RadiaCode')

# macOS — подключение по CoreBluetooth UUID (стабильный идентификатор)
device = RadiaCode(bluetooth_address='XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX')

# Linux — подключение по MAC-адресу (через bluepy)
device = RadiaCode(bluetooth_mac='52:43:01:02:03:04')

# USB — подключение к конкретному устройству по серийному номеру
device = RadiaCode(serial_number='RC-101-xxxxxx')
```

#### Дополнительные возможности

```python
# Энергетическая калибровка
coefs = device.energy_calib()
print(f"Коэффициенты калибровки: {coefs}")

# Сброс накопленных данных
device.dose_reset()
device.spectrum_reset()

# Управление поведением устройства
device.set_sound_on(True)
device.set_vibro_on(True)
device.set_display_off_time(30)  # автовыключение дисплея через 30 с
```

## Настройка для разработки

1. Установить uv:

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Клонировать репозиторий:

   ```bash
   git clone https://github.com/Steaeavean/radiacode_stuff.git
   cd radiacode_stuff
   ```

3. Запустить пример:

   ```bash
   uv run python -m radiacode.examples.basic
   ```

## Платформенные особенности

### macOS

- USB работает из коробки.
- Bluetooth (BLE) **полностью поддерживается** через [bleak](https://github.com/hbldh/bleak)
  (CoreBluetooth). Sudo не требуется.
- Требуется: `brew install libusb`

**Подключение по BLE на macOS:**

CoreBluetooth не раскрывает MAC-адреса устройств. Используйте одно из:

```bash
# Автоматический поиск — найдёт ближайший RadiaCode
python3 -m radiacode.examples.basic --bluetooth-name RadiaCode

# Конкретное устройство по CoreBluetooth UUID
python3 -m radiacode.examples.basic --bluetooth-address XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
```

Как узнать CoreBluetooth UUID вашего устройства:

```python
import asyncio
from bleak import BleakScanner

async def scan():
    devices = await BleakScanner.discover(timeout=5)
    for d in devices:
        if 'RadiaCode' in (d.name or ''):
            print(d.address, d.name)

asyncio.run(scan())
```

### Linux

- USB и Bluetooth полностью поддерживаются.
- USB: возможно, потребуются [udev-правила](radiacode.rules) для работы без root.
- Bluetooth: используется [bluepy](https://github.com/IanHarvey/bluepy) (`bluetooth_mac=`),
  требует Bluetooth-библиотеки (`sudo apt install libbluetooth-dev`) и обычно root.

### Windows

- USB поддерживается.
- Bluetooth поддерживается через [bleak](https://github.com/hbldh/bleak).
  Используйте `bluetooth_name` или `bluetooth_address`.
- Требуются USB-драйверы (WinUSB через Zadig или драйвер производителя).

## Миграция с `bluetooth_mac` (Linux/bluepy) на `bluetooth_name` (macOS/bleak)

| Старый вариант (Linux, bluepy) | Новый вариант (macOS/Windows, bleak) |
|---|---|
| `RadiaCode(bluetooth_mac='AA:BB:CC...')` | `RadiaCode(bluetooth_name='RadiaCode')` |
| `--bluetooth-mac AA:BB:CC...` | `--bluetooth-name RadiaCode` |

Публичный API `RadiaCode` (все методы кроме конструктора) идентичен для обоих транспортов.

## Утилиты

### `timesync.py` — Синхронизация часов устройства

Библиотека автоматически вызывает `set_local_time(now)` при каждом подключении,
но `timesync.py` делает синхронизацию явной и выводит подтверждение с оценкой
дрейфа.

**Когда использовать:** если дисплей устройства показывает UTC вместо местного
времени (типично после последнего подключения официального Android-приложения
RadiaCode — оно устанавливает UTC).

```bash
# Автоматический поиск (любой ближайший RadiaCode):
uv run python timesync.py

# Быстрое подключение по известному CoreBluetooth UUID:
uv run python timesync.py --bluetooth-address XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX

# Только отчёт о дрейфе, без синхронизации:
uv run python timesync.py --dry-run
```

Подробнее — [docs/TIMESYNC.md](docs/TIMESYNC.md) (настройка cron, технические детали).

## Скрипты валидации Phase 0

Каталог `phase0/` содержит BLE-скрипты, использовавшиеся для проверки
wire-протокола на macOS до интеграции bleak-транспорта в библиотеку.
Полезны для низкоуровневой отладки, тестирования гипотез и длительных soak-прогонов:

| Скрипт | Назначение |
|---|---|
| `ble_transport.py` | Async bleak-транспорт + командный уровень (эталонная реализация) |
| `ble_soak.py` | Многочасовой BLE soak-логгер (циклы переподключения / простоя) |
| `analyze_soak.py` | Генератор offline-отчётов по soak-логам |
| `busy_probe.py` | H5/H21: флаг connectable, состояние busy/free |
| `h16_search_probe.py` | H16/H17: каденция RealTimeData / RareData |
| `dose_compare.py` | H18: сравнение накопления дозы |
| `spectrum_probe.py` | H20: спектр через BLE |
| `h14_buffer_depth.py` | H14: глубина DATA_BUF (USB, требует sudo) |
| `h15_reconnect.py` | H15: сохранение состояния после переподключения (USB) |

Запуск:

```bash
cd phase0
uv sync          # устанавливает bleak + in-repo пакет radiacode
caffeinate -is uv run python ble_soak.py --hours 1
```

## Лицензия

Проект распространяется под лицензией MIT — см. файл [LICENSE](LICENSE).
