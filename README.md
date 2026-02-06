# Flow

Инструмент для управления изолированными Odoo dev-окружениями на базе Docker. Каждое окружение получает собственный клон ветки, базу данных (из шаблона) и файловое хранилище (через fuse-overlayfs).

## Системные требования

- Docker
- Python 3.10+
- Git
- fuse-overlayfs (для монтирования файлового хранилища)

Установка fuse-overlayfs:

```bash
sudo apt install fuse-overlayfs
```

Устройство `/dev/fuse` должно быть доступно (есть по умолчанию в Ubuntu).

В `/etc/fuse.conf` должна быть раскомментирована строка `user_allow_other` — это необходимо, чтобы Docker daemon (root) мог обращаться к FUSE-маунтпоинтам, созданным пользователем:

```bash
sudo sed -i 's/^#user_allow_other/user_allow_other/' /etc/fuse.conf
```

## Структура путей

| Путь | Переменная окружения | По умолчанию | Описание |
|------|---------------------|--------------|----------|
| Dump файл | `FLOW_DUMP_PATH` | `~/.flow/odoo_ref.dump` | Дамп reference базы данных |
| Reference filestore | `FLOW_REF_FILESTORE_PATH` | `~/.flow/odoo_ref_filestore` | Директория с файлами reference базы |
| Рабочие директории | `FLOW_WORKSPACES_DIR` | `~/.flow/workspaces` | Корневая директория для окружений |

## Структура окружения

Для каждой ветки создаётся отдельная рабочая директория:

```
~/.flow/workspaces/{branch}/
  repo/                ← клон git-репозитория
  filestore_upper/     ← изменения файлового хранилища (overlay upper)
  filestore_work/      ← служебная директория overlay
  filestore/           ← смонтированное файловое хранилище (overlay merged)
```

Файловое хранилище организовано через overlay-монтирование:

- `odoo_ref_filestore` — read-only нижний слой, общий для всех окружений
- `filestore_upper` — индивидуальный для каждого окружения, хранит только изменения
- `filestore` — результат наложения, монтируется в контейнер
- При удалении окружения upper слой удаляется, reference filestore не затрагивается

## Docker ресурсы

- Сеть: `flow-net`
- БД контейнер: `flow-db` (PostgreSQL 15, общий для всех окружений)
- Volume: `flow-db-data`
- Template БД: `odoo_ref` (создаётся из dump файла)
