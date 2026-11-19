# Automatic Monitoring and Printer Selection — Design

**Date:** 2026-11-19
**Status:** Approved in conversation; plan pending
**Scope:** Anycubic Cloud OrcaSlicer plugin (`plugin/plugin.py`)

## Intent

Remove the manual **Start monitoring** step. Monitoring should follow the
selected printer automatically, the only printer on an account should be
selected without opening the picker, and OrcaSlicer's **Device** tab should
point users at the **Anycubic Cloud** tab instead of showing a JSON auth error.

## Goals

- Start MQTT monitoring automatically after a printer is selected.
- Start monitoring automatically after login and when the dashboard loads.
- Auto-select the printer when an account has exactly one FDM printer.
- Remove the **Start monitoring** button and script capability.
- Keep **Stop monitoring** as an explicit escape hatch.
- Make picker rendering tolerate a JSON-string payload instead of failing silently.
- In OrcaSlicer's Device tab, show a plain message directing users to the
  **Anycubic Cloud** tab for print progress.

## Non-goals

- No cloud-protocol changes, new dependencies, or new permissions.
- No changes to upload, print control, or camera behavior beyond copy.
- No automatic selection when the account has more than one printer.

## Constraints

- Support OrcaSlicer 2.5.0+.
- Network work stays off the UI thread (`on_load`, `on_message`).
- The existing Stop Monitoring capability remains available.
- Preserve the existing working-tree edits; do not commit unless asked.

## Approaches Considered

1. Start monitoring inside `select_printer()` itself.
   - Rejected: `select_printer()` also runs from background refreshes and login
     reconciliation; it would restart MQTT on unrelated state updates.
2. Background auto-setup after login/page load, plus a selection hook.
   - Chosen: keeps network work off the UI thread, respects Stop outside
     login/page-load boundaries, and covers the single-printer case.
3. Start monitoring from the two-second status poll whenever it is stopped.
   - Rejected: it would immediately undo the explicit Stop button.

## Design

### Auto-setup

Add `_auto_setup_once()` and `auto_setup_async()` near the printer helpers.

`_auto_setup_once()`:

1. Return quietly when not signed in.
2. Fetch the account's printers.
3. If exactly one printer exists and it is not selected, call
   `select_printer_by_id()` and set a notice.
4. If the cached selection is no longer on the account, clear it with
   `select_printer({})`.
5. If a printer is selected, call `start_monitoring()`.
6. Return `(ok, message)` for tests; expected "nothing to do" paths return
   `(False, "")` so they do not produce scary notices.

`auto_setup_async()` runs that work on a daemon thread, guarded by a lock so
login and page-load callbacks cannot race. It is called from:

- `login_async()` after a successful token exchange.
- `AnycubicCloudPage.on_load()`.
- `_sync_config_token()` when an external `state.json` change is detected.

`start_monitoring()` already starts its own worker thread, so callers do not
block.

### Selection

Replace `restart_monitoring_if_running()` with
`start_monitoring_for_selection()`: stop the existing service when it is
running, then call `start_monitoring()` unconditionally. Use it from:

- `AnycubicCloudPage._pick_printer_async()`
- `AnycubicSelectPrinterScript.execute()`

### Removal of Start monitoring

- Delete the `btnStart` button and its JavaScript handlers.
- Delete the `start_monitor` page-message branch.
- Delete `AnycubicStartMonitorScript` and its registration.
- Keep `btnStop`, `stop_monitor`, and `AnycubicStopMonitorScript`.

### Picker resilience

`render()` will parse a string payload as JSON before checking `d.type`.
`renderPrinters()` will parse string `items` and treat a non-array as empty.
This prevents a host-version serialization difference from leaving the picker
blank or throwing.

### Device tab message

`_BridgeHandler._require_auth()` keeps the JSON 401 for upload requests but,
for GET requests, returns a plain-text message:

> Open the Anycubic Cloud tab in OrcaSlicer to view print progress.

OrcaSlicer's Device tab performs GET requests without the bridge key, so this
avoids displaying `{"error": "Invalid API key..."}` in the embedded webview.

### Frontend design (existing dashboard)

The dashboard already has an established OrcaSlicer visual system: the host
font stack, `--orca-*` color variables, cards, badges, and a single green
primary action. Do not introduce a new palette, typeface, or card treatment.

- **Control row.** Remove the green **Start monitoring** primary button.
  **Select printer** remains the only action next to a quiet **Stop
  monitoring**. Below the row, add one muted line that explains the automatic
  behavior: "Monitoring starts automatically with the selected printer."
  Show it only while a printer is selected and monitoring is not active, so
  it teaches the new behavior without adding permanent chrome.
- **Picker.** Keep the existing full-width row appearance, but render each
  printer as a real `<button type="button">` with `text-align: left`,
  inherited font/color, a visible focus ring, and an accessible label. This
  makes the picker keyboard-usable without changing its visual hierarchy.
- **Device tab.** Return a small HTML page, not JSON. Use
  `color-scheme: light dark`, the system font stack, a 42rem maximum line
  length, and two short lines: a heading "Print progress is in Anycubic
  Cloud" and the sentence "Open the Anycubic Cloud tab in OrcaSlicer to view
  status, temperatures, and the camera." Add a muted note: "This local bridge
  only handles print uploads." No new images, gradients, or decorative
  elements.

The camera hints and README text change from "Start monitoring first" to
wording that describes the automatic connection. The dashboard badge
continues to show `Connecting…`, `Live`, `Stale`, or `Not monitoring`.

## Acceptance

- Signing in on an account with one printer selects it and starts monitoring
  without opening the picker or pressing Start.
- Picking a printer from the picker or the Select Printer script starts MQTT.
- The dashboard no longer has a Start monitoring button; Stop still works.
- The Device tab shows the Anycubic Cloud redirect message, not a JSON error.
- `plugin/tests/smoke_test.py` passes for source and `dist` artifacts.
