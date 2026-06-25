# RadiaCode Library — Examples

[Описание на русском языке](README_ru.md)

These example projects are installed with the library when you run
`pip install 'radiacode[examples]'`.  Each example prints usage with `--help`.

**Bluetooth connection options (all examples):**

| Flag | Platform | Description |
|---|---|---|
| `--bluetooth-name PREFIX` | macOS, Windows, Linux | BLE auto-scan by name prefix, e.g. `RadiaCode` |
| `--bluetooth-address UUID` | macOS, Windows, Linux | Connect to specific device by CoreBluetooth UUID or address |
| `--bluetooth-mac AA:BB:CC...` | Linux only | Connect via bluepy MAC address |

### Security (webserver example)

The web dashboard listens on **`127.0.0.1` by default** (loopback only). If you pass
`--listen-host 0.0.0.0` to expose it on the LAN, note that **`POST /spectrum/reset` has
no authentication** — anyone who can reach the port can reset the device spectrum.
Use only on a trusted network; do not expose this example to the public Internet.

---

### 1. [basic.py](./basic.py)

Minimal example: serial number, firmware, spectrum and continuous data_buf loop.

```bash
# USB
python3 -m radiacode.examples.basic

# macOS/Windows — BLE auto-scan
python3 -m radiacode.examples.basic --bluetooth-name RadiaCode

# Linux — BLE via bluepy
python3 -m radiacode.examples.basic --bluetooth-mac 52:43:01:02:03:04
```

### 2. [webserver.py](./webserver.py) & [webserver.html](./webserver.html)

Real-time spectrum and dose-rate web dashboard with WebSocket updates.

```bash
# USB
python3 -m radiacode.examples.webserver --listen-port 8080

# macOS/Windows — BLE
python3 -m radiacode.examples.webserver --bluetooth-name RadiaCode --listen-port 8080
```

### 3. [narodmon.py](./narodmon.py)

Sends measurements to the public monitoring project [narodmon.ru](https://narodmon.ru).

```bash
# Linux
python3 -m radiacode.examples.narodmon --bluetooth-mac 52:43:01:02:03:04

# macOS/Windows
python3 -m radiacode.examples.narodmon --bluetooth-name RadiaCode
```

### 4. [radiacode-exporter.py](./radiacode-exporter.py)

Exports metrics for [Prometheus](https://prometheus.io/).

```bash
python3 -m radiacode.examples.radiacode-exporter --bluetooth-name RadiaCode --port 5432
curl http://127.0.0.1:5432/metrics
```

### 5. [show-spectrum.py](./show-spectrum.py)

Animated terminal display of the differential and cumulative gamma spectrum,
with optional YAML export.

```bash
python3 -m radiacode.examples.show-spectrum --bluetooth-name RadiaCode
python3 -m radiacode.examples.show-spectrum --bluetooth-name RadiaCode -f spectrum_run
```
