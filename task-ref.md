# Консолидация переменных окружения для справочных данных

## Проблема

В проекте используются две отдельные переменные окружения для справочных данных Odoo:
- `ODUFLOW_DUMP_PATH` — путь к файлу дампа БД (`odoo_ref.dump`)
- `ODUFLOW_REF_DATA_PATH` — путь к папке с файловым хранилищем (`odoo_ref_data`)

Это создает неконсистентность: данные разделены по двум местам вместо единой структуры.

## Решение

Использовать единую переменную окружения `ODUFLOW_REF_DATA_PATH`, которая указывает на папку со следующей структурой:

```
$ODUFLOW_REF_DATA_PATH/
├── db.dump          # дамп базы данных (содержимое текущего odoo_ref.dump)
└── data/            # файловое хранилище (содержимое текущего odoo_ref_data)
```

**По умолчанию:** `$ODUFLOW_HOME/odoo_ref`

## Изменения

### 1. `src/oduflow/settings.py`
- Убрать поле `dump_file_path`
- Переименовать `ref_filestore_path` на `ref_data_path`
- Добавить методы для получения путей:
  - `get_dump_path()` → `$ref_data_path/db.dump`
  - `get_filestore_path()` → `$ref_data_path/data`

### 2. `.env.example`
- Обновить `ODUFLOW_REF_DATA_PATH` комментарий (Default: `$ODUFLOW_HOME/odoo_ref`)
- Удалить `ODUFLOW_DUMP_PATH`

### 3. `src/oduflow/docker_ops/system_ops.py`
- Заменить `settings.dump_file_path` на `settings.get_dump_path()`
- Функция `init_system()` получает `dump_path` параметром, который переопределяет `get_dump_path()`

### 4. `src/oduflow/docker_ops/env_ops.py`
- Обновить логику работы с файловым хранилищем на использование `get_filestore_path()`

### 5. `README.md`
- Обновить примеры (--dump-path → использование структуры папки)
- Обновить описание путей в секции Configuration

### 6. `tests/test_server.py`, `tests/test_odoo_manager.py`
- Обновить тестовые `Settings` на новую структуру
- Убедиться, что тесты проходят

## Обратная совместимость

Если нужна поддержка старой структуры на период миграции:
- Добавить миграционный код в `Settings.from_env()` для автоматического преобразования старых путей

## Проверка

После реализации:
```bash
pytest tests/ -m "not heavyweight"
```

Убедиться, что:
- ✅ Все тесты проходят
- ✅ Структура папки правильно создается при `init_system()`
- ✅ Дамп правильно загружается из новой структуры
