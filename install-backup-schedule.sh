#!/usr/bin/env bash
# Устанавливает периодический запуск резервного копирования.
# Варианты: systemd --user (по умолчанию) или вывод строки для crontab.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_SCRIPT="${BACKUP_SCRIPT:-$ROOT/backup-projects}"
CONFIG="${CONFIG:-$ROOT/backup-config.yaml}"
SCHEDULE_ON_CALENDAR="${SCHEDULE_ON_CALENDAR:-daily}" # для systemd: daily, hourly, или "*-*-* 02:00:00"

usage() {
  echo "Usage: $0 [--cron] [--calendar SPEC]" >&2
  echo "  --cron       только показать строку для crontab, не трогать systemd" >&2
  echo "  --calendar   systemd OnCalendar (default: $SCHEDULE_ON_CALENDAR)" >&2
  exit 1
}

USE_CRON=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --cron) USE_CRON=true; shift ;;
    --calendar)
      SCHEDULE_ON_CALENDAR="${2:-}"
      [[ -n "$SCHEDULE_ON_CALENDAR" ]] || usage
      shift 2
      ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

if [[ ! -x "$BACKUP_SCRIPT" ]] && [[ -f "$BACKUP_SCRIPT" ]]; then
  echo "warning: $BACKUP_SCRIPT is not executable; chmod +x recommended" >&2
fi

CRON_LINE="0 2 * * * cd $(printf '%q' "$ROOT") && $(printf '%q' "$BACKUP_SCRIPT") -c $(printf '%q' "$CONFIG")"

if $USE_CRON; then
  echo "Add this line to your crontab (crontab -e):"
  echo "$CRON_LINE"
  exit 0
fi

UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"

SERVICE_FILE="$UNIT_DIR/backup-projects.service"
TIMER_FILE="$UNIT_DIR/backup-projects.timer"

# Paths may contain spaces; one argv to bash -lc keeps ExecStart parsing correct.
RUN_CMD=$(printf '%q' "cd $ROOT && exec $BACKUP_SCRIPT -c $CONFIG")

cat >"$SERVICE_FILE" <<EOF
[Unit]
Description=Project backups (rsync from YAML)

[Service]
Type=oneshot
ExecStart=/bin/bash -lc $RUN_CMD
EOF

cat >"$TIMER_FILE" <<EOF
[Unit]
Description=Timer for project backups

[Timer]
OnCalendar=$SCHEDULE_ON_CALENDAR
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now backup-projects.timer
echo "Installed user timer: backup-projects.timer"
echo "Check: systemctl --user list-timers | grep backup-projects"
echo "Logs: journalctl --user -u backup-projects.service"
