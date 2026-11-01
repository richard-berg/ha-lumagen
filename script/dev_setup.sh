#!/usr/bin/env bash
# Set up a local Home Assistant for testing against a real Lumagen.
#
# Home Assistant runs from a core checkout's venv, with this integration
# symlinked into its config and aiolumagen installed editable from the sibling
# checkout, so a change to either takes effect on the next restart. Safe to
# run again; it only adds what is missing.
#
# HA_CORE     core checkout with a Python 3.14 .venv  (default: ../ha-dev)
# AIOLUMAGEN  aiolumagen checkout                     (default: ../aiolumagen)
# HA_CONFIG   Home Assistant config directory          (default: $HA_CORE/config)
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
core=${HA_CORE:-$(dirname "$repo")/ha-dev}
lib=${AIOLUMAGEN:-$(dirname "$repo")/aiolumagen}
config=${HA_CONFIG:-$core/config}
python=$core/.venv/bin/python

[ -x "$python" ] || { echo "No venv at $core/.venv; set HA_CORE" >&2; exit 1; }

# A manifest pin that differs from the local version would otherwise make
# Home Assistant replace the editable install from PyPI; dev_hass.sh passes
# --skip-pip-packages aiolumagen for the same reason.
"$python" -m uv pip install --python "$python" \
    -c "$core/homeassistant/package_constraints.txt" -e "$lib"

mkdir -p "$config/custom_components"
ln -sfn "$repo/custom_components/lumagen" "$config/custom_components/lumagen"

if [ ! -f "$config/configuration.yaml" ]; then
    # Lean on purpose: default_config pulls in Bluetooth, streaming and more.
    cat > "$config/configuration.yaml" <<'EOF'
frontend:
config:
history:
logbook:
usb:

logger:
  default: info
  logs:
    custom_components.lumagen: debug
    aiolumagen: debug
EOF
fi

echo "Ready. Start Home Assistant with script/dev_hass.sh, or the"
echo "'Home Assistant (Lumagen hardware)' launch configuration to debug."
