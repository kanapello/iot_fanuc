# Быстрый старт (разработка)

## Что нужно

Docker с Compose v2, `make`, для тестов и линтеров — `uv` (Python 3.12 он скачает сам).

## Запуск

```bash
make up
```

Команда создаёт `.env` из `.env.example` (если его нет), собирает образы и поднимает
брокер, БД, Redis, миграции, `ingest`, `worker`, `api`, `web` и два симулятора: `edge-sim-a`
(площадка `plant1`, станки `sim01`…`sim03`) и `edge-sim-b` (площадка `plant2`, станки `sim04`…`sim06`).
Интерфейс: <http://localhost:8080> (адреса экранов — в `README.md`).

Проверка, что данные идут:

```bash
make psql
```

```sql
SELECT machine_id, count(*), max(ts) FROM telemetry GROUP BY 1 ORDER BY 1;
SELECT machine_id, type, code, ts FROM machine_events ORDER BY ts DESC LIMIT 20;
SELECT * FROM ingest_rejects ORDER BY id DESC LIMIT 20;
```

Проверка разрывов `seq` (пустой результат — потерь нет):

```sql
SELECT * FROM (
  SELECT machine_id, seq, seq - lag(seq) OVER (PARTITION BY machine_id ORDER BY seq) AS step
  FROM (SELECT machine_id, seq FROM telemetry UNION ALL SELECT machine_id, seq FROM machine_events) s
) g WHERE step > 1;
```

Счётчик `seq` хранится в SQLite-буфере коллектора и переживает его перезапуск.

## Остальные команды

| Команда | Что делает |
|---|---|
| `make logs s=ingest` | логи сервиса |
| `make ps` | состояние контейнеров |
| `make down` | остановить, тома сохранить |
| `make clean` | остановить и **удалить тома** (БД, очередь брокера) |
| `make migrate` | `alembic upgrade head` |
| `make recalc from=<iso> [to=<iso>] [m=<id>]` | пересчёт таймлайна и OEE |
| `make test` / `make lint` | тесты и линтеры backend + edge |

## Сборка за прокси с подменой TLS

Dockerfile принимают необязательный секрет сборки `extra_ca` — файл с корневым
сертификатом прокси. Подключается дополнительным compose-файлом:

```yaml
# compose.ca.yml
x-ca: &ca { secrets: [extra_ca] }
services:
  migrate: { build: *ca }
  ingest: { build: *ca }
  worker: { build: *ca }
  api: { build: *ca }
  web: { build: *ca }
  simulator: { build: *ca }
secrets:
  extra_ca: { file: ./ca.crt }
```

```bash
COMPOSE_EXTRA="-f compose.ca.yml" make up
```

## Проверка стойки (этап 0)

```bash
python edge/tools/focas_probe.py --ip 192.168.10.57 --port 8193 --lib edge/vendor/Fwlib64.dll --out probe.txt
```

Библиотеку FOCAS положить в `edge/vendor/` вручную (в git не хранится) — `Fwlib64.dll` вместе с
`fwlibe64.dll` и `fwlib0iD64.dll` в одной папке. Разрядность Python должна совпадать с разрядностью
библиотеки. Проба сама ставит эту папку первой в `PATH` (на Windows — ещё `SetDllDirectoryW`):
вспомогательные DLL грузятся по имени, `os.add_dll_directory` для них не работает (`EW_NODLL`).
`--out` дописывает вывод в файл UTF-8, `--watch` — минимум раз в секунду, `--recon` — один проход
расширенного набора. Сеть коллектора — [collector-network.md](collector-network.md), порядок у первой
стойки — [first-machine.md](first-machine.md).

## Подключение реального коллектора

1. Под администратором организации: «Админка → Коллекторы» — добавить коллектор на площадку (ID, например `edge-shop1`).
2. В `.env` сервера добавить учётную запись брокера и перезапустить `mosquitto`:
   `MM_MQTT_COLLECTORS=edge-shop1:<код организации>:<код площадки>:<пароль>` (несколько — через запятую;
   в пароле нельзя `,` и `:`).
3. В `config.yaml` коллектора: `org`, `site`, `collector_id`, `mqtt.username` = ID коллектора,
   пароль — в переменной окружения из `mqtt.password_env`.

Брокер пропустит от коллектора только топики его площадки и его собственный heartbeat;
`ingest` дополнительно сверяет площадку топика с площадкой коллектора в справочнике.

## Состояние коллекторов

`GET /api/v1/collectors` (технолог, администратор, сервис): heartbeat, `online`, разрывы `seq`
и `buffer_lost` — коллектор вернулся с новым буфером (эпохой): неотправленные данные потеряны,
`seq` начат заново. Событие также пишется в лог `ingest` («collector buffer lost, seq restarted»).
