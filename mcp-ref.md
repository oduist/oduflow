# MCP Tools Refinement — ТЗ

Дополнения к набору MCP-инструментов Oduflow, нацеленные на повышение продуктивности AI-агента при разработке модулей Odoo.

Текущий набор: 32 инструмента. Предлагается: +6 новых инструментов, +3 доработки существующих.

---

## Приоритет 0 — Блокирующая проблема

### 0.1 Output Cache: кеширование + smart summary + интерактивный drill-down

**Проблема:** Odoo при install/upgrade/test выдаёт десятки тысяч строк INFO-логов (загрузка моделей, парсинг XML views, компиляция assets). Типичный `upgrade_odoo_modules` — 68K+ символов. MCP-клиенты (Claude Code, Cursor) имеют лимит на размер ответа tool — при превышении результат сохраняется в файл и агент его не видит. **Агент теряет обратную связь — не знает, была ли ошибка.**

Реальный пример:
```
oduflow_velesagro - upgrade_odoo_modules (branch_name: "manuf-plan", modules: "supply")
⎿  Error: result (68,449 characters) exceeds maximum allowed tokens.
   Output has been saved to /Users/max/.claude/projects/.../tool-results/mcp-....txt
```

**Затронутые инструменты:**
| Инструмент | Источник больших output |
|---|---|
| `install_odoo_modules` | `odoo -i` лог: загрузка всех зависимых модулей |
| `upgrade_odoo_modules` | `odoo -u` лог: перезагрузка views, assets, data |
| `run_odoo_tests` | Полный лог тестов + загрузка модулей |
| `pull_and_apply` | Делегирует в install/upgrade, передаёт их output |
| `run_odoo_command` | Произвольная команда может вернуть что угодно |
| `run_db_query` | SELECT без LIMIT на большой таблице |

---

**Решение: Output Cache — кеширование полного вывода + smart summary**

Идея: любой инструмент с потенциально большим output **кеширует полный вывод на сервере** и возвращает агенту:
1. Smart summary (ошибки, head, tail) — помещается в контекст
2. `output_id` — ключ кеша для последующих запросов
3. Метаданные (total_lines, total_chars, has_errors) — агент понимает масштаб

Агент может потом через отдельный tool `read_output` запросить из кеша:
- Конкретный диапазон строк
- Поиск по паттерну (grep)
- Только ошибки
- Полный вывод порциями

Это превращает вывод в **интерактивный буфер**, аналог `less` с поиском.

---

#### Компонент 1: `OutputCache` — серверный кеш вывода

Новый модуль `src/oduflow/output_cache.py`:

```python
from __future__ import annotations

import hashlib
import os
import time
import threading
from dataclasses import dataclass, field
from typing import Any

_CACHE_TTL = 3600        # 1 час
_MAX_ENTRIES = 50        # макс записей в кеше
_MAX_OUTPUT_SIZE = 10_000_000  # 10 MB — не кешировать аномально большое


@dataclass
class CachedOutput:
    output_id: str
    lines: list[str]
    total_chars: int
    created_at: float
    source_tool: str        # "upgrade_odoo_modules", "run_odoo_command", etc.
    source_args: str        # краткое описание: "branch=manuf-plan, modules=supply"
    error_line_indices: list[int] = field(default_factory=list)  # индексы ERROR/WARNING строк

    @property
    def total_lines(self) -> int:
        return len(self.lines)

    @property
    def has_errors(self) -> bool:
        return len(self.error_line_indices) > 0


class OutputCache:
    """Thread-safe in-memory cache for large tool outputs."""

    def __init__(self) -> None:
        self._store: dict[str, CachedOutput] = {}
        self._lock = threading.Lock()

    def store(self, output: str, source_tool: str, source_args: str) -> CachedOutput:
        """Cache output, return CachedOutput with generated ID."""
        if len(output) > _MAX_OUTPUT_SIZE:
            output = output[:_MAX_OUTPUT_SIZE]

        # Short hash ID: first 8 chars of sha256 of content + timestamp
        raw = f"{output[:1000]}{time.time()}".encode()
        output_id = hashlib.sha256(raw).hexdigest()[:8]

        lines = output.splitlines()

        # Pre-index error lines
        error_indices = []
        for i, line in enumerate(lines):
            upper = line.upper()
            if any(m in upper for m in (
                " ERROR ", " WARNING ", " CRITICAL ",
                "TRACEBACK", "RAISE ", "EXCEPTION",
            )):
                error_indices.append(i)

        entry = CachedOutput(
            output_id=output_id,
            lines=lines,
            total_chars=len(output),
            created_at=time.time(),
            source_tool=source_tool,
            source_args=source_args,
            error_line_indices=error_indices,
        )

        with self._lock:
            self._evict()
            self._store[output_id] = entry

        return entry

    def get(self, output_id: str) -> CachedOutput | None:
        with self._lock:
            entry = self._store.get(output_id)
            if entry and (time.time() - entry.created_at) > _CACHE_TTL:
                del self._store[output_id]
                return None
            return entry

    def _evict(self) -> None:
        """Remove expired entries + oldest if over limit."""
        now = time.time()
        expired = [k for k, v in self._store.items() if now - v.created_at > _CACHE_TTL]
        for k in expired:
            del self._store[k]
        while len(self._store) >= _MAX_ENTRIES:
            oldest = min(self._store, key=lambda k: self._store[k].created_at)
            del self._store[oldest]
```

**Ключевые решения:**
- In-memory (не на диск) — быстро, не нужно cleanup, кеш живёт пока жив процесс
- TTL 1 час — output от вчерашнего upgrade бесполезен
- Макс 50 записей — ограничение памяти (~50 * 10MB = 500MB worst case, обычно ~50 * 100KB = 5MB)
- Pre-index error lines при записи — поиск ошибок O(1)
- Thread-safe — MCP-сервер обслуживает несколько клиентов
- ID: 8-char hex — короткий, легко передавать, нет коллизий в пределах 50 записей

---

#### Компонент 2: `_make_summary()` — smart summary для ответа tool

Helper в `server.py`, генерирует summary из `CachedOutput`:

```python
_SUMMARY_HEAD_LINES = 20
_SUMMARY_TAIL_LINES = 30
_SUMMARY_ERROR_CONTEXT = 5  # строк контекста после каждой ошибки

def _make_summary(cached: CachedOutput) -> str:
    """Build a smart summary from cached output: head + errors + tail + metadata."""
    lines = cached.lines
    total = cached.total_lines
    parts = []

    # Head
    head = lines[:_SUMMARY_HEAD_LINES]
    parts.extend(head)

    # Errors with context (deduplicated)
    if cached.error_line_indices:
        parts.append(f"\n--- Errors/Warnings ({len(cached.error_line_indices)} occurrences) ---")
        seen = set()
        for idx in cached.error_line_indices:
            context_end = min(idx + _SUMMARY_ERROR_CONTEXT + 1, total)
            for i in range(idx, context_end):
                if i not in seen:
                    parts.append(lines[i])
                    seen.add(i)
            parts.append("")  # blank line between errors

    # Skipped count
    skip_start = _SUMMARY_HEAD_LINES
    skip_end = total - _SUMMARY_TAIL_LINES
    if skip_end > skip_start:
        parts.append(f"--- Skipped {skip_end - skip_start} lines of output ---")

    # Tail
    tail_start = max(total - _SUMMARY_TAIL_LINES, _SUMMARY_HEAD_LINES)
    parts.extend(lines[tail_start:])

    # Metadata footer
    parts.append("")
    parts.append(f"[Cached output: id={cached.output_id}, {cached.total_lines} lines, {cached.total_chars} chars]")
    parts.append(f"[Use read_output(output_id=\"{cached.output_id}\", ...) to search, read ranges, or get full output]")

    return "\n".join(parts)
```

**Что видит агент (пример):**
```
2024-01-15 12:00:00 INFO odoo.service.server: Odoo version 17.0-20240115
2024-01-15 12:00:00 INFO odoo.service.server: Using configuration file /etc/odoo/odoo.conf
... (первые 20 строк) ...

--- Errors/Warnings (3 occurrences) ---
2024-01-15 12:00:12 ERROR db odoo.modules.registry: Failed to load module supply
Traceback (most recent call last):
  File "/usr/lib/python3/dist-packages/odoo/modules/registry.py", line 92
    ...
odoo.exceptions.ValidationError: Field 'x_custom_field' already exists on model 'mrp.production'

2024-01-15 12:00:12 WARNING db odoo.modules.loading: Some modules could not be loaded

--- Skipped 1,847 lines of output ---

2024-01-15 12:00:13 INFO db odoo.modules.loading: 142 modules loaded in 12.3s
2024-01-15 12:00:13 INFO db odoo.service.server: Modules loaded.
... (последние 30 строк) ...

[Cached output: id=a3f7c012, 1897 lines, 68449 chars]
[Use read_output(output_id="a3f7c012", ...) to search, read ranges, or get full output]
```

Агент сразу видит: (a) ошибку, (b) её stacktrace, (c) масштаб пропущенного output, (d) ключ кеша для drill-down.

---

#### Компонент 3: `read_output` — MCP-инструмент для drill-down в кешированный вывод

```python
@mcp.tool()
@handle_errors
def read_output(
    output_id: str,
    mode: str = "lines",
    start: int = 1,
    end: int = 0,
    grep: str = "",
    ctx: Context = None,
) -> str:
    """
    Read from a cached tool output by its ID.

    After calling tools like install_odoo_modules, upgrade_odoo_modules,
    run_odoo_tests, etc., large outputs are cached on the server. The tool
    response includes an output_id and metadata. Use this tool to explore
    the cached output interactively.

    Modes:
    - "lines" (default): Return a range of lines. Use start/end for pagination
      (1-indexed). Default: first 200 lines. Example: start=100, end=200.
    - "errors": Return only ERROR/WARNING/CRITICAL lines with ±5 lines of context.
    - "grep": Search for a pattern (case-insensitive substring). Returns matching
      lines with line numbers. Combine with start/end to paginate results.
    - "info": Return metadata only — line count, char count, error count, source tool.
    - "tail": Return last 100 lines.

    Args:
        output_id: The cached output ID (e.g. "a3f7c012"), returned by the original tool.
        mode: Read mode — "lines", "errors", "grep", "info", "tail".
        start: First line number to return (1-indexed, default 1). Used with mode="lines" and "grep".
        end: Last line number to return (0 = start+200 for "lines", all results for "grep").
        grep: Search pattern for mode="grep". Case-insensitive substring match.
    """
```

**Реализация (server.py):**

```python
_output_cache = OutputCache()

@mcp.tool()
@handle_errors
def read_output(
    output_id: str,
    mode: str = "lines",
    start: int = 1,
    end: int = 0,
    grep: str = "",
    ctx: Context = None,
) -> str:
    cached = _output_cache.get(output_id)
    if cached is None:
        return f"Output '{output_id}' not found or expired (TTL: 1 hour)."

    lines = cached.lines
    total = cached.total_lines

    if mode == "info":
        return (
            f"Cached output: {output_id}\n"
            f"Source: {cached.source_tool}({cached.source_args})\n"
            f"Lines: {total}\n"
            f"Characters: {cached.total_chars}\n"
            f"Errors/Warnings: {len(cached.error_line_indices)} lines\n"
            f"Age: {int(time.time() - cached.created_at)}s"
        )

    if mode == "errors":
        if not cached.error_line_indices:
            return "No errors or warnings found in cached output."
        result_lines = []
        seen = set()
        for idx in cached.error_line_indices:
            ctx_start = max(0, idx - 2)
            ctx_end = min(total, idx + 6)
            for i in range(ctx_start, ctx_end):
                if i not in seen:
                    result_lines.append(f"{i+1:>6}| {lines[i]}")
                    seen.add(i)
            result_lines.append("")
        return "\n".join(result_lines)

    if mode == "grep":
        if not grep:
            return "Error: grep parameter is required for mode='grep'."
        pattern = grep.lower()
        matches = []
        for i, line in enumerate(lines):
            if pattern in line.lower():
                matches.append(f"{i+1:>6}| {line}")
        if not matches:
            return f"No matches for '{grep}' in {total} lines."
        # Paginate
        s = max(start - 1, 0)
        e = end if end > 0 else s + 200
        page = matches[s:e]
        header = f"Matches for '{grep}': {len(matches)} total (showing {s+1}-{min(e, len(matches))})"
        return header + "\n" + "\n".join(page)

    if mode == "tail":
        tail = lines[-100:]
        start_num = total - len(tail) + 1
        numbered = [f"{start_num+i:>6}| {l}" for i, l in enumerate(tail)]
        return f"Last {len(tail)} lines (of {total}):\n" + "\n".join(numbered)

    # mode == "lines" (default)
    s = max(start - 1, 0)
    e = end if end > 0 else s + 200
    e = min(e, total)
    page = lines[s:e]
    numbered = [f"{s+i+1:>6}| {l}" for i, l in enumerate(page)]
    header = f"Lines {s+1}-{e} of {total}:"
    return header + "\n" + "\n".join(numbered)
```

---

#### Компонент 4: интеграция с существующими tools

Каждый tool, возвращающий потенциально большой output, проходит через кеш:

```python
# Новый порог — если output превышает, кешируем и возвращаем summary
_CACHE_THRESHOLD = 5_000  # символов (~1.2K tokens)


@mcp.tool()
@handle_errors
@with_branch_lock
def upgrade_odoo_modules(branch_name: str, modules: str, ctx: Context = None) -> str:
    """
    Upgrade Odoo modules in an environment.

    Args:
        branch_name: The name of the branch/environment.
        modules: Comma-separated list of modules to upgrade.
    """
    modules_list = [m.strip() for m in modules.split(",") if m.strip()]
    if not modules_list:
        return "Error: At least one module name is required."

    settings = _get_settings()
    team = _resolve_team(ctx)
    result = odoo_ops.upgrade_odoo_modules(settings, team, branch_name, *modules_list)
    exit_code = result["exit_code"]
    modules_str = ", ".join(result["modules"])
    output = result.get("output", "")

    status = "Success" if exit_code == 0 else "Error"
    header = f"{status}. Modules: {modules_str}. Exit code: {exit_code}."

    # Cache large output, return summary
    if len(output) > _CACHE_THRESHOLD:
        cached = _output_cache.store(
            output,
            source_tool="upgrade_odoo_modules",
            source_args=f"branch={branch_name}, modules={modules}",
        )
        return f"{header}\n\n{_make_summary(cached)}"

    return f"{header}\n\nOutput:\n{output}"
```

**Применить кеширование к:**
- `install_odoo_modules` — аналогично
- `upgrade_odoo_modules` — аналогично
- `run_odoo_tests` — аналогично
- `pull_and_apply` — кешировать `result["output"]`
- `run_odoo_command` — кешировать при превышении порога
- `run_db_query` — кешировать + добавить `max_rows` параметр для защиты на уровне запроса

**Для `run_db_query` — дополнительная защита:**

```python
@mcp.tool()
def run_db_query(
    branch_name: str,
    query: str,
    output_format: str = "csv",
    max_rows: int = 100,
    ctx: Context = None,
) -> str:
    """
    ...
    Args:
        ...
        max_rows: Maximum rows to return (default 100). The query itself is not
                  modified — truncation happens on the output. If more rows are
                  available, a note is appended suggesting to add LIMIT to the query.
    """
```

Реализация: обрезать вывод по количеству строк CSV/таблицы, добавлять `... (showing 100 of N+ rows, add LIMIT to your query)`.

---

#### Пример workflow агента с кешем

```
Агент: upgrade_odoo_modules("manuf-plan", "supply")

→ Ответ (помещается в контекст):
  Error. Modules: supply. Exit code: 1.

  2024-01-15 INFO odoo.service.server: Odoo version 17.0
  ...

  --- Errors/Warnings (3 occurrences) ---
  2024-01-15 ERROR odoo.modules.registry: Failed to load module supply
  Traceback (most recent call last):
    ...
  odoo.exceptions.ValidationError: Field 'x_custom' already exists

  --- Skipped 1,847 lines of output ---
  ...

  [Cached output: id=a3f7c012, 1897 lines, 68449 chars]
  [Use read_output(output_id="a3f7c012", ...) to search or read ranges]

Агент видит ошибку, фиксит код, пушит, вызывает pull_and_apply.
Если нужно больше контекста:

Агент: read_output("a3f7c012", mode="grep", grep="supply")
→ 15 строк, где упоминается модуль supply

Агент: read_output("a3f7c012", mode="lines", start=450, end=500)
→ строки 450-500 — контекст вокруг ошибки

Агент: read_output("a3f7c012", mode="info")
→ метаданные: 1897 lines, 68449 chars, 3 errors, age 45s
```

---

#### Обзор решения

| Аспект | Решение |
|---|---|
| **Дефолтное поведение** | output < 5K → как раньше. output ≥ 5K → кеш + summary |
| **Обратная совместимость** | Полная: мелкие output не затронуты. Новый параметр tools не добавляется (кеширование прозрачно) |
| **Агент без поддержки кеша** | Работает: summary содержит ошибки + head + tail, достаточно для 90% случаев |
| **Агент с поддержкой кеша** | Может drill-down: grep, line ranges, errors-only |
| **Хранение** | In-memory, TTL 1 час, макс 50 записей |
| **Thread safety** | `threading.Lock` в OutputCache |
| **Новые сущности** | 1 модуль (`output_cache.py`), 1 MCP tool (`read_output`), 1 helper (`_make_summary`) |

**Обновить `agent_instructions.md`:** новая секция "Working with Large Outputs" — объяснить агенту паттерн кеша, когда использовать `read_output`, примеры grep и line ranges.

**Тесты:**
- Unit: OutputCache.store → корректный ID, lines, error_indices
- Unit: OutputCache TTL — expired entry возвращает None
- Unit: OutputCache eviction — при 51-й записи удаляется старейшая
- Unit: _make_summary — head + errors + tail + metadata footer
- Unit: output < 5K → возвращается без кеширования
- Unit: output 68K с ERROR → кеш + summary с ошибками
- Unit: read_output mode="lines" с пагинацией
- Unit: read_output mode="grep" — поиск, пагинация результатов
- Unit: read_output mode="errors" — только error lines с контекстом
- Unit: read_output mode="info" — метаданные
- Unit: read_output mode="tail" — последние 100 строк
- Unit: read_output с несуществующим output_id → "not found"
- Unit: run_db_query с max_rows=10 → обрезка + примечание
- Integration: upgrade_odoo_modules → read_output("...", mode="grep", grep="ERROR")

---

## Приоритет 1 — Критичные

### 1.1 `write_file_in_odoo`

**Проблема:** Единственный способ записать файл в контейнер — `run_odoo_command` с `echo`/`cat heredoc`. Ломается на многострочном контенте, спецсимволах, бинарных данных.

**Сигнатура:**

```python
@mcp.tool()
@handle_errors
@with_branch_lock
def write_file_in_odoo(
    branch_name: str,
    path: str,
    content: str,
    user: str = "odoo",
    ctx: Context = None,
) -> str:
    """
    Write a text file inside the Odoo container.

    Creates parent directories if they don't exist. Overwrites the file if it
    already exists. Content is transferred via container stdin to avoid
    shell escaping issues.

    Common use cases:
    - Write CSV files for data import
    - Create/modify odoo.conf settings
    - Write one-off Python scripts for odoo shell execution
    - Place test fixture files (demo data, config)

    Do NOT use this to edit source code in the repository — all code changes
    must go through git commit → git push → pull_and_apply.

    Args:
        branch_name: The name of the branch/environment.
        path: Absolute path inside the container (e.g. "/tmp/import_data.csv").
        content: Text content to write to the file.
        user: OS user to own the file (default "odoo"). Use "root" for system paths.
    """
```

**Реализация (odoo_ops.py):**

```python
def write_file_in_environment(
    settings: Settings, branch_name: str, path: str, content: str, user: str = "odoo"
) -> dict[str, Any]:
    client = get_client()
    container_name = get_resource_name(branch_name, "odoo", settings.prefix)
    container = client.containers.get(container_name)  # NotFound → raise

    # Ensure parent directory exists
    parent = os.path.dirname(path)
    container.exec_run(["mkdir", "-p", parent], user=user)

    # Transfer content via tar stream to avoid shell escaping
    import io, tarfile
    data = content.encode("utf-8")
    tar_stream = io.BytesIO()
    with tarfile.open(fileobj=tar_stream, mode="w") as tar:
        info = tarfile.TarInfo(name=os.path.basename(path))
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    tar_stream.seek(0)
    container.put_archive(parent, tar_stream)

    # Fix ownership if needed
    if user != "root":
        container.exec_run(["chown", user, path], user="root")

    return {"path": path, "size": len(data)}
```

**Ключевые решения:**
- Использовать `put_archive` (Docker API) вместо shell — полностью исключает проблемы экранирования
- Лимит размера: 1 MB (soft limit, проверка в server.py)
- Создавать parent dirs автоматически
- Лок: `@with_branch_lock` — запись конкурирует с другими операциями

**Тесты:**
- Unit: запись файла, проверка содержимого через `read_file_in_environment`
- Unit: запись в несуществующую директорию (auto-mkdir)
- Unit: файл с Unicode, спецсимволами, пустой файл
- Unit: превышение лимита размера → ToolError
- Integration: запись CSV → импорт через run_odoo_command

---

### 1.2 `run_odoo_shell`

**Проблема:** Нет способа корректно выполнить Python-код в контексте Odoo ORM (с доступом к `self.env`, моделям, registry). Через `run_odoo_command` multi-line Python передаётся ненадёжно.

**Сигнатура:**

```python
@mcp.tool()
@handle_errors
@with_branch_lock
def run_odoo_shell(
    branch_name: str,
    python_code: str,
    ctx: Context = None,
) -> str:
    """
    Execute Python code in the Odoo shell context with full ORM access.

    The code runs inside `odoo shell` with access to `self.env`, all Odoo
    models, and the environment's database. Use `print()` to produce output
    that will be returned to you.

    Common use cases:
    - Test computed fields: print(self.env['sale.order'].search([]).mapped('amount_total'))
    - Create test records: self.env['res.partner'].create({'name': 'Test'})
    - Inspect models: print(self.env['ir.model.fields'].search([('model','=','sale.order')]).mapped('name'))
    - Debug business logic: check workflow transitions, access rights
    - Run data-fix scripts

    The code is executed in a single transaction that is committed at the end.
    If the code raises an exception, the transaction is rolled back and the
    traceback is returned.

    Args:
        branch_name: The name of the branch/environment.
        python_code: Python code to execute. Use print() for output.
    """
```

**Реализация (odoo_ops.py):**

```python
def run_odoo_shell(
    settings: Settings, team: TeamSettings, branch_name: str, python_code: str
) -> dict[str, Any]:
    client = get_client()
    container_name = get_resource_name(branch_name, "odoo", settings.prefix)
    env_db = get_db_name(branch_name, team.team_id)
    container = client.containers.get(container_name)  # NotFound → raise

    creds = load_credentials(
        branch_name, team.workspaces_dir, settings.db_user, settings.db_password
    )

    # Write code to temp file to avoid shell escaping issues
    # (reuse write_file_in_environment or put_archive directly)
    script_path = "/tmp/_oduflow_shell_script.py"
    # ... write python_code to script_path via put_archive ...

    cmd = (
        f"odoo shell --no-http --stop-after-init "
        f"--db_host={settings.shared_db_container} "
        f"-r {creds['pg_user']} -w {creds['pg_password']} "
        f"--database={env_db} "
        f"< {script_path}"
    )

    exit_code, output = container.exec_run(
        ["sh", "-c", cmd],
        user="odoo",
    )
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)

    return {
        "exit_code": exit_code,
        "output": output_str,
    }
```

**Ключевые решения:**
- Python-код пишется во временный файл через `put_archive`, затем подаётся через stdin в `odoo shell` — исключает любые проблемы экранирования
- `--no-http --stop-after-init` — shell не запускает HTTP-сервер, завершается после выполнения
- Timeout: 120 секунд (параметр `exec_run`), чтобы бесконечные циклы не вешали контейнер
- Лок: `@with_branch_lock` — shell-сессия конкурирует с другими операциями на этом окружении
- Удалять tmp-файл после выполнения

**Тесты:**
- Unit: `print(1+1)` → `"2"` в output
- Unit: `print(self.env['res.partner'].search_count([]))` → число
- Unit: код с синтаксической ошибкой → traceback
- Unit: multi-line код с кавычками, f-strings, Unicode
- Integration: создание записи через ORM → проверка через `run_db_query`

---

### 1.3 `http_request_to_odoo`

**Проблема:** Агент не может протестировать web controllers, JSON-RPC API, REST endpoints с точки зрения клиента. Единственный workaround — `run_odoo_command` + `curl`.

**Сигнатура:**

```python
@mcp.tool()
@handle_errors
def http_request_to_odoo(
    branch_name: str,
    path: str,
    method: str = "GET",
    body: str = "",
    headers: str = "",
    session_id: str = "",
    ctx: Context = None,
) -> str:
    """
    Make an HTTP request to the running Odoo instance for a specific branch.

    Useful for testing web controllers, JSON-RPC API, REST endpoints, and
    verifying that Odoo responds correctly. The request is made from the
    host to the container's mapped port.

    Common use cases:
    - Health check: GET /web/health
    - JSON-RPC call: POST /jsonrpc with JSON body
    - Test a custom controller: GET /my/custom/endpoint
    - Verify access rights: check 200 vs 403 responses
    - Test REST API endpoints

    Args:
        branch_name: The name of the branch/environment.
        path: URL path (e.g. "/web/health", "/jsonrpc", "/my/invoices").
        method: HTTP method (default "GET"). One of GET, POST, PUT, DELETE.
        body: Request body as a string (typically JSON). Empty for GET requests.
        headers: Comma-separated KEY:VALUE pairs (e.g. "Content-Type:application/json,Accept:text/html").
        session_id: Odoo session ID for authenticated requests. Obtain by calling POST /web/session/authenticate first.
    """
```

**Реализация (odoo_ops.py):**

```python
def http_request_to_odoo(
    settings: Settings, team: TeamSettings, branch_name: str,
    path: str, method: str = "GET", body: str = "",
    headers: dict[str, str] | None = None, session_id: str = "",
) -> dict[str, Any]:
    import urllib.request
    import urllib.error

    # Resolve environment URL
    info = get_environment_info(settings, team, branch_name)
    base_url = info["url"]  # e.g. http://localhost:50042

    url = f"{base_url}{path}"
    req_headers = headers or {}
    if session_id:
        req_headers["Cookie"] = f"session_id={session_id}"

    data = body.encode("utf-8") if body else None
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            response_body = resp.read().decode("utf-8", errors="replace")
            return {
                "status_code": resp.status,
                "headers": dict(resp.headers),
                "body": response_body[:100_000],  # 100KB limit
            }
    except urllib.error.HTTPError as e:
        response_body = e.read().decode("utf-8", errors="replace")
        return {
            "status_code": e.code,
            "headers": dict(e.headers),
            "body": response_body[:100_000],
        }
    except urllib.error.URLError as e:
        return {
            "status_code": 0,
            "headers": {},
            "body": f"Connection failed: {e.reason}",
        }
```

**Ключевые решения:**
- Использовать `urllib.request` из stdlib (нет внешних зависимостей)
- Резолвить URL из `get_environment_info` — агент не обязан знать порт
- Ограничение body ответа: 100 KB — защита от огромных HTML-страниц
- Timeout: 30 секунд
- Без лока — read-only операция, не мешает другим
- `session_id` параметр — для authenticated requests (агент сначала делает POST /web/session/authenticate, получает session_id, потом использует его)

**Формат ответа в server.py:**

```python
lines = [
    f"HTTP {result['status_code']}",
    f"Headers: {json.dumps(dict(list(result['headers'].items())[:20]), indent=2)}",
    f"\nBody:\n{result['body'][:50000]}",  # truncate for MCP response
]
```

**Тесты:**
- Unit: mock urllib → проверка формирования URL, headers, body
- Integration: GET /web/health → 200 OK
- Integration: POST /jsonrpc → JSON response
- Integration: GET несуществующий path → 404

---

### 1.4 Доработка `get_environment_logs`: фильтрация и поиск

**Проблема:** `get_environment_logs(n_lines)` возвращает последние N строк. Ошибка, произошедшая 500 строк назад, не будет найдена.

**Изменение сигнатуры (server.py):**

```python
@mcp.tool()
@handle_errors
def get_environment_logs(
    branch_name: str,
    n_lines: int = 100,
    grep: str = "",
    level: str = "",
    ctx: Context = None,
) -> str:
    """
    Get the last N lines of logs from the Odoo container for a specific branch.

    Args:
        branch_name: The name of the branch/environment.
        n_lines: The number of recent log lines to retrieve (default 100).
        grep: Filter logs to only show lines matching this pattern (case-insensitive substring search). Useful to find specific errors, modules, or messages.
        level: Filter by Odoo log level. One of: "ERROR", "WARNING", "CRITICAL". Returns only lines containing the specified level marker. Can be combined with grep.
    """
```

**Реализация (изменение в odoo_ops.py):**

```python
def get_environment_logs(
    settings: Settings, branch_name: str, n_lines: int = 100,
    grep: str = "", level: str = "",
) -> str:
    # Fetch more lines when filtering to have a meaningful result
    fetch_lines = n_lines * 10 if (grep or level) else n_lines
    fetch_lines = min(fetch_lines, 10_000)  # cap at 10k

    # ... existing code to get logs ...
    logs_str = logs.decode("utf-8")

    if grep or level:
        lines = logs_str.splitlines()
        filtered = []
        for line in lines:
            if level and f" {level} " not in line.upper():
                continue
            if grep and grep.lower() not in line.lower():
                continue
            filtered.append(line)
        # Return last n_lines of filtered results
        return "\n".join(filtered[-n_lines:])

    return logs_str
```

**Ключевые решения:**
- При наличии фильтра запрашивать `n_lines * 10` строк (до 10 000), чтобы фильтрация возвращала осмысленное количество результатов
- `grep` — case-insensitive substring (не regex, чтобы агент не ошибался с экранированием)
- `level` — точное совпадение по маркеру Odoo: ` ERROR `, ` WARNING `, ` CRITICAL `
- Обратная совместимость: без новых параметров поведение идентично текущему

**Аналогичная доработка для `get_service_logs`:** добавить тот же `grep` параметр.

**Обновить agent_instructions.md:** раздел Debugging & Logs — описать новые возможности фильтрации.

**Тесты:**
- Unit: фильтрация по level=ERROR выбирает только ERROR-строки
- Unit: grep="ValidationError" находит строки с этой подстрокой
- Unit: level + grep комбинируются
- Unit: без фильтров — поведение не изменилось

---

## Приоритет 2 — Важные улучшения

### 2.1 `list_installed_modules`

**Проблема:** Самая частая проверка — «какие модули установлены?» — требует ручного SQL через `run_db_query`.

**Сигнатура:**

```python
@mcp.tool()
@handle_errors
def list_installed_modules(
    branch_name: str,
    name_filter: str = "",
    state_filter: str = "installed",
    ctx: Context = None,
) -> str:
    """
    List Odoo modules and their states in an environment.

    Returns a table of module name, state, and installed version. By default
    shows only installed modules. Use state_filter="" to show all modules.

    Args:
        branch_name: The name of the branch/environment.
        name_filter: Filter modules by name (substring match, e.g. "sale" matches "sale", "sale_management", "pos_sale").
        state_filter: Filter by module state (default "installed"). Common values: "installed", "uninstalled", "to upgrade", "to install". Pass empty string to show all states.
    """
```

**Реализация:** обёртка над `run_db_query`:

```python
query = "SELECT name, state, latest_version FROM ir_module_module"
conditions = []
if state_filter:
    conditions.append(f"state = '{state_filter}'")
if name_filter:
    conditions.append(f"name ILIKE '%{name_filter}%'")
if conditions:
    query += " WHERE " + " AND ".join(conditions)
query += " ORDER BY name"
```

**Ключевые решения:**
- Реализовать через `run_db_query` внутренне (не дублировать логику подключения к БД)
- Формат вывода: таблица name | state | version для удобства агента
- SQL injection: использовать параметризацию через `psql -v` или экранирование — **name_filter и state_filter нуждаются в санитизации** (только alphanumeric + underscore + пробел)
- Без лока — read-only

**Тесты:**
- Unit: вызов без фильтров → список установленных модулей
- Unit: name_filter="sale" → только модули с "sale" в имени
- Unit: state_filter="" → все модули
- Unit: name_filter с SQL injection → ToolError

---

### 2.2 Доработка `restart_environment` / `start_environment`: wait_for_ready

**Проблема:** После restart/start Odoo нужно время на загрузку (5–60+ секунд). Агент не знает, когда можно делать следующее действие.

**Изменение сигнатуры:**

```python
@mcp.tool()
@handle_errors
def restart_environment(
    branch_name: str,
    wait: bool = True,
    ctx: Context = None,
) -> str:
    """
    Restart the Odoo container for a specific branch.

    Args:
        branch_name: The name of the branch/environment to restart.
        wait: Wait for Odoo to become ready after restart (default True). Polls /web/health every 2 seconds for up to 120 seconds.
    """
```

**Реализация (env_ops.py):**

```python
def _wait_for_odoo_ready(settings: Settings, team: TeamSettings, branch_name: str, timeout: int = 120) -> bool:
    """Poll Odoo /web/health until it responds 200 or timeout."""
    import urllib.request
    import urllib.error
    import time

    info = get_environment_info(settings, team, branch_name)
    url = f"{info['url']}/web/health"

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False
```

**Ключевые решения:**
- По умолчанию `wait=True` — агент ожидает готовности (меняет текущее поведение, но в лучшую сторону)
- Health check: `GET /web/health` (доступен в Odoo 16+; для ранних версий — `GET /web/login`)
- Timeout: 120 секунд
- Polling interval: 2 секунды
- Если timeout — вернуть предупреждение, а не ошибку
- Применить к `restart_environment` и `start_environment`

**Тесты:**
- Unit: mock urlopen → readiness через 3 poll-а → success
- Unit: mock urlopen → всегда fail → timeout warning
- Integration: restart → wait → get_environment_logs (нет ошибок)

---

### 2.3 `search_in_odoo`

**Проблема:** Чтобы найти определение поля, метода, или использование модели в Odoo source, агент вынужден делать `run_odoo_command("grep -rn 'pattern' /path")`.

**Сигнатура:**

```python
@mcp.tool()
@handle_errors
def search_in_odoo(
    branch_name: str,
    pattern: str,
    path: str = "/mnt/extra-addons",
    glob: str = "*.py",
    max_results: int = 50,
    ctx: Context = None,
) -> str:
    """
    Search for a pattern in files inside the Odoo container.

    Runs a recursive grep inside the container and returns matching lines
    with file paths and line numbers. Useful for finding field definitions,
    method implementations, model usage across addons.

    Common use cases:
    - Find where a field is defined: pattern="x_custom_field", path="/mnt/extra-addons"
    - Find model usage: pattern="class SaleOrder", path="/usr/lib/python3/dist-packages/odoo/addons"
    - Find all imports of a module: pattern="from odoo.addons.sale"
    - Find XML record: pattern='id="action_sale_order"', glob="*.xml"

    Args:
        branch_name: The name of the branch/environment.
        pattern: Search pattern (fixed string, case-sensitive). Regex is not supported to avoid escaping issues.
        path: Directory to search in (default "/mnt/extra-addons"). Use "/usr/lib/python3/dist-packages/odoo/addons" to search Odoo core.
        glob: File glob pattern (default "*.py"). Use "*.xml" for views/data, "*.js" for frontend, "*" for all files.
        max_results: Maximum number of matching lines to return (default 50).
    """
```

**Реализация (odoo_ops.py):**

```python
def search_in_environment(
    settings: Settings, branch_name: str,
    pattern: str, path: str = "/mnt/extra-addons",
    glob: str = "*.py", max_results: int = 50,
) -> dict[str, Any]:
    client = get_client()
    container_name = get_resource_name(branch_name, "odoo", settings.prefix)
    container = client.containers.get(container_name)

    # Use grep -rn --include for reliable search
    # -F = fixed string (no regex), -n = line numbers, -r = recursive
    cmd = [
        "grep", "-rn", "-F", "--include", glob,
        "-m", str(max_results),  # max matches per file
        pattern, path,
    ]

    exit_code, output = container.exec_run(cmd)
    output_str = output.decode("utf-8") if isinstance(output, bytes) else str(output)

    lines = output_str.strip().splitlines()[:max_results]

    return {
        "matches": len(lines),
        "output": "\n".join(lines),
        "truncated": len(output_str.strip().splitlines()) > max_results,
    }
```

**Ключевые решения:**
- `grep -F` (fixed string) вместо regex — агенты часто ошибаются с regex escaping
- `--include` для фильтрации по расширению файлов
- `max_results` ограничивает вывод — grep по всему Odoo source может вернуть тысячи строк
- Без лока — read-only операция
- Формат вывода: `filepath:lineno:line_content` (стандартный grep output)

**Тесты:**
- Unit: поиск по существующему паттерну → совпадения с номерами строк
- Unit: поиск по несуществующему паттерну → "No matches found"
- Unit: glob="*.xml" → ищет только в XML
- Unit: max_results=5 → не более 5 строк
- Integration: поиск определения модели в Odoo core

---

## Обзор изменений по файлам

| Файл | Изменения |
|---|---|
| `src/oduflow/output_cache.py` | **Новый модуль**: `OutputCache` class (store, get, evict), `CachedOutput` dataclass |
| `src/oduflow/server.py` | +`_output_cache` singleton, +`_make_summary()` helper, +`read_output` MCP tool, кеширование в 6 existing tools, +`max_rows` к run_db_query, +5 новых tool-функций (write_file_in_odoo, run_odoo_shell, http_request_to_odoo, list_installed_modules, search_in_odoo), +2 изменения существующих (get_environment_logs, restart_environment), переименование exec_in_odoo → run_odoo_command |
| `src/oduflow/docker_ops/odoo_ops.py` | +4 новых функции (write_file, run_odoo_shell, search_in, http_request), изменение get_environment_logs, переименование exec_in_environment → run_command_in_environment |
| `src/oduflow/docker_ops/env_ops.py` | +1 функция (_wait_for_odoo_ready), изменение restart/start |
| `docs/mcp-tools.md` | Обновить таблицу инструментов (+6 строк), добавить примечание об output truncation, переименование exec_in_odoo → run_odoo_command |
| `src/oduflow/templates/agent_guides/agent_instructions.md` | Обновить разделы Debugging & Logs, добавить секции про новые инструменты, описать output_mode |
| `tests/test_odoo_ops.py` | Новые тесты для всех добавленных функций |
| `tests/test_server.py` | Новые тесты для MCP tool-обёрток + тесты smart truncation |

---

## Порядок реализации

```
Фаза 0 (блокер — делать первым):
  0a. output_cache.py — модуль OutputCache + CachedOutput
  0b. server.py — _make_summary(), read_output tool, интеграция кеша в 6 tools + max_rows

Фаза 1 (последовательно, есть зависимости):
  1. write_file_in_odoo — фундамент для run_odoo_shell
  2. run_odoo_shell — зависит от (1)

Фаза 2 (параллельно, нет зависимостей между собой):
  3. Доработка get_environment_logs (grep + level)
  4. list_installed_modules
  5. search_in_odoo
  6. http_request_to_odoo

Фаза 3 (зависит от фазы 2):
  7. Доработка restart/start (wait_for_ready) — использует http util из (6)
```
