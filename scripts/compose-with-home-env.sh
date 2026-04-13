#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
HOME_ENV="${HOME}/.env"

if [[ -f "${HOME_ENV}" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "${HOME_ENV}"
  set +a
fi

cd "${REPO_ROOT}"
exec docker compose "$@"
