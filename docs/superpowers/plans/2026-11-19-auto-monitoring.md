# Automatic Monitoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make printer selection and login start monitoring automatically, auto-select the only printer on an account, remove the manual Start monitoring capability, and replace the Device-tab JSON auth error with a plain redirect message.

**Architecture:** Add a background auto-setup helper that reconciles the cached printer with the cloud account and starts MQTT. Selection paths reconnect monitoring directly. UI and bridge copy changes are isolated to the dashboard HTML/JS and the localhost bridge handler.

**Tech Stack:** Python 3.12, OrcaSlicer plugin API, `paho-mqtt`, existing `unittest`-style smoke script.

**Spec:** `docs/superpowers/specs/2026-11-19-auto-monitoring-design.md`

## Global Constraints

- Support OrcaSlicer 2.5.0+.
- No new dependencies or permissions.
- Network work must stay off the UI thread.
- Keep `Stop Monitoring` and its script capability.
- Do not commit; the working tree already has unrelated edits.
- Version floor and plugin metadata stay at `0.9.1` after the bump.

## Review Focus

- Account with exactly one printer and no cached selection: must select it and start monitoring.
- Account with multiple printers and no selection: must not pick arbitrarily.
- Account whose cached printer was deleted: must clear the stale selection rather than connect to it.
- User presses Stop, then does not select again: polling must not silently restart monitoring.
- OrcaSlicer Device tab GET without `X-Api-Key`: must receive readable text, not JSON braces.

---

### Task 1: Auto-setup helpers and tests

**Files:**
- Modify: `plugin/plugin.py` around `select_printer_by_id()` / `restart_monitoring_if_running()` (~lines 6006-6021)
- Test: `plugin/tests/smoke_test.py` around the picker test (~lines 682-705)

**Interfaces:**
- Consumes: `STATE`, `list_printers()`, `select_printer_by_id()`, `select_printer()`, `start_monitoring()`
- Produces:
  - `_auto_setup_once() -> tuple[bool, str]`
  - `auto_setup_async() -> bool`
  - `start_monitoring_for_selection() -> tuple[bool, str]`

- [ ] **Step 1: Write failing tests**

Add after the picker assertions in `plugin/tests/smoke_test.py`:

```python
    # Auto-setup selects the only printer and starts monitoring. Multiple
    # printers without a selection must be left for the user.
    saved_list = module.list_printers
    saved_select_by_id = module.select_printer_by_id
    saved_start = module.start_monitoring
    saved_printer = module.STATE.printer
    saved_token = module.STATE.workbench_token
    saved_exp = module.STATE.token_exp
    auto_calls = []
    try:
        module.STATE.workbench_token = "auto-token"
        module.STATE.token_exp = 0
        module.STATE.printer = {}
        module.list_printers = lambda: [{
            "id": 7, "name": "Test Kobra",
            "model": "Anycubic Kobra 2 Pro", "online": True,
        }]
        module.select_printer_by_id = lambda pid: (
            auto_calls.append(("select", pid)) or fake[0])
        module.start_monitoring = lambda: (
            auto_calls.append(("start",)) or (True, "Connecting..."))
        ok, message = module._auto_setup_once()
        assert ok, message
        assert auto_calls == [("select", 7), ("start",)], auto_calls

        auto_calls.clear()
        module.STATE.printer = {}
        module.list_printers = lambda: [
            {"id": 7, "name": "A", "model": "", "online": True},
            {"id": 8, "name": "B", "model": "", "online": True},
        ]
        ok, message = module._auto_setup_once()
        assert not auto_calls and not ok and message == "", (ok, message, auto_calls)
    finally:
        module.list_printers = saved_list
        module.select_printer_by_id = saved_select_by_id
        module.start_monitoring = saved_start
        module.STATE.printer = saved_printer
        module.STATE.workbench_token = saved_token
        module.STATE.token_exp = saved_exp
    print("auto-setup: OK")
```

Also make the existing `pick_printer` test assert that monitoring starts:

```python
    started = []
    saved_start = module.start_monitoring
    module.start_monitoring = lambda: (
        started.append(True) or (True, "Connecting..."))
    try:
        page.on_message({"type": "pick_printer", "id": 7})
        picked = wait_for("printers", lambda m: m.get("closed"))
        assert picked is not None, "pick_printer did not close the picker"
        assert started, "selecting a printer must start monitoring"
    finally:
        module.start_monitoring = saved_start
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd plugin && PYTHONPATH=tests python3 tests/smoke_test.py`
Expected: FAIL because `_auto_setup_once` is missing and selection does not start monitoring.

- [ ] **Step 3: Implement helpers**

Replace `restart_monitoring_if_running()` with:

```python
def start_monitoring_for_selection():
    """Connect, or reconnect, MQTT after a printer selection."""
    with STATE.lock:
        service = STATE.mqtt_service
    if service is not None and service.running:
        service.stop()
    return start_monitoring()


def _auto_setup_once():
    """Select the account's only printer and start monitoring."""
    with STATE.lock:
        if not STATE.is_logged_in():
            return False, ""
        printer = dict(STATE.printer or {})

    try:
        printers = list_printers()
    except Exception as exc:
        if not printer.get("id"):
            return False, "Could not load printers: %s" % exc
        printers = None

    if printers is not None:
        ids = [item.get("id") for item in printers]
        if len(printers) == 1 and printer.get("id") != ids[0]:
            chosen = select_printer_by_id(ids[0])
            if chosen is None:
                return False, "That printer is no longer on this account."
            printer = chosen
            STATE.set_notice("Selected %s." % (chosen.get("name") or "printer"))
        elif printer.get("id") not in ids:
            select_printer({})
            printer = {}
            if len(printers) > 1:
                return False, ""

    if not printer.get("id"):
        return False, ""
    return start_monitoring()


_auto_setup_lock = threading.Lock()
_auto_setup_thread = None


def auto_setup_async():
    """Run auto-setup on a worker thread so page callbacks stay responsive."""
    global _auto_setup_thread
    with STATE.lock:
        if not STATE.is_logged_in():
            return False
    with _auto_setup_lock:
        if _auto_setup_thread is not None and _auto_setup_thread.is_alive():
            return False

        def worker():
            global _auto_setup_thread
            try:
                ok, message = _auto_setup_once()
                if message:
                    STATE.set_notice(message, error=not ok)
            except Exception as exc:
                STATE.set_notice("Auto-connect failed: %s" % exc, error=True)
            finally:
                with _auto_setup_lock:
                    _auto_setup_thread = None

        _auto_setup_thread = threading.Thread(
            target=worker, name="anycubic-auto-setup", daemon=True)
        _auto_setup_thread.start()
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd plugin && PYTHONPATH=tests python3 tests/smoke_test.py`
Expected: PASS through `auto-setup: OK` and `picker pick: OK`.

---

### Task 2: Wire automatic set-up and remove Start capability

**Files:**
- Modify: `plugin/plugin.py`:
  - `login_async()` (~6032-6052)
  - `AnycubicCloudPage.on_load()` (~6546-6561)
  - `AnycubicCloudPage._sync_config_token()` (~6485-6526)
  - `AnycubicCloudPage._pick_printer_async()` (~6277-6297)
  - `AnycubicCloudPage.on_message()` (~6120-6130)
  - `AnycubicSelectPrinterScript.execute()` (~6671-6707)
  - `AnycubicStartMonitorScript` and registration (~6710-6771)
- Test: `plugin/tests/smoke_test.py` registration assertion (~line 77)

**Interfaces:**
- Consumes: `auto_setup_async()`, `start_monitoring_for_selection()`
- Produces: no new public API; removes `AnycubicStartMonitorScript`

- [ ] **Step 1: Change the capability-count test**

Change:

```python
assert len(orca_stub.registered) == 5, "expected 5 capabilities"
```

to:

```python
assert len(orca_stub.registered) == 4, "expected 4 capabilities"
```

- [ ] **Step 2: Run tests to verify the new assertion fails**

Run: `cd plugin && PYTHONPATH=tests python3 tests/smoke_test.py`
Expected: FAIL with `expected 4 capabilities`.

- [ ] **Step 3: Wire the helpers and delete Start**

- In `login_async()` worker, after `STATE.set_notice(message, error=not ok)`, add:

```python
            if ok:
                auto_setup_async()
```

- In `on_load()`, after `_sync_config_token()` and before `start_bridge()`, add:

```python
        try:
            auto_setup_async()
        except Exception:
            pass
```

- In `_sync_config_token()`, capture the reload result:

```python
        reloaded = False
        try:
            reloaded = STATE.reload_if_changed()
            if reloaded:
                self._config_synced = False
        except Exception:
            pass
```

  Then, in the signed-in branch, after `_update_config_after_login()`:

```python
            if reloaded:
                auto_setup_async()
```

- In `_pick_printer_async()`, replace
  `restart_monitoring_if_running(name)` with
  `start_monitoring_for_selection()`.
- In `AnycubicSelectPrinterScript.execute()`, replace
  `restart_monitoring_if_running(name)` with
  `start_monitoring_for_selection()`.
- Delete the `start_monitor` branch from `on_message()`.
- Delete `AnycubicStartMonitorScript` and its `orca.register_capability(...)`
  line. Keep `AnycubicStopMonitorScript`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd plugin && PYTHONPATH=tests python3 tests/smoke_test.py`
Expected: PASS, capability list has 4 entries.

---

### Task 3: Dashboard UI, picker resilience, and Device-tab message

**Files:**
- Modify: `plugin/plugin.py`:
  - `_BridgeHandler._require_auth()` (~3417-3423)
  - dashboard HTML (`btnStart`, `btnStop` ~4589-4591)
  - camera hint (~5593-5601)
  - `render()` (~5604-5609)
  - `renderPrinters()` (~5136-5165)
  - status/button JS (~5721-5733)
- Test: `plugin/tests/smoke_test.py` bridge section (~587-605) and UI assertions (~670-705)

**Interfaces:**
- Consumes: existing `_BridgeHandler._send_json()`
- Produces: `_BridgeHandler._send_text(status: int, message: str) -> None`

- [ ] **Step 1: Write the bridge and UI tests**

Replace the wrong-key assertion with:

```python
        code, text = bridge_request_raw(
            base + "/api/version", headers={"X-Api-Key": "wrong"})
        assert code == 401, (code, text)
        assert "Anycubic Cloud tab" in text and "print progress" in text, text

        body, content_type = bridge_multipart("denied.gcode", b"G28\n", False)
        code, payload = bridge_request(
            base + "/api/files/local", method="POST", data=body,
            headers={"X-Api-Key": "wrong", "Content-Type": content_type})
        assert code == 401 and "api key" in payload["error"].lower(), (code, payload)
```

Add `bridge_request_raw()` next to `bridge_request()`:

```python
    def bridge_request_raw(url, method="GET", data=None, headers=None):
        req = urllib.request.Request(
            url, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
```

Add UI assertions where `ui = page.get_ui()` is checked:

```python
    assert 'id="btnStart"' not in ui, "manual Start monitoring button must be gone"
    assert 'id="btnStop"' in ui, "Stop monitoring must remain"
    assert 'id="autoNote"' in ui, "automatic-monitoring note must exist"
    assert "Monitoring starts automatically" in ui
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd plugin && PYTHONPATH=tests python3 tests/smoke_test.py`
Expected: FAIL because GET still returns JSON and `btnStart` is still present.

- [ ] **Step 3: Implement the UI and bridge changes**

- Add:

```python
    def _send_text(self, status, message):
        body = (
            "<!doctype html><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width\">"
            "<title>Anycubic Cloud</title>"
            "<style>"
            "body{margin:0;padding:32px;color-scheme:light dark;"
            "font:15px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',"
            "sans-serif;max-width:42rem}"
            "h1{font-size:20px;line-height:1.25;margin:0 0 12px}"
            "p{margin:0 0 10px}"
            ".note{opacity:.68;font-size:13px}"
            "</style>"
            "<h1>Print progress is in Anycubic Cloud</h1>"
            "<p>Open the Anycubic Cloud tab in OrcaSlicer to view status, "
            "temperatures, and the camera.</p>"
            "<p class=\"note\">This local bridge only handles print uploads.</p>"
        ).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass
```

  (The message text is fixed, so no user input is interpolated.)

- Change `_require_auth()` to send the plain page for GET:

```python
    def _require_auth(self):
        if self._authorized():
            return True
        if self.command == "GET":
            self._send_text(401)
        else:
            self._error(
                401,
                "Invalid API key. Copy the key from the Anycubic Cloud tab.")
        return False
```

- Replace the `btnStart`/`btnStop` row with:

```html
    <button id="btnSelect">Select printer</button>
    <button id="btnStop">Stop monitoring</button>
```

  and add below the row:

```html
    <div class="muted" id="autoNote" style="display:none">
      Monitoring starts automatically with the selected printer.
    </div>
```

- Remove `$("btnStart")` status/onclick lines and the `start_monitor` message.
- In `render()`, after the printer name is set, show the note only when a
  printer is selected and monitoring is off:

```javascript
    var autoNote = $("autoNote");
    autoNote.style.display = (printer.name && !d.monitoring) ? "block" : "none";
```

- Change the camera hint to:

```javascript
    else if (!monitoring) hint = "Connecting to the printer's cloud session…";
```

- In `render()`, add string normalization at the top:

```javascript
  function render(d) {
    if (typeof d === "string") {
      try { d = JSON.parse(d); } catch (e) { return; }
    }
```

- In `renderPrinters()`, replace the direct `d.items` checks with:

```javascript
    var items = d.items;
    if (typeof items === "string") {
      try { items = JSON.parse(items); } catch (e) { items = []; }
    }
    if (!Array.isArray(items)) items = [];
    if (!items.length) {
      list.innerHTML = '<div class="muted">No FDM printers on this Anycubic account.</div>';
      return;
    }
    items.forEach(function (p) {
```

- Render each printer as a keyboard-usable button while keeping the row look:

```javascript
    items.forEach(function (p) {
      var row = document.createElement("button");
      row.type = "button";
      row.className = "row printer-choice";
      row.style.cssText = "width:100%;font:inherit;color:inherit;text-align:left;"
        + "background:transparent;padding:8px;border:1px solid "
        + "var(--orca-border,#e3e3e3);border-radius:8px;margin-bottom:6px;"
        + "cursor:pointer";
      row.setAttribute("aria-label", "Select " + (p.name || "printer"));
      ...
      row.onclick = function () {
        list.innerHTML = '<div class="muted">Selecting…</div>';
        B.postMessage({ type: "pick_printer", id: p.id });
      };
      list.appendChild(row);
    });
```

  Add a focus style next to the existing dashboard CSS:

```css
  .printer-choice:focus-visible { outline: 2px solid var(--orca-accent, #2f9e79); outline-offset: 2px; }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd plugin && PYTHONPATH=tests python3 tests/smoke_test.py`
Expected: PASS, including bridge and UI assertions.

---

### Task 4: Documentation, version, and release artifacts

**Files:**
- Modify: `README.md` (version table, sign-in, camera, troubleshooting)
- Modify: `plugin/plugin.py` header and `PLUGIN_VERSION` to `0.9.1`
- Generate: `dist/anycubic_cloud_any.py`, `dist/anycubic_cloud_any.py.sha256`,
  `dist/anycubic_linux_x86_64.py`, `dist/anycubic_linux_x86_64.py.sha256`
- Test: same smoke suite against source and dist

**Interfaces:**
- Consumes: the completed plugin source
- Produces: release artifacts identical to `plugin/plugin.py`

- [ ] **Step 1: Update README copy**

- Change the version table row to `0.9.1`.
- Replace `Sign in → Select printer → Start monitoring` with
  `Sign in → choose a printer` and note monitoring starts automatically.
- Replace camera text `Start monitoring first` with a note that monitoring
  follows the selected printer.
- Replace `Press Start monitoring` with `Select an online printer; monitoring
  starts automatically`.
- Add a bridge note: the **Device** tab is not the status view; use the
  **Anycubic Cloud** tab for print progress.

- [ ] **Step 2: Bump the version**

Change the header `# version = "0.9.0"` and `PLUGIN_VERSION = "0.9.0"` to
`0.9.1`.

- [ ] **Step 3: Sync dist artifacts**

Run:

```bash
cp plugin/plugin.py dist/anycubic_cloud_any.py
cp plugin/plugin.py dist/anycubic_linux_x86_64.py
shasum -a 256 dist/anycubic_cloud_any.py > dist/anycubic_cloud_any.py.sha256
shasum -a 256 dist/anycubic_linux_x86_64.py > dist/anycubic_linux_x86_64.py.sha256
```

- [ ] **Step 4: Run the full verification**

Run:

```bash
python3 -m py_compile plugin/plugin.py
(cd plugin && PYTHONPATH=tests python3 tests/smoke_test.py)
(cd plugin && ANYCUBIC_PLUGIN_PATH=../dist/anycubic_cloud_any.py \
  PYTHONPATH=tests python3 tests/smoke_test.py)
```

Expected: both smoke-test runs end with `ALL CHECKS PASSED` (or the existing
success marker) and no traceback.

---

## Self-Review Notes

- Spec coverage: auto-setup (Task 1-2), selection (Task 2), removal of Start
  (Task 2-3), picker resilience (Task 3), Device-tab copy (Task 3), docs and
  artifacts (Task 4).
- Type consistency: all helpers return `(bool, str)`; `auto_setup_async()`
  returns `bool` for callers that want to know whether a worker started.
- Deliberate omission: no commit steps, because the working tree contains
  pre-existing edits and the user has not asked for commits.
