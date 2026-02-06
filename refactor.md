# Архитектурный рефакторинг Oduist Flow

## Контекст

Система для **одного разработчика**. Одновременные запросы крайне маловероятны.
Вместо сложной concurrency-инфраструктуры — простой mutex: если операция уже выполняется, вернуть ошибку клиенту.

## Текущая архитектура

Проект состоит из 3 файлов:

- `src/flow/server.py` — MCP transport layer (13 тулов, чистый pass-through)
- `src/flow/odoo_manager.py` — God Object (630 строк, вся логика)
- `src/flow/config.py` — сырые константы (39 строк)

## Проблемы

### 1. God Object — `odoo_manager.py`

Один файл отвечает за всё:
- Docker-оркестрация (контейнеры, сети, volumes)
- Управление PostgreSQL (SQL через `exec_run`)
- Git-операции (`subprocess.run` clone)
- Файловая система (`mkdir`, `rmtree`)
- Аллокация портов (scan 50000–50100)
- Именование ресурсов (slugify, prefixes)
- Валидация состояния системы

Нет разделения ответственностей, нет абстракций, невозможно тестировать без Docker daemon.

### 2. Нет иерархии ошибок

- `odoo_manager.py` везде поднимает generic `Exception`
- `server.py` содержит 13 одинаковых блоков `except Exception as e: return f"Error: {str(e)}"`
- Клиент не может отличить "система не инициализирована" от "контейнер не найден" от "порт занят"
- Stack trace теряется, отладка наугад

### 3. Конфигурация при импорте

`config.py` вычисляет `os.getenv()` при импорте модуля:
- Нельзя переопределить в тестах без хаков с env vars
- Нет валидации (невалидный порт обнаружится только в runtime)
- Нет типизации — просто модульные переменные

### 4. TOCTOU race condition в портах

`_get_available_port()` делает `bind()` для проверки, потом закрывает сокет. Между проверкой и запуском контейнера другой процесс может занять порт.

### 5. Микс docker-py + subprocess

`docker cp` выполняется через `subprocess.run(["docker", "cp", ...])`, всё остальное — через docker-py SDK. Непоследовательный подход, лишняя зависимость на CLI `docker`, разное поведение ошибок.

### 6. Нет логирования

Ни одного вызова `logging`. При сбоях — только строка ошибки, без контекста (branch, container, command, exit code).

## Решения, которые мы НЕ принимаем

| Отвергнуто | Причина |
|---|---|
| NATS.io как ядро | Overkill для single-dev tool. Лишний демон, async impedance mismatch с docker-py, MCP клиенты не подпишутся на NATS |
| ThreadPoolExecutor + file locks | Избыточная сложность. Одновременные запросы крайне маловероятны |
| Job model (JobStore/JobRunner/JobEvent) | Не нужен. Операции синхронные, клиент ждёт ответа |
| Async tools + polling API | Усложняет и клиент, и сервер без реальной выгоды |
| Multiple backends (podman/k8s) | Не нужны. Только Docker |
| Multiple uvicorn workers | Один процесс, один worker. Нет shared state проблем |

## Целевая архитектура

```
src/flow/
  server.py          # MCP transport: тулы + единый error handler + mutex
  settings.py        # @dataclass Settings с from_env() и validate()
  errors.py          # FlowError, NotFoundError, ConflictError, BusyError...
  models.py          # EnvironmentRef — типизированные результаты
  naming.py          # Чистые функции: slugify, get_db_name, get_resource_name

  docker/
    client.py        # get_client() — docker.from_env()
    system_ops.py    # init_system / destroy_system
    env_ops.py       # create / delete / start / stop / restart / list / status
    odoo_ops.py      # install / upgrade / test / logs
```

Бэкенд — **только Docker** (docker-py SDK). Один процесс, один worker.

### Принципы

1. **server.py** — только транспорт. Определяет MCP-тулы, маппит `FlowError` → строку. Глобальный mutex для долгих операций.
2. **docker/** — вся инфраструктурная логика, разбитая по ответственности.
3. **naming.py** — чистые функции без side effects. Тестируются тривиально.
4. **settings.py** — конфигурация создаётся явно через `Settings.from_env()` в `main()`.
5. **errors.py** — типизированная иерархия ошибок.

### Concurrency model: простой mutex

Система для одного разработчика. Вместо сложной инфраструктуры параллелизма — один `threading.Lock`:

- Долгие операции (`create/delete_environment`, `init/destroy_system`, `install/upgrade_modules`) захватывают лок
- Если лок занят → сразу `BusyError("Another operation is in progress. Try again later.")`
- Быстрые операции (`list_environments`, `get_status`, `get_logs`, `start/stop/restart`) работают без лока
- Uvicorn: 1 worker, без масштабирования

```python
import threading
from flow.errors import FlowError, BusyError

_busy = threading.Lock()

def with_mutex(fn):
    """Decorator: reject if another heavy operation is running."""
    def wrapper(*args, **kwargs):
        if not _busy.acquire(blocking=False):
            raise BusyError("Another operation is in progress. Try again later.")
        try:
            return fn(*args, **kwargs)
        finally:
            _busy.release()
    return wrapper
```

## План рефакторинга

### Фаза 1: Фундамент

- [ ] Создать `errors.py` — `FlowError`, `NotFoundError`, `PrerequisiteNotMetError`, `ConflictError`, `ExternalCommandError`, `BusyError`
- [ ] Создать `settings.py` — `@dataclass Settings` с `from_env()` и `validate()`
- [ ] Создать `naming.py` — вынести `_slugify_branch`, `_get_resource_name`, `_get_db_name`
- [ ] Создать `models.py` — `EnvironmentRef`
- [ ] Написать unit-тесты для `naming.py`

### Фаза 2: Docker layer

- [ ] Создать `docker/__init__.py`
- [ ] Создать `docker/client.py` — `get_client()`
- [ ] Создать `docker/system_ops.py` — `init_system`, `destroy_system`
- [ ] Создать `docker/env_ops.py` — `create/delete/start/stop/restart/list/status`
- [ ] Создать `docker/odoo_ops.py` — `install/upgrade/test/logs`
- [ ] Заменить `subprocess docker cp` на Docker SDK `put_archive`
- [ ] Порты: `ports={'8069/tcp': None}` — Docker аллоцирует
- [ ] Удалить `odoo_manager.py`

### Фаза 3: Transport (server.py)

- [ ] Единый error handler: `except FlowError as e: return f"Error: {e}"`
- [ ] Mutex-декоратор `with_mutex` на долгие операции
- [ ] Убрать 13 одинаковых try/except
- [ ] Обновить `config.py` → использовать `settings.py`

### Фаза 4: Logging + polish

- [ ] Добавить `logging` во все модули (branch, container, db, command, exit code)
- [ ] Настроить logging в `main()`
- [ ] Удалить `config.py`
- [ ] Обновить тесты

## Детальные рекомендации

### A. Иерархия ошибок

```python
# errors.py
class FlowError(Exception):
    """Base error for all Flow operations."""

class BusyError(FlowError):
    """Another operation is already in progress."""

class NotFoundError(FlowError):
    """Environment or resource not found."""

class PrerequisiteNotMetError(FlowError):
    """System not initialized or dependency missing."""

class ConflictError(FlowError):
    """Resource already exists."""

class ExternalCommandError(FlowError):
    """External command (git, psql, docker) failed."""
    def __init__(self, cmd: str, exit_code: int, output: str):
        self.cmd = cmd
        self.exit_code = exit_code
        self.output = output
        super().__init__(f"Command '{cmd}' failed (exit {exit_code}): {output}")
```

### B. Settings dataclass

```python
# settings.py
from dataclasses import dataclass
import os

@dataclass(frozen=True)
class Settings:
    external_host: str = "localhost"
    port_range_start: int = 50000
    port_range_end: int = 50100
    workspaces_dir: str = ""
    dump_file_path: str = ""
    db_user: str = "odoo"
    db_password: str = "odoo"
    odoo_image: str = "odoo"
    postgres_image: str = "postgres:15"
    shared_network: str = "flow-net"
    shared_db_container: str = "flow-db"
    shared_db_volume: str = "flow-db-data"
    template_db_name: str = "odoo_ref"

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            external_host=os.getenv("EXTERNAL_HOST", "localhost"),
            port_range_start=int(os.getenv("PORT_RANGE_START", "50000")),
            port_range_end=int(os.getenv("PORT_RANGE_END", "50100")),
            workspaces_dir=os.getenv("FLOW_WORKSPACES_DIR", os.path.expanduser("~/.flow/workspaces")),
            dump_file_path=os.getenv("FLOW_DUMP_PATH", os.path.expanduser("~/.flow/odoo_ref.dump")),
            db_user=os.getenv("ODOO_DB_USER", "odoo"),
            db_password=os.getenv("ODOO_DB_PASSWORD", "odoo"),
        )

    def validate(self) -> None:
        if self.port_range_start >= self.port_range_end:
            raise ValueError(f"Invalid port range: {self.port_range_start}-{self.port_range_end}")
        if not self.workspaces_dir:
            raise ValueError("workspaces_dir must be set")
```

### C. Resource model

```python
# models.py
from dataclasses import dataclass

@dataclass(frozen=True)
class EnvironmentRef:
    branch: str
    slug: str
    db_name: str
    odoo_container: str
    labels: dict[str, str]
```

### D. server.py — единый error handler + mutex

```python
import os
import threading
from fastmcp import FastMCP
from flow.errors import FlowError, BusyError
from flow.docker import system_ops, env_ops, odoo_ops

mcp = FastMCP("Flow")
_busy = threading.Lock()

def with_mutex(fn):
    def wrapper(*args, **kwargs):
        if not _busy.acquire(blocking=False):
            raise BusyError("Another operation is in progress. Try again later.")
        try:
            return fn(*args, **kwargs)
        finally:
            _busy.release()
    return wrapper

def handle_errors(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except FlowError as e:
            return f"Error: {e}"
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper

@mcp.tool()
@handle_errors
@with_mutex
def create_environment(branch_name: str, repo_url: str, version: str = "15.0") -> str:
    """Provision a new ephemeral Odoo environment for a specific branch."""
    result = env_ops.create_environment(branch_name, repo_url, version)
    return f"Environment provisioned!\nURL: {result['url']}\nDatabase: {result['database']}"

@mcp.tool()
@handle_errors
def list_environments() -> str:
    """List all managed Odoo environments. (no mutex — fast read)"""
    envs = env_ops.list_environments()
    if not envs:
        return "No active environments."
    # format...
```

### E. Заменить `subprocess docker cp` на Docker SDK

```python
import io, tarfile

def copy_file_to_container(container, src_path: str, dest_dir: str) -> None:
    with open(src_path, "rb") as f:
        data = f.read()
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        info = tarfile.TarInfo(name=os.path.basename(src_path))
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    tar_stream.seek(0)
    container.put_archive(dest_dir, tar_stream)
```

### F. Порты — пусть Docker аллоцирует

```python
container = client.containers.run(
    image,
    ports={"8069/tcp": None},  # Docker выберет свободный порт
    ...
)
container.reload()
port = container.ports["8069/tcp"][0]["HostPort"]
```

Убирает TOCTOU race condition полностью.

### G. Логирование

```python
import logging

logger = logging.getLogger("flow")

def create_environment(branch_name: str, repo_url: str, version: str) -> dict:
    logger.info("Creating environment", extra={"branch": branch_name, "version": version})
    # ...
    logger.info("Environment created", extra={"branch": branch_name, "url": url, "container": name})
```

## Риски и защитные меры

- **Регрессии при рефакторинге**: перед рефакторингом написать unit-тесты для `naming.py` (чистые функции, легко тестить)
- **Docker integration**: интеграционные тесты опциональны (`RUN_DOCKER_TESTS=1`)
- **Mutex слишком грубый?**: для single-dev достаточно. Если потребуется per-branch — легко заменить на `dict[str, Lock]`

## Чек-лист антипаттернов

| Антипаттерн | Где | Решение |
|---|---|---|
| God Object | `odoo_manager.py` — 630 строк | Разбить на `docker/system_ops`, `env_ops`, `odoo_ops` |
| Transport layer возвращает error strings | `server.py` — 13 try/except | `handle_errors` декоратор + `FlowError` |
| Generic `Exception` везде | `odoo_manager.py` | Иерархия `FlowError` |
| Import-time конфигурация | `config.py` | `Settings.from_env()` в `main()` |
| TOCTOU port selection | `_get_available_port()` | Docker аллоцирует: `ports={'8069/tcp': None}` |
| Микс Docker SDK + CLI subprocess | `docker cp` через subprocess | Docker SDK `put_archive` |
| Implicit conventions разбросаны | Имена контейнеров, лейблы | Централизовать в `EnvironmentRef` |
| Нет логирования | Весь проект | Python `logging` |
| Нет concurrency guard | Shared resources | `threading.Lock` + `BusyError` |
