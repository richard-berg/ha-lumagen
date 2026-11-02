# ha-lumagen

[![Validate](https://github.com/frankrosano/ha-lumagen/actions/workflows/validate.yml/badge.svg)](https://github.com/frankrosano/ha-lumagen/actions/workflows/validate.yml)
[![Tests](https://github.com/frankrosano/ha-lumagen/actions/workflows/tests.yml/badge.svg)](https://github.com/frankrosano/ha-lumagen/actions/workflows/tests.yml)

Home Assistant custom integration for a [Lumagen Radiance Pro](https://www.lumagen.com/) video processor.

This integration is a thin wrapper over [`aiolumagen`](https://github.com/frankrosano/aiolumagen). All protocol work — parsing, state tracking, commands — lives in the library; the integration adds HA lifecycle, entities, and a config flow.

## Topology

```
Home Assistant              Lumagen Radiance Pro
  │                              ▲
  │  (native serial_proxy)       │
  ▼                              │
ESPHome integration ─── esphome://host:6053/?port_name=Lumagen&key=...
  │                              │
  ▼                              │
esphome-lumagen firmware ── USB  │
  │                              │
  └──────── USB-C → USB-B ───────┘
```

The ESPHome bridge (the `esphome-lumagen` firmware) exposes the Lumagen's USB-B serial port to Home Assistant via ESPHome's `serial_proxy` component. When you adopt the ESPHome device in HA, HA automatically lists its serial proxies alongside any physical `/dev/tty*` ports. This integration's config flow presents that list as a dropdown — no host/PSK fields to fill in.

Direct RS-232 (cabled from your HA host to the Lumagen's DB9) also works — the same dropdown will show the `/dev/tty*` entry.

## Requirements

- Home Assistant **2026.5** or newer (native serial_proxy surfacing in the `usb` integration)
- The `esphome-lumagen` firmware on a Waveshare ESP32-S3-POE-ETH (or a direct RS-232 cable)
- The ESPHome integration adopted in HA for the bridge device (if going the ESPHome route)
- Network connectivity between HA and the bridge

## Installation

### Marketplace (Home Assistant 2026.11+) or HACS

[![Open your Home Assistant instance and open this repository in the Marketplace or HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=frankrosano&repository=ha-lumagen&category=integration)

1. Select the badge above. It opens this repository in **Settings** → **Marketplace** on Home Assistant 2026.11 and newer, or in HACS on older versions, and offers to add it as a custom repository.
2. Install **Lumagen Radiance Pro**.
3. On a first install, you can set it up right away. After an update, restart Home Assistant when the repair asks you to.

Once the integration is in the Marketplace catalog, you can also find it by searching the Marketplace for **Lumagen Radiance Pro**.

### Manual

1. Copy `custom_components/lumagen` into your HA config directory's `custom_components/`.
2. Restart Home Assistant.

## Setup

[![Open your Home Assistant instance and start setting up Lumagen Radiance Pro.](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=lumagen)

1. Settings → Devices & services → **Add integration** → search for **Lumagen Radiance Pro** (or select the badge above).
2. From the dropdown, pick the serial port for the Lumagen. ESPHome-proxied ports appear with their friendly name ("Lumagen") plus the bridge's hostname; physical ports appear with their `/dev/tty*` path.
3. The flow opens the port, queries `ZQS01` for the device info, and creates the config entry with the detected model and firmware in the title.

### A note on `aioesphomeapi`

This integration requires plain `aiolumagen`, deliberately **not**
`aiolumagen[esphome]`. The ESPHome serial transport needs `aioesphomeapi`, but
Home Assistant already ships it — pinned exactly — for its own ESPHome
integration, and installs custom-integration requirements into the same
site-packages. If `aiolumagen` also declared it (necessarily at a looser
range), the resolver could move HA's pinned version; because `aioesphomeapi`
is a Cython package, that leaves mismatched `.so` files behind and breaks the
ESPHome integration with errors like `APIConnection size changed` or
`does not export expected C function make_noise_packets`.

Nothing is lost: an `esphome://` URL means you're using the ESPHome
integration, which provides the package. HA stays its single owner.

## Lumagen-side prerequisite: unsolicited reporting

For real-time updates rather than polling, enable Full v5 reporting on the Lumagen:

1. On the Lumagen remote or OSD, press `MENU`.
2. Navigate: **Other → I/O Setup → RS-232 Setup → Report mode changes**.
3. Cycle to **Full v5**.
4. Press `OK`, then `SAVE` to persist.

The integration works either way; this just makes it snappier. Leaving it at **Full v4** also works — those pushes still parse — you just don't get power and memory changes in real time.

Separately, note that Full v5 is a **firmware requirement**, not just a menu preference: `aiolumagen` polls status exclusively with `ZQI25`, so firmware predating it isn't supported. A device on such firmware won't show an error, its status sensors simply stay unknown; the library logs a warning at startup when it detects that case.

## Exposed entities

- **Binary sensors**: Power, HDR active, Auto aspect detection (diagnostic), Display supports Rec.2020 (diagnostic), Serial connected (diagnostic)
- **Sensors** (diagnostic): Model, Firmware, HDR source max/min luminance, HDR source MaxCLL
- **Sensors** (primary): Current input, Input memory, Source/Output resolution, Source/Output refresh rate (Hz), Source/Content aspect, Colorspace (enum), HDR status (enum), Input status (enum), Source mode (enum: Interlaced / Progressive / No input)
- **Buttons**: Power on/Standby, full OSD nav (Menu/Exit/OK/Menu off/Up/Down/Left/Right), direct inputs 1–8 + Previous, all aspect presets (4:3, Letterbox, 16:9, 16:9 NZ, 1.85, 2.35, 2.40), Auto aspect on/off, Redetect aspect, Memory A–D, HDR setup, Test pattern, OSD on/off, Save to NVRAM, Query status
- **Selects**: Input (1–8), Aspect ratio (7 options), Memory (A–D), Sharpness sensitivity (Normal/High), Subtitle shift (Off/Small/Large), HDR gamma mode (Auto/HDR/SDR)
- **Switches**: Sharpness, Game mode
- **Numbers**: Sharpness level (0–7), Minimum fan speed (0–9), HDR mapping max nits (0 to disable; 50–10000 to set display peak)
- **Update**: Firmware — offers new Radiance Pro firmware from lumagen.com and installs it (see below)

The enum sensors and the Sharpness sensitivity, Subtitle shift and HDR gamma mode selects show translated labels, but their states are lowercase keys: use those in automations and templates, e.g. `rec_709`, `hdr`, `no_source`, `no_input`, `disabled`, `small`, `auto`. (Before 0.10.0 they were the raw labels, like `Rec.709` or `HDR`.)

## Firmware updates

The **Firmware** update entity watches Lumagen's [release page](https://www.lumagen.com/software-updates/radiance-pro-updates) and can install a new release from Home Assistant.

> **Use at your own risk.** This is an independent, reverse-engineered implementation with no association with or endorsement by Lumagen, Inc. It has been tested on one Radiance Pro 4242. Read the [aiolumagen firmware warning](https://github.com/frankrosano/aiolumagen#firmware-updates) before your first install.

- **Checking.** The release page is checked once a day. The updater zip (~3 MB) is downloaded only when you press **Install**, and is handled in memory. Disable the entity to stop all checks.
- **Channel.** Set under the integration's **Configure** options. **Beta** (the default) offers the newest release of any label, since Lumagen rarely posts Production builds; "Production candidate" counts as beta. **Production** offers only releases labelled Production.
- **Standby.** If the Lumagen is in standby when you install, it's powered on automatically, given 15 seconds to finish starting up, and then updated.
- **The unit powers off at the end of a successful update. That's expected**: it's how the Lumagen loads new firmware. Power is then restored to how it was: if it was on, it's powered back on; if it was off, it's left off.
- **During the update** every other Lumagen entity shows as unavailable and the `lumagen.*` services refuse. The update entity shows progress. A section-0-only update takes about a minute. One that also rewrites section 1 is listed by Lumagen as about five minutes; the one such install tested from Home Assistant took about 7½.
- **Don't restart Home Assistant or reload the integration while an update is running.** Changing the integration's options mid-update is safe: the reload waits until the update finishes.
- **If an update fails**, read the message and retry before power-cycling the unit. An update that stops before touching live firmware leaves it unchanged.

### Tested on hardware

From Home Assistant, over its own ESPHome connection to the bridge at 230400, on one Radiance Pro 4242:

- Three scratch-only section-0 transfers (live firmware untouched): two started from standby, one from on, each with zero flush retries.
- One real upgrade, 112325 → 030326, rewriting section 1 and section 0. The unit was on, powered down at the end as expected, and was powered back on automatically. One flush retry occurred and recovered; verification passed.

Not yet tested on hardware: a real install started from standby.

## Service

- `lumagen.send_raw_command` — send any RS-232 command directly to the Lumagen. Useful for advanced features that aren't surfaced as entities (e.g. HDR test-pattern info frames during calibration). Pass `command` and optional `cr` (most `ZY`-prefixed commands need `cr: true`).

Invalid device/entity targets fail rather than falling back to another device.
With multiple Lumagens loaded, explicitly target exactly one device or entity;
area targets and calls matching multiple Lumagens are rejected.
OSD message calls validate before changing the sticky block character and keep
that setting together with its message when calls overlap. Concurrent HDR and
sharpness controls serialize their compound writes so changing one field does
not reset another.

## Status

Alpha / prototype. Not yet in the Marketplace/HACS catalog — add it from the badge under [Installation](#installation).

### Unreleased hardening

The current adversarial-hardening changes require the matching editable
`aiolumagen` sibling checkout. Versions and the integration's published-library
pin are intentionally unchanged while this development work is tested. Do not
deploy this integration checkout against the older published library: first
release the library fixes and update the pin together.

## License

MIT. See [`LICENSE`](https://github.com/frankrosano/ha-lumagen/blob/main/LICENSE).
