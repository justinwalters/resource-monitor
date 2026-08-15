#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
repo_dir=${script_dir:h}
template_path="${repo_dir}/deploy/com.resource-monitor.plist"
label="com.resource-monitor.service"
launch_agents_dir="${HOME}/Library/LaunchAgents"
dest_path="${launch_agents_dir}/${label}.plist"
secret_dir="${HOME}/Library/Application Support/Resource Monitor"
secret_dest="${secret_dir}/api-token"

python_path=""
bind_host="127.0.0.1"
port="8765"
db_path="${repo_dir}/data/resource-monitor.db"
source_token_file=""
generate_token=0
load_service=0
dry_run=0

usage() {
  cat <<'EOF'
Usage: install-service.sh --python PATH (--token-file PATH | --generate-token) [options]

Options:
  --python PATH       Python interpreter for the launchd job.
  --token-file PATH   Existing token file to copy into launchd's private store.
  --generate-token    Generate a random token in launchd's private store.
  --host HOST         Bind host for the RM service. Default: 127.0.0.1
  --port PORT         RM service port. Default: 8765
  --db PATH           SQLite database path. Default: REPO/data/resource-monitor.db
  --load              Bootstrap and kickstart the launchd job after writing the plist.
  --dry-run           Print the rendered plist path and exit without writing.
  --help              Show this help text.

The token is copied/generated at ${secret_dest} with mode 0600. It is never placed
in the plist, command-line arguments, or service logs. Replace that file and rerun
the installer with --load to rotate the token.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --python)
      python_path="${2:-}"
      shift 2
      ;;
    --token-file)
      source_token_file="${2:-}"
      shift 2
      ;;
    --generate-token)
      generate_token=1
      shift
      ;;
    --host)
      bind_host="${2:-}"
      shift 2
      ;;
    --port)
      port="${2:-}"
      shift 2
      ;;
    --db)
      db_path="${2:-}"
      shift 2
      ;;
    --load)
      load_service=1
      shift
      ;;
    --dry-run)
      dry_run=1
      shift
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "${python_path}" ]]; then
  echo "--python is required" >&2
  usage >&2
  exit 1
fi
if [[ ! -x "${python_path}" ]]; then
  echo "Python interpreter is not executable: ${python_path}" >&2
  exit 1
fi
if [[ "${generate_token}" -eq 1 && -n "${source_token_file}" ]]; then
  echo "Choose exactly one of --token-file or --generate-token" >&2
  exit 1
fi
if [[ "${generate_token}" -eq 0 && -z "${source_token_file}" ]]; then
  echo "A token source is required: use --token-file or --generate-token" >&2
  exit 1
fi
if [[ -n "${source_token_file}" && ! -f "${source_token_file}" ]]; then
  echo "Token file does not exist: ${source_token_file}" >&2
  exit 1
fi

mkdir -p "${launch_agents_dir}" "${repo_dir}/data"

if [[ "${dry_run}" -eq 0 ]]; then
  mkdir -p "${secret_dir}"
  umask 077
  if [[ "${generate_token}" -eq 1 ]]; then
    if ! command -v openssl >/dev/null 2>&1; then
      echo "openssl is required for --generate-token" >&2
      exit 1
    fi
    openssl rand -hex 32 > "${secret_dest}.tmp"
  else
    if [[ -z "$(tr -d '[:space:]' < "${source_token_file}")" ]]; then
      echo "Token file is empty" >&2
      exit 1
    fi
    cp "${source_token_file}" "${secret_dest}.tmp"
  fi
  chmod 600 "${secret_dest}.tmp"
  mv -f "${secret_dest}.tmp" "${secret_dest}"
fi

rendered=$(
  sed \
    -e "s|__PYTHON__|${python_path}|g" \
    -e "s|__REPO__|${repo_dir}|g" \
    -e "s|__RM_BIND_HOST__|${bind_host}|g" \
    -e "s|__RM_PORT__|${port}|g" \
    -e "s|__RM_DB__|${db_path}|g" \
    -e "s|__RM_TOKEN_FILE__|${secret_dest}|g" \
    "${template_path}"
)

if [[ "${dry_run}" -eq 1 ]]; then
  echo "${rendered}"
  exit 0
fi

printf '%s\n' "${rendered}" > "${dest_path}"
chmod 600 "${dest_path}"
plutil -lint "${dest_path}" >/dev/null

if [[ "${load_service}" -eq 1 ]]; then
  launchctl bootout "gui/$(id -u)/${label}" >/dev/null 2>&1 || true
  launchctl bootstrap "gui/$(id -u)" "${dest_path}"
  launchctl kickstart -k "gui/$(id -u)/${label}"
fi

echo "Installed ${dest_path}"
