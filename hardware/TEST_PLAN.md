# Hardware test plan

The unit tests prove the integration and aiolumagen behave as designed against
a mocked client and recorded byte streams. They can't prove the design matches
a real Radiance Pro, its firmware's reporting, or a real serial link. This plan
does that, over a **direct USB** connection to the Lumagen's USB-B port (FTDI
FT232R). The ESPHome bridge route is covered only in the release check at the
end.

Run it before a release that touches I/O, entities or firmware updating, after
a Lumagen firmware change, and once before relying on the integration
unattended.

## Where to run what

| Where | Use it for |
| --- | --- |
| A dev container or VM with the Lumagen's USB | Everything below except the release check. Fastest loop: a fix in this repo or in aiolumagen is live after a restart. |
| A disposable Home Assistant OS instance | The [release check](#release-check): Marketplace install, requirements from PyPI, upgrading an existing install, the ESPHome route. |
| A production Home Assistant | Nothing here. Firmware tests power the unit off, and only one client can hold the serial link. |

**One owner at a time.** A serial port can be opened by one process, and an
ESPHome `serial_proxy` serves one subscriber. Before testing, disable every
other Lumagen entry that could reach the unit (on any Home Assistant), and
close any script using the port. On a hypervisor, pass the FTDI device
(`0403:6001`) to exactly one VM; to move it, detach it from one VM before
attaching it to the other.

**People may be watching.** Commands change the picture, inputs and aspect.
Run the command and firmware sections when the theater is free; the soak is
passive.

## Set up the dev loop

1. Check out this repo and [aiolumagen](https://github.com/frankrosano/aiolumagen)
   side by side, next to a Home Assistant core checkout with a Python 3.14
   `.venv` (default: `../ha-dev`; see the variables in
   [`script/dev_setup.sh`](../script/dev_setup.sh)).
2. Run `script/dev_setup.sh`. It installs aiolumagen editable into that venv,
   symlinks `custom_components/lumagen` into the config, and writes a lean
   `configuration.yaml` with debug logging for both packages.
3. Start Home Assistant with the **Home Assistant (Lumagen hardware)** launch
   configuration (breakpoints work in both repos and in core), or with
   `script/dev_hass.sh`. Open port 8123 and complete onboarding once.

To fix an issue: reproduce it, write a failing test in the repo that owns the
behaviour (protocol and parsing in aiolumagen, entities and Home Assistant
wiring here), fix it, restart Home Assistant, and repeat the step that found
it. Both launchers pass `--skip-pip-packages aiolumagen`, so Home Assistant
never replaces the editable library with the version the manifest pins.

### Tools

| Tool | Use |
| --- | --- |
| Debug logging | On by default in the dev config; every frame and state change is logged. |
| **Developer tools** > **States** | Raw states and attributes. Enum states are keys like `rec_709`, not labels. |
| aiolumagen `examples/via_url.py <port>` | The library alone: handshake, then state updates. **Disable the entry first.** |
| aiolumagen `examples/send_raw.py` | Send one command outside Home Assistant. **Disable the entry first.** |
| aiolumagen `examples/update_firmware.py` | Read-only flash inspection, scratch-only firmware writes, audit. See [Firmware](#firmware). |
| `lumagen.send_raw_command` | Send any command through the integration. |

## Pass criteria for every test

- The entry never ends up in **Failed to set up**; **Retrying setup** is
  acceptable when it recovers by itself.
- Entities match the Lumagen's own OSD (**Info** screen) within a few seconds
  with Full v5 reporting on, and within one poll interval with it off.
- After a fault clears, entities are available again and commands work,
  without a reload.
- No tracebacks in the log, no `Unexpected error`, and no log line repeated on
  every poll during an outage.

## 1. Set up

| # | Test | Expect |
| --- | --- | --- |
| 1.1 | Add the integration; pick the FTDI port from the dropdown. | Entry titled with the model and firmware; one device; all platforms load. |
| 1.2 | Add it again with the same port. | Aborts as already configured. |
| 1.3 | Add it while `via_url.py` holds the port. | A clear error on the form, no entry created. |
| 1.4 | Add it with the USB cable unplugged (type the old port path). | A clear error on the form. |
| 1.5 | **Configure**: change the poll interval and the firmware channel. | Saved; the entry reloads; polling follows the new interval (debug log). |

## 2. State

With Full v5 reporting on (see the README), unless the test says otherwise.

| # | Test | Expect |
| --- | --- | --- |
| 2.1 | Compare every sensor and binary sensor with the OSD **Info** screen. | They match. Diagnostic entities match too. |
| 2.2 | Change input, memory and aspect with the **Lumagen's remote**. | Current input, memory, aspect sensors and the selects follow within seconds. |
| 2.3 | Play SDR, HDR10, and a Rec.2020 source; then no source. | HDR status `sdr`/`hdr`, colorspace `rec_709`/`rec_2020`, input status `no_source`, source mode `no_input`; labels read "SDR", "Rec.2020", "No source", "No input". |
| 2.4 | Interlaced and progressive sources. | Source mode `interlaced`/`progressive`; resolution sensors end in `i`/`p`. |
| 2.5 | Turn auto aspect off and on (remote, then the switch). | Auto aspect status `off`/`disabled`/`on` as the firmware reports it; the switch follows. |
| 2.6 | Standby and power on with the remote. | Power follows; entities stay available (the unit still answers in standby). |
| 2.7 | Set reporting to **Full v4**, restart the entry, repeat 2.2 and 2.6. | Inputs still push; power and memory follow on the next poll. Set it back to Full v5. |
| 2.8 | Leave it idle for an hour. | No growth in log volume; Serial connected stays on. |

## 3. Commands

| # | Test | Expect |
| --- | --- | --- |
| 3.1 | Every button, one by one, watching the screen. | Each does what its name says; OSD navigation moves the OSD. |
| 3.2 | Input, aspect and memory selects. | The unit changes; the select shows the new value without a refresh. |
| 3.3 | Sharpness switch, level and sensitivity, in mixed order. | Changing one never resets the other two. |
| 3.4 | Subtitle shift; then change it with the remote. | The select follows the remote where the firmware reports it, otherwise keeps the last value set. |
| 3.5 | HDR gamma mode and HDR mapping max nits, together. | Changing one keeps the other. Max nits 0 disables mapping. |
| 3.6 | Game mode, minimum fan speed. | The unit changes; the entities follow. |
| 3.7 | `lumagen.send_raw_command` with `ZQS01`, and a `ZY` command with `cr: true`. | Both reach the unit; the log shows the reply. |
| 3.8 | `send_raw_command` targeting a device of another integration. | Refused with a validation error. |
| 3.9 | Commands while in standby. | Power on works; others fail cleanly or are ignored by the unit, without errors in the log. |

Avoid **Save to NVRAM** unless you mean to keep the unit's current settings.

## 4. Recovery

| # | Test | Expect |
| --- | --- | --- |
| 4.1 | Unplug the USB cable for 10 seconds. | Entities unavailable while unplugged, back within a reconnect attempt after. |
| 4.2 | Unplug it for 10 minutes. | Retries back off (debug log); at most a few log lines; recovers when plugged back. |
| 4.3 | Switch the Lumagen off at the wall for 1 minute. | Same as 4.1; firmware and model correct after. |
| 4.4 | Restart Home Assistant with the cable unplugged; plug it in after a minute. | **Retrying setup**, then loaded, without a reload. |
| 4.5 | Reload the entry, and disable and enable it, five times quickly. | Ends loaded; one connection; no "port busy" errors. |
| 4.6 | Run aiolumagen's `via_url.py` on the port while the entry is loaded. | The script fails to open it (serialx locks the port), not the integration. |

When the device comes back as a different port (`ttyUSB1`), the entry can't
follow it; note it, and use a stable path (`/dev/serial/by-id/…`) where udev
provides one. In a container with `--device=/dev/ttyUSB0`, re-plugging must
bring it back as the same node, or the container must be restarted.

## 5. Firmware

Direct USB has **never been tested** for firmware updates: aiolumagen and this
integration were qualified over an ESPHome bridge at 230400 on one 4242. The
updater keeps the bootloader intact, so the vendor's own Windows updater stays
a recovery route; have a Windows machine that can reach the unit's USB port
before you start. Follow the order; stop at the first failure.

With the entry **disabled**, from the aiolumagen checkout:

| # | Test | Expect |
| --- | --- | --- |
| 5.1 | `update_firmware.py <port> --status` | Identity, Radiance Pro device id, both slot headers; nothing written. |
| 5.2 | Download the updater for the next release; `update_firmware.py <port> <exe> --dry-run` | A plan naming the sections that differ. |
| 5.3 | `update_firmware.py <port> <exe> --only section0 --no-promote`, then `--only section0 --audit`. Three times. | Each run verifies; audits find no bad blocks; flush retries recorded. Live firmware untouched. |

Then from Home Assistant, with the entry enabled:

| # | Test | Expect |
| --- | --- | --- |
| 5.4 | The update entity, with each channel. | Offers the newest release for the channel; release notes link. |
| 5.5 | Install with the unit **on**. | Progress climbs once from 0 to 100; other entities unavailable and services refused during it; the unit powers off at the end, then back on; firmware sensor shows the new version. |
| 5.6 | Change the options mid-install (on a later release, or on a second unit). | The reload waits for the update. |
| 5.7 | Install with the unit in **standby** (needs a newer release, or a second unit). | Powered on, waits 15 seconds, updates, left off at the end. Not yet tested anywhere. |

Record the duration and flush retries of every run.

## 6. Soak

Leave the entry running for 72 hours across normal use: viewing, standby
overnight, a Lumagen power cut if one happens. Expect no unavailable periods
beyond real outages, steady memory use, and no repeated log lines.

## Release check

On a disposable Home Assistant OS instance, with the Lumagen's USB passed to
it and the dev instance's entry disabled:

1. Install the previous release from the Marketplace (Home Assistant 2026.11 or
   newer), set it up, and add an automation that compares an enum state.
2. Update to the new release from **Settings** > **Updates**. Expect the
   restart repair; aiolumagen installed from PyPI at the pinned version.
3. Check every entity loads, and the automation's comparison against the
   release notes (0.10.0 renamed the enum states).
4. Remove the integration from the Marketplace; expect the restart repair, and
   no leftover files in `custom_components`.
5. Where an ESPHome bridge is available: move the unit's USB to the bridge, set
   up through its serial proxy, and repeat sections 1, 2.2 and 4.1.

## Results

Copy for each run:

```text
Date:                 Integration / aiolumagen: 0.10.0 / 0.12.2
Home Assistant:       Lumagen model / firmware:
Connection:           direct USB (FT232R) | ESPHome bridge
Passed:
Failed (with log excerpt and issue link):
Firmware runs (duration, flush retries):
```
