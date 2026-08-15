#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
repo_dir=${script_dir:h}
template_path="${repo_dir}/deploy/com.resource-monitor.backup.plist"
label="com.resource-monitor.backup"
launch_agents_dir="${HOME}/Library/LaunchAgents"
dest_path="${launch_agents_dir}/${label}.plist"
support_dir="${HOME}/Library/Application Support/Resource Monitor"

python_path=""
db_path="${repo_dir}/data/resource-monitor.db"
backup_destination="${support_dir}/backups"
retain_recent="96"
retain_daily="30"
interval="900"
load_service=0
dry_run=0

usage() {
  cat <<'EOF'
Usage: install-backup.sh --python PATH [options]

Options:
  --python PATH          Python interpreter for the launchd job.
  --db PATH              Live SQLite database. Default: REPO/data/resource-monitor.db
  --destination PATH     Backup directory. Default: Application Support/Resource Monitor/backups
  --retain-recent N      Keep the newest N recovery points. Default: 96
  --retain-daily N       Also keep one older recovery point per UTC day. Default: 30
  --interval SECONDS     launchd interval. Default: 900 (15 minutes)
  --load                 Bootstrap and kickstart the launchd job.
  --dry-run              Print the rendered plist without writing or loading it.
  --help                 Show this help text.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python) python_path="${2:-}"; shift 2 ;;
    --db) db_path="${2:-}"; shift 2 ;;
    --destination) backup_destination="${2:-}"; shift 2 ;;
    --retain-recent) retain_recent="${2:-}"; shift 2 ;;
    --retain-daily) retain_daily="${2:-}"; shift 2 ;;
    --interval) interval="${2:-}"; shift 2 ;;
    --load) load_service=1; shift ;;
    --dry-run) dry_run=1; shift ;;
    --help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

if [[ -z "${python_path}" ]]; then
  echo "--python is required" >&2
  exit 1
fi
if [[ ! -x "${python_path}" ]]; then
  echo "Python interpreter is not executable: ${python_path}" >&2
  exit 1
fi
if [[ ! -f "${db_path}" ]]; then
  echo "Database does not exist: ${db_path}" >&2
  exit 1
fi
for value_name in retain_recent retain_daily interval; do
  value="${(P)value_name}"
  if [[ ! "${value}" =~ '^[1-9][0-9]*$' ]]; then
    echo "--${value_name//_/-} must be a positive integer" >&2
    exit 1
  fi
done
if (( interval < 60 )); then
  echo "--interval must be at least 60 seconds" >&2
  exit 1
fi

rendered=$(
  sed \
    -e "s|__PYTHON__|${python_path}|g" \
    -e "s|__REPO__|${repo_dir}|g" \
    -e "s|__RM_DB__|${db_path}|g" \
    -e "s|__RM_BACKUP_DESTINATION__|${backup_destination}|g" \
    -e "s|__RM_RETAIN_RECENT__|${retain_recent}|g" \
    -e "s|__RM_RETAIN_DAILY__|${retain_daily}|g" \
    -e "s|__RM_BACKUP_INTERVAL__|${interval}|g" \
    -e "s|__RM_BACKUP_LOG_DIR__|${support_dir}|g" \
    "${template_path}"
)

if [[ "${dry_run}" -eq 1 ]]; then
  printf '%s\n' "${rendered}"
  exit 0
fi

mkdir -p "${launch_agents_dir}" "${support_dir}" "${backup_destination}"
chmod 700 "${support_dir}" "${backup_destination}"
printf '%s\n' "${rendered}" > "${dest_path}"
chmod 600 "${dest_path}"
plutil -lint "${dest_path}" >/dev/null

if [[ "${load_service}" -eq 1 ]]; then
  launchctl bootout "gui/$(id -u)/${label}" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "${dest_path}"
  launchctl kickstart -k "gui/$(id -u)/${label}"
fi

echo "Installed ${dest_path}"
