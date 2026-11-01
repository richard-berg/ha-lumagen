#!/usr/bin/env bash
# Run the local Home Assistant set up by dev_setup.sh. Extra arguments are
# passed to Home Assistant, e.g. --debug.
#
# A restart from Home Assistant (Settings, or homeassistant.restart) exits
# with code 100 and expects a supervisor to start it again; this script does.
set -uo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
core=${HA_CORE:-$(dirname "$repo")/ha-dev}
config=${HA_CONFIG:-$core/config}

while true; do
    "$core/.venv/bin/python" -m homeassistant -c "$config" \
        --skip-pip-packages aiolumagen "$@"
    code=$?
    [ "$code" -eq 100 ] || exit "$code"
    echo "Home Assistant asked to restart; starting it again" >&2
done
