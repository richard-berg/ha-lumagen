# Project Structure

```
ha-lumagen/
├── pyproject.toml                       # dev metadata only (NOT shipped — deploy artifact is custom_components/lumagen/)
├── hacs.json                            # HACS manifest (name, country, min HA version)
├── README.md
├── LICENSE
├── .github/workflows/                   # validate.yml (hassfest + HACS), tests.yml (ruff, mypy, pytest on newest HA)
├── .vscode/launch.json                  # debug the local HA from script/dev_setup.sh
├── hardware/
│   ├── TEST_PLAN.md                     # end-to-end and adversarial tests against a real Lumagen, soak, release check
│   ├── fault_proxy.py                   # serial<->TCP proxy: audits writes, injects protocol/link faults
│   ├── lumagen_probe.py                 # HA API checks: monitor (soak), exercise, scenarios, snapshot, mark
│   └── usb_fault.sh                     # USB unplug/unbind/flap/chaos, as root on the VM that owns the adapter
├── script/
│   ├── dev_setup.sh                     # local HA: integration symlinked in, aiolumagen editable
│   └── dev_hass.sh                      # run it (skips reinstalling aiolumagen)
├── custom_components/
│   └── lumagen/                         # ← the actual integration; this is what ships
│       ├── manifest.json                # domain, deps, requirements, quality scale
│       ├── const.py                     # DOMAIN, MANUFACTURER, CONF_*, PLATFORMS tuple, timeouts, service constants
│       ├── __init__.py                  # async_setup_entry / async_unload_entry / send_raw_command service
│       ├── config_flow.py               # serial-port dropdown + ZQS01 device-info validation
│       ├── coordinator.py               # DataUpdateCoordinator wrapping aiolumagen.LumagenClient
│       ├── entity.py                    # shared base class for Lumagen entities
│       ├── binary_sensor.py             # Power, HDR active, Auto aspect (diag), Serial connected (diag)
│       ├── sensor.py                    # Model/Firmware (diagnostic) + state sensors
│       ├── button.py                    # OSD nav, direct inputs, aspects, memories, redetect aspect, etc.
│       ├── select.py                    # Input / Aspect / Memory / Sharpness sensitivity / Subtitle shift
│       ├── switch.py                    # Sharpness enabled, Game mode
│       ├── number.py                    # Sharpness level (0-7), Min fan speed (0-9)
│       ├── update.py                    # Firmware update entity (version compare, install, progress)
│       ├── firmware.py                  # Install orchestration: download, power-on/settle, client pause, restore
│       ├── release_coordinator.py       # Daily lumagen.com release-index check (HTTP here, parsing in aiolumagen)
│       ├── services.yaml                # service schemas
│       ├── strings.json                 # English UI strings (source of truth for translations)
│       └── translations/                # generated/translated locale JSON
└── tests/
    ├── conftest.py                      # shared fixtures (HA, mocked aiolumagen client)
    ├── test_config_flow.py
    ├── test_select.py
    ├── test_switch_number_select.py     # Phase 1 entity dispatch helpers
    ├── test_send_raw_command.py         # service registration / dispatch / validation
    ├── firmware_helpers.py              # synthetic release page / zip, mocked client, entry setup
    ├── test_release_coordinator.py      # release-index fetch, fail-closed, channel selection
    ├── test_update.py                   # update entity: versions, channels, gating, progress
    ├── test_firmware_install.py         # install orchestration, power-restore branches, error mapping
    └── test_firmware_gating.py          # services refused mid-update, deferred reload
```

## Conventions

- **`custom_components/lumagen/` is the deployable artifact.** Anything outside it (root `pyproject.toml`, `tests/`, etc.) is dev tooling and does not ship to user installs.
- **Domain string lives in `const.py` only.** Always import `DOMAIN` from `.const` — never hardcode `"lumagen"` in entity unique_ids, service names, or storage keys.
- **`PLATFORMS` is the single source of truth** for what gets forwarded in `async_setup_entry` and unloaded in `async_unload_entry`. Adding a platform = add the file + add to the tuple.
- **`from __future__ import annotations`** at the top of every Python module.
- **No protocol logic in the integration.** If you find yourself parsing an `!` response or formatting a `ZQ` query in `coordinator.py` or `button.py`, that logic belongs in `aiolumagen` instead.
- **Coordinator with `always_update=False`.** `LumagenState` implements `__eq__` so the coordinator can skip redundant entity writes — preserve that behavior.
- **Manifest `requirements` pins `aiolumagen` from git.** During development, `[tool.uv.sources]` in the root `pyproject.toml` redirects to the sibling repo via path. End users always get the git pin.
- **Entity unique_ids** must include the config-entry-id or device-identifying string from the Lumagen's `ZQS01` response, never the friendly name (which the user can change).
- **Strings.** UI-facing copy goes in `strings.json` and the matching `translations/en.json`. Don't hardcode user-visible strings in Python.

## Testing

- Tests use `pytest-homeassistant-custom-component`, which spins up a fake HA core. Fixtures live in `tests/conftest.py`.
- `asyncio_mode = "auto"` — async tests don't need the `@pytest.mark.asyncio` decorator.
- Mock `aiolumagen.LumagenClient` rather than running real serial I/O. The protocol layer is already tested in the `aiolumagen` repo; here, test HA-side wiring (config flow, coordinator translation, entity attributes).

## Where Things Live (Cross-Repo)

| Concern | Location |
|---|---|
| Bytes on the wire, command formatting, state parsing | `aiolumagen` |
| HA entities, config flow, coordinator, translations | This repo |
| ESP32 firmware bridging the Lumagen's serial port to the network | `esphome-lumagen` |
| Lumagen protocol PDFs, Crestron driver, Pronto codes, firmware tooling | `lumagen-research` (private repo) |

When in doubt: if it could be useful to a non-HA Python consumer, it goes in `aiolumagen`. If it's HA-shaped (entity descriptions, platforms, config flows, translations), it goes here.
