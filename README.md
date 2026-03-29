# Резервное копирование проектов (rsync + YAML)

Утилита `backup-projects` копирует заданные каталоги на локальный носитель или удалённый хост через **rsync**, используя конфигурацию в **YAML**. Поддерживаются режимы инкремента, отдельной копии с меткой времени и архивов `.tgz`. После успешного прогона создаётся **флаг успеха**; все шаги пишутся в **журнал** (файл и/или stderr).

## Требования

- **ОС:** Linux
- **Python:** 3.9+
- **Внешние программы:** `rsync`, `tar` (для режима `tgz`)
- **Python-пакеты:** `PyYAML` (см. `requirements.txt`)

Для расписания по умолчанию используется **systemd** (пользовательские unit’ы); альтернатива — **cron** (скрипт выводит строку для `crontab`).

## Установка

### Вариант A: подготовка системы (Debian/Ubuntu) и виртуальное окружение

```bash
sudo make system-deps    # rsync, python3-yaml, python3-venv, python3-pip
make deps                # создаёт .venv и ставит пакет в режиме разработки + pytest
```

Установка только зависимостей пакетного менеджера (без venv):

```bash
sudo apt-get install -y rsync python3-yaml python3-pip
pip install --user -r requirements.txt
```

### Вариант B: установка из исходников (`setup.py`)

```bash
pip install .
# или в режиме разработки
pip install -e ".[dev]"
```

После установки в `$PATH` появится команда `backup-projects`.

### Запуск из клонированного репозитория без установки

```bash
./backup-projects -c /path/to/backup-config.yaml
```

Скрипт в корне добавляет каталог репозитория в `PYTHONPATH` и вызывает тот же код, что и установленная команда.

## Настройка

1. Скопируйте пример конфигурации:

   ```bash
   cp config.example.yaml backup-config.yaml
   ```

2. Отредактируйте `backup-config.yaml`:

   - `target` — путь назначения или `user@host:/path`
   - `mode` — `update` | `copy` | `tgz`
   - `sources` — список каталогов
   - `success_flag` — относительный путь файла-флага (от корня `target` для локальной цели)
   - `log_file` — путь к журналу, `true` (каталог по XDG), `false` (только stderr), или опустите ключ для журнала по умолчанию (`$XDG_STATE_HOME/backup-projects/backup.log` или `~/.local/state/backup-projects/backup.log`)
   - опционально: `sync_delete`, `rsync_extra`

## Примеры использования

Инкрементальное обновление на флэшку:

```bash
backup-projects -c ~/backup-config.yaml
```

Указать файл журнала и подробный вывод в консоль:

```bash
backup-projects -c ./backup-config.yaml -l /var/log/backup-projects.log -v
```

Тихий stderr (уровень WARNING), журнал — из конфига или по умолчанию в файл:

```bash
backup-projects -c ./backup-config.yaml -q
```

Запуск как модуля (удобно в CI или при `PYTHONPATH`):

```bash
python3 -m backup_projects -c backup-config.yaml
```

Расписание (systemd для пользователя):

```bash
./install-backup-schedule.sh
```

Только строка для cron:

```bash
./install-backup-schedule.sh --cron
```

## Журнал (log)

- В файл пишутся сообщения уровня **DEBUG** (команды rsync/tar и шаги).
- В **stderr** по умолчанию — **INFO** и выше; `-v` включает DEBUG; `-q` оставляет WARNING и ошибки.
- Путь к файлу: ключ `log_file` в YAML, опция `-l` / `--log-file` (имеет приоритет над конфигом), либо каталог по умолчанию, если ключ `log_file` в конфиге не задан.

## Тесты

```bash
make test
# или
pytest tests/ -v
```

Интеграционные тесты пропускаются, если в системе нет `rsync`.

## Структура репозитория

| Путь | Назначение |
|------|------------|
| `backup_projects/cli.py` | Логика и точка входа |
| `backup-projects` | Обёртка для запуска из клона |
| `config.example.yaml` | Пример конфигурации |
| `install-backup-schedule.sh` | Установка таймера systemd или подсказка для cron |
| `Makefile` | `system-deps`, `deps`, `test` |
| `setup.py` | Установка пакета и консольной команды |
