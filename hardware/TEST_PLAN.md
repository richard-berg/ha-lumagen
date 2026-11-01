# Hardware test plan

The goal is an integration that stays up for months or years: through USB that
drops out, a Lumagen that loses power or is reconfigured from its own remote,
Home Assistant restarts and reconfiguration, network outages, and every source
and display it gets connected to. Unit tests prove the code does what it was
designed to do. This plan tries to break it against real hardware.

Every test says how it's verified:

- **A** — automated: a tool decides pass or fail. The tools are in this
  directory; see [Tools](#tools).
- **O** — operator: what you watch on screen, on the Lumagen's OSD, or in Home
  Assistant, and what counts as a failure.

Run the whole plan before a release that touches I/O, entities or firmware
updating, and after a Lumagen firmware change. Run the automated suites and a
soak before any release.

## Invariants

These hold during and after every test. A test that breaks one fails, whatever
its own expectation says.

| # | Invariant | Verified by |
| --- | --- | --- |
| I1 | The entry never ends up in **Failed to set up**, **Migration error** or **Failed to unload**. **Retrying setup** is fine while the device is unreachable. | A: `monitor`, `scenarios` |
| I2 | The integration never changes the device on its own. On start, reconnect, recovery, reload and restart it sends queries only (`ZQ…`, `ZE2`); anything else must come from a user or automation action. | A: `fault_proxy.py` audit, unsolicited writes = 0. O: no OSD message, input, aspect, memory or power change on screen during restarts and reconnects. |
| I3 | A lost link is noticed within 75 s (45 s stale timeout at the default 15 s poll, plus a poll and slack), and the integration recovers within 360 s of the cause ending (300 s maximum backoff plus slack), without a reload. | A: `monitor`, `scenarios` |
| I4 | Every outage has a cause: an injected fault, a USB removal, an entry reload, a Home Assistant restart, or an operator mark. | A: `monitor` (unexplained outages = 0) |
| I5 | What Home Assistant shows is what the device reports, including straight after a recovery. | O: OSD **Info** against `lumagen_probe.py snapshot` |
| I6 | Malformed, unexpected or damaged device output never changes the unit's identity (model, firmware), raises an error, or leaves an enum entity in a state outside its options. | A: `scenarios` (identity check, error logs) |
| I7 | Errors: none. Warnings: at most 12 an hour, even during a day-long outage. | A: `monitor` |
| I8 | No leaks: memory grows less than 20 MB/day after the first hour, open files stay within a band of 10, at most one handle to the serial port. | A: `monitor` |
| I9 | One owner: never a second connection to the port. | A: `fault_proxy.py` (concurrent connections = 0), `monitor` (serial handles ≤ 1) |
| I10 | Entities are stable: the same entities and entity IDs after restarts, reloads, reconnects and re-adding the entry; no `_2` duplicates. | A: `monitor` (entity count). O: entity list |

## The rig

```text
D  direct        Home Assistant ──── /dev/ttyUSB0 ──── Lumagen USB-B      (the production path)
P  fault proxy   Home Assistant ── socket:// ── fault_proxy.py ── /dev/ttyUSB0 ── Lumagen
B  ESPHome       Home Assistant ── network ── ESPHome serial proxy ── USB ── Lumagen
```

**One owner at a time.** A serial port can be opened by one process and an
ESPHome serial proxy serves one subscriber. Disable every other Lumagen entry
that can reach the unit, on every Home Assistant, before you test. On a
hypervisor, pass the FTDI device (`0403:6001`) to exactly one VM; detach it
from one before attaching it to another.

**People may be watching.** Command tests change the picture; run them when
the theater is free. Monitoring is passive.

### Set up

1. Check out this repo and [aiolumagen](https://github.com/frankrosano/aiolumagen)
   side by side, next to a Home Assistant core checkout with a Python 3.14
   `.venv` (default `../ha-dev`; see [`script/dev_setup.sh`](../script/dev_setup.sh)).
2. Run `script/dev_setup.sh`, then start Home Assistant with the **Home
   Assistant (Lumagen hardware)** launch configuration or `script/dev_hass.sh`.
3. Onboard, then create a long-lived access token (your profile, **Security**)
   and save it to `../ha-dev/config/.lumagen_test_token`. That folder is not
   in git.
4. **Topology D:** add the integration with the FTDI port.
5. **Topology P:** start the proxy, then add a second entry with the URL
   `socket://127.0.0.1:14999`, and disable the direct entry while you use it:

   ```bash
   ../ha-dev/.venv/bin/python hardware/fault_proxy.py /dev/ttyUSB0 --log proxy.jsonl
   ```

Run the Python tools with Home Assistant's venv (`../ha-dev/.venv/bin/python`);
it has everything they need.

### Tools

| Tool | Use |
| --- | --- |
| [`fault_proxy.py`](fault_proxy.py) | Sits between Home Assistant and the port. Logs every byte, flags every state-changing command nobody announced, and injects faults on command: latency, silence in either direction, bit flips, noise, duplicated, truncated or malformed lines, byte-at-a-time delivery, stalls, echo, other line endings, closed, reset and refused connections, the device port going away, and a random `chaos` schedule. `nc 127.0.0.1 14998`, then `help`. |
| [`lumagen_probe.py`](lumagen_probe.py) `scenarios` | The automated adversarial suite over topology P: 24 scenarios (25 with `--long`), each checked against I1–I4, I6, I7 and I9. |
| `lumagen_probe.py exercise` | Closed-loop commands: set, confirm from the device, restore. Checks that settings written together (sharpness on/level/sensitivity, HDR gamma/max nits) don't reset each other. `--allow sound picture power` adds steps that are noticeable; `--operator` asks you to confirm each on screen. |
| `lumagen_probe.py monitor` | The soak monitor: I1, I3, I4, I7, I8, I9, I10. `--tty ttyUSB0` treats USB removals as causes; `--proxy-log` the proxy's faults; `--marks` your own. Survives Home Assistant restarts. Prints a report hourly and on exit; exits non-zero on failure. |
| `lumagen_probe.py snapshot` | Every Lumagen entity's state, to hold against the OSD. |
| `lumagen_probe.py mark` | Record a cause you created by hand: `mark --marks marks.jsonl --seconds 60 pulled the AC power`. |
| [`usb_fault.sh`](usb_fault.sh) | On the machine whose kernel owns the adapter, as root: logical unplug, driver unbind, flapping, a random schedule. `MARKS=` writes its faults to a marks file. |
| aiolumagen `examples/` | `via_url.py`, `send_raw.py`, and `update_firmware.py` for the firmware section. Disable the entry first. |

Both tool files are unit tested (`tests/test_hardware_*.py`), because a
monitor that blames the wrong cause makes a soak meaningless.

### Timing

At the default 15 s poll: the link is stale after 45 s without input;
reconnects start immediately, then back off 5, 10, 20, 40… up to 300 s. Hence
75 s to notice and 360 s to recover. With a different poll interval, pass
`--detect-deadline` and `--recovery-deadline` to the tools.

## 1. Setup and configuration

| # | Do | Expect | Verify |
| --- | --- | --- | --- |
| 1.1 | Add the integration from the dropdown. | Entry titled with model and firmware; one device; every platform loads. | O: device page. A: `snapshot` lists every entity. |
| 1.2 | Add it again with the same port. | Aborts as already configured. | O |
| 1.3 | Add it while `via_url.py` holds the port. | A clear error on the form; no entry. | O |
| 1.4 | Add it with the cable unplugged, typing the port path. | A clear error on the form. | O |
| 1.5 | Add it with the Lumagen switched off at the wall. | A clear error; succeeds once it's back on. | O |
| 1.6 | Add it with the Lumagen in standby. | Succeeds; power reads off. | O |
| 1.7 | **Configure**: poll interval 5, then 600, then 15; channel Production, then Beta. | Each saves and reloads; I2 holds through each reload. | A: `monitor --marks`, proxy audit. O |
| 1.8 | Add a second entry for the same port under another path (`/dev/./ttyUSB0`, or `/dev/serial/by-id/…` where udev provides it). | The second retries setup (the port is locked); the first keeps working and never flaps. Remove the second. | A: `monitor` on the first shows no outage. O |
| 1.9 | Delete the entry and add it again. | Same entity IDs as before; history continues. | O: I10 |

## 2. State fidelity

With Full v5 reporting on (see the README), unless the test says otherwise.
For each, compare the OSD **Info** screen with `lumagen_probe.py snapshot`.

| # | Do | Expect | Verify |
| --- | --- | --- | --- |
| 2.1 | Idle, with a source playing. | Every sensor and binary sensor matches the OSD. | O |
| 2.2 | Change input, memory and aspect with the **Lumagen's remote**. | Entities follow within seconds, without a poll. | O; A: `monitor` journal timestamps |
| 2.3 | Standby and on with the remote, ten times in 30 s. | Ends in the right state; no errors. | O; A: `monitor` |
| 2.4 | Set reporting to **Full v4**, then **Off**, then back to **Full v5**, without touching Home Assistant. | v4: inputs still follow; power and memory on the next poll. Off: everything follows on the next poll. No outage, no errors. | O; A: `monitor` |
| 2.5 | Open the Lumagen's menu with its remote and leave it open for 10 minutes. | No outage; polls don't disturb the menu. | O: the menu stays put. A: `monitor` |
| 2.6 | Rename an input label and change a memory's settings on the unit. | Picked up after a reload at the latest; no errors. | O |
| 2.7 | Idle for 24 hours. | No state churn beyond real changes. | A: `monitor` (changes per hour) |

## 3. Commands

| # | Do | Expect | Verify |
| --- | --- | --- | --- |
| 3.1 | `lumagen_probe.py exercise --rounds 3 --allow sound picture power --operator` | Every step confirmed by the device and restored; settings written together don't reset each other; what you see matches each prompt. | A: exit 0. O: each prompt |
| 3.2 | Every button not covered by 3.1 (OSD navigation, inputs, aspect presets, test pattern, OSD on/off, restart inputs/outputs, HDR setup, show aspect, clear message). | Each does what its name says. | O |
| 3.3 | `lumagen.send_raw_command`: `ZQS01`; a `ZY` command with `cr: true`; an unknown command; an empty one. | The first two work; the others fail cleanly or are ignored by the unit. | O; A: no error logs |
| 3.4 | `send_raw_command` targeting another integration's device. | Refused with a validation error. | O |
| 3.5 | Commands while in standby. | Power on works; others fail cleanly or are ignored. | O; A: no error logs |
| 3.6 | An automation that sends a command every second for 10 minutes. | Every command lands in order; no backlog after it stops; no errors. | A: `monitor`; O: the unit keeps up |
| 3.7 | 20 `send_raw_command` calls at once (a script with parallel actions). | All complete; no errors. | A: `monitor` |
| 3.8 | Commands while the link is down. | Each fails promptly with an error; nothing is queued and replayed when the link returns. | O; A: proxy audit after recovery |

Avoid **Save to NVRAM** unless you mean to keep the unit's current settings.

## 4. Protocol and link faults

Topology P. Run the suite, then the long scenario once per release:

```bash
../ha-dev/.venv/bin/python hardware/lumagen_probe.py --out scenarios.jsonl scenarios
../ha-dev/.venv/bin/python hardware/lumagen_probe.py scenarios --long --only "long outage"
```

| Scenario | Fault | Expect |
| --- | --- | --- |
| latency 1s, latency 4s, jitter | delay either direction | stays up |
| rare bit flips, line noise, duplicate lines, byte at a time, command echo | damaged but flowing data | stays up; identity unchanged |
| heavy bit flips, truncated lines, LF line endings | badly damaged data | may drop; recovers; no errors |
| malformed lines | 12 kinds: empty, short, garbage, unknown code, partial, lone CR, NUL, binary, 10 KB, no `!`, bad power, blank firmware | stays up; identity unchanged |
| short stall / long stall | output held 30 s / 90 s, then burst | stays up / drops and recovers |
| device silent, commands lost | one direction silent for 2 minutes | drops within 75 s, recovers |
| connection closed, connection reset | FIN / RST | reconnects |
| connections refused 2 min, device port gone 1 min | nothing to connect to | drops, backs off, recovers |
| flapping link | closed 12 times, 5 s apart | recovers; warnings bounded |
| reload during outage, restart during outage | Home Assistant reload or restart while unreachable | **Retrying setup**, never **Failed**; loads when the link returns |
| reload storm | five reloads in 2.5 s | ends loaded; one connection |
| long outage 20 min (`--long`) | refused for 20 minutes | backoff caps at 300 s; at most a few warnings |

Every scenario also checks I2 (no unsolicited writes), I9 (no second
connection), that a command works after recovery, and that no errors were
logged. Then by hand:

| # | Do | Expect | Verify |
| --- | --- | --- | --- |
| 4.1 | `chaos 3600` on the proxy with `monitor --proxy-log` running. | Every outage explained, every recovery in time. | A: `monitor` |
| 4.2 | During `stall on`, change input with the Lumagen's remote; then `stall off`. | The burst is applied in order; the final state matches the OSD. | O: I5 |
| 4.3 | `inject` your own lines from the protocol documentation, wrong in new ways. | No errors; identity unchanged. | A: `monitor`; O |

## 5. USB faults (topology D)

Run `monitor --tty ttyUSB0 --marks marks.jsonl` throughout. `usb_fault.sh` runs
as root on the machine whose kernel owns the adapter, with `MARKS=` pointing to
a file the monitor reads, or mark each fault by hand.

| # | Do | Expect | Verify |
| --- | --- | --- | --- |
| 5.1 | `usb_fault.sh unplug 10`, `unplug 120`, `unplug 900` | Unavailable within seconds; back within 360 s of replugging (immediately if it reconnects at once). | A: `monitor` |
| 5.2 | `usb_fault.sh unbind 30` (the device stays, the driver goes) | Same as 5.1. | A |
| 5.3 | `usb_fault.sh flap 20 1 1`, then `flap 10 5 30` | Recovers after the last; warnings bounded; never two handles. | A: I7, I9 |
| 5.4 | Pull the cable at the Lumagen end; then at the host end; then pull and push back within a second. | As 5.1. | A: `monitor --tty`; O |
| 5.5 | Replug into a different USB port, and through a hub. | Comes back where the path stays the same. Note what happens when it returns as `ttyUSB1`: the entry can't follow, and in a container started with `--device=/dev/ttyUSB0` nothing can until the container restarts. Use `/dev/serial/by-id/…` where udev provides it. | O |
| 5.6 | Unplug during Home Assistant startup; during a command burst (3.6); during a reload. | No **Failed** state; recovers. | A |
| 5.7 | Detach the device from the VM at the hypervisor (Proxmox: remove the USB device, re-add it). | As 5.1. | A |
| 5.8 | Suspend the VM for 10 minutes (Proxmox: `qm suspend`, then `qm resume`). | Notices within 75 s of resuming, recovers; no errors after the catch-up. | A |
| 5.9 | `usb_fault.sh chaos 168` for a week, with `monitor` running. | Every invariant holds for the week. | A |

## 6. Device faults and reconfiguration outside Home Assistant

| # | Do | Expect | Verify |
| --- | --- | --- | --- |
| 6.1 | Cut the Lumagen's AC power for 1 minute, then for 30 minutes. `mark` each. | Unavailable; recovers once it has booted; power, firmware and model right. | A: `monitor`; O: I5 |
| 6.2 | Cut AC power five times in a minute. | Recovers after the last. | A |
| 6.3 | Cut AC power while Home Assistant starts. | **Retrying setup**, then loaded. | A: I1 |
| 6.4 | Cut AC power right after sending a command. | No error beyond the failed command; no replay on return. | A: proxy audit (topology P) |
| 6.5 | Change the Lumagen's RS-232 baud rate (**Other → I/O Setup → RS-232 Setup**) away from 9600, wait 10 minutes, set it back. | Unavailable while mismatched, with a warning that helps; recovers when set back. Never **Failed**. | A: `monitor`; O: the log message is useful |
| 6.6 | Update or downgrade the Lumagen's firmware with Lumagen's own updater (entry disabled), then enable the entry. | Firmware sensor and device info show the new version; the update entity re-evaluates; no errors with fields the other firmware lacks. | O; A: no errors |
| 6.7 | Restore the Lumagen's settings from a saved configuration or defaults (only with a backup of its configuration). | Recovers; entities reflect the new settings. | O |
| 6.8 | Leave the unit in standby for 24 hours, then power it on with its remote. | No outage while in standby; state correct after power on. | A; O |

## 7. Home Assistant reconfiguration and lifecycle

| # | Do | Expect | Verify |
| --- | --- | --- | --- |
| 7.1 | Reload the entry 20 times, a few seconds apart. | Loaded at the end; one connection; I2. | A: `monitor`, proxy audit |
| 7.2 | Disable and enable the entry 10 times. | As 7.1. | A |
| 7.3 | Restart Home Assistant gracefully: with the unit on, in standby, unplugged. | Back to loaded (or **Retrying setup** while unplugged); I2: no command on startup. | A; O: no OSD activity |
| 7.4 | `kill -9` Home Assistant, then start it. | As 7.3; the port isn't left locked. | A |
| 7.5 | Hard-reset the VM. | As 7.3. | A |
| 7.6 | Disable the update entity. | No more release checks; nothing else changes. | O: debug log |
| 7.7 | Disable Serial connected, then enable it. | Other entities unaffected. (The monitor needs it, so do this last.) | O |
| 7.8 | Rename the device and some entities; restart. | Names kept; integration unaffected. | O |
| 7.9 | Upgrade Home Assistant to the next release or beta; restore a Home Assistant backup. | Loads; same entities; no errors. | A; O |
| 7.10 | Change options while the link is down. | The reload goes to **Retrying setup**; recovers when the link returns. | A |

## 8. Sources and displays

Operator-led. For every row: compare the OSD **Info** screen with
`lumagen_probe.py snapshot`, and keep `monitor` running for I6 and I7.
Note each source and display model in the results.

| # | Do | Expect |
| --- | --- | --- |
| 8.1 | Each source you own (streamer, disc player, console, PC) at its native output. | Input, resolution, refresh rate, colorspace, HDR status and aspect match. |
| 8.2 | Resolutions: 480i, 576i, 720p50, 1080i, 1080p24, 2160p23.976, 2160p60. | Resolution sensors end in `i`/`p` correctly; source mode `interlaced`/`progressive`. |
| 8.3 | Frame rates 23.976, 24, 25, 29.97, 50, 59.94, 60. | Refresh rate sensors exact (59.94, not 59). |
| 8.4 | SDR Rec.709; SDR in a BT.2020 container; HDR10 with mastering metadata; HLG; Dolby Vision (low-latency). | HDR status and colorspace right; MaxCLL and mastering luminance populated for HDR10, unknown for SDR. |
| 8.5 | Content at 1.33, 1.78, 1.85, 2.35, 2.40 with auto aspect on; then off. | Detected aspects and auto aspect status follow; aspect select matches. |
| 8.6 | Turn the source off; put it in standby; unplug its HDMI cable mid-play. | Input status `no_source`, source mode `no_input`; no errors; back when the source returns. |
| 8.7 | Turn the display off and on; unplug its HDMI cable; swap to a different display (other EDID: with and without Rec.2020). | Display supports Rec.2020 follows; output resolution and rate follow; no errors. |
| 8.8 | Game console with ALLM, and VRR if supported; toggle game mode. | Game mode switch and the unit agree. |
| 8.9 | Switch inputs as fast as the remote allows for a minute. | Ends on the right input; no errors. |
| 8.10 | Test patterns from the Lumagen; HDR setup screens. | Input status `test_pattern` where reported; no errors. |

## 9. Network faults

| # | Do | Expect | Verify |
| --- | --- | --- | --- |
| 9.1 | Block lumagen.com (DNS or firewall), reload the entry to force a release check, wait a day. | The update entity keeps its last known release or shows none; nothing else affected; at most one warning per check. | A: `monitor`; O |
| 9.2 | Restore access; reload. | The update entity finds the latest release again. | O |
| 9.3 | Block lumagen.com after pressing **Install**, before the download finishes. | A clear error; the unit untouched (the download finishes before the session starts). | O |
| 9.4 | Topology B: unplug the bridge's Ethernet for 1 and 15 minutes; power-cycle it; update its firmware over the air; reload the ESPHome integration; change its address. | Each recovers on its own within 360 s of the cause ending; never **Failed**. | A: `monitor --marks` |
| 9.5 | Topology B: change the bridge's API encryption key in ESPHome, then in Home Assistant. | Clear failure while they differ; recovers when they match. | O; A |

## 10. Firmware

Direct USB has **never carried a firmware update**: aiolumagen and this
integration were qualified over an ESPHome bridge at 230400 on one 4242. The
updater never writes the bootloader, so Lumagen's own Windows updater stays a
recovery route; have a Windows machine that can reach the unit's USB port
before you start. Follow the order; stop at the first failure.

With the entry **disabled**, from the aiolumagen checkout:

| # | Do | Expect |
| --- | --- | --- |
| 10.1 | `update_firmware.py <port> --status` | Identity, Radiance Pro device id, both slot headers; nothing written. |
| 10.2 | Download the next release's updater; `update_firmware.py <port> <exe> --dry-run` | A plan naming the sections that differ. |
| 10.3 | `--only section0 --no-promote`, then `--only section0 --audit`. Three times. | Each verifies; no bad blocks; flush retries recorded. Live firmware untouched. |
| 10.4 | Repeat 10.3, and unplug USB partway through the transfer. Then `--resync`, rerun, audit. | The run fails cleanly; recovery without a power cycle; the audit is clean. Live firmware untouched. |

Then from Home Assistant, with the entry enabled:

| # | Do | Expect | Verify |
| --- | --- | --- | --- |
| 10.5 | The update entity on each channel. | Offers the newest release for the channel; release notes. | O |
| 10.6 | Install with the unit **on**. | Progress rises once from 0 to 100; other entities unavailable and services refused during it; the unit powers off at the end, then on; the firmware sensor shows the new version. | O; A: `monitor` (an explained outage) |
| 10.7 | Mid-install: reload the entry, change options, press other buttons. | The reload waits; buttons and services are refused; the install completes. | O |
| 10.8 | Install with the unit in **standby** (needs a newer release, or a second unit). | Powered on, waits 15 s, updates, left off. Not tested anywhere yet. | O |

Record each run's duration and flush retries.

## 11. Soak

Each stage starts only when the previous one passed. `monitor` runs throughout
with `--out` and the cause sources for the stage, and must exit 0.

| Stage | Duration | Running | Pass |
| --- | --- | --- | --- |
| S1 | 72 hours | Topology D; `exercise --rounds 144 --interval 1800`. | Every invariant; availability 100% outside explained outages. |
| S2 | 7 days | Topology P; proxy `chaos 604800`; `exercise` as S1 with `--proxy-control`. | Every invariant; zero unsolicited writes. |
| S3 | 7 days | Topology D; `usb_fault.sh chaos 168`; `monitor --tty`. | Every invariant. |
| S4 | 30 days | Topology D; normal use of the theater; `mark` anything you cause by hand. | Every invariant; memory and open files flat. |

## Release check

On a disposable Home Assistant OS instance with the Lumagen's USB passed to it
and every other entry disabled:

1. Install the previous release from the Marketplace (Home Assistant 2026.11 or
   newer), set it up, and add an automation that compares an enum state.
2. Update from **Settings** > **Updates**. Expect the restart repair, and
   aiolumagen installed from PyPI at the pinned version.
3. Every entity loads with the same entity ID; the automation behaves as the
   release notes say (0.10.0 renamed the enum states).
4. Install an older version from the Marketplace's menu, then the new one
   again. Both load.
5. Remove the integration; expect the restart repair and nothing left in
   `custom_components`.
6. Topology B, if a bridge is available: sections 1.1, 2.2, 5.1 (at the
   bridge), 9.4.

## Results

Keep each run's journals (`--out`), the proxy log and the Home Assistant log.
File failures where the symptom points: parsing or reconnect behaviour in
aiolumagen, entities and Home Assistant behaviour here.

```text
Date:                 Integration / aiolumagen: 0.10.0 / 0.12.2
Home Assistant:       Lumagen model / firmware:
Topology:             D | P | B          Sources / displays:
Passed:
Failed (with journal excerpt and issue link):
Firmware runs (duration, flush retries):
Soak stage, hours, availability, outages (explained/total):
```
