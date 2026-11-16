# iot_fanuc — мониторинг станков с ЧПУ

Self-hosted платформа мониторинга станков (Fanuc 0i-MD и др.) и расчёта OEE.

- Архитектура и этапы: [ARCHITECTURE.md](ARCHITECTURE.md)
- Правила разработки: [CLAUDE.md](CLAUDE.md)
- Быстрый старт: [docs/runbooks/dev-quickstart.md](docs/runbooks/dev-quickstart.md)
- Архитектурные решения: [docs/adr/](docs/adr/)

Состояние: выполнены этап 0 (проба FOCAS) и этап 1 (каркас, контракт сообщений,
симулятор, миграции, приём сырых данных).

```bash
make up     # сервер + симулятор на 5 станков
make test   # тесты backend и edge
make lint
```
