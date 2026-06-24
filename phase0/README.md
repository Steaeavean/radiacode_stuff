# Phase 0 hardware-validation scripts (RadiaCode BLE integration)

Автоматизированные скрипты для закрытия **оставшихся** гипотез Фазы 0 плана интеграции
RadiaCode по BLE — **H14** (глубина кольцевого `DATA_BUF`) и **H15** (поведение прибора при
reconnect). Сделаны так, чтобы **не** требовать ручного «жди 15–20 минут и жми Ctrl+C» —
паузы автоматические, с прогрессом в консоль.

Связанные документы:
- План: [`../../docs/radiacode-ble-integration-plan.md`](../../docs/radiacode-ble-integration-plan.md) → § «Фаза 0».
- Протокол + уже снятые результаты: [`../../docs/radiacode-ble-protocol.md` §14](../../docs/radiacode-ble-protocol.md#14-валидация-на-железе-фаза-0-rc-110-fw-414).

> Это про **живой обмен** с прибором (USB/BLE), **не** про файлы `.rctrk` (те — в
> `radiacode_stuff/` уровнем выше).

## Предусловия (macOS)

USB-путь reference-либы `cdump/radiacode` (BLE на macOS у либы — заглушка, `bluepy`
Linux-only; прикладной слой USB и BLE идентичен, поэтому H14/H15 валидны для BLE):

```zsh
brew install libusb
cd /Users/vadimkz/Projects/radiacode
uv sync --extra examples          # создаёт .venv с pyusb
```

Прибор подключить **USB-C дата-кабелем**. USB на macOS требует root → запуск под `sudo`.
Скрипты используют интерпретатор из venv клона (там установлен `radiacode`):

```zsh
RCPY=/Users/vadimkz/Projects/radiacode/.venv/bin/python
HERE=/Users/vadimkz/atomapp-ios/radiacode_stuff/phase0
```

## H14 — глубина кольцевого `DATA_BUF`

Идл-свип: держит **одно** USB-соединение открытым, не читает буфер `gap` секунд (прибор
копит в свой кольцевой буфер), затем вычитывает всё и меряет, сколько истории вернулось.
Если `recovered < gap` → буфер переполнился, его глубина ≈ `recovered`. Это ровно наш
v1-сценарий (foreground после простоя при живом коннекте) → задаёт реальный предел backfill.

```zsh
sudo $RCPY $HERE/h14_buffer_depth.py --gaps 60,180,300,600
```

- `--gaps` — список пауз (с) через запятую. По умолчанию `60,180,300,600`.
- Подсказка из `CONFIGURATION`: `DBLag_ms=180000` (180 c) — вероятный порядок глубины.
- Вывод: таблица `gap → recovered RealTimeData (≈ секунд истории) / total / dt_span` +
  эвристическая оценка глубины. Числа занести в протокол-док §14.6 и план §«История».

## H15 — поведение при reconnect

Снимает `RareData` (накопленная доза/`duration`) **до** и **после** разрыва соединения:
прибор **продолжает** свою сессию или **обнуляет**? Влияет на baseline-shift в Фазе 3
(маппинг в `AMDosimeter`).

```zsh
sudo $RCPY $HERE/h15_reconnect.py --gap 20 --cycles 1 --rare-timeout 300
```

- `--gap` — пауза между disconnect и reconnect (с). `--cycles` — число повторов (default 1).
- `--rare-timeout` — макс. ожидание записи `RareData` (default **300**). ⚠️ DB-группы
  (`RareData`/`DoseRateDB`) пишутся на медленной каденции **~`DBLag_ms`=180 c**, поэтому
  timeout обязан быть **> 180 c** — иначе проба промахивается (так первый прогон с 120 c дал
  сплошной timeout). Если и 300 c мало — поднять до 600. Один цикл ≈ 10–11 мин (две паузы по
  `rare-timeout` + gap), но всё автоматически, без Ctrl+C; во время ожидания печатается
  heartbeat с числом полученных записей (видно, что коннект жив).
- На USB «разрыв» = `usb.util.dispose_resources` + повторное открытие (прокси к
  BLE-дисконнекту: проверяем поведение **firmware-сессии**, не транспорт).
- Вывод: `Δdose`/`Δduration` + вердикт «ПРОДОЛЖАЕТ / ОБНУЛЯЕТ».
- ⚠️ **Итог прогонов 2026-06-15: USB-проба H15 оказалась непрактичной.** После того как
  лаговый backlog `RareData` вычитан первым коннектом, новая `RareData` не приходит даже за
  **2×300 c** (период DB-записей > 5 мин; поток `RealTimeData` при этом идёт). Поэтому
  сравнить dose/duration до/после reconnect по USB в разумное время не выходит. **H15 закрывать
  на iPhone** (реальная BLE-сессия: дождаться первой `RareData`, дисконнект/реконнект,
  сравнить) либо очень длинным захватом (`--rare-timeout 900+`). Косвенно прибор «продолжает»
  (persistent-счётчик 221 день, протокол §14.4).

## BLE soak (многочасовой лог + финал Фазы 0)

Закрывает остаток Фазы 0 **на macOS по BLE** реальными данными: H1/H2 (реклама
сервиса/имя), H3 (MTU), H4 (CCCD), **BLE-часть H8** (нарезка write + склейка
нотификаций — «по построению»), **H15** (dose/duration до/после reconnect через
post-connect drain), плюс числа: реальный период `RareData`, дрейф заряда/температуры,
период заворота `ts`. Транспорт — **bleak/CoreBluetooth** (у `cdump/radiacode` BLE на
macOS заглушка; command/декодеры переиспользуем из либы).

Файлы: `ble_transport.py` (async-транспорт + командный слой), `ble_soak.py` (раннер),
`analyze_soak.py` (оффлайн-отчёт), логи — `soak_logs/` (gitignored).

### Установка (отдельный venv, клон не трогаем)

```zsh
cd /Users/vadimkz/atomapp-ios/radiacode_stuff/phase0
uv sync          # создаст .venv: bleak + radiacode (path-dep на /Users/vadimkz/Projects/radiacode) + CoreBluetooth
```

### Запуск (БЕЗ sudo!)

BLE на macOS **не требует root** — нужно лишь **разрешение Bluetooth** для терминала
(первый запуск спросит: System Settings → Privacy & Security → Bluetooth → включить для
Terminal/iTerm/Cursor). `caffeinate` не даёт Mac уснуть за 6 ч:

```zsh
caffeinate -is uv run python ble_soak.py --hours 6
```

Профиль по умолчанию = согласованный: poll 1 c; idle-фаза **5–12 мин** (random) между
poll-стретчами 2–6 мин; reconnect каждые **15–45 мин** (random); спектр каждые ~12 мин.
Переопределяется флагами (`--idle-min/--idle-max`, `--reconnect-min/--reconnect-max`,
`--poll-min/--poll-max`, `--spectrum-every`, `--hours`, `--no-raw-frames` чтобы уменьшить
лог). Остановка — Ctrl+C (грейсфул, допишет `session_end`).

Лог пишется потоково в `soak_logs/soak_<ISO>.jsonl.gz` (gzip-JSONL, событие на строку:
`adv`/`session_start`/`databuf`/`spectrum`/`vsfr_probe`/`frame`/`reconnect`/`phase`/`error`).

**Живой статус в консоли** (чтобы видеть, что прогон идёт, а не повис): печатаются
таймстампленные строки на скане/подключении (имя, addr/UUID, RSSI, serial, fw, MTU), на
сменах фаз (poll/idle с обратным отсчётом, reconnect), на чтении спектра и ошибках, плюс
троттл-heartbeat (по умолчанию раз в 10 c, `--status-every`) вида:

```
[12:53:20] CONNECTED serial=RC-110-000513 fw=4.14 mtu=185 addr=XXXXXXXX-... [initial]
[12:53:31] CONN poll | cps=21.3 dr=0.106uSv/h | rec=842 rare(dose=0.107R chg=84% t=27.4C) | t+12m/6h00m next_rc=22m
[13:05:02] -> idle for 8m12s (connection kept alive)
[13:13:14] -> resume polling (drain catch-up); next idle in 4m30s
[13:27:40] == reconnect #1: disconnect, wait 10s, rescan ==
```

В idle-фазе heartbeat показывает `idle(resume <таймер>)`, так что многоминутная пауза не
выглядит зависанием.

### H5 (single-central) — ручная проверка во время прогона

Пока логгер держит BLE-коннект, **официальное приложение RadiaCode не должно
подключиться** к тому же прибору. Один раз во время soak попробуйте подключиться офиц.
приложением и зафиксируйте, что не выходит (= H5 подтверждён).

### Анализ

```zsh
uv run python analyze_soak.py soak_logs/soak_<ISO>.jsonl.gz
```

Печатает и пишет `soak_logs/report_<...>.md`: identity-стабильность, H1/H2/H3 (adv/MTU),
гистограмма eid/gid + **независимый пермиссивный переразбор сырого `DATA_BUF`** (флагует
неизвестные/неуверенные группы — напр. `(0,4)` DoseCounter — с сырыми байтами для ручного
декода), каденции (период `RareData`), дрейф заряда/температуры, **H15** до/после reconnect,
VSFR-матрица, ts-wrap, фиделити транспорта (H8), и раздел «дельты к протокол-доку».

> Скрипты опираются на внутренности `cdump/radiacode` (enum/декодеры) — throwaway
> валидация Фазы 0, не продакшн-код atomapp-ios.

## H16 probe — быстрый «поисковый» канал (gate для Searching в v1)

Soak показал: под обычным поллингом прибор по BLE отдаёт только усреднённый `RealTimeData`
(за 6 ч cps 7.8–9.7, плоский), а сырой/быстрый канал не доставляется (протокол §14.10). Это
блокирует Searching (быстрый отклик при поиске источника). `h16_search_probe.py` определяет,
можно ли получить отзывчивый канал по BLE.

```zsh
# READ-ONLY (безопасно): быстрый поллинг, двигайте прибор у источника в каждом окне
uv run python h16_search_probe.py

# + перебор режимов прибора (ПИШЕТ MS_MODE/MS_SUB_MODE/CPS_FILTER/RAW_FILTER, restore в конце)
uv run python h16_search_probe.py --try-modes
```

- В каждом окне «MOVE NOW» подносите/отдаляйте прибор от источника ИИ; живой вывод показывает
  `count_rate` по каналам (`RealTimeData` vs `RawData`/`RawCountRate`/`RawDoseRate`).
- Итог окна (`window_summary`) даёт **spread** по каналам — **канал с наибольшим разбросом =
  самый отзывчивый**. Если такой канал найден по BLE → Searching реализуем в v1; если нет → v1.1.
- `--try-modes` сохраняет исходные значения VSFR в начале и **восстанавливает** их в конце
  (в т.ч. при Ctrl-C); отвергнутые прибором записи логируются и пропускаются.
- Лог — `soak_logs/h16_<ts>.jsonl.gz`.

## Доп. раунд валидации H17–H20 (после Фазы 0; уточняющий)

> **СТАТУС: H17–H20 ЗАКРЫТЫ (2026-06-17, RC-101 fw4.14).** Итоги — протокол §14.11.
> H17 ✅ подача `RawData` гейтится частотой опроса (poll ≤0.5 c обязателен для Searching);
> H18 ✅ дисплейная «Доза» = `RareData.dose` (DoseCounter — диагностика); H19 ✅ `RawData`=cps
> (шаг 2 cps, `cp_2s=cr·2`); H20 ✅ декодер v1/1024 + линия Pb-212 238 keV на канале.
> Инструменты ниже остаются для воспроизведения/регрессий.
>
> H17–H20 — уточнения для дизайна Фазы 2/3 (cp_2s, источник дозы, Searching), **не блокеры**
> Фазы 1. **H18/H19/H20 частично закрывались офлайн** переразбором уже снятых логов
> (`soak_*`/`h16_*`); H17 и точечные подтверждения (дисплей-доза, фотопик) — на железе.

Общий надёжный декодер: **`rc_reparse.py`** — value-извлекающий пермиссивный
переразбор `DATA_BUF`, **исправляющий десинк `cdump` на `(0,4) DoseCounter`**
(штатный `decode_VS_DATA_BUF` считает `(0,4)` 16-байтовой `UserData` и теряет все
записи после неё в том же буфере — в 6-часовом soak это 606 потерянных записей).
Используется и пробами, и офлайн-анализатором.

### H18 + H19 — офлайн record-level анализ (`analyze_records.py`)

Гоняется на ЛЮБОМ логе (`soak_*`/`h16_*`/`spectrum_*`), **железо не нужно**:

```zsh
uv run python analyze_records.py soak_logs/<log>.jsonl.gz
```

- **H18 (источник дозы):** извлекает `RareData.dose` (R→нЗв ×1e7) и `DoseCounter`
  (мкР→нЗв ×10), печатает first/last/Δ и **скорость (нЗв/ч)** по каждому, разбивку
  `(0,4)` по флагам с per-flag монотонностью, и rate-ratio. Критерий: «дозовый»
  аккумулятор должен на фоне давать ~100 нЗв/ч (= живой dose_rate). Абсолютная
  калибровка требует показания дисплея — см. dose-compare ниже.
- **H19 (RawData = counts/окно или cps):** гранулярность (gcd значений),
  `mean(RawData)/mean(RealTimeData)` (≈1 → cps; ≈N → counts/N-сек), per-label.

### H18 dose-compare — какой аккумулятор = дисплейная «Доза»: `dose_compare.py`

Решает, откуда маппить накопленную дозу (`RareData.dose` vs `DoseCounter`). Оба —
лайфтайм-аккумуляторы; сравниваем их **Δ за фикс. экспозицию** с **Δ дисплейной
«Дозы»**. Read-only; ты вводишь показание экрана до/после:

```zsh
uv run python dose_compare.py --minutes 15 --display-unit uSv --source "thorium mantle"
```

- Поднеси источник к детектору на время окна (или оставь фон). Перед/после окна
  скрипт спросит дисплейную «Дозу» (число в `--display-unit`: uSv/mSv/uR/mR).
- Итог: ratio Δ-аккумулятора к Δ-дисплея; **≈1.0 = это и есть доза**. `(0,4)`
  разбит по флагам (берём `0x9000`). Лог → `soak_logs/dosecmp_<ts>.jsonl.gz`,
  пере-разбор — `analyze_records.py`.

### H17 — confound (poll-rate vs активность): `h16_search_probe.py --matrix`

Read-only 2×2 (+stable-elevated). Разводит «что включает обильный `RawData` —
частота опроса или активность поля». Следуйте инструкции каждого окна (move/still,
poll 1.0/0.5; последнее окно — неподвижно ВПЛОТНУЮ к источнику):

```zsh
uv run python h16_search_probe.py --matrix            # 5 окон по 40 c, без записей
uv run python h16_search_probe.py --matrix --window 60
```

Сравнить `rate_per_s` канала `RawData` между ячейками: доминирует ось движения →
подача событийная (poll 0.5 c достаточно); доминирует ось poll-rate → нужен
быстрый непрерывный опрос. Лог → `soak_logs/h16_<ts>.jsonl.gz`, разбор —
`analyze_records.py`.

### H20 — валидация декодера спектра: `spectrum_probe.py`

Read-only. `VS.SPECTRUM` — накопительный лайфтайм-спектр (нет чистого пика), поэтому
берём **разностный** спектр: два чтения через `--window` секунд с поднесённым
известным источником; пик `counts1−counts0` → энергия по `E=a0+a1·ch+a2·ch²`.
Плюс независимая ре-декодировка v1 как cross-check `cdump` (offline уже совпала
байт-в-байт на 24 спектрах soak):

```zsh
uv run python spectrum_probe.py --window 180 --source "Cs-137 (662 keV)"
# природный источник (Th/U) — сложный спектр, несколько линий:
uv run python spectrum_probe.py --window 300 --source "thorium mantle (Th-232 series)"
```

Держите источник близко и неподвижно во время паузы. Лог →
`soak_logs/spectrum_<ts>.jsonl.gz`.

**Ориентиры линий для природного источника** (пик `counts1−counts0` должен лечь на
одну из них; `--window` побольше, 300–600 c, т.к. природный фон слабее точечного):
- **Th-232 / ториевая мантия:** Pb-212 **238 keV**, Tl-208 **583 keV** и **2614 keV**,
  Ac-228 **911/969 keV**.
- **U-238 / урановое стекло:** Pb-214 **295/352 keV**, Bi-214 **609 keV** (и 1120/1764),
  Th-234 **63/93 keV**.
Сильнейший Δ-пик в нижней части (≈80–352 keV) для слабого источника — норма; ключ —
**совпадение канала пика с одной из этих линий** по `E=a0+a1·ch+a2·ch²`.

## Куда писать результаты

После прогона — дописать числа и вердикты в:
1. [`../../docs/radiacode-ble-protocol.md`](../../docs/radiacode-ble-protocol.md) §14 (факты протокола);
2. [`../../docs/radiacode-ble-integration-plan.md`](../../docs/radiacode-ble-integration-plan.md)
   § «Статус гипотез H1–H15» (H14/H15 → ✅) и § «История валидации».

> Скрипты опираются на внутренности `cdump/radiacode` (`rc._connection`) — это throwaway
> валидация Фазы 0, не продакшн-код atomapp-ios. Код приложения не затронут.
