# Tech Stack

## Language & Runtime

- **Python 3.14+** (matches HA core's pinned interpreter)
- Async throughout — every public coroutine in `aiolumagen` is awaited from HA's event loop
- `from __future__ import annotations` at the top of every module

## Home Assistant

- Minimum HA core: **2026.5.0** (`hacs.json`). This is hard — the integration relies on the `usb` integration surfacing ESPHome `serial_proxy` ports in its discovery list.
- `manifest.json` declares:
  - `dependencies: ["usb"]` — needed for the serial-port dropdown
  - `after_dependencies: ["esphome"]` — so adopted ESPHome devices have set up before us
  - `iot_class: "local_push"` — Lumagen `Full v5` reporting is the primary path; polling is fallback
  - `config_flow: true`, `integration_type: "device"`, `quality_scale: "bronze"`
  - `categories` and `connectivity` — the Marketplace browses by category; hassfest and the HACS action validate both

## Runtime Dependency

```
aiolumagen==<version>   # from PyPI
```

The integration imports from `aiolumagen` directly (client, state, exceptions, enums). During development, `pyproject.toml` overrides this with `[tool.uv.sources]` pointing at the sibling `../aiolumagen` repo via `editable = true`.

## Dev Dependencies

In the `dev` group of `pyproject.toml`:

- `pytest-homeassistant-custom-component >= 0.13` — pulls in HA core + pytest fixtures pinned to a real release
- `aiousbwatcher` — pulled explicitly because HA's `usb` integration imports it (it's an optional extra on the `homeassistant` package)
- `aioesphomeapi` — also for `usb`: its `serial_proxy_stub` imports `serialx.platforms.serial_esphome` at module scope, which imports `aioesphomeapi`, yet `usb/manifest.json` doesn't declare it (HA relies on the `esphome` integration having installed it). `aiolumagen` deliberately doesn't pull it either, so without this entry importing `usb` fails at test collection.
- `aiolumagen` (editable, via uv source override pointing at the sibling `aiolumagen` repo) — needed because integration code imports it
- `ruff >= 0.7`, `mypy >= 1.11`

### The tested HA version floats, on purpose

`pytest-homeassistant-custom-component` is what decides which `homeassistant`
release lands in `.venv`, and it's deliberately left as an open range with
`uv.lock` gitignored. This stack is developed against whatever HA is current
because that's what the author runs in production, so a floating test env
surfaces HA regressions at the same time they'd bite live.

Two consequences to be aware of rather than surprised by:

- **`uv sync` can move the HA version under you**, including mid-session after
  an unrelated `pyproject.toml` edit. If behaviour changes without a code
  change, check `.venv`'s HA version first.
- **Any HA pin quoted in docs is a snapshot.** Observed drift:
  `aioesphomeapi` went `==44.21.0` (HA 2026.5.1) → `==45.3.1` (HA 2026.7.4),
  and `serialx` `==1.7.1` → `==1.8.2`, across two minor releases. Read live
  values from `homeassistant/components/<domain>/manifest.json` instead of
  trusting a number written down anywhere.

Don't "fix" this by committing a lock or pinning the plugin.

## Tooling Configuration

- **Ruff**: `line-length = 100`, `target-version = "py314"`, lint rules: `E F W I UP B C4 SIM RUF`
- **Mypy**: `python_version = "3.14"`, `strict = false` (HA's stubs aren't strict-clean — keep false until they are), `warn_unreachable = true`
- **Pytest**: `asyncio_mode = "auto"`, `testpaths = ["tests"]`, `addopts = "-ra --strict-markers --strict-config"`

## Common Commands

```bash
uv sync                # install + sync .venv
uv run pytest          # run the test suite (uses pytest-homeassistant-custom-component)
uv run ruff check .    # lint
uv run ruff format .   # format
uv run mypy custom_components/lumagen   # type check
```

To test against a real Lumagen during development, point the integration at the sibling `aiolumagen` checkout (already wired via `[tool.uv.sources]`) and copy `custom_components/lumagen/` into your HA config dir's `custom_components/`.

## Exception Handling Contract (from `aiolumagen`)

| `aiolumagen` exception | Translation |
|---|---|
| `LumagenConnectionError` | Raise `ConfigEntryNotReady` from `async_setup_entry`, or mark device unavailable from the coordinator. From an entity action or service, `@lumagen_command` raises `HomeAssistantError` (`not_connected`), never a bare "Unknown error" |
| `LumagenCommandError` | Log a warning; don't surface to the user. Also subclasses `ValueError` |
| `LumagenFirmwareImageError` | `HomeAssistantError` (`firmware_image_invalid`): the downloaded updater is unusable; device untouched |
| `LumagenFirmwareAbortError` | `HomeAssistantError` (`firmware_aborted`): stopped before touching live firmware; nothing changed, retry |
| `LumagenFirmwareError` (other) | `HomeAssistantError` (`firmware_failed`) with the message verbatim, plus a persistent notification; power left alone |
| `LumagenReleaseIndexError` (`aiolumagen.firmware` only) | `UpdateFailed` in the release coordinator; offer no update, keep the last known release |

No `LumagenAuthError` — Lumagen has no auth. ESPHome PSK errors arrive as `LumagenConnectionError`.

No timeout exception: `aiolumagen` has nothing request/response, so it never raises one. `UpdateFailed` comes from this side — `_async_update_data` raises it when the client isn't connected — and the config flow imposes its own deadline with `asyncio.timeout`, catching the builtin `TimeoutError`.

## Distribution

- HACS custom repository (not yet in default store): `https://github.com/frankrosano/ha-lumagen`, type *Integration*
- The deployable artifact is `custom_components/lumagen/` — the top-level `pyproject.toml` is dev metadata only, not shipped
