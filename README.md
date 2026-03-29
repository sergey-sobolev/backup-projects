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

   - `target` — **цель по умолчанию** (локальный путь или `user@host:/path`); используется, если у источника не заданы свои цели
   - `default_mode` — `update` | `copy` | `tgz` для источников без своего `mode` (для совместимости читается и устаревший ключ `mode`, если `default_mode` не задан)
   - `sources` — список каталогов: строка или объект с `path`, опционально `name`, **свой** `mode`, **одна цель** `target` или **несколько** `targets` (список строк; тот же `mode` для каждой копии). Если указан и `target`, и `targets`, используется только `targets`
   - `success_flag` — относительный путь файла-флага от **корневого** `target` (общий флаг после всех копий, не привязан к per-source целям)
   - `log_filename` — имя файла журнала в каталоге состояния (`$XDG_STATE_HOME/backup-projects/` или `~/.local/state/backup-projects/`); по умолчанию `backup.log`
   - `log_file` — полный путь к журналу (перекрывает только имя из `log_filename`), либо `true` / `false` / отсутствие ключа — см. раздел «Журнал»
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
- **Имя файла** в общем конфиге: `log_filename` (только basename, путь к каталогу фиксирован: `…/backup-projects/<log_filename>`).
- **Полный путь** к журналу: строка в `log_file`; она имеет приоритет над `log_filename`.
- `log_file: false` — не писать в файл (только stderr). `log_file: true` или отсутствие / `null` — файл в каталоге состояния с именем из `log_filename` (по умолчанию `backup.log`).
- Опция **`-l` / `--log-file`** переопределяет и `log_file`, и `log_filename`.

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
