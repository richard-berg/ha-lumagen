#!/usr/bin/env bash
# Run the local Home Assistant set up by dev_setup.sh. Extra arguments are
# passed to Home Assistant, e.g. --debug.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
core=${HA_CORE:-$(dirname "$repo")/ha-dev}
config=${HA_CONFIG:-$core/config}

exec "$core/.venv/bin/python" -m homeassistant -c "$config" \
    --skip-pip-packages aiolumagen "$@"
