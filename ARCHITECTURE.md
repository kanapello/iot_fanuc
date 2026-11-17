# Архитектура платформы мониторинга станков с ЧПУ

Self-hosted система промышленного мониторинга и производственной аналитики (класс MachineMetrics):
от опроса стойки Fanuc 0i-MD до расчёта OEE, экрана оператора и цехового дашборда.

---

## 1. Ключевые принципы

1. **Сырые данные неизменяемы.** Всё производное (таймлайн состояний, OEE, отчёты) можно пересчитать из сырых данных в любой момент — при смене норм цикла, правил состояний, границ смен.
2. **Таймлайн состояний — центральная сущность.** OEE и почти вся аналитика считаются из интервалов состояний (RUN / IDLE / STOP / ALARM / SETUP / OFFLINE), а не из сырых точек.
3. **Edge «тупой и надёжный».** Коллектор только читает стойку, ставит метку времени и доставляет данные. Никакой бизнес-логики.
4. **Ни одной потерянной секунды.** Store-and-forward буфер на edge, идемпотентный приём на сервере, явное состояние `OFFLINE` вместо дыр.
5. **Время — с источника.** Метка времени ставится на edge в момент чтения со стойки (UTC). NTP на всех узлах.
6. **Контракт edge↔сервер версионирован** (JSON Schema в `shared/schemas`). Новые типы стоек = новые драйверы, сервер не меняется.
7. **Модульный монолит.** Один backend-кодбейз с изолированными модулями; запускается несколькими процессами (api, ingest, worker). Микросервисы — только при реальной необходимости.
8. **Одна БД.** PostgreSQL + TimescaleDB для временных рядов и справочников.
9. **Симулятор с первого дня.** Вся цепочка тестируется без реальных станков.

---

## 2. Общая схема (4 контура)

```
 КОНТУР 1: EDGE (цех)                 КОНТУР 2–4: СЕРВЕР (Docker Compose)
┌──────────────────────────┐        ┌──────────────────────────────────────────────┐
│ edge-collector           │  MQTT  │  mosquitto (MQTT broker, TLS, auth)           │
│  ├ drivers/              │ QoS 1  │        │                                      │
│  │  ├ fanuc_focas        │───────▶│        ▼                                      │
│  │  └ simulator          │        │  backend: ingest  ──▶ raw hypertables         │
│  ├ poller (поток/станок) │        │     │  state machine ──▶ state_intervals       │
│  ├ buffer (SQLite)       │        │     ▼                                         │
│  └ publisher (MQTT)      │        │  backend: worker  ──▶ OEE aggregates, пересчёт │
└──────────────────────────┘        │     │                                         │
       │ FOCAS/Ethernet :8193       │  backend: api (REST + WebSocket) ◀── Redis    │
       ▼                            │     │                    (live state)          │
 Fanuc 0i-MD × N                    │     ▼                                         │
                                    │  frontend (nginx): operator / dashboard /      │
                                    │                    analytics / admin           │
                                    │  timescaledb · redis · prometheus · grafana    │
                                    └──────────────────────────────────────────────┘
```

| Контур | Назначение | Компоненты |
|---|---|---|
| 1. Сбор (Edge) | Опрос стоек, буферизация, доставка | `edge/` (Python), драйвер FOCAS, SQLite-буфер |
| 2. Приём и хранение | Брокер, нормализация, таймлайн, хранение | Mosquitto, `backend/app/ingest`, TimescaleDB, Redis |
| 3. Аналитика | OEE, простои, брак, отчёты, уведомления | `backend/app/oee`, `backend/app/reports`, `backend/app/notify` |
| 4. Представление | API, экран оператора, цеховой ТВ-дашборд, админка | `backend/app/api`, `frontend/` |

---

## 3. Контур 1 — Edge-коллектор

### 3.1. Назначение
Опрашивает N станков, ставит метку времени, кладёт в локальный буфер, публикует в MQTT. Работает автономно при отсутствии связи с сервером.

### 3.2. Платформы
- **Windows (основной вариант в цеху):** Python 3.12, `Fwlib64.dll` (FOCAS2). Упаковка PyInstaller → `.exe`, запуск как службы Windows (NSSM или pywin32). Если на объекте доступна только `Fwlib32.dll` — сборка под 32-битный Python.
- **Linux:** `libfwlib32.so` (x86 / ARM, из Fanuc FOCAS Linux library), запуск в Docker (`deploy/docker-compose.edge.yml`) или systemd.
- Библиотеки FOCAS **не хранятся в репозитории** (лицензия Fanuc) — кладутся в `edge/vendor/` вручную, путь задаётся в конфиге.

### 3.3. Структура
```
edge/
├── collector/
│   ├── main.py              # точка входа, загрузка конфига, запуск пулеров
│   ├── config.py            # pydantic-модель конфига (YAML)
│   ├── drivers/
│   │   ├── base.py          # интерфейс MachineDriver
│   │   ├── fanuc_focas.py   # ctypes-обёртка над Fwlib
│   │   └── simulator.py     # эмулятор станка
│   ├── poller.py            # цикл опроса одного станка (поток)
│   ├── buffer.py            # SQLite store-and-forward
│   ├── publisher.py         # MQTT (paho-mqtt), QoS 1, переотправка из буфера
│   └── health.py            # heartbeat коллектора, метрики
├── tools/focas_probe.py     # этап 0: проверка связи со стойкой
├── vendor/                  # Fwlib*.dll / libfwlib32.so (в .gitignore)
├── config.example.yaml
└── Dockerfile
```

### 3.4. Интерфейс драйвера
```python
class MachineDriver(Protocol):
    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def read_snapshot(self) -> MachineSnapshot: ...   # все сигналы за один опрос
    def read_alarms(self) -> list[Alarm]: ...
```
Реализации: `FanucFocasDriver`, `SimulatorDriver`; позже `OpcUaDriver`, `MTConnectDriver`, `SiemensDriver`.

### 3.5. Сигналы Fanuc 0i-MD (FOCAS2)
| Сигнал | Функция FOCAS | Период |
|---|---|---|
| Подключение | `cnc_allclibhndl3(ip, 8193, timeout)` / `cnc_freelibhndl` | — |
| Статус (aut, run, motion, mstb, emergency, alarm, edit) | `cnc_statinfo` | 1 с |
| Номер программы (основная / выполняемая) | `cnc_rdprgnum` | 1 с |
| Имя программы | `cnc_exeprgname` (если поддерживается) | 1 с |
| Фактическая подача | `cnc_actf` | 1 с |
| Фактические обороты шпинделя | `cnc_acts` | 1 с |
| Нагрузка шпинделя | `cnc_rdspmeter` / `cnc_rdspload` | 1 с |
| Счётчик деталей | `cnc_rdmacro(#3901)` или `cnc_rdparam(6711)` — настраивается | 1 с |
| Аварии | `cnc_rdalmmsg2` | при `alarm != 0` |
| Overrides подачи/шпинделя | `pmc_rdpmcrng` (адреса G-сигналов задаются в конфиге) | 1 с, опционально |

Требования к стойке: встроенный Ethernet, FOCAS2 включён, порт 8193 доступен из сети коллектора. **Этап 0 проекта — проверить всё это скриптом `focas_probe.py` на реальной стойке.**

### 3.6. Поведение
- Один поток на станок; общий publisher.
- Ошибка FOCAS → переподключение с экспоненциальной задержкой (1 → 30 с); публикуется событие `connection: lost/restored`.
- Публикация телеметрии: **по изменению + не реже 1 раза в 10 с** (keyframe). Счётчик деталей и статус — всегда при изменении.
- Буфер SQLite: каждое сообщение пишется в буфер до отправки; удаляется после PUBACK. Срок хранения — **не дольше 7 дней** (`buffer.max_age_days`, значения больше 7 конфиг не принимает); более старые сообщения вытесняются.
- **Правило сроков:** буфер edge ≤ 7 дней < сжатие `telemetry` через 30 дней. Опоздавшие данные никогда не попадают в сжатый чанк. Менять один срок без другого нельзя.
- Heartbeat коллектора каждые 15 с (версия, аптайм, размер буфера, статусы подключений).

### 3.7. Конфиг (пример)
```yaml
site: plant1
collector_id: edge-shop1
mqtt:
  host: mm-server.local
  port: 8883
  tls: true
  username: edge-shop1
  password_env: MM_MQTT_PASSWORD
focas:
  library_path: ./vendor/Fwlib64.dll
buffer:
  path: ./data/buffer.db
  max_age_days: 7
machines:
  - id: m07
    name: "Фрезерный 07"
    driver: fanuc_focas
    ip: 192.168.10.57
    port: 8193
    poll_interval_ms: 1000
    part_counter: { source: macro, address: 3901 }
  - id: sim01
    driver: simulator
    profile: milling_normal
```

---

## 4. Контракт сообщений (MQTT)

### 4.1. Топики
```
mm/v1/{site}/{machine_id}/telemetry     # снимок сигналов
mm/v1/{site}/{machine_id}/event         # аварии, подключение/отключение, смена программы
mm/v1/{site}/collector/{collector_id}/heartbeat
```
QoS 1, `retain=false` (кроме heartbeat — `retain=true`). Схемы — `shared/schemas/*.json` (JSON Schema draft 2020-12).

### 4.2. Конверт
```json
{
  "schema": "telemetry.v1",
  "msg_id": "0192f3b4-7c1e-7a3d-9f00-1a2b3c4d5e6f",
  "collector_id": "edge-shop1",
  "machine_id": "m07",
  "ts": "2026-11-16T19:01:02.345Z",
  "seq": 184223,
  "data": { }
}
```
- `msg_id` — UUIDv7, ключ идемпотентности на сервере.
- `ts` — время чтения со стойки (UTC, edge).
- `seq` — монотонный счётчик на станок; разрыв последовательности = потеря данных (логируется).

### 4.3. `telemetry.v1` → `data`
```json
{
  "connected": true,
  "mode": "MEM",
  "run": "START",
  "motion": true,
  "emergency": false,
  "alarm": false,
  "program": "O1234",
  "program_name": "O1234",
  "feed_actual": 1250.0,
  "spindle_speed": 8000,
  "spindle_load": 37.5,
  "feed_override": 100,
  "spindle_override": 100,
  "part_count": 15873
}
```

### 4.4. `event.v1` → `data`
```json
{ "type": "alarm_raised", "code": "SV0401", "message": "...", "category": "servo" }
```
Типы: `alarm_raised`, `alarm_cleared`, `connection_lost`, `connection_restored`, `program_changed`.

---

## 5. Контур 2 — Приём и хранение

### 5.1. Mosquitto
- TLS (порт 8883), пароли на коллектор, ACL: коллектор пишет только в `mm/v1/{site}/...` своих станков; backend — только читает.
- Persistence включён.

### 5.2. Модуль `ingest` (процесс backend)
1. Подписка на `mm/v1/#`: обычная, с постоянной сессией MQTT. Shared subscription не используется: Mosquitto не восстанавливает её после собственного перезапуска, и сообщения, пришедшие раньше переподключения `ingest`, теряются. Один экземпляр `ingest`; масштабирование — разбиением топиков, отдельным решением.
2. Валидация по JSON Schema; невалидное → `ingest_rejects` + метрика.
3. Идемпотентная запись: `INSERT ... ON CONFLICT (msg_id, ts) DO NOTHING` (ключ включает `ts` — требование hypertable, см. ADR 0001). PUBACK брокеру — только после `COMMIT` (ADR 0002).
4. `ingest` ничего не выводит из данных: в той же транзакции он ставит отметку `timeline_dirty` — «данные станка изменились с такого-то момента». Опоздавшие данные из буфера edge обрабатываются тем же путём.
5. Таймлайн, детали, OEE и live-состояние в Redis строит `worker` (п. 6.2).
6. Проверка разрывов `seq`: вычисляется по сырым данным (`worker` пишет в лог, API `/collectors` показывает). Дыра считается разрывом, если не закрылась за 60 с.

### 5.3. Модель данных (PostgreSQL + TimescaleDB)

**Справочники**
| Таблица | Ключевые поля |
|---|---|
| `sites`, `areas` | id, name |
| `machines` | id, area_id, name, controller_type, collector_id, state_rules_id, part_counter_cfg, is_active |
| `collectors` | id, name, last_heartbeat, version |
| `state_rules` | id, name, rules (jsonb), version |
| `shift_templates` | id, name, start_time, end_time, days_of_week, planned_breaks (jsonb) |
| `shift_calendar` | id, machine_id/area_id, shift_template_id, date, start_ts, end_ts, is_working |
| `downtime_reasons` | id, parent_id, code, name, category (planned/unplanned), color, requires_comment |
| `scrap_reasons` | id, code, name |
| `programs` | id, machine_id, program, ideal_cycle_time_s, parts_per_cycle, valid_from |
| `users`, `roles` | RBAC: operator, master, engineer, admin |

**Сырые данные (hypertables, неизменяемые)**
| Таблица | Поля | Политики |
|---|---|---|
| `telemetry` | ts, machine_id, msg_id (PK: msg_id + ts), seq, collector_id, connected, mode, run, motion, alarm, emergency, program, feed, speed, load, overrides, part_count, received_at | chunk 1 день, compression > 30 дней, retention 1–2 года |
| `machine_events` | ts, machine_id, msg_id (PK: msg_id + ts), seq, collector_id, type, code, message, payload jsonb | retention 3 года |

**Производные данные (пересчитываемые)**
| Таблица | Поля |
|---|---|
| `state_intervals` | id, machine_id, state, start_ts, end_ts (null = открыт), program, rules_version |
| `part_events` | ts, machine_id, program, delta (из приращения part_count, с обработкой сброса счётчика) |
| `oee_hourly`, `oee_shift` | machine_id, period_start, planned_time_s, run_time_s, idle_s, stop_s, alarm_s, offline_s, total_parts, good_parts, ideal_time_s, availability, performance, quality, oee, calc_version |

**Пользовательский ввод**
| Таблица | Поля |
|---|---|
| `downtime_annotations` | id, machine_id, start_ts, end_ts, reason_id, comment, user_id, created_at |
| `scrap_records` | id, machine_id, ts, program, qty, reason_id, user_id |
| `operator_setup_marks` | id, machine_id, start_ts, end_ts, user_id (ручная отметка «наладка») |

Аннотации привязаны ко **времени**, а не к id интервала, — поэтому переживают пересчёт таймлайна.
Таблицы пользовательского ввода меняет только пользователь. Причина простоя без окончания действует до следующего запуска станка; это окончание вычисляется при чтении (представление `downtime_annotations_resolved`), `worker` записи не трогает.

### 5.4. Правила определения состояний (по умолчанию, настраиваемые)
Порядок проверки — сверху вниз, первое совпадение:
| Состояние | Условие |
|---|---|
| `OFFLINE` | нет данных > 30 с или `connected=false` |
| `ALARM` | `alarm=true` или `emergency=true` |
| `SETUP` | активна отметка оператора «наладка» (или режим EDIT/MDI/JOG > N мин — опционально) |
| `RUN` | `run=START` и `mode=MEM`/`DNC` |
| `IDLE` | не RUN, длительность < `idle_to_stop_threshold` (по умолчанию 2 мин) |
| `STOP` | не RUN, длительность ≥ порога → требует причину от оператора |

`FEED HOLD` (`run=HOLD`) трактуется как не-RUN. Интервалы короче `min_interval` (5 с) сливаются с соседними (антидребезг).

### 5.5. Redis
- `machine:{id}:live` — текущий снимок + текущее состояние + начало состояния + счётчики смены.
- Pub/Sub `live.{site}` — поток изменений для WebSocket.
- Только кэш: потеря Redis не теряет данных.

---

## 6. Контур 3 — Аналитика

### 6.1. Расчёт OEE
Для периода (час / смена / сутки) и станка:

- **Planned Production Time (PPT)** = длительность смены − плановые перерывы − простои с причинами категории `planned`.
- **Run Time** = Σ `RUN` в пределах PPT.
- **Availability** = Run Time / PPT
- **Performance** = Σ(ideal_cycle_time(program) × parts) / Run Time
  (по каждой программе своя норма; если нормы нет — Performance не считается, период помечается `no_standard`; значение > 100% не обрезается, а подсвечивается как признак неверной нормы)
- **Quality** = (total_parts − scrap) / total_parts
- **OEE** = A × P × Q

Периоды вне смены (нерабочее время) в OEE не входят, но отображаются в таймлайне.

### 6.2. Модуль `worker` (процесс backend)
- Раз в несколько секунд по каждому станку: достройка таймлайна (`state_intervals`, `part_events`), затем OEE, затем live-состояние в Redis.
- **Отметки пересчёта хранятся в БД**, а не в памяти: `timeline_dirty` и `oee_dirty` («пересчитать с такого-то момента»). Отметка снимается в той же транзакции, что и пересчёт, поэтому перезапуск `worker` в любой момент ничего не теряет. Длинные периоды обрабатываются кусками по суткам.
- Отметки ставят: `ingest` (новые и опоздавшие данные), ввод оператора (причины, брак, наладка), изменение справочников через API (смены, нормы цикла, правила состояний, причины простоя) — на глубину `MM_RECALC_HORIZON_DAYS` (31 день).
- **Пересчёт вручную**: `make recalc from=… [to=…] [m=…]`. С `to` пересчитывается закрытый период: интервалы после `to` не меняются, на границе выполняется стыковка.
- Норма цикла действует с `valid_from` до следующей нормы той же программы; изменение нормы не меняет прошлые периоды.
- Брак учитывается по времени ввода и не обрезается числом деталей часа: сумма часов даёт то же качество, что смена.
- Уведомления (`notify`): простой > N мин без причины, авария, коллектор offline → Telegram / email / webhook.

### 6.3. Отчёты
- OEE по станкам / участкам / сменам, тренды.
- Парето простоев по причинам, по времени и по количеству.
- Таймлайн (Gantt) по станкам за период.
- Выпуск: план/факт по сменам, брак по причинам.
- Экспорт CSV/XLSX.

---

## 7. Контур 4 — Представление

### 7.1. API (FastAPI)
- REST `/api/v1/...`: machines, timeline, oee, reasons, annotations, scrap, shifts, programs, reports, admin, auth.
- WebSocket `/ws/live?site=...` — изменения live-состояния.
- Аутентификация: JWT (access + refresh), роли. Киоск-режим оператора — device-токен, привязанный к станку (без логина), действия оператора подписываются выбранным табельным номером/PIN.
- OpenAPI-схема → генерация TypeScript-клиента для фронтенда.

### 7.2. Frontend (React + TypeScript + Vite)
| Режим | Маршрут | Назначение |
|---|---|---|
| Экран оператора | `/operator/:machineId` | Крупный статус и таймер, программа, план/факт смены; при `STOP` — автоматический запрос причины (6–8 крупных кнопок, 2 уровня); ввод брака; отметка «наладка» |
| Цеховой дашборд | `/dashboard/:areaId` | ТВ-режим: плитки станков (цвет состояния, длительность, OEE смены, текущая причина), авторотация участков |
| Аналитика | `/analytics/...` | OEE, Парето, таймлайн, выпуск, экспорт |
| Админка | `/admin/...` | Станки, коллекторы, смены, причины, нормы цикла, правила состояний, пользователи |

Цвета состояний (единые везде): RUN — зелёный, IDLE — жёлтый, STOP — красный, ALARM — пурпурный, SETUP — синий, OFFLINE — серый.

---

## 8. Развёртывание

### 8.1. Сервер — `deploy/docker-compose.yml`
| Сервис | Образ | Профиль |
|---|---|---|
| `mosquitto` | eclipse-mosquitto:2 | — |
| `db` | timescale/timescaledb:2.32.0-pg16 (версия закреплена) | — |
| `redis` | redis:7 | — |
| `migrate` | backend (alembic upgrade head) | — |
| `api` | backend (`uvicorn`) | — |
| `ingest` | backend (`python -m app.ingest`) | — |
| `worker` | backend (`python -m app.worker`) | — |
| `web` | frontend (nginx, static + reverse proxy `/api`, `/ws`) | — |
| `simulator` | edge (driver=simulator, N станков) | `dev` |
| `prometheus`, `grafana` | — | `monitoring` |
| `backup` | pg_dump по расписанию | `backup` |

Конфиг через `.env` (пример — `.env.example`). Тома для БД, брокера, бэкапов.

### 8.2. Edge — `deploy/docker-compose.edge.yml` (Linux) или Windows-установщик
Коллектор, конфиг `config.yaml`, том для буфера.

### 8.3. Сеть и безопасность
- Сеть станков (OT) отделена от офисной (IT). Коллектор стоит на границе: FOCAS — в OT, MQTT — исходящее соединение в сторону сервера.
- Наружу с сервера открыты только 443 (web) и 8883 (MQTT для коллекторов).
- Секреты — в `.env` / переменных окружения, не в git.

### 8.4. Наблюдаемость
- Prometheus-метрики: backend (`/metrics`), ingest lag, размер очереди пересчёта, коллекторы (через heartbeat → экспортер в backend).
- Grafana-дашборд «Здоровье системы»: коллекторы online, буферы, задержка приёма, ошибки FOCAS по станкам.
- Структурированные JSON-логи.

---

## 9. Стек

| Слой | Технологии |
|---|---|
| Edge | Python 3.12, ctypes (FOCAS2), paho-mqtt, SQLite, pydantic, PyInstaller |
| Backend | Python 3.12, FastAPI, SQLAlchemy 2 (async), asyncpg, Alembic, pydantic v2, aiomqtt, redis-py |
| БД | PostgreSQL 16 + TimescaleDB, Redis 7 |
| Брокер | Mosquitto 2 |
| Frontend | React 18, TypeScript, Vite, TanStack Query, TanStack Router, Tailwind CSS, ECharts |
| Тесты | pytest, testcontainers, vitest, Playwright (e2e) |
| Инструменты | uv (Python), pnpm (frontend), ruff, mypy, pre-commit, Makefile |

---

## 10. Структура монорепозитория

```
machine-monitor/
├── CLAUDE.md
├── ARCHITECTURE.md
├── Makefile
├── .env.example
├── edge/                     # Контур 1 (см. 3.3)
├── backend/
│   ├── pyproject.toml
│   ├── alembic/
│   ├── app/
│   │   ├── core/             # config, db, logging, security, общие модели
│   │   ├── ingest/           # MQTT-приём, валидация, state machine
│   │   ├── timeline/         # правила состояний, построение и пересчёт интервалов
│   │   ├── oee/              # расчёт OEE
│   │   ├── reports/          # отчёты, экспорт
│   │   ├── notify/           # уведомления
│   │   ├── worker/           # очередь задач, планировщик
│   │   ├── api/              # FastAPI роутеры, WebSocket
│   │   └── admin/            # справочники
│   └── tests/
├── frontend/
│   └── src/
│       ├── api/              # сгенерированный клиент
│       ├── features/operator|dashboard|analytics|admin
│       └── shared/
├── shared/
│   └── schemas/              # JSON Schema сообщений MQTT (контракт)
├── deploy/
│   ├── docker-compose.yml
│   ├── docker-compose.edge.yml
│   ├── mosquitto/
│   ├── grafana/
│   └── prometheus/
└── docs/
    ├── adr/                  # архитектурные решения
    └── runbooks/             # установка, обновление, восстановление из бэкапа
```

---

## 11. Этапы

| Этап | Результат | Критерий готовности |
|---|---|---|
| 0 | `edge/tools/focas_probe.py` | На реальной стойке читаются статус, программа, счётчик деталей, аварии |
| 1 | Каркас монорепо, compose, схемы сообщений, симулятор, миграции БД, ingest сырых данных | `make up` → данные симулятора (5 станков) пишутся в `telemetry` |
| 2 | Edge-коллектор: драйвер FOCAS, буфер, переподключение, heartbeat, сборка Windows/Linux | Отключение сети на 10 мин → после восстановления все данные на сервере, разрывов `seq` нет |
| 3 | Таймлайн: правила, state machine, OFFLINE, пересчёт, part_events | Автотесты на сценариях симулятора дают ожидаемые интервалы |
| 4 | API + WebSocket, цеховой дашборд, экран оператора (причины, брак, наладка) | Дашборд обновляется < 2 с; оператор вводит причину простоя одним касанием |
| 5 | Смены, нормы цикла, OEE (час/смена), отчёты, Парето, экспорт | OEE за смену совпадает с ручным расчётом по таймлайну |
| 6 | Роли и авторизация, уведомления, мониторинг, бэкапы, TLS, runbooks | Систему можно эксплуатировать без разработчика |

---

## 12. Решения, от которых сознательно отказались (на старте)

| Не делаем | Почему |
|---|---|
| Микросервисы / Kubernetes | Масштаб 10–200 станков не требует; модульный монолит выносится в сервисы позже |
| Kafka | MQTT + PostgreSQL достаточно; лишний компонент эксплуатации |
| Отдельная InfluxDB | TimescaleDB закрывает временные ряды в той же БД |
| Celery / RabbitMQ | Очередь задач на PostgreSQL (`SKIP LOCKED`) |
| Логика на edge | Обновление и отладка сложнее; правила должны пересчитываться централизованно |
| Keycloak на старте | Встроенный JWT + RBAC; SSO/LDAP — отдельным этапом при необходимости |
