# Примеры использования библиотеки

Устанавливаются с пакетом: `pip install 'radiacode[examples]'`

У каждого примера есть справка по `--help`.

**Варианты Bluetooth-подключения:**

| Флаг | Платформа | Описание |
|---|---|---|
| `--bluetooth-name ПРЕФИКС` | macOS, Windows, Linux | Авто-поиск по имени, например `RadiaCode` |
| `--bluetooth-address UUID` | macOS, Windows, Linux | Подключение по CoreBluetooth UUID или адресу |
| `--bluetooth-mac AA:BB:CC...` | только Linux | Подключение через bluepy по MAC-адресу |

---

### 1. [basic.py](./basic.py)

Минимальный пример: серийный номер, прошивка, спектр и непрерывный data_buf.

```bash
# USB
python3 -m radiacode.examples.basic

# macOS/Windows — BLE
python3 -m radiacode.examples.basic --bluetooth-name RadiaCode

# Linux — BLE через bluepy
python3 -m radiacode.examples.basic --bluetooth-mac 52:43:01:02:03:04
```

### 2. [webserver.py](./webserver.py) & [webserver.html](./webserver.html)

Веб-интерфейс с реальным временем: спектр и мощность дозы.

```bash
python3 -m radiacode.examples.webserver --bluetooth-name RadiaCode --listen-port 8080
```

### 3. [narodmon.py](./narodmon.py)

Отправка измерений в [народный мониторинг narodmon.ru](https://narodmon.ru).

```bash
# macOS/Windows
python3 -m radiacode.examples.narodmon --bluetooth-name RadiaCode

# Linux
python3 -m radiacode.examples.narodmon --bluetooth-mac 52:43:01:02:03:04
```

### 4. [radiacode-exporter.py](./radiacode-exporter.py)

Экспорт метрик для [Prometheus](https://prometheus.io/).

```bash
python3 -m radiacode.examples.radiacode-exporter --bluetooth-name RadiaCode --port 5432
```

### 5. [show-spectrum.py](./show-spectrum.py)

Анимированный дифференциальный и накопленный гамма-спектр с опциональным YAML-экспортом.

```bash
python3 -m radiacode.examples.show-spectrum --bluetooth-name RadiaCode
```
