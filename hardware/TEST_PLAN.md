# Real-hardware operator acceptance plan

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

## How to run this campaign

This is a checklist, not a list of things to try while someone is watching a
film. Use one operator at Home Assistant (HA) and the physical Lumagen remote;
ideally a second observer watches the display and timestamps unexpected changes.
The **screen** means the actual projector/TV output, not merely an HA state.

| Pass | Order / budget | Gate |
| --- | --- | --- |
| P0, no hardware | Automated suites and fixture checks below; inspect the inventory and backups. | No unit-test failures; matching editable library, not the old published pin. |
| P1, everyday UX | Setup, UI, state and controls: sections 1-3. Allow 60-90 minutes. | Correct screen and HA behavior; baseline restored after each case. |
| P2, hostile link | Deterministic recipes in 3A, then sections 4-7 and 9. Allow several hours for backoff and recovery. | No unexplained actuation, ownership conflict, failed entry or unrecovered outage. |
| P3, compatibility | Section 8, chosen source/display combinations, then a repeat of P1 smoke checks. | Every claimed format/topology has measured evidence. |
| P4, firmware | Section 10, only after its separate approvals. | Do not use a destructive firmware test to debug a failed P1/P2. |
| P5, soak/release | Section 11 and release checks. | No release claim from a short smoke run or from skipped measurements. |

Use **PASS / FAIL / BLOCKED / NOT APPLICABLE** per case. BLOCKED means missing
equipment, baseline, newer firmware, permissions or automation evidence; it
does not count as a pass. N/A needs a reason (for example no ESPHome bridge).
Record the topology and repeat case IDs with a suffix, e.g. `1.1-D`, `1.1-B`.

### Safety, restoration and stop rules

1. Back up HA and the Lumagen's configuration using your established vendor
   procedure. Verify you can restore them. Photograph all values in the
   baseline sheet below. Do not factory-reset or press **Save** just to test it.
2. Reserve an unused input and a safe memory/preset. Do not overwrite calibrated
   labels or settings without recording **every affected bank** first.
3. There must be exactly one owner of the actual port. Stop tools/entries on
   other machines too. Never run the vendor updater, a probe, and HA against
   the same port together. A proxy owns the upstream port continuously.
4. Link faults may affect the serial connection, never arbitrarily modify bytes
   sent *to* the unit. The provided corruption/injection fixtures modify only
   unit-to-HA traffic. Do not improvise raw write/erase/updater commands.
5. After each case: clear faults, wait for a healthy link, restore the saved
   physical settings, and compare the screen and HA again. Clear persistent OSD
   messages; leave the input, memory, aspect/auto-aspect and power as found.
6. **Stop immediately** for unrequested power/input/memory changes, a second
   port owner, identity corruption, an unrecoverable entry, missing restoration,
   or unexpectedly continuing actions after a test stopped. Preserve logs
   before restarting. An expected action error/toast is not an integration
   error log or a traceback.
7. Never cut power, kill HA, change baud, inject serial faults, suspend the VM,
   or reload/disable/delete an entry during a live firmware write. The
   explicitly approved firmware exception cases use simulated sessions first.
   If a real update fails, follow its recovery advice: retry before
   power-cycling an unconfirmed live image.
8. Never disable TLS verification or change global DNS/firewall rules as an
   improvised fixture. Host/root work requires the administrator's approval,
   a scoped target, a reversal procedure and an outage mark.

## Invariants

These hold during and after every test. A test that breaks one fails, whatever
its own expectation says.

| # | Invariant | Verified by |
| --- | --- | --- |
| I1 | The entry never ends up in **Failed to set up**, **Migration error** or **Failed to unload** during normal operation. **Retrying setup** is fine while unreachable. The deliberate mid-firmware manual-unload exception is isolated in 10.11, not scored as a normal pass. | A: `monitor`, `scenarios` |
| I2 | The integration never changes the device on its own. On start, reconnect, recovery, reload and restart it sends queries only (`ZQ…`, `ZE2`); anything else must come from a user or automation action. | A: `fault_proxy.py` audit, unsolicited writes = 0. O: no OSD message, input, aspect, memory or power change on screen during restarts and reconnects. |
| I3 | At the default 15 s poll, a lost link is noticed within 75 s of fault start, and recovery completes within 360 s of fault end, without a reload. Continuous junk must not extend liveness. Measure both deadlines, not merely eventual recovery. | A: `monitor`, `scenarios` |
| I4 | Every outage has a cause: an injected fault, a USB removal, an entry reload, a Home Assistant restart, or an operator mark. | A: `monitor` (unexplained outages = 0) |
| I5 | Report-backed HA values match fresh device reports, including after recovery. Write-only/optimistic values are explicitly distinguished and checked against the OSD; HA success alone is not proof of actuation. | O: OSD **Info** / setting menu against `snapshot` |
| I6 | Malformed, unexpected or damaged device output never changes the unit's identity (model, firmware), raises an error, or leaves an enum entity in a state outside its options. | A: `scenarios` (identity check, error logs) |
| I7 | Errors: none. Warnings: at most 12 an hour, even during a day-long outage. | A: `monitor` |
| I8 | No leaks: memory grows less than 20 MB/day after the first hour, open files stay within a band of 10, at most one handle to the serial port. | A: `monitor` |
| I9 | One owner: never a second connection to the port. | A: `fault_proxy.py` (concurrent connections = 0), `monitor` (serial handles ≤ 1) |
| I10 | Entities/unique IDs are stable for the same exact stored URL across reconnect, reload and restart; no `_2` duplicates. Remove/re-add and URL changes are checked separately in 1.9/1.12. A count alone does not prove registry identity. | A: entity count. O: registry IDs and history |

Serial reports have no checksum/authentication: a damaged byte can sometimes
form another valid value. I6 is still a failure oracle, not a promise that all
valid-looking corruption is detectable. Screenshot and journal a wrong value
even if the next poll repairs it. Do not treat every damaged-stream loss of
availability as a failure; require truthful unavailability and bounded recovery.

## Before attaching hardware

Run both repositories' automated suites in their existing development venvs:

```bash
.venv/bin/python -m pytest -q
(cd ../aiolumagen && .venv/bin/python -m pytest -q)
.venv/bin/python -m pytest -q tests/test_hardware_fault_proxy.py tests/test_hardware_probe.py
```

The integration's [adversarial tests](../tests/test_adversarial.py) cover
overlapping compound controls, stale readback, command gating across firmware
ownership changes, invalid target routing, OSD partial writes, 2,000 seeded
label configurations, repeated outage/reload cycles and cancellation-listener
cleanup. [Firmware orchestration tests](../tests/test_firmware_install.py)
also inject preparation failures, cancellation, concurrent installs and unloads
during download, power-on and transfer, and exercise actual deferred reloads
and power-restoration confirmation. [Cross-layer TCP tests](../tests/test_stack_lifecycle.py)
run Home Assistant with the real library against fragmented replies, dropped
connections and continuous damaged traffic, checking identity, state, one-owner
recovery and query-only startup. These use synthetic data, not the physical unit.

Passing these is not hardware qualification: RS-232 has no checksum, simulated
devices cannot establish firmware timing, and only the hardware soak can
establish the memory/day and recovery deadlines above. Develop against the
editable sibling library; before release, publish its fixes and update the
integration's pinned library requirement.

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
   Use the script for anything that restarts Home Assistant (7.3, and the
   restart scenario): a restart exits with code 100, which the script answers
   by starting it again and the debugger doesn't.
3. Onboard, then create a long-lived access token (your profile, **Security**)
   and save it to `../ha-dev/config/.lumagen_test_token`. That folder is not
   in git.
4. **Topology D:** add the integration with the FTDI port.
5. **Topology P:** start the proxy, then add a second entry with the URL
   `socket://127.0.0.1:14999`. **Disable the direct entry before starting the
   proxy**, not after adding the second entry:

   ```bash
   mkdir -p ../ha-dev/config/lumagen-uat
   ../ha-dev/.venv/bin/python hardware/fault_proxy.py /dev/ttyUSB0 --seed 20261102 --log ../ha-dev/config/lumagen-uat/proxy.jsonl
   ```

Run the Python tools with Home Assistant's venv, which has everything they
need: `../ha-dev/.venv/bin/python` (not through `realpath`, which resolves
the venv's symlink to a Python without its packages).

`127.0.0.1` is relative to HA's network namespace. A host proxy is not reachable
at container loopback unless they share networking. Resolve that before adding
the entry; bind the proxy/control ports to a private, access-controlled
interface only if needed. They have no authentication; never expose them to the
Internet or an untrusted LAN. If topology B is tested through P, use a bridge
URL reachable by the standalone proxy process; an `esphome-hass://` URL depends
on HA's own integration and is not a standalone host connection.

### Baseline and evidence sheet

Keep test output outside the checkout, e.g. create a run directory under
`../ha-dev/config/lumagen-uat/`. Do not publish tokens, PSKs, unredacted serial
URLs or household/device information.

- Record UTC date, both Git SHAs, Python/HA version, topology, adapter/bridge
  firmware, display/source capabilities, stored URL (redacted), entry ID and
  the model/model number/firmware. A zero serial is legitimately not displayed.
- Export/screenshot the entry/device/entity list, including disabled entities,
  entity IDs, names, unique IDs, options and enabled automations. Use the same
  entity IDs in all action examples; examples below are placeholders.
- Photograph the actual input, memory, manual aspect preset, auto-aspect/NLS,
  sharpness triple, game mode, subtitle shift, fan minimum, HDR gamma/nits,
  reporting mode, OSD block character if known, and input labels in A-D.
- Record a known SDR clip, HDR10 clip with known metadata, aspect/grid pattern,
  subtitle pattern and source frame-rate settings. These are the screen oracles.
  Do not judge a tone-mapping number from an arbitrary film frame.
- For optimistic controls, first record the OSD value, then explicitly set HA
  to that same value. This synchronizes its in-memory baseline without
  pretending it was queried. If the OSD cannot reveal it, do not automate its
  restoration: mark the case BLOCKED or use a disposable preset.
- Start `monitor` in a separate terminal with `--proxy-log` in P and/or
  `--marks` for hand faults. Save a `snapshot` before and after each group.
  Mark the case ID before a disconnect/reload/restart and record when its cause
  actually ends. Keep the final JSONL result and exit code, not just console PASS.
- In P, type `expect-write 30 <case-ID>` on the control connection just before
  intentional UI commands, including restoration. Do not leave a broad write
  window open during a query-only startup/recovery check: it would hide a bug.

Examples, run from this checkout; substitute the actual entry ID and run path:

```bash
../ha-dev/.venv/bin/python hardware/lumagen_probe.py --entry ENTRY_ID --out ../ha-dev/config/lumagen-uat/monitor.jsonl monitor --marks ../ha-dev/config/lumagen-uat/marks.jsonl --proxy-log ../ha-dev/config/lumagen-uat/proxy.jsonl
../ha-dev/.venv/bin/python hardware/lumagen_probe.py --entry ENTRY_ID --out ../ha-dev/config/lumagen-uat/before.jsonl snapshot
../ha-dev/.venv/bin/python hardware/lumagen_probe.py mark --marks ../ha-dev/config/lumagen-uat/marks.jsonl --seconds 60 CASE_ID unplug serial
```

For D omit `--proxy-log` and add `--tty ttyUSB0` if that is the actual kernel
name. For process/leak evidence, the monitor must see HA's `/proc` namespace
and identify the correct HA process; otherwise I8 is **unmeasured**, not passed.
With several HA processes, specify the verified `--hass-pid`; update/restart
that measurement after a process restart. Supplement serial-handle counts with
host `lsof`/`fuser` inspection of the specific adapter, not all serial devices.

### Tools

| Tool | Use |
| --- | --- |
| [`fault_proxy.py`](fault_proxy.py) | Logs/audits traffic and injects downstream corruption, latency, silence, duplication, truncation, splitting, stalls, echo and connection faults. `mute-report I30 30` selectively withholds readback; `repeat-inject bad_power 0.2 120` generates deterministic invalid output even with real replies dropped. Both are time-boxed; `clear` cancels them. Open `nc 127.0.0.1 14998`, then `help`. |
| [`lumagen_probe.py`](lumagen_probe.py) `scenarios` | Automated P cases, including noise-only traffic and malformed/overflow reports. Check individual results and selected/planned totals. No match or cancellation is not a pass. This does not certify screen fidelity or every entity/registry ID. |
| `lumagen_probe.py exercise` | Set/confirm/restore report-backed controls, checking compound preservation. `--allow sound picture power` widens impact; `--operator` asks for screen confirmation. Ambiguous/optimistic aspect, HDR, fan and subtitle controls are excluded unless `--allow-unverified --operator` is explicitly supplied **after** aligning the recorded physical baseline. It attempts restoration on failure, restores collateral fields, and aborts if restoration fails. Inspect every skip. In P pass `--proxy-control` so writes are announced and logged. |
| `lumagen_probe.py monitor` | The soak monitor: I1, I3, I4, I7, I8, I9, I10. `--tty ttyUSB0` treats USB removals as causes; `--proxy-log` the proxy's faults; `--marks` your own. Survives Home Assistant restarts. Prints a report hourly and on exit; exits non-zero on failure. |
| `lumagen_probe.py snapshot` | Every Lumagen entity's state, to hold against the OSD. |
| `lumagen_probe.py mark` | Record a cause you created by hand: `mark --marks marks.jsonl --seconds 60 pulled the AC power`. |
| [`usb_fault.sh`](usb_fault.sh) | On the machine whose kernel owns the adapter, as root: logical unplug, driver unbind, flapping, a random schedule. `MARKS=` writes its faults to a marks file. |
| aiolumagen `examples/` | `via_url.py`, `send_raw.py`, and `update_firmware.py` for the firmware section. Disable the entry first. |

Both tool files are unit tested (`tests/test_hardware_*.py`), because a
monitor that blames the wrong cause makes a soak meaningless.

### Timing

At the default 15 s poll: the link is stale after 45 s without accepted reports,
not simply without bytes;
reconnects start immediately, then back off 5, 10, 20, 40… up to 300 s. Hence
75 s to notice and 360 s to recover. With a different poll interval, pass
`--detect-deadline` and `--recovery-deadline` to the tools.

Use 15 s for fault qualification. At 600 s polling, staleness is 900 s, so
the default 75 s detection threshold is intentionally inapplicable. Restore
15 s after testing the option extremes. For ordinary polled fields allow one
poll plus serial/query scheduling; measure push fields independently. A
successful firmware power restore can wait up to 180 s for standby, then up to
60 s for power-on; do not score it using the ordinary poll timeout.

## 1. Setup and configuration

Run add-flow failure cases on a fresh/disposable HA configuration, or before
creating its entry. **A disabled entry still deduplicates the same URL**, so
disabling an existing entry does not let the form reach connection validation.
Do not delete the normal entry merely to manufacture an error; 1.8 separately
tests an alias while preserving the original entry.

| # | Do | Expect | Verify |
| --- | --- | --- | --- |
| 1.1 | Add through **Settings > Devices & services > Add integration > Lumagen Radiance Pro**. Inspect physical USB and, in B, adopted ESPHome proxy labels. Submit the intended port. | Friendly port names, no need to enter a PSK; one entry titled `Lumagen <model>` (firmware belongs on the device page, not the title), one device, all enabled platforms. No screen change during validation or setup. | O: form, entry, device, physical screen. A: snapshot/audit. |
| 1.2 | Add it again with the same port. | Aborts as already configured. | O |
| 1.3 | Before creating an entry for that URL, let `via_url.py` hold the actual port, with every other owner disabled. Attempt to add it through the form, then release the tool and retry. | Clear connection error; no duplicate owner or partial entry. Once the tool releases the port the same form succeeds. | O; A: owner/audit evidence |
| 1.4 | Add it with the cable unplugged, typing the port path. | A clear error on the form. | O |
| 1.5 | Add it with the Lumagen switched off at the wall. | A clear error; succeeds once it's back on. | O |
| 1.6 | Add it with the Lumagen in standby. | Succeeds; power reads off. | O |
| 1.7 | Open **Configure**, change then cancel. Reopen; save poll 5, then 600, then 15, and Production then Beta. Restart HA and reopen. Try 0/601/non-numeric input without saving it. | Cancel leaves options/connection unchanged; defaults/current values are correct, units/range clear, invalid input cannot silently apply; valid values persist and cause one safe reload. Channel changes only release offerings, not installed firmware. | O: options, update card, unchanged screen. A: monitor/audit |
| 1.8 | With the first D entry healthy, try adding the same port through an alias (`/dev/./ttyUSB0` or its by-id path). | Exact-URL duplicate is already configured; an alias can reach a connection-error form because deduplication is URL-based. It must not create another owner or flap the first entry. Cancel/remove the attempted second entry. | O; A: monitor on first entry and host ownership |
| 1.9 | Save IDs/history screenshots; delete the entry, confirm services go away if it was the last, then add the **identical stored URL**. | No stale duplicate device/entities or `_2` names. Compare actual IDs/history rather than assuming a stable URL hash preserves HA's deleted registry rows; record any history/ID loss as a UX finding. | O: device/entity registry and history; A: service availability |
| 1.10 | Open then cancel an add flow. With P refusing connections, submit a manual URL and cancel/close the form while validation waits. After its bounded validation finishes, clear P and try again. Also open two browser add flows for the same URL. | No stuck spinner/ghost entry or port leak; clear already-configured/in-progress/connection result, never two successful entries. Time the entire form; the device-info response wait itself is 5 s plus transport/startup work. | O; A: proxy ownership/logs |
| 1.11 | With USB/proxy discovery empty, open add flow; type a valid reachable socket URL, then separately an unreachable one and a malformed one. | No-ports guidance does not hide manual entry. A valid manual URL works; invalid/unreachable input fails clearly without a traceback, persisted bad entry or changes to the screen. | O |
| 1.12 | Exercise a port/address change: disable the old entry, move D to P or to another bridge, and inspect entry menu choices before trying to change its URL. | Current implementation has **no URL-reconfigure or Lumagen reauthentication flow**. Configure changes only polling/channel. Supported move is remove/add with the new URL; expect new identity/history association, not I10 continuity. Cancel unsupported attempts; never edit `.storage`. | O: compare old/new entry/device IDs; record UX limitation |
| 1.13 | In B, change/recover bridge adoption/address/key through **ESPHome's** own flows, with only one Lumagen entry enabled. | HA-owned `esphome-hass://` connection is retained verbatim; credentials are not copied into a Lumagen form. See 9.4/9.5 for recovery and errors. | O; A |
| 1.14 | Run 1.1/1.6/1.7 once on D and each other claimed topology. | A pass on a TCP fixture or B is not a D serial/USB qualification. Record N/A/BLOCKED for unavailable topologies. | O; A |

### 1A. Frontend and day-to-day usability

Use the device page and a temporary dashboard containing the media player,
remote, key switches/selects/numbers and firmware entity. Test desktop and the
phone/tablet normally used in the theater; retain screenshots, not credentials.

| # | Action | Screen / HA acceptance and restoration |
| --- | --- | --- |
| U1 | Inspect names, icons, categories, units, ranges, selectors, more-info and history. Expand disabled entities too. | No untranslated keys, duplicate controls or unsupported media transport/volume buttons. Fan is **1-10**, sharpness **0-7**, HDR nits **0 to disable; 50-10000 otherwise**. Input selection is the media player's source list, not an input-select entity. Identity sensors are diagnostic; firmware is configuration. |
| U2 | Use the media card to power on/off and select two known inputs; repeat through more-info and a dashboard action. | Device, card power and source agree, source list is unique, and source/output scan letters and fractional rates are readable. The card must not keep showing stale "now playing" signal text in standby. Restore input/power. |
| U3 | Change memory/aspect/auto-aspect and power using the physical remote while HA is open. Leave/reopen the dashboard. | Genuine report-backed changes appear without browser refresh. The aspect select is the nearest **detected content ratio**, not reliable readback of the manually selected preset; Letterbox/16:9 NZ cannot be distinguished. Verify applied geometry on the actual grid pattern, and restore the original preset and auto-aspect separately. |
| U4 | In Developer Tools > States inspect translated enums; build one temporary automation using a lowercase state key. Change browser language if another supported frontend locale is used. | Pretty labels may translate; states/options remain valid keys (`normal`, `high`, `auto`, `hdr`, `sdr`, `off`, `small`, etc.). No raw invalid enum values or missing-translation errors. Delete the temporary automation. |
| U5 | Cause a marked P outage while a card is open. Try a button, source, switch, select, number and domain action, then recover. | Controls become unavailable; any action already in flight fails with a useful translated connection error, not "Unknown error"/traceback. No accumulated actions run after recovery. Serial connected reads off while other entities are unavailable. |
| U6 | Reload, restart, rename device/entities, disable/enable one ordinary entity, and reopen dashboard/history. | Names and IDs persist as scoped by I10; no lost dashboard targets, duplicate entities or frontend reload required. Disabled controls do not leak work or break other entities. Restore names/enablement. |
| U7 | Open each of the four domain actions in Developer Tools > Actions. Try both form and YAML modes. | Descriptions/defaults/selectors are usable; the normal single-unit call does not demand a target the schema would reject. Error messages identify bad data. No input is sent merely by opening/cancelling the action. |
| U8 | Check firmware release summary, full release notes, channel change, unknown installed revision, no-newer-release and vendor-unavailable states. | No false upgrade/downgrade offer, valid date ordering across year boundaries, readable notes, no invented version when unknown. Perform unavailable/year-boundary variants in the fixtures mapped below if real listings cannot supply them; never falsify live installed firmware to enable Install. |

## 2. State fidelity

With Full v5 reporting on (see the README), unless the test says otherwise.
For each, compare the OSD **Info** screen with `lumagen_probe.py snapshot`.

| # | Do | Expect | Verify |
| --- | --- | --- | --- |
| 2.1 | Idle, with the known SDR clip, then HDR10. Read both Info and relevant setting menus. | Identity, input/memory, source/output resolution/rate/scan, aspect, HDR/colorspace and reported settings agree. Record supported/unknown metadata separately; fan/HDR mapping are not proven by a matching optimistic HA value. | O |
| 2.2 | Change input, memory and aspect with the **Lumagen's remote**. | Entities follow within seconds, without a poll. | O; A: `monitor` journal timestamps |
| 2.3 | Standby/on three times, allowing boot/HDMI lock each time; then one short, deliberate rapid-command sample on the Lumagen only. | Ends in the right state with no delayed power action. Do not repeatedly power-cycle the projector/display to create this test. | O; A: monitor |
| 2.4 | Set reporting to **Full v4**, then **Off**, then back to **Full v5**, without touching Home Assistant. | v4: inputs still follow; power and memory on the next poll. Off: everything follows on the next poll. No outage, no errors. | O; A: `monitor` |
| 2.5 | Open the Lumagen's menu with its remote and leave it open for 10 minutes. | No outage; polls don't disturb the menu. | O: the menu stays put. A: `monitor` |
| 2.6 | Rename an input label and change a memory's settings on the unit. | Picked up after a reload at the latest; no errors. | O |
| 2.7 | Idle for 24 hours. | No state churn beyond real changes. | A: `monitor` (changes per hour) |
| 2.8 | HDR to SDR, source disconnect/no-signal, interlaced to progressive, and standby to on; repeat after recovery. | No NaN/infinity, impossible enum, concatenated resolution/rate or stale signal text presented as live. Metadata that the device omits may be unknown/zero or last reported; record which, do not claim independent readback. | O; A: history/snapshot |
| 2.9 | Start/recover while a source is absent or a menu/test pattern is active. | No required source signal for entry setup. Startup queries do not dismiss menus, switch input, enable sharpening or turn standby into on. | O; A: zero unannounced writes |

## 3. Commands

| # | Do | Expect | Verify |
| --- | --- | --- | --- |
| 3.1 | Run the guarded `exercise` command below after recording baselines. | Report-backed set/confirm/restore succeeds; compound fields stay intact. Review every excluded/unknown skip and manually cover it below, not as a hidden pass. Stop if restoration fails. | A: final result/exit code. O: every prompt |
| 3.2 | Buttons: Power on/Standby; Menu/Exit/OK/arrows; Redetect aspect; Test pattern; OSD on/off; Query status; Show aspect; Clear OSD message; Restart inputs/outputs. Exercise Save only on a disposable configuration with explicit approval. | Every advertised button's physical effect matches its label; no double press or missing CR. There are no separate direct-input/aspect/HDR-setup buttons: use the media player/select/remote surfaces below. Exit pattern/menu and restore OSD/input/power. | O; A: audit |
| 3.3 | `send_raw_command`: safe `ZQS01`; compare game-mode switch with `ZY5511`/`ZY5510`, **`cr: true`**, after saving its current value. Submit empty, over-64-character and non-ASCII commands. Do not send arbitrary unknown ASCII to a real unit. | Read/query and documented setter work; restore game mode. Invalid forms/commands give clear validation errors, no traceback and no upstream write. Unknown-command passthrough is tested only against a dummy endpoint: it might be a real device command. | O; A: audit/tests |
| 3.4 | `send_raw_command` targeting another integration's device. | Refused with a validation error. | O |
| 3.5 | Commands while in standby. | Power on works; others fail cleanly or are ignored. | O; A: no error logs |
| 3.6 | Temporary queued automation sending **query-only `ZQS01`** once/second for 10 minutes. Stop/disable it, then watch another minute; repeat with a marked mid-run link loss. | No unbounded backlog or replay after recovery; no writes to the picture. Do not promise ordering between separate parallel automations. Delete/disable the test automation afterward. | A: monitor/audit/trace |
| 3.7 | Send 20 concurrent query-only calls; then the bounded paired-control recipe in 3A. | Calls settle or explicitly fail on a lost link, one port owner, no hang. Compound setting preservation is verified on screen, not inferred from call completion. | A; O |
| 3.8 | Commands while the link is down. | Each fails promptly with an error; nothing is queued and replayed when the link returns. | O; A: proxy audit after recovery |

Avoid **Save to NVRAM** unless you mean to keep the unit's current settings.

Start with report-backed steps (add `--proxy-control 127.0.0.1:14998` in P):

```bash
../ha-dev/.venv/bin/python hardware/lumagen_probe.py --entry ENTRY_ID --out ../ha-dev/config/lumagen-uat/exercise.jsonl exercise --rounds 3 --allow picture power --operator --seed 20261102
```

Only after aligning **all** OSD/HA optimistic baselines, repeat with
`--allow-unverified --allow sound picture power --operator`. This explicitly
includes aspect, HDR mapping, fan and subtitle steps; a pass confirms what HA
sent and the operator saw, not a nonexistent device query. Use the individual
manual cases when a setting cannot safely be restored. In particular, the
aspect exercise requires a test pattern/preset where the actual selected
numeric preset already matches the detected-ratio label, with auto-aspect/NLS
recorded; otherwise leave aspect as a manual-only case.

### 3A. Concrete command and race recipes

Use Developer Tools > Actions in YAML mode. Replace sample entity IDs and use
your reserved test input. Announce intentional writes in P, and keep the
settings menu/grid visible. A script's **parallel** action with two branches is
the way to submit the two calls concurrently; delete that temporary script.

**C1 - Remote, source and setting coverage.**
Select every connected source and at least one disconnected input via the media
card. With the menu initially closed, send this innocuous navigation sequence:

```yaml
action: remote.send_command
target:
  entity_id: remote.lumagen
data:
  command: [menu, down, up, exit]
  num_repeats: 1
  delay_secs: 0.3
```

Check three repeated arrows with a nonzero delay. Test one representative input,
memory and aspect friendly name; compare remaining names with the mappings in
[remote.py](../custom_components/lumagen/remote.py). `save` and `show_aspect` are
not friendly remote names: use their buttons, never assume an unknown friendly
word is rejected (unknown ASCII passes through raw). Do not navigate through
menus where OK/arrow repeats might change calibration.

**C2 - Compound sharpness preservation, including stale/equal readback.**
Record the triple; set a visible, non-extreme baseline (e.g. on / level 2 /
Normal), and wait for an actual `!I30` in the proxy log.

1. Change sensitivity to High, then immediately level to 3; repeat in the
   opposite order and as a parallel pair. On screen, both requested fields
   survive, enabled is unchanged, and the next valid readback agrees.
2. In P: `mute-report I30 30`. Repeat step 1 before that timer expires.
   Immediate HA display may retain old reported state, but the **physical**
   second command must preserve the first new field. Inspect the menu, then
   let the timer expire and verify readback reconciliation.
3. Before any writes, restore Normal/2 and confirm it from a real I30.
   `drop-up on 15` now simulates the device not receiving a write: choose High
   once, while the actual unit stays Normal. Within this short window, inject
   `!I30,Y9N\r\n` (invalid): it must not become a reported value. Then inject
   `!I30,Y2N\r\n`, an **equal, valid** baseline report. Turn `drop-up off`,
   change only level to 3, and inspect the physical menu: sensitivity must
   remain Normal, not resurrect the rejected High write.
   Verify `drop_up` is active before choosing High and the physical menu stays
   Normal. If the timer expired or an unrelated action changed the baseline,
   do not inject a guessed report: restore, repeat, and score the first attempt
   inconclusive rather than passed.
4. Repeat with a real report arriving during the first action using a short
   downstream delay, not by guessing a transaction-specific ACK. Reports are
   untagged; record exact traffic and treat ambiguous timing as inconclusive.
5. `clear`, restore the whole saved triple, await real readback. If baseline
   enabled/level differ, adapt the injected **valid** triple accordingly; the
   injected report must describe the real unit, not a fabricated setting.

For the parallel pair, create a **temporary script** in HA, open its YAML
editor, replace entity IDs, and inspect its execution trace. Run only on the
recorded test preset, then delete it:

```yaml
alias: Lumagen UAT compound burst
mode: single
sequence:
  - parallel:
      - action: select.select_option
        target:
          entity_id: select.lumagen_sharpness_sensitivity
        data:
          option: high
      - action: number.set_value
        target:
          entity_id: number.lumagen_sharpness_level
        data:
          value: 3
```

**C3 - Compound HDR mapping and bounds.**
With the known HDR10 pattern and the OSD mapping menu, record gamma/nits, align
HA, then change nits and gamma sequentially in both orders and as a parallel
pair (e.g. 1000 and HDR). Neither command may reset the other field. Try 0
(disabled), 50 and one reasonable peak; try 10000 only on a safe test preset,
not as an image-quality recommendation. Reject out-of-range data; restore both
fields manually and in HA. This command is write-only: no HA round-trip proves
what the hardware applied.

**C4 - OSD messages, boundaries and sticky block character.**

```yaml
action: lumagen.send_osd_message
data:
  line1: "UAT first row"
  line2: "Second row"
  duration: 9
  center: true
```

Check centered/left-aligned rows, exactly 30 characters, wrapping beyond one
row, truncation beyond two rows with `...`, blank second row, and unsupported
characters being removed. Clear with the **Clear OSD message** button; test
one timed duration too, without assuming the 0-9 scale is seconds.

Set `block_char: "#"` with `message: "Bar ####"`, then send a message omitting
block_char: the setting remains sticky. Submit Message **and** Line 1 together
with a *different* block character, then an empty/unrenderable message:
validation must fail **before changing the sticky setting**. Confirm by
sending the previous bar again. Submit two parallel calls using `#` and `@`:
each message must use its own just-selected character, not the other call's.
Test invalid duration (-1/10) and multi-character block data. Restore the known
block character and clear the message; if its original value was unknowable,
use a disposable preset/approved documented baseline instead of claiming restoration.

**C5 - Labels, collisions, and optional targeting.**
On the reserved input, use `set_input_label` with a 10-character ASCII label,
an empty label (fallback), and a duplicate of another input. Also use another input's numbered fallback as a custom label (for example
input 1 labelled `Input 2`, only if both are in the approved baseline):
all eight sources must remain present, distinct and correctly routed.
Select every affected name and check the physical input number. Reject 11
characters, non-ASCII, input 0/9 and invalid memory with **no write**. Test a
single-bank label only after recording A-D; label reads currently use bank A,
so do not infer B-D contents from the HA source list. Restore each bank.

For each domain action, a single loaded entry permits no target or its own
device/entity target. Unknown/deleted entity, unrelated device and area targets
must fail without fallback. With two **independent real units or an isolated
dummy second unit**, no target or targets matching both units must fail;
targeting one must reach only that unit. Never use two aliases to the same
physical port to simulate a multi-unit installation.

**C6 - Recovery intersects commands.**
Start a benign delayed remote menu sequence, then create a marked P outage.
Check the sequence stops/fails instead of finishing later on the recovered
unit. Repeat with a queued compound pair. Firmware ownership variants are
covered by 10.7 and the simulated tests, not by crashing a real flash. Clear
faults and verify there is no delayed input/power/OSD change for one minute.

## 4. Protocol and link faults

Topology P. Run the suite, then the long scenario once per release:

```bash
../ha-dev/.venv/bin/python hardware/lumagen_probe.py --entry ENTRY_ID --out ../ha-dev/config/lumagen-uat/scenarios.jsonl scenarios
../ha-dev/.venv/bin/python hardware/lumagen_probe.py --entry ENTRY_ID --out ../ha-dev/config/lumagen-uat/long-outage.jsonl scenarios --long --only "long outage"
```

| Scenario | Fault | Expect |
| --- | --- | --- |
| latency 1s, latency 4s, jitter | delay either direction | stays up |
| rare bit flips, line noise, duplicate lines, byte at a time, command echo | damaged but flowing data | stays up; identity unchanged |
| heavy bit flips, truncated lines, LF line endings | badly damaged data | may drop; recovers; no errors |
| malformed lines | Named presets include empty/short/garbage, unknown code, partial, lone CR, NUL/binary, overlong, no `!`, bad power/firmware/sharpness, nonfinite HDR and overflow with a valid-looking power suffix | Healthy reports still keep the link alive; none of the invalid reports poison identity/settings or appear as valid measurements. |
| short stall / long stall | output held 30 s / 90 s, then burst | stays up / drops and recovers |
| device silent, commands lost | one direction silent for 2 minutes | drops within 75 s, recovers |
| noise-only traffic | real replies dropped, invalid reports continue every 0.2 s | Still unavailable within 75 s of fault start; junk must not restart liveness/backoff. |
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
| 4.4 | Run the deterministic noise/overflow recipes below while a stable source is playing. | I3/I5/I6; the display never changes from downstream-only synthetic bytes. | A; O |

### Deterministic downstream fixtures

Open the proxy control connection and keep HA, the actual display and the log
visible. Commands below go to that control prompt, **not** to
`lumagen.send_raw_command`. `inject` and `repeat-inject` are HA-facing only.
Do not use these fixtures during firmware activity.

```text
mark 4.4 noise-only start
drop-down on 120
repeat-inject bad_power 0.2 120
```

HA must become unavailable by 75 s despite continuing received bytes. The
physical picture/input/power must not change. At the end (or after recording a
failure), `clear`; note its UTC time and require recovery within 360 s.
Supplement with:

```text
inject bad_sharpness
inject nonfinite_hdr
inject overflow_suffix
```

Start overflow_suffix with the unit **on**: the valid-looking standby suffix
must not turn HA's power state off, even briefly; inspect the state-event
journal as well as the screen. Follow with a real Query status. Invalid HDR
measurements must not show `nan`/`inf`. `status` shows active report mutes,
repeated injection, and counters; `clear` cancels both new fixtures.

The proxy preserves stream ordering, not per-command transaction IDs. Stalls
and upstream latency deliberately hold queued traffic; after those tests,
allow its queue to drain before checking no-replay or restoring settings.
For a strict "commands while down are not replayed" case use `drop-up`, not
upstream latency. Restart the proxy with a fresh log between unrelated phases
if a clean audit baseline is needed.

## 5. USB faults (topology D)

Run `monitor --tty ttyUSB0 --marks marks.jsonl` throughout. `usb_fault.sh` runs
as root on the machine whose kernel owns the adapter, with `MARKS=` pointing to
a file the monitor reads, or mark each fault by hand.

| # | Do | Expect | Verify |
| --- | --- | --- | --- |
| 5.1 | `usb_fault.sh unplug 10`, `unplug 120`, `unplug 900` | Unavailable within seconds; back within 360 s of replugging (immediately if it reconnects at once). | A: `monitor` |
| 5.2 | Administrator: verify `usb_fault.sh list`, select the adapter with `USB_SERIAL` if necessary, then `usb_fault.sh unbind 30` (device stays, driver goes). | Same as 5.1. No unrelated USB device is touched. | A; O |
| 5.3 | `usb_fault.sh flap 20 1 1`, then `flap 10 5 30` | Recovers after the last; warnings bounded; never two handles. | A: I7, I9 |
| 5.4 | Pull the cable at the Lumagen end; then at the host end; then pull and push back within a second. | As 5.1. | A: `monitor --tty`; O |
| 5.5 | Replug into a different USB port, and through a hub. | Comes back where the path stays the same. Note what happens when it returns as `ttyUSB1`: the entry can't follow, and in a container started with `--device=/dev/ttyUSB0` nothing can until the container restarts. Use `/dev/serial/by-id/…` where udev provides it. | O |
| 5.6 | Unplug during Home Assistant startup; during a command burst (3.6); during a reload. | No **Failed** state; recovers. | A |
| 5.7 | Detach the device from the VM at the hypervisor (Proxmox: remove the USB device, re-add it). | As 5.1. | A |
| 5.8 | With administrator approval and no firmware work, suspend/resume the correct VM for 10 minutes. Mark the full suspension and save host/guest timestamps. | Notices a dead link within 75 s after execution resumes; recovers without stale writes. Do not count the time HA cannot execute as a detection failure or silently ignore clock/PID changes. | A; O |
| 5.9 | `usb_fault.sh chaos 168` for a week, with `monitor` running. | Every invariant holds for the week. | A |

## 6. Device faults and reconfiguration outside Home Assistant

| # | Do | Expect | Verify |
| --- | --- | --- | --- |
| 6.1 | Cut the Lumagen's AC power for 1 minute, then for 30 minutes. `mark` each. | Unavailable; recovers once it has booted; power, firmware and model right. | A: `monitor`; O: I5 |
| 6.2 | Optional stress only with vendor/operator approval: repeat a supported Lumagen-only power interruption, allowing its documented boot intervals. | Recovers after the last. Never rapidly cycle the projector, storage, bridge host or a unit that might still be writing firmware/configuration. | A; O |
| 6.3 | Cut AC power while Home Assistant starts. | **Retrying setup**, then loaded. | A: I1 |
| 6.4 | Cut AC power right after sending a command. | No error beyond the failed command; no replay on return. | A: proxy audit (topology P) |
| 6.5 | Photograph RS-232 settings; change baud away from 9600, wait for an outage, restore it using the physical remote. | Unavailable while mismatched, useful warning, query-only recovery after restoring 9600. The remote is the recovery route; do not rely on an unavailable HA control. | A; O |
| 6.6 | Update or downgrade the Lumagen's firmware with Lumagen's own updater (entry disabled), then enable the entry. | Firmware sensor and device info show the new version; the update entity re-evaluates; no errors with fields the other firmware lacks. | O; A: no errors |
| 6.7 | Optional destructive configuration test on a spare/test profile: restore a known saved configuration, then restore the original one. Factory defaults require separate approval and a verified vendor backup. | After refresh/reload where required, report-backed values reflect the restored settings; unchanged optimistic values are not proof. Confirm saved reporting/baud and labels too. | O |
| 6.8 | Leave the unit in standby for 24 hours, then power it on with its remote. | No outage while in standby; state correct after power on. | A; O |

## 7. Home Assistant reconfiguration and lifecycle

| # | Do | Expect | Verify |
| --- | --- | --- | --- |
| 7.1 | Reload the entry 20 times, a few seconds apart. | Loaded at the end; one connection; I2. | A: `monitor`, proxy audit |
| 7.2 | Disable and enable the entry 10 times. | As 7.1. | A |
| 7.3 | Restart Home Assistant gracefully: with the unit on, in standby, unplugged. | Back to loaded (or **Retrying setup** while unplugged); I2: no command on startup. | A; O: no OSD activity |
| 7.4 | In the isolated dev instance only, mark the crash, verify HA's exact PID, terminate that PID forcibly, then start it through the dev script. | As 7.3; port not locked, no saved command replay or unexpected actuation. Never use name-wide killing or do this during firmware work. | A; O |
| 7.5 | Hard-reset the VM. | As 7.3. | A |
| 7.6 | Disable/re-enable the update entity and restart/reload once with it disabled. Compare request logs over the next scheduled check or automated timer fixture. | Disabled means no new scheduled vendor release traffic; an already-started fetch may finish. Device control stays healthy. Re-enable triggers a check if no cached listing exists. | O; A: HTTP/log evidence |
| 7.7 | Disable Serial connected, then enable it. | Other entities unaffected. (The monitor needs it, so do this last.) | O |
| 7.8 | Rename the device and some entities; restart. | Names kept; integration unaffected. | O |
| 7.9 | Upgrade Home Assistant to the next release or beta; restore a Home Assistant backup. | Loads; same entities; no errors. | A; O |
| 7.10 | Change options while the link is down. | The reload goes to **Retrying setup**; recovers when the link returns. | A |
| 7.11 | Restore the HA test backup into the isolated instance with all competing entries disabled; inspect dashboards, automations, IDs, options and bridge adoption. | No second owner. Existing entry identity is preserved by backup restore; unsupported URL moves are not silently migrated. Restore the normal test instance afterward. | O; A |
| 7.12 | Leave the configure form open, reload/disable the entry in another browser tab, then submit/cancel. Repeat a harmless action from a stale dashboard tab. | Clear outcome; no ghost controls, traceback or hidden write. Reopen current forms before continuing. | O; A |
| 7.13 | Run ordinary theater controls as the normal non-admin user, and setup/options as an admin. | Intended controls work; unauthorized setup/settings remain unavailable according to HA permissions. No integration-specific credential form is invented. | O |

## 8. Sources and displays

Operator-led. For every row: compare the OSD **Info** screen with
`lumagen_probe.py snapshot`, and keep `monitor` running for I6 and I7.
Note each source and display model in the results.

| # | Do | Expect |
| --- | --- | --- |
| 8.1 | Each source you own (streamer, disc player, console, PC) at its native output. | Input, resolution, refresh rate, colorspace, HDR status and aspect match. |
| 8.2 | Resolutions: 480i, 576i, 720p50, 1080i, 1080p24, 2160p23.976, 2160p60. | Resolution sensors end in `i`/`p` correctly; source mode `interlaced`/`progressive`. |
| 8.3 | Frame rates 23.976, 24, 25, 29.97, 50, 59.94, 60, as supported. | Decoded rates agree at the reported precision, not truncated integer wire codes (e.g. 59.94 rather than 59). Compare the OSD/source setting, not a rounded TV badge. |
| 8.4 | SDR Rec.709, SDR in BT.2020, HDR10 with known metadata; HLG/low-latency Dolby Vision only where the actual chain supports/converts them. | Compare what the Lumagen actually reports, not the format name on the source. Metadata may be unknown/zero/last-reported when omitted; no fabricated value, NaN or invalid enum. This is not a certification of unsupported DV/VRR formats. |
| 8.5 | Patterns at 1.33, 1.78, 1.85, 1.90, 2.10, 2.35, 2.40; auto aspect on/off; one deliberately mismatched manual preset. | Detected/applied geometry and auto-aspect agree with the OSD. Aspect select remains a detected-content approximation; it must not be used to claim preset readback. Restore the actual manual preset/NLS/auto-aspect. |
| 8.6 | Turn the source off; put it in standby; unplug its HDMI cable mid-play. | Input status `no_source`, source mode `no_input`; no errors; back when the source returns. |
| 8.7 | Turn the display off and on; unplug its HDMI cable; swap to a different display (other EDID: with and without Rec.2020). | Display supports Rec.2020 follows; output resolution and rate follow; no errors. |
| 8.8 | Console/PC with the features supported by this Lumagen/display chain; toggle the Lumagen's game mode. | Switch and OSD agree. Record ALLM/VRR as N/A unless genuinely supported; no promise that a generic game-mode switch enables them. |
| 8.9 | Switch inputs as fast as the remote allows for a minute. | Ends on the right input; no errors. |
| 8.10 | Test patterns from the Lumagen; HDR setup screens. | Input status `test_pattern` where reported; no errors. |

## 9. Network faults

| # | Do | Expect | Verify |
| --- | --- | --- | --- |
| 9.1 | With a scoped administrator-approved server-side network block, wait for the next daily release check on the current loaded entry; separately reload/restart while still blocked. Use the timer fixture for the short automated variant. | A failed refresh in the **same coordinator** preserves its last listing. A new coordinator after reload may have no listing; device controls/setup still work. Do not claim the in-memory cache survives reload. At most one warning per check. | A; O |
| 9.2 | Restore access; reload. | The update entity finds the latest release again. | O |
| 9.3 | Block lumagen.com after pressing **Install**, before the download finishes. | A clear error; the unit untouched (the download finishes before the session starts). | O |
| 9.4 | Topology B: unplug the bridge's Ethernet for 1 and 15 minutes; power-cycle it; update its firmware over the air; reload the ESPHome integration; change its address. | Each recovers on its own within 360 s of the cause ending; never **Failed**. | A: `monitor --marks` |
| 9.5 | Topology B: change the bridge's API encryption key in ESPHome, then in Home Assistant. | Clear failure while they differ; recovers when they match. | O; A |

Browser "offline" mode does not block HA's own downloads. Scope a network
fixture to HA's vendor HTTP requests, preserve LAN/ESPHome reachability, and
document the reversal. Never redirect a production firmware URL to a synthetic
updater. HTTP 503/redirect/oversize/ZIP corruption and year-boundary release
variants are deterministically covered by the automated fixtures in the
coverage matrix; mark their *visual* variants BLOCKED if no isolated safe
frontend fixture is available.

There is no dedicated vendor-refresh action. Do not assume
`homeassistant.update_entity` on a device-coordinator entity refreshes the
independent release coordinator. Waiting for the daily check is a soak case;
unit tests can advance time without modifying the running hardware instance.

## 10. Firmware

**Separate approval gate; not part of an ordinary UX smoke test.** Earlier
qualification was over a native ESPHome bridge at 230400 on one 4242; that is
not qualification of this new hardening revision, a different model, or D.
Direct USB has not yet carried a qualified real install in this project.
The updater excludes the bootloader, but recovery is not guaranteed by a test
plan: have a vendor-approved recovery route and a Windows machine able to reach
the unit before any flash write. Follow the order and stop at the first failure.

| Gate | Permitted work | Approval/evidence required |
| --- | --- | --- |
| F0 | Synthetic image, HTTP, identity, transfer and cancellation fixtures only. | Run the mapped automated tests; no actual port or vendor EXE execution. Mandatory for everyone. |
| F1 | Trusted EXE parsed offline; actual identity/status, dry-run and audits with the normal owner disabled. | Backup verified, physical remote/recovery available, operator acknowledges temporary updater ownership; no flash erase/write. |
| F2 | Section-0 **scratch only**, explicitly `--only section0 --no-promote`; then audit. | F1 passes on this exact transport/model; theater free; explicit flash-write approval. Never add section 1 or omit `--no-promote`. |
| F3 | Real HA install/live promotion. | F2 passes on the exact transport/model, genuine newer trusted release available, tested vendor recovery, uninterrupted power/network, explicit operator approval. Otherwise BLOCKED, not forced. |

**Do not flash through `fault_proxy.py`.** It owns a fixed-baud upstream and
does not propagate a firmware session's baud changes. P is a normal-protocol
fault fixture, not a qualified firmware transport. Stop it and release the
port before F1-F3; use the actual supported D/B connection. Do not inject a
corrupt image or simulated flash ACK into a real firmware session.

For offline inspection, the example requires a URL argument but does not open
it in `--offline` mode:

```bash
../aiolumagen/.venv/bin/python ../aiolumagen/examples/update_firmware.py UNUSED /path/to/radiance_proMMDDYY.exe --offline
```

For hardware CLI cases below, disable HA's entry **and stop any proxy** first,
verify the port has been released, and power the Lumagen on using its remote.
Run `../aiolumagen/examples/update_firmware.py` with the library's venv Python;
substitute the actual URL and trusted EXE path. A standalone process cannot
borrow an `esphome-hass://` URL from HA. Keep bridge credentials private.
"Read-only" here means no flash write, not no serial commands or temporary
entry into updater mode.

With the entry **disabled**, from the aiolumagen checkout:

| # | Do | Expect |
| --- | --- | --- |
| 10.1 | F1: `update_firmware.py <port> --status`; save output before and after the group. | Correct normal model/serial and updater family, flash layout and both slot headers; no erase/write/promote. Don't confuse model number `1018` with updater family `0x16`. |
| 10.2 | F1: trusted updater parsed offline first; `update_firmware.py <port> <exe> --dry-run`. | Validated bundle/revision and plan naming selected sections/destinations; no flash write. Malformed files are tested synthetically, never downloaded from an arbitrary host. |
| 10.3 | `--only section0 --no-promote`, then `--only section0 --audit`. Three times. | Each verifies; no bad blocks; flush retries recorded. Live firmware untouched. |
| 10.4 | Optional F2-only interruption on approved scratch transfer after synthetic cancellation tests pass. Use the documented `--resync --baudrate <rate-left-by-failed-run>` recovery, then repeat scratch/audit. | Explicit failure, no live promotion; successful resync/retry/audit without guessing baud or power-cycling. Never perform this on a live section-1 or promoting update. If recovery is unclear, stop and use the maintainer/vendor procedure. |

Then from Home Assistant, with the entry enabled:

| # | Do | Expect | Verify |
| --- | --- | --- | --- |
| 10.5 | Without installing: inspect Beta/Production, notes, installed/latest dates, and unavailable/unknown cases from U8/9.1. Attempt a specific version/backup flag only in the synthetic HA fixture. | No silently chosen different version, downgrade or unsupported backup. Newest Production may be older than installed: that is not an update to offer. | O; A |
| 10.6 | F3: install with the unit **on**, recording screen and HA progress continuously. | Download precedes device actions. During ownership transfer controls gate off while update stays available. Progress does not restart for each section; 100% is not completion before cleanup. After successful context exit, fresh standby is observed, then the original on state and new firmware are confirmed. No premature success toast. | O; A: marked monitor, timestamped log |
| 10.7 | Synthetic first: change options during download, power settle and transfer; submit another install; attempt a queued remote/compound action. Approved F3 happy path may change **options only** once and try gated UI controls. | Latest options apply via one reload only after the firmware lock/cleanup finishes; entry stays LOADED, not **Failed to unload**. Second install is refused without clearing the first gate/progress. Direct/queued actions cannot bypass gating. | O; A |
| 10.8 | F3, separate newer release required: install from **standby**. | Powers on, confirms on, settles 15 s before session; after update, standby is confirmed from real reports. No final "left off" claim solely from sending Z97. Mark BLOCKED if no safe newer release exists. | O; A |
| 10.9 | F0 and, where safely available, real download-only failure: deny vendor/CDN traffic before completion or return 503 in the isolated HTTP fixture; change options while download is pending. | Clear translated failure; no power/baud/client handoff or flash command; options still reload once the operation releases its lock. Original picture and power remain. Never continue the network fault after a live session has begun. | O; A |
| 10.10 | F0: wrong model/serial, malformed/oversize/redirected archive, cancelled power-on/settle, disconnect during erase/payload/copy, post-commit verify failure, failed hand-back and unconfirmed power restore. | Exact guards, cleanup, no-success-shaped fallback and useful notifications verified by fixtures below. Do not reproduce these by sabotaging a real live write. | A; visual variant BLOCKED without a safe isolated UI rig |
| 10.11 | **Isolated exception test, synthetic session preferred:** manually reload/unload/disable/delete during download or session. | Refused manual unload may leave HA **Failed to unload**; no firmware work begins after an entry disappeared during preparation, no competing client, no orphan client restart. Finish safely, then restart HA to recover. Record **I1-EXCEPTION**, never normal PASS. Do not use this experiment during a real flash. | A; O in disposable instance |

Record each run's duration and flush retries.
At completion photograph the firmware/Info and power state, check device-page
software version, dismiss only understood notifications, verify entry LOADED
and all controls restored, and perform one query plus a harmless OSD message.
After a failure retain its exact recovery advice; do not retry an uncertain
write by simply toggling power. A successful write with unconfirmed original
power state is a separate warning, not proof the unit is now on or off.

## Coverage ledger: physical evidence versus artificial failures

Every row needs its indicated evidence. A synthetic pass does not fill the
physical column, and a happy-path real flash does not cover cancellation or
hostile image parsing. Record newly found issues against the closest row.

| Failure family / regression | Physical/operator cases | Deterministic automation / fixture |
| --- | --- | --- |
| Form retry, missing/occupied port, empty/wrong identity, flow cancel/duplicate, options persistence | 1.1-1.14, U6-U8 | [config flow](../tests/test_config_flow.py), [setup retry](../tests/test_setup_retry.py) |
| Lost link, reconnect notification, setup/reload warning flood, one owner, cancelled startup/stop, stale bytes | 4-7, U5, 3.8, C6 | [cross-layer TCP](../tests/test_stack_lifecycle.py), [HA adversarial](../tests/test_adversarial.py), library [robustness](../../aiolumagen/tests/test_robustness.py); PTY duplicate-open and waiter/cancellation stress |
| Junk liveness, overflow suffix, atomic malformed status/booleans, nonfinite measurements | 2.8/2.9, 4.4 | New proxy presets/repeated injection, library [protocol tests](../../aiolumagen/tests/test_protocol.py) and robustness fuzz; HA TCP noise test |
| Sharpness merge/race and equal-reply rejection, HDR merge, failed compound writes | C2/C3, 3.7 | Report mute plus inject; [HA adversarial](../tests/test_adversarial.py), [compound controls](../tests/test_switch_number_select.py), library report-generation tests |
| Label collisions/fallbacks, multi-bank late/misattributed replies, ambiguous targets | C5, 1.8/1.12 | [HA adversarial](../tests/test_adversarial.py), [domain actions](../tests/test_send_raw_command.py), library serial/quarantined label-query tests; dummy second unit, not a second owner |
| OSD prevalidation/sticky state/parallel calls, CR termination and friendly commands | C1/C4, 3.2/3.3, U7 | [OSD services](../tests/test_osd_services.py), [remote](../tests/test_remote.py), [command errors](../tests/test_command_errors.py), library command tests |
| Enum options, fractional rates, model/firmware and signal presentation | U1-U4, 2, 8 | [translations](../tests/test_translations.py), [media player](../tests/test_media_player.py), [sensors](../tests/test_sensor.py), library formatting/state tests |
| Release date ordering, unavailable site, unknown charset, size/redirect/allowlist limits | U8, 7.6, 9.1-9.3 | [release coordinator](../tests/test_release_coordinator.py), [update entity](../tests/test_update.py), [firmware install](../tests/test_firmware_install.py); synthetic HTTP/release pages |
| ZIP/PE graph/range/count/work limits, malformed containers, flash geometry/checksum/identity/plan bypass | F0, 10.1/10.2; **never hostile data on real flash** | Library [firmware robustness](../../aiolumagen/tests/test_firmware_robustness.py) plus extraction/container/plan/protocol tests |
| Cancellation at command/payload/erase/copy/baud boundaries, leaked callbacks/transport, hung flush/buffer limits | F0 and approved scratch-only 10.4 | Library firmware robustness: systematic cancellation injections and fake transport faults; [session tests](../../aiolumagen/tests/test_firmware_session.py) |
| Missing final verification/promotion/hand-back, partial live commit wrongly called safe, identity at handoff | F0; happy-path F3 only | Library firmware robustness/overrides; [HA install](../tests/test_firmware_install.py) argument wiring/error/power tests |
| Firmware command gates, early abort/unload, failed preparation, real deferred reload, no false power success | 10.6-10.11 | [firmware gating](../tests/test_firmware_gating.py), HA install real-reload and unconfirmed-power fixtures; runtime logs/screens for successful hardware path |
| Fixture lies: wrong outage cause, warning overcount, missed detect deadline, cancelled/empty campaign, unsafe restore | P0, results review | [proxy tests](../tests/test_hardware_fault_proxy.py), [probe tests](../tests/test_hardware_probe.py); check planned/completed counts and skips |
| Long-lived resource drift, hotplug/VM/network timing and actual firmware compatibility | 5-11 | No short unit test substitutes for physical timing and soak. RSS/FD/owner measurements and screen evidence required. |

F0 command set, without hardware access:

```bash
.venv/bin/python -m pytest -q tests/test_config_flow.py tests/test_setup_retry.py tests/test_adversarial.py tests/test_stack_lifecycle.py tests/test_hardware_fault_proxy.py tests/test_hardware_probe.py
.venv/bin/python -m pytest -q tests/test_firmware_gating.py tests/test_firmware_install.py tests/test_release_coordinator.py tests/test_update.py
(cd ../aiolumagen && .venv/bin/python -m pytest -q tests/test_robustness.py tests/test_firmware_robustness.py)
```

These fixtures run real HA service/error/notification plumbing where applicable,
but mock the physical firmware session and HTTP. They do **not** substitute for
looking at the frontend's rendering. For unavailable synthetic frontend
variants, attach automated evidence and explicitly leave visual status BLOCKED.

## 11. Soak

Each stage starts only when the previous one passed. `monitor` runs throughout
with `--out` and the cause sources for the stage, and must exit 0.

| Stage | Duration | Running | Pass |
| --- | --- | --- | --- |
| S1 | 72 hours | Topology D; `exercise --rounds 144 --interval 1800`. | Every invariant; availability 100% outside explained outages. |
| S2 | 7 days | Topology P; proxy `chaos 604800`; `exercise` as S1 with `--proxy-control`. | Every invariant; zero unsolicited writes. |
| S3 | 7 days | Topology D; `usb_fault.sh chaos 168`; `monitor --tty`. | Every invariant. |
| S4 | 30 days | Topology D; normal use of the theater; `mark` anything you cause by hand. | Every invariant; memory and open files flat. |

These are elapsed observation durations, not estimates from the exerciser's
round count. For S2 schedule enough report-backed rounds for the full week
(approximately 336 at a 30-minute interval); do not enable unverified controls
for an unattended soak. Record skipped steps and manually cover them in P1.
Exercise actions/standby tests are intentional; mark their outages if any.

I8 needs a warm-up hour and enough subsequent process samples; a short run or
an absent `/proc` metric is not "zero growth". Record baseline/end RSS,
post-warm-up MB/day, FD min/max and actual serial ownership. Investigate monotonic
growth, not just endpoint equality after a process restart. Preserve each PID's
segment; a crash/restart is not a memory-leak fix.

Keep raw warning counts for short cases and use the per-scenario budget to spot
repeats. A short intentionally dense fault campaign is not evidence of a
steady-state hourly rate. Verify the 12/hour limit over real full-hour windows,
including an hour of a **single continuing outage**; never raise thresholds or
hide warnings to turn a failed soak green.

If a root USB fault script is interrupted while deauthorized/unbound, its sleep
may never reach the re-enable step. The administrator must restore the exact
adapter/driver and verify the port before restarting HA; clearing the TCP proxy
cannot repair a kernel USB state.

## Release check

On a disposable Home Assistant OS instance with the Lumagen's USB passed to it
and every other entry disabled:

**Blocked until the matching library is published and the integration pin
updated together.** Current editable-only hardening cannot be qualified by
installing the old published library. Do not waive this gate because both
development metadata versions still print the same version numbers.

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
7. Check the existing ESPHome integration still works and its HA-pinned
   `aioesphomeapi` was not replaced by an incompatible dependency. Check for
   Cython/ABI/import errors on a clean HA restart. Record the actual installed
   package versions and the release source, not only the editable source SHAs.

## Results

Keep each run's journals (`--out`), the proxy log and the Home Assistant log.
File failures where the symptom points: parsing or reconnect behaviour in
aiolumagen, entities and Home Assistant behaviour here.

```text
Run ID / UTC start-end:
Operator / observer:
Integration SHA / library SHA / installed package versions:
HA / Python / host-container-VM layout:
Lumagen model / firmware / reporting mode:
Topology / adapter-bridge firmware / sources-displays:
Backup and vendor-recovery check / F0-F3 approval:
Entry and redacted URL / entity inventory evidence:

Case ID + topology:
PASS | FAIL | BLOCKED | N/A | I1-EXCEPTION:
Preconditions / actual actions / actual timing:
Screen before-during-after and HA screenshot/video references:
Expected result / observed result / issue reference:
Automation command + exit code + final/planned/completed/skipped counts:
Fault start / detected unavailable / fault end / recovered UTC:
Baseline fully restored? Actual device check, not HA optimism:
Evidence paths (journal, proxy log, HA log, automation trace):

Firmware: selected release/sections, duration, flush retries,
          session/context completion, confirmed final firmware/power:
Soak: hours per PID, availability, explained/total outages, warning counts,
      post-warm-up MB/day, FD range, exact serial owners:
```

### Exit / sign-off

- Every mandatory row in the coverage ledger has linked A and/or O evidence;
  no empty selection, interrupted campaign, unknown metric or skip is a pass.
- No unresolved regression in everyday UX, identity, control preservation,
  connection recovery or ownership. Known limitations and the isolated manual
  firmware unload exception are listed explicitly, not hidden in totals.
- All temporary automations/scripts, network blocks, proxy mutes/injection,
  VM/USB faults and disposable entries are removed/disabled. Restore baseline
  settings and enablement; confirm the correct production topology is the
  sole port owner. Do not leave a test fault proxy as an accidental dependency.
- One final real-screen check: original source/memory/aspect and auto-aspect,
  no leftover OSD/pattern, expected sharpness/HDR/fan/subtitle/power, correct HA
  device identity, a successful query and a harmless action.
- Archive redacted evidence outside git and name the next blocked qualification
  (for example direct-USB firmware, standby-started real install or long soak).
  Obtain operator sign-off before resuming normal theater use.
