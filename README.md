# iot_fanuc — мониторинг станков с ЧПУ

Self-hosted платформа мониторинга станков (Fanuc 0i-MD и др.) и расчёта OEE.
Сейчас это сквозной рабочий каркас на симуляторе: от опроса «станка» до дашборда,
экрана оператора и аналитики.

- Архитектура и этапы: [ARCHITECTURE.md](ARCHITECTURE.md)
- Правила разработки: [CLAUDE.md](CLAUDE.md)
- Что сделано в каркасе, что упрощено: [docs/skeleton-report.md](docs/skeleton-report.md)
- Быстрый старт и команды: [docs/runbooks/dev-quickstart.md](docs/runbooks/dev-quickstart.md)

## Запуск

Нужны Docker с Compose v2 и `make`.

```bash
make up
```

Команда создаёт `.env` из `.env.example`, собирает образы и поднимает брокер, БД, Redis,
`ingest`, `worker`, `api`, `web` и симулятор на 6 станков. При первом старте симулятор
досылает историю за последние 8 часов, поэтому через полминуты экраны уже заполнены.

## Экраны

Интерфейс открывается на <http://localhost:8080> (порт — `MM_WEB_PORT` в `.env`).

| Экран | Адрес | Доступ |
|---|---|---|
| Цеховой дашборд | `http://localhost:8080/dashboard?token=kiosk-demo-token` | device-токен |
| Дашборд одного участка | `http://localhost:8080/dashboard/1?token=kiosk-demo-token` | device-токен |
| Экран оператора | `http://localhost:8080/operator/sim02?token=kiosk-demo-token` | device-токен |
| Аналитика | `http://localhost:8080/analytics` | логин `admin` / `admin` |
| Админка | `http://localhost:8080/admin` | логин `admin` / `admin` |

Токен киоска и пароль администратора задаются в `.env` (`MM_DEVICE_TOKEN`,
`MM_ADMIN_USERNAME`, `MM_ADMIN_PASSWORD`). Токен из адреса запоминается в браузере, после
первого открытия его можно не указывать. С device-токеном доступны чтение и ввод оператора
(причины, брак, наладка); справочники меняет только администратор.

Что попробовать:

- на дашборде нажать плитку станка — откроется экран оператора;
- дождаться простоя на `sim02` (красный «Простой») — экран оператора сам спросит причину,
  выбор занимает два касания;
- в аналитике переключить «Текущая смена / Предыдущая смена / Сегодня / Вчера»;
- в админке изменить норму цикла и нажать «Пересчитать за 24 ч».

![Дашборд](docs/img/skeleton-dashboard.png)
![Аналитика](docs/img/skeleton-analytics.png)
![Экран оператора](docs/img/skeleton-operator.png)

## Команды

```bash
make test
```

```bash
make lint
```

```bash
make recalc from=2026-11-16T05:00:00Z
```

Остальные команды (`logs`, `ps`, `down`, `clean`, `psql`, `migrate`) — в runbook.

## Проверка реальной стойки

Проба FOCAS (этап 0) **на реальной стойке не проверена**:

```bash
python edge/tools/focas_probe.py --ip 192.168.10.57 --port 8193 --lib edge/vendor/Fwlib64.dll --watch
```
