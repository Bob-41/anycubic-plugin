"""Smoke test for plugin.py using the stub orca module.

Run from the plugin folder:
    PYTHONPATH=tests python3 tests/smoke_test.py

Set ANYCUBIC_PLUGIN_PATH to test a packaged artifact instead:
    ANYCUBIC_PLUGIN_PATH=../dist/anycubic_cloud_any.py \
        PYTHONPATH=tests python3 tests/smoke_test.py
"""

import atexit
import base64
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_PATH = os.environ.get("ANYCUBIC_PLUGIN_PATH") or os.path.join(
    os.path.dirname(HERE), "plugin.py")

sys.path.insert(0, HERE)
import orca_stub  # noqa: E402

# The plugin does `import orca` and only uses module attributes; expose the
# stub under that name so the plugin can be imported outside OrcaSlicer.
sys.modules["orca"] = orca_stub


def load_plugin():
    spec = importlib.util.spec_from_file_location("anycubic_plugin", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    # Never read or write the plugin's real state.json during tests. Point the
    # module at a throwaway state file and start from a clean PluginState so a
    # test run cannot leak tokens, printer details, or temp watch paths.
    state_dir = tempfile.mkdtemp(prefix="anycubic-plugin-state-")
    module.STATE_PATH = os.path.join(state_dir, "state.json")
    module.STATE = module.PluginState()
    atexit.register(shutil.rmtree, state_dir, True)
    return module


def main():
    module = load_plugin()
    print("import: OK")

    # Directory scans can block on macOS; production uses the background
    # watcher. Most unit tests use the deterministic synchronous fallback.
    saved_watch_background = module.WATCH_BACKGROUND
    module.WATCH_BACKGROUND = False

    def reset_watch():
        module.stop_watch_thread()
        with module._watch_lock:
            module._watch_state.update({
                "dir": "", "resolved": "", "items": [], "error": "",
                "scanning": False, "scan_started": 0.0,
                "last_scan": 0.0, "last_ok": 0.0, "force": False,
            })

    package_cls = None
    for value in vars(module).values():
        if isinstance(value, type) and getattr(value, "__orca_plugin__", False):
            package_cls = value
            break
    assert package_cls is not None, "no @orca.plugin package class found"

    package = package_cls()
    package.register_capabilities()
    print("registered capabilities:", [c.__name__ for c in orca_stub.registered])
    print("permissions:", json.dumps(orca_stub.permissions, indent=2))
    assert len(orca_stub.registered) == 5, "expected 5 capabilities"
    assert isinstance(orca_stub.permissions[0].get("network_socket"), list)
    assert "('', 0)" in orca_stub.permissions[0]["network_socket"], \
        "paho's local bind must be requested up front"
    fs_read = orca_stub.permissions[0].get("fs_read", [])
    for root in module.WATCH_ROOTS:
        assert root in fs_read, "watch-folder root must be granted: %s" % root
        assert os.path.join(root, "*") in fs_read, \
            "watch-folder tree must be granted: %s" % root
    network_http = orca_stub.permissions[0].get("network_http", [])
    assert "https://kinesisvideo.us-east-2.amazonaws.com" in network_http, \
        "KVS control-plane host must be granted for the camera"
    assert "https://webrtc2-ap-web-1.agora.io" in network_http, \
        "Agora access-point host must be granted for the camera"
    log_dir = module._anycubic_log_dir()
    if log_dir:
        assert log_dir in fs_read, \
            "Anycubic Slicer Next log dir must be granted for token auto-detect"
        assert os.path.join(log_dir, "*") in fs_read, \
            "Anycubic Slicer Next log tree must be granted for token auto-detect"
    print("watch permissions: OK")

    # Names must be unique and semicolon-free.
    names = []
    instances = []
    for cls in orca_stub.registered:
        instance = cls()
        instances.append(instance)
        name = instance.get_name()
        assert name and ";" not in name, "bad capability name: %r" % name
        names.append(name)
    assert len(set(names)) == len(names), "duplicate capability names: %r" % names
    print("names:", names)

    page = instances[0]
    assert page.get_ui().count("<html>") == 1
    assert page.has_config_ui() is True
    assert page.get_config_ui().count("<html>") == 1
    default_config = page.get_default_config()
    assert isinstance(default_config, dict) and "rs256_token" in default_config
    print("config ui: OK, default config keys:", sorted(default_config))

    # A first MQTT connect can be blocked while OrcaSlicer's permission prompt
    # is open. The worker must stay alive and retry so approving the prompt is
    # enough to finish connecting (v0.6.2).
    import types

    state = module.STATE
    saved_state = (
        state.workbench_token, state.email, state.printer,
        state.mqtt_error, state.mqtt_connected, state.monitoring,
    )
    state.workbench_token = "retry-token"
    state.email = "retry@example.com"
    state.printer = {"id": 1, "key": "retry-unionid", "machine_type": 20021}

    attempts, sleeps, loops = [], [], []

    class FakeMqttClient:
        def __init__(self, *args, **kwargs):
            pass
        def username_pw_set(self, *args, **kwargs):
            pass
        def tls_set_context(self, *args, **kwargs):
            pass
        def connect(self, host, port, keepalive=30):
            attempts.append((host, port))
            if len(attempts) < 3:
                raise PermissionError("audit permission required")
            return 0
        def loop_forever(self, **kwargs):
            loops.append(kwargs)

    paho = types.ModuleType("paho")
    paho.__path__ = []
    paho_mqtt = types.ModuleType("paho.mqtt")
    paho_mqtt.__path__ = []
    paho_client = types.ModuleType("paho.mqtt.client")
    paho_client.CallbackAPIVersion = types.SimpleNamespace(VERSION2=2)
    paho_client.MQTTv311 = 4
    paho_client.Client = FakeMqttClient
    paho_mqtt.client = paho_client
    paho.mqtt = paho_mqtt

    saved_modules = {
        key: sys.modules.get(key)
        for key in ("paho", "paho.mqtt", "paho.mqtt.client")
    }
    saved_sleep = module.time.sleep
    sys.modules.update({
        "paho": paho, "paho.mqtt": paho_mqtt, "paho.mqtt.client": paho_client,
    })
    module.time.sleep = lambda seconds: sleeps.append(seconds)
    service = module.MqttService()
    service.running = True
    service._credentials = lambda token, email: ("client", "user", "pass")
    try:
        service._run()
    finally:
        module.time.sleep = saved_sleep
        for key, value in saved_modules.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
        (state.workbench_token, state.email, state.printer,
         state.mqtt_error, state.mqtt_connected, state.monitoring) = saved_state
        service.running = False
    assert len(attempts) == 3, attempts
    assert sleeps == [2.0, 4.0], sleeps
    assert len(loops) == 1, loops
    print("mqtt permission retry: OK")

    # Poll without any saved config must not explode and must post a status.
    page.on_message({"type": "poll"})
    posted = getattr(page, "_posted_local", [])
    assert posted, "poll did not post a status message"
    status = posted[-1]
    assert status["type"] == "status"
    for key in ("login", "printer", "mqtt", "temps", "print", "raw",
                "printing", "watch", "upload", "job"):
        assert key in status, "missing status key %r" % key
    for key in ("scanning", "stalled", "status", "scan_age", "files", "dir"):
        assert key in status["watch"], "missing watch key %r" % key
    print("poll status: OK")
    print(json.dumps(status, indent=2)[:1200])

    def wait_for(kind, pred=None, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for msg in getattr(page, "_posted_local", []):
                if isinstance(msg, dict) and msg.get("type") == kind:
                    if pred is None or pred(msg):
                        return msg
            time.sleep(0.02)
        return None

    # ---- watch folder, upload and start print (v0.6.0) ---------------------
    import tempfile

    watch_root = os.path.realpath(tempfile.mkdtemp(prefix="anycubic-watch-"))
    gcode = os.path.join(watch_root, "part.gcode")
    with open(gcode, "w", encoding="utf-8") as fh:
        fh.write("; smoke test\nG28\nM104 S0\n")
    with open(os.path.join(watch_root, "notes.txt"), "w", encoding="utf-8") as fh:
        fh.write("not g-code\n")
    with open(os.path.join(watch_root, ".hidden.gcode"), "w", encoding="utf-8") as fh:
        fh.write("hidden\n")

    saved_roots = module.WATCH_ROOTS
    saved_default = module.DEFAULT_WATCH_DIR
    saved_watch = module.STATE.watch_dir
    module.WATCH_ROOTS = (watch_root,)
    module.DEFAULT_WATCH_DIR = watch_root
    module.STATE.watch_dir = watch_root
    try:
        reset_watch()
        items, err = module.watch_files(force=True)
        assert err == "", err
        assert [item["name"] for item in items] == ["part.gcode"], items
        assert module._safe_gcode_path(gcode) == os.path.realpath(gcode)
        assert module._safe_gcode_path(os.path.join(watch_root, "notes.txt")) is None
        assert module._safe_gcode_path("/etc/hosts") is None
        print("watch folder: OK", items[0]["name"], items[0]["size_text"])

        page.on_message({"type": "refresh_files"})
        files_msg = wait_for("files")
        assert files_msg is not None, "refresh_files did not post a file list"
        assert files_msg["dir"] == watch_root and files_msg["items"][0]["name"] == "part.gcode"
        print("file list message: OK")

        # build_status() runs on the UI thread and must never enumerate a
        # directory. With background scanning enabled it only copies the cache.
        saved_scandir = module.os.scandir

        def forbidden_scandir(*args, **kwargs):
            raise AssertionError("build_status() performed filesystem I/O")

        module.os.scandir = forbidden_scandir
        try:
            status = module.build_status()
        finally:
            module.os.scandir = saved_scandir
        assert status["watch"]["files"][0]["name"] == "part.gcode", status["watch"]
        assert status["watch"]["scanning"] is False
        print("nonblocking build_status: OK")

        # The background watcher owns scans, and a forced refresh picks up new
        # files without the UI thread doing any work.
        module.WATCH_BACKGROUND = True
        reset_watch()
        module.ensure_watch_thread()
        deadline = time.time() + 5.0
        snapshot = module.watch_snapshot()
        while time.time() < deadline:
            snapshot = module.watch_snapshot()
            if any(f["name"] == "part.gcode" for f in snapshot["files"]):
                break
            time.sleep(0.05)
        assert any(f["name"] == "part.gcode" for f in snapshot["files"]), snapshot

        added = os.path.join(watch_root, "second.gcode")
        with open(added, "w", encoding="utf-8") as fh:
            fh.write("; second\nG28\n")
        module.request_watch_refresh(force=True)
        deadline = time.time() + 5.0
        names = []
        while time.time() < deadline:
            snapshot = module.watch_snapshot()
            names = [f["name"] for f in snapshot["files"]]
            if "second.gcode" in names:
                break
            time.sleep(0.05)
        assert "second.gcode" in names, snapshot
        print("background watcher: OK", names)

        module.stop_watch_thread()
        time.sleep(0.3)
        module.WATCH_BACKGROUND = False
        reset_watch()

        # A scan stuck in the kernel must be reported as stalled even though
        # the UI thread keeps polling normally.
        with module._watch_lock:
            module._watch_state["dir"] = watch_root
            module._watch_state["scanning"] = True
            module._watch_state["scan_started"] = (
                time.time() - module.WATCH_STALL_SECONDS - 5.0)
            module._watch_state["last_scan"] = time.time()
        snapshot = module.watch_snapshot()
        assert snapshot["stalled"] is True, snapshot
        assert "stuck" in snapshot["error"].lower(), snapshot
        status = module.build_status()
        assert status["watch"]["stalled"] is True, status["watch"]
        assert "stuck" in status["watch"]["error"].lower(), status["watch"]
        reset_watch()
        print("stalled watchdog: OK")

        assert "Scanning the watch folder" in page.get_ui()
        print("dashboard scan states present: OK")

        # Upload: lock -> PUT -> claim -> unlock, using a fake cloud.
        saved_http = module._http_json
        saved_put = module._put_presigned
        saved_token = module.STATE.workbench_token
        saved_exp = module.STATE.token_exp
        calls = []

        def fake_http(method, path, body=None, token="", timeout=20):
            calls.append((path, body))
            if path == module.UPLOAD_LOCK_PATH:
                return {"code": 1, "data": {"id": 77, "preSignUrl": "https://s3.example/put"}}
            if path == module.UPLOAD_CLAIM_PATH:
                return {"code": 1, "data": {"id": 4242}}
            if path == module.UPLOAD_UNLOCK_PATH:
                return {"code": 1, "data": {}}
            raise AssertionError("unexpected API path %r" % path)

        module._http_json = fake_http
        module._put_presigned = lambda url, path, size, on_progress=None: (
            on_progress(50.0) if on_progress else None)
        module.STATE.workbench_token = "smoke-token"
        module.STATE.token_exp = int(time.time()) + 3600
        try:
            result = module.upload_gcode(gcode)
        finally:
            module._http_json = saved_http
            module._put_presigned = saved_put
            module.STATE.workbench_token = saved_token
            module.STATE.token_exp = saved_exp
        assert result["file_id"] == 4242, result
        assert [entry[0] for entry in calls] == [
            module.UPLOAD_LOCK_PATH, module.UPLOAD_CLAIM_PATH, module.UPLOAD_UNLOCK_PATH]
        assert calls[0][1] == {"size": os.path.getsize(gcode), "name": "part.gcode", "is_temp_file": 0}
        assert calls[2][1] == {"id": 77, "is_delete_cos": 0}
        print("upload flow: OK, file_id", result["file_id"])

        # A failed PUT must release the lock as a deletion.
        calls = []
        module._http_json = fake_http
        module.STATE.workbench_token = "smoke-token"
        module.STATE.token_exp = int(time.time()) + 3600

        def failing_put(url, path, size, on_progress=None):
            raise module.UploadError("simulated PUT failure")

        module._put_presigned = failing_put
        try:
            try:
                module.upload_gcode(gcode)
                raise AssertionError("upload should have failed")
            except module.UploadError:
                pass
        finally:
            module._http_json = saved_http
            module._put_presigned = saved_put
            module.STATE.workbench_token = saved_token
            module.STATE.token_exp = saved_exp
        assert calls[0][0] == module.UPLOAD_LOCK_PATH
        assert calls[-1] == (module.UPLOAD_UNLOCK_PATH, {"id": 77, "is_delete_cos": 1})
        print("upload failure cleanup: OK")

        # Start print: payload shape, No-file retry, offline and busy guards.
        fake_printer = {
            "id": 52074, "name": "Test Kobra", "device_status": 1,
            "is_printing": 1, "machine_type": 20021,
        }
        saved_select = module.select_printer_by_id
        saved_http = module._http_json
        saved_delay = module.START_PRINT_RETRY_DELAY
        saved_status_payload = module.STATE.status_payload
        saved_print_payload = module.STATE.print_payload
        saved_printer = module.STATE.printer
        module.select_printer_by_id = lambda pid: fake_printer
        module.STATE.printer = fake_printer
        module.STATE.status_payload = None
        module.STATE.print_payload = None
        module.STATE.workbench_token = "smoke-token"
        module.STATE.token_exp = int(time.time()) + 3600
        module.START_PRINT_RETRY_DELAY = 0
        bodies = []
        responses = [{"code": 0, "msg": "No file found"}, {"code": 1, "msg": "Operation successful"}]

        def fake_order(method, path, body=None, token="", timeout=20):
            assert path == module.SEND_ORDER_PATH
            bodies.append(body)
            return responses.pop(0)

        module._http_json = fake_order
        try:
            ok, message = module.start_print(4242)
            assert ok, message
        finally:
            module._http_json = saved_http
        assert len(bodies) == 2, bodies
        body = bodies[0]
        assert body["order_id"] == 1 and body["printer_id"] == 52074 and body["project_id"] == 0
        assert body["ams_info"] is None and body["settings"] is None
        assert body["data"]["filetype"] == 0 and body["data"]["file_id"] == 4242
        assert body["data"]["project_type"] == 1 and body["data"]["is_delete_file"] == 0
        assert body["data"]["file_name"] == "" and body["data"]["file_key"] == ""
        print("start print payload + retry: OK")

        fake_printer["device_status"] = 2
        ok, message = module.start_print(4242)
        assert not ok and "offline" in message.lower(), message
        fake_printer["device_status"] = 1
        fake_printer["is_printing"] = 2
        ok, message = module.start_print(4242)
        assert not ok and "in progress" in message.lower(), message
        fake_printer["is_printing"] = 1
        print("start print guards: OK")

        # An invalid path through the page must fail without touching the network.
        module._upload_in_flight = False
        module.STATE.set_upload(None)
        page.on_message({"type": "upload", "path": "/etc/hosts", "start": False})
        failed = wait_for("upload", lambda m: m.get("done") is True, timeout=5.0)
        assert failed is not None and failed.get("ok") is False, failed
        assert "watch folder" in failed.get("message", "").lower(), failed
        print("page upload validation: OK")

        module.select_printer_by_id = saved_select
        module.START_PRINT_RETRY_DELAY = saved_delay
        module.STATE.status_payload = saved_status_payload
        module.STATE.print_payload = saved_print_payload
        module.STATE.printer = saved_printer
        module.STATE.workbench_token = saved_token
        module.STATE.token_exp = saved_exp
    finally:
        module.WATCH_ROOTS = saved_roots
        module.DEFAULT_WATCH_DIR = saved_default
        module.STATE.watch_dir = saved_watch
        reset_watch()
        module.WATCH_BACKGROUND = saved_watch_background
    print("watch/upload/start print: OK")

    # The page must drive printer selection without host dialogs, and without
    # doing the network I/O on the UI thread (on_message runs there).
    fake = [{
        "id": 7, "name": "Test Kobra", "model": "Anycubic Kobra 2 Pro",
        "machine_type": 20021, "key": "deadbeef", "device_status": 1,
        "parameter": {"curr_nozzle_temp": 25, "curr_hotbed_temp": 24},
    }]
    module.get_printers = lambda: fake
    module.list_printers = lambda: [{"id": 7, "name": "Test Kobra",
                                     "model": "Anycubic Kobra 2 Pro", "online": True}]
    module.select_printer_by_id = lambda pid: fake[0] if pid == 7 else None

    def wait_for(kind, pred=None, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for msg in getattr(page, "_posted_local", []):
                if isinstance(msg, dict) and msg.get("type") == kind:
                    if pred is None or pred(msg):
                        return msg
            time.sleep(0.02)
        return None

    page.on_message({"type": "select_printer"})
    listed = wait_for("printers")
    assert listed is not None, "select_printer did not post a printer list"
    assert listed["items"] and listed["items"][0]["id"] == 7
    print("picker list: OK", listed["items"])

    page.on_message({"type": "pick_printer", "id": 7})
    picked = wait_for("printers", lambda m: m.get("closed"))
    assert picked is not None, "pick_printer did not close the picker"
    print("picker pick: OK")

    # ---- print controls: pause / resume / stop (v0.6.3) ------------------
    saved_project = module.STATE.project
    saved_project_ts = module.STATE.project_ts
    saved_project_error = module.STATE.project_error
    saved_printer_state = module.STATE.printer
    saved_token = module.STATE.workbench_token
    saved_exp = module.STATE.token_exp
    saved_get_projects = module.get_projects
    saved_http = module._http_json
    saved_control_fn = module.control_print
    saved_control_busy = module._control_in_flight

    control_printer = {
        "id": 52074, "name": "Test Kobra", "device_status": 1,
        "is_printing": 2, "machine_type": 20021,
    }
    control_project = {
        "id": 135390760, "printer_id": 52074, "gcode_name": "part.gcode",
        "print_status": 1, "pause": 0, "progress": 42, "remain_time": 600,
        "settings": '{"state":"printing","curr_layer":12,"total_layers":50}',
    }
    module.STATE.printer = control_printer
    module.STATE.workbench_token = "smoke-token"
    module.STATE.token_exp = int(time.time()) + 3600
    module.STATE.project = dict(control_project)
    module.STATE.project_ts = time.time()
    module.STATE.project_error = ""
    module._control_in_flight = False

    try:
        status = module.build_status()
        job = status["job"]
        assert job["known"] and job["active"] and not job["paused"], job
        assert job["can_pause"] and not job["can_resume"] and job["can_stop"], job
        assert job["id"] == 135390760 and job["file"] == "part.gcode", job
        assert status["printing"] is True, status["printing"]
        assert status["print"]["file"] == "part.gcode", status["print"]
        assert status["print"]["progress"] == 42, status["print"]
        assert status["print"]["state"] == "printing", status["print"]
        print("job status: OK", job)

        other_printer = dict(control_project, id=1, printer_id=7, print_status=6)
        finished = dict(control_project, id=2, print_status=2)
        found = module.find_active_project(
            [finished, other_printer, control_project], 52074)
        assert found and found["id"] == 135390760, found
        assert module.find_active_project([finished], 52074) is None
        print("active project selection: OK")

        paused_project = dict(control_project, pause=1)
        module.STATE.project = paused_project
        status = module.build_status()
        job = status["job"]
        assert job["paused"] and job["can_resume"], job
        assert not job["can_pause"] and job["can_stop"], job
        assert status["print"]["state"] == "paused", status["print"]
        module.STATE.project = dict(control_project)
        print("paused detection: OK")

        projects_holder = [dict(control_project)]
        sent = []

        def fake_projects():
            return [dict(projects_holder[0])]

        def fake_order(method, path, body=None, token="", timeout=20):
            assert method == "POST" and path == module.SEND_ORDER_PATH, (method, path)
            sent.append(body)
            return {"code": 1, "msg": "Request accepted"}

        module.get_projects = fake_projects
        module._http_json = fake_order

        before = len(sent)
        ok, message = module.control_print("stop")
        assert not ok and "confirmation" in message.lower(), message
        assert len(sent) == before, sent
        print("control confirmation guard: OK")

        ok, message = module.control_print("pause", confirmed=True)
        assert ok, message
        body = sent[-1]
        assert body == {
            "order_id": 2, "printer_id": 52074, "project_id": 135390760,
            "data": None, "ams_info": None, "settings": None,
        }, body
        assert module.STATE.project["pause"] == 1, module.STATE.project
        assert "pause requested" in message.lower(), message
        projects_holder[0] = dict(control_project, pause=1)

        ok, message = module.control_print("pause", confirmed=True)
        assert not ok and "already paused" in message.lower(), message

        ok, message = module.control_print("resume", confirmed=True)
        assert ok, message
        body = sent[-1]
        assert body["order_id"] == 3 and body["project_id"] == 135390760, body
        assert module.STATE.project["pause"] == 0, module.STATE.project
        projects_holder[0] = dict(control_project)

        ok, message = module.control_print("resume", confirmed=True)
        assert not ok and "not paused" in message.lower(), message

        ok, message = module.control_print("stop", confirmed=True)
        assert ok, message
        body = sent[-1]
        assert body["order_id"] == 4 and body["printer_id"] == 52074, body
        assert module.STATE.project is None, module.STATE.project
        print("control payloads: OK", [b["order_id"] for b in sent])

        # No active job means no control order, even if the button was stale.
        projects_holder[0] = dict(control_project, print_status=2, progress=100)
        ok, message = module.control_print("stop", confirmed=True)
        assert not ok and "no active print job" in message.lower(), message

        # Pause/resume are only meaningful while print_status == 1.
        projects_holder[0] = dict(control_project, print_status=6)
        ok, message = module.control_print("pause", confirmed=True)
        assert not ok and "only available while" in message.lower(), message
        print("control guards: OK")

        # Offline printers are rejected before any order is sent.
        module.STATE.printer = dict(control_printer, device_status=0)
        before = len(sent)
        ok, message = module.control_print("stop", confirmed=True)
        assert not ok and "offline" in message.lower(), message
        assert len(sent) == before, sent
        module.STATE.printer = dict(control_printer)
        print("control offline guard: OK")

        # The page must reject a control message that did not come from the
        # confirmation dialog, and accept one that did.
        page_calls = []

        def fake_control(action, confirmed=False):
            page_calls.append((action, confirmed))
            return True, "Stopped."

        module.control_print = fake_control
        projects_holder[0] = dict(control_project)
        module.STATE.project = dict(control_project)
        page.on_message({"type": "stop_print"})
        time.sleep(0.05)
        assert page_calls == [], page_calls
        posted_status = page._posted_local[-1]
        assert posted_status["type"] == "status", posted_status
        assert "confirmation" in posted_status["notice"].lower(), posted_status["notice"]

        page.on_message({"type": "stop_print", "confirm": True})
        deadline = time.time() + 3.0
        while time.time() < deadline and not page_calls:
            time.sleep(0.02)
        assert page_calls == [("stop", True)], page_calls
        print("page control confirmation: OK")
    finally:
        module.STATE.project = saved_project
        module.STATE.project_ts = saved_project_ts
        module.STATE.project_error = saved_project_error
        module.STATE.printer = saved_printer_state
        module.STATE.workbench_token = saved_token
        module.STATE.token_exp = saved_exp
        module.get_projects = saved_get_projects
        module._http_json = saved_http
        module.control_print = saved_control_fn
        module._control_in_flight = saved_control_busy

    # ---- camera / webcam (v0.7.0) ----------------------------------------
    # Lock the presigned WSS URL to the KVS JavaScript SDK's own fixture.
    kvs_creds = {
        "access_key": "AKIA4F7WJQR7FMMWMNXI",
        "secret_key": "FakeSecretKey",
        "session_token": "FakeSessionToken",
    }
    signed = module._aws_presign_url(
        "wss://kvs.awsamazon.com",
        {"X-Amz-TestParam": "test-param-value"},
        "us-west-2", "kinesisvideo", kvs_creds,
        now=time.strptime("20191201T000000Z", "%Y%m%dT%H%M%SZ"),
    )
    assert signed == (
        "wss://kvs.awsamazon.com/?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=AKIA4F7WJQR7FMMWMNXI%2F20191201%2Fus-west-2"
        "%2Fkinesisvideo%2Faws4_request&X-Amz-Date=20191201T000000Z"
        "&X-Amz-Expires=299&X-Amz-Security-Token=FakeSessionToken"
        "&X-Amz-Signature=fc268038be276315822b4f73eafd28ee3a5632a2"
        "a35fdb0a88db9a42b13d6c92&X-Amz-SignedHeaders=host"
        "&X-Amz-TestParam=test-param-value"
    ), signed
    print("KVS presigned URL vector: OK")

    camera_creds = module._camera_credentials({
        "region": "us-east-2", "tmpSecretId": "AKIA-TEST",
        "tmpSecretKey": "secret", "sessionToken": "session",
    })
    assert camera_creds["region"] == "us-east-2"
    assert camera_creds["access_key"] == "AKIA-TEST"
    try:
        module._camera_credentials({"region": "us-east-2"})
        raise AssertionError("missing AWS credentials should fail")
    except RuntimeError:
        pass

    auth_headers = module._aws_sigv4_headers(
        "POST",
        "https://kinesisvideo.us-east-2.amazonaws.com/describeSignalingChannel",
        b'{"ChannelName":"x"}',
        "us-east-2", "kinesisvideo", camera_creds,
        now=time.strptime("20191201T000000Z", "%Y%m%dT%H%M%SZ"),
    )
    authorization = auth_headers.get("Authorization", "")
    assert authorization.startswith(
        "AWS4-HMAC-SHA256 Credential=AKIA-TEST/20191201/us-east-2/"
        "kinesisvideo/aws4_request, SignedHeaders="), authorization
    for name in ("content-type", "host", "x-amz-date"):
        assert name in authorization, authorization
    assert len(authorization.rsplit("Signature=", 1)[-1]) == 64, authorization
    print("SigV4 headers: OK")

    # Camera open/close orders use the signed Anycubic endpoint.
    saved_http = module._http_json
    saved_token = module.STATE.workbench_token
    saved_printer = module.STATE.printer
    order_calls = []
    order_response = {"code": 1, "data": {
        "msgid": "camera-msgid",
        "token": {
            "region": "us-east-2", "tmpSecretId": "AKIA-TEST",
            "tmpSecretKey": "secret", "sessionToken": "session",
        },
    }}

    def fake_camera_order(method, path, body=None, token="", timeout=20):
        order_calls.append((method, path, body, token))
        if str(body.get("order_id")) == str(module.CAMERA_OPEN_ORDER):
            return order_response
        return {"code": 1, "data": {}}

    module._http_json = fake_camera_order
    module.STATE.workbench_token = "smoke-token"
    module.STATE.printer = {
        "id": 52074, "key": "camera-key", "machine_type": 20021,
    }
    try:
        opened, error = module.camera_open()
        assert opened and not error, (opened, error)
        assert opened["mode"] == "kvs", opened
        assert opened["credentials"]["access_key"] == "AKIA-TEST"
        # The slicer sends order_id as a string and the top-level flag;
        # without either the cloud refuses Agora with "Video service
        # upgraded. Update the slicer to enable."
        assert order_calls[-1][2] == {
            "order_id": "1001", "printer_id": 52074,
            "shengwang_rtc_support": True,
        }, order_calls[-1][2]

        # Agora printers return data.shengwang instead of AWS credentials.
        order_response = {"code": 1, "data": {
            "msgid": "agora-msgid",
            "shengwang": {
                "appid": "agora-app",
                "channel": "agora-channel",
                "rtc_token": "agora-token",
                "client_uid": 4242,
                "encryption_mode": "AES_256_GCM2",
                "encryption_key": "0123456789abcdef0123456789abcdef",
                "encryption_kdf_salt": "c2FsdHNhbHRzYWx0c2FsdHNhbHRzYWx0c2FsdHNhbHQ=",
                "event_id": "event-1",
            },
            "shengwang_device": {"uid": 9001},
        }}
        opened, error = module.camera_open()
        assert opened and not error, (opened, error)
        assert opened["mode"] == "agora", opened
        assert opened["credentials"].client_uid == 4242
        assert opened["credentials"].device_uid == 9001
        assert opened["credentials"].web_encryption_mode == "aes-256-gcm2"
        assert opened["credentials"].is_encrypted

        # A camera already held by another Anycubic session answers
        # "Operation successful" with no credential block at all.
        order_response = {
            "code": 1, "msg": "Operation successful",
            "data": {"msgid": "empty"},
        }
        opened, error = module.camera_open()
        assert not opened and "no camera credentials" in error.lower(), error

        order_response = {
            "code": 1,
            "msg": "Video service upgraded. Update the slicer to enable.",
            "data": None,
        }
        opened, error = module.camera_open()
        assert not opened and "video service upgraded" in error.lower(), error

        ok, message = module.camera_close()
        assert ok, message
        assert order_calls[-1][2] == {
            "order_id": module.CAMERA_CLOSE_ORDER, "printer_id": 52074}
    finally:
        module._http_json = saved_http
        module.STATE.workbench_token = saved_token
        module.STATE.printer = saved_printer
    print("camera orders: OK", [call[2]["order_id"] for call in order_calls])

    # ---- Agora ("shengwang") camera signaling ----------------------------
    # A browser offer with the H.264/RTX pair, feedback and one host
    # candidate, mirroring what Chromium generates for the dashboard.
    agora_offer = "\r\n".join([
        "v=0",
        "o=- 4611731400430051336 2 IN IP4 127.0.0.1",
        "s=-",
        "t=0 0",
        "a=group:BUNDLE 0",
        "a=extmap-allow-mixed",
        "a=msid-semantic: WMS",
        "m=video 9 UDP/TLS/RTP/SAVPF 96 97",
        "c=IN IP4 0.0.0.0",
        "a=rtcp:9 IN IP4 0.0.0.0",
        "a=ice-ufrag:F7gI",
        "a=ice-pwd:x+xNhQ8Q9k1Cp0kFqPXpMlSw",
        "a=ice-options:trickle",
        "a=fingerprint:sha-256 D2:FA:0E:C3:22:59:5E:14:95:69:92:3D:13:B4:"
        "84:24:2C:C2:A2:C0:3E:FD:34:8E:5F:C2:BE:B9:36:97:B0:B4",
        "a=setup:actpass",
        "a=mid:0",
        "a=extmap:1 urn:ietf:params:rtp-hdrext:toffset",
        "a=extmap:3 urn:3gpp:video-orientation",
        "a=recvonly",
        "a=rtcp-mux",
        "a=rtcp-rsize",
        "a=rtpmap:96 H264/90000",
        "a=rtcp-fb:96 nack",
        "a=rtcp-fb:96 nack pli",
        "a=rtcp-fb:96 goog-remb",
        "a=fmtp:96 level-asymmetry-allowed=1;packetization-mode=1;"
        "profile-level-id=42e01f",
        "a=rtpmap:97 rtx/90000",
        "a=fmtp:97 apt=96",
        "a=candidate:1 1 udp 2130706431 192.168.1.10 54321 typ host "
        "generation 0",
        "",
    ])

    parsed_offer = module._sdp_parse_offer(agora_offer)
    assert parsed_offer["ice_ufrag"] == "F7gI", parsed_offer
    assert parsed_offer["bundle_mids"] == "0", parsed_offer
    assert parsed_offer["extmap_allow_mixed"] is True
    assert [item["extensionName"]
            for item in parsed_offer["video_extensions"]] == [
        "urn:ietf:params:rtp-hdrext:toffset",
        "urn:3gpp:video-orientation",
    ], parsed_offer["video_extensions"]

    offer_ortc = module._sdp_offer_to_ortc(parsed_offer["parsed"])
    assert offer_ortc["iceParameters"]["candidates"] == [{
        "foundation": "1", "protocol": "udp", "priority": 2130706431,
        "ip": "192.168.1.10", "port": 54321, "type": "host",
    }], offer_ortc["iceParameters"]
    assert offer_ortc["dtlsParameters"]["role"] == "client"
    recv_codecs = offer_ortc["rtpCapabilities"]["recv"]["videoCodecs"]
    assert {item["payloadType"] for item in recv_codecs} == {96, 97}
    assert not offer_ortc["rtpCapabilities"]["send"]["videoCodecs"]

    gateway_fingerprint = (
        "35:28:40:20:80:F6:E0:11:E4:40:42:E8:7C:55:D4:34:18:F4:35:04:"
        "F3:3F:1A:EB:66:7A:13:85:6A:33:D2:6B")
    gateway_ortc = {
        "iceParameters": {
            "iceUfrag": "agoraUF",
            "icePwd": "agoraPWDagoraPWDagoraPWD",
            "candidates": [{
                "foundation": "1", "protocol": "udp",
                "priority": 2130706431, "ip": "98.98.143.194",
                "port": 4701, "type": "host",
            }],
        },
        "dtlsParameters": {
            "fingerprints": [{
                "hashFunction": "sha-256",
                "fingerprint": gateway_fingerprint,
            }],
            "role": "server",
        },
        "rtpCapabilities": {
            "sendrecv": {
                "videoCodecs": [{
                    "payloadType": 96,
                    "rtpMap": {
                        "encodingName": "H264", "clockRate": 90000,
                        "encodingParameters": None,
                    },
                    "rtcpFeedbacks": [
                        {"type": "nack", "parameter": None},
                        {"type": "nack", "parameter": "pli"},
                    ],
                    "fmtp": {"parameters": {
                        "packetization-mode": "1",
                        "profile-level-id": "42e01f",
                    }},
                }, {
                    "payloadType": 97,
                    "rtpMap": {
                        "encodingName": "rtx", "clockRate": 90000,
                        "encodingParameters": None,
                    },
                    "rtcpFeedbacks": [],
                    "fmtp": {"parameters": {"apt": "96"}},
                }],
                "videoExtensions": [
                    {"entry": 1,
                     "extensionName": "urn:ietf:params:rtp-hdrext:toffset"},
                    {"entry": 9, "extensionName": "urn:not:offered"},
                ],
                "audioCodecs": [],
                "audioExtensions": [],
            },
        },
    }
    answer = module._sdp_build_answer(
        gateway_ortc, parsed_offer,
        {"uid": 9001, "ssrc": 1234, "rtx_ssrc": 1235, "cname": "printer"},
    )
    for token in (
        "a=ice-lite", "a=setup:active",
        "a=fingerprint:sha-256 " + gateway_fingerprint,
        "a=candidate:1 1 udp 2130706431 98.98.143.194 4701 typ host",
        "a=mid:0", "a=sendonly", "a=rtpmap:96 H264/90000",
        "a=rtcp-fb:96 nack pli", "a=ssrc-group:FID 1234 1235",
        "a=extmap:1 urn:ietf:params:rtp-hdrext:toffset",
    ):
        assert token in answer, (token, answer)
    assert "urn:not:offered" not in answer, answer
    print("Agora SDP/ORTC answer: OK")

    # Access-point parsing: code 0 blocks, positional DTLS fingerprints.
    ap_payload = {
        "opid": 123, "enter_ts": 456,
        "detail": {"502": "107.155.14.132"},
        "response_body": [{
            "uri": 23,
            "buffer": {
                "code": 0, "cid": 9, "cname": "agora-channel",
                "flag": 4096, "uid": 4242, "cert": "edge-ticket",
                "detail": {"19": "sha-256 AA:BB;sha-256 CC:DD"},
                "edges_services": [
                    {"ip": "1.2.3.4", "port": 4701},
                    {"ip": "5.6.7.8", "port": 4702},
                ],
            },
        }],
    }
    ap_block = module._agora_parse_ap(ap_payload)
    assert ap_block["flag"] == 4096 and ap_block["ticket"] == "edge-ticket"
    assert ap_block["addresses"][0] == {
        "ip": "1.2.3.4", "port": 4701, "ticket": "edge-ticket",
        "fingerprint": "sha-256 AA:BB",
    }, ap_block["addresses"][0]
    assert ap_block["addresses"][1]["fingerprint"] == "sha-256 CC:DD"
    try:
        module._agora_parse_ap({"response_body": [
            {"buffer": {"code": 2010005}}]})
        raise AssertionError("a rejected AP block must raise")
    except RuntimeError as exc:
        assert "2010005" in str(exc), exc

    agora_creds = module._agora_credentials({
        "msgid": "agora-msgid",
        "shengwang": {
            "appid": "app", "channel": "agora-channel",
            "rtc_token": "rtc-token", "client_uid": 4242,
            "encryption_mode": "AES_256_GCM2",
            "encryption_key": "0123456789abcdef0123456789abcdef",
            "encryption_kdf_salt":
                "c2FsdHNhbHRzYWx0c2FsdHNhbHRzYWx0c2FsdHNhbHQ=",
            "event_id": "event-1",
        },
        "shengwang_device": {"uid": 9001},
    }, 52074)
    assert agora_creds.appid == "app"
    assert agora_creds.client_uid == 4242
    assert agora_creds.device_uid == 9001
    assert agora_creds.web_encryption_mode == "aes-256-gcm2"
    assert agora_creds.is_encrypted
    assert module._agora_credentials({"token": {}}) is None
    assert module._agora_wrap_key(
        "0123456789abcdef0123456789abcdef")
    assert len(base64.b64decode(module._agora_wrap_key(
        "0123456789abcdef0123456789abcdef"))) == 128
    try:
        module._agora_validate_salt("not base64")
        raise AssertionError("a bad KDF salt must be rejected")
    except RuntimeError:
        pass

    join = module._agora_join_message(
        agora_creds, ap_block, ap_block["addresses"][0],
        offer_ortc, "session-id",
    )
    assert join["_type"] == "join_v3", join
    joined = join["_message"]
    assert joined["channel_name"] == "agora-channel"
    assert joined["channel_key"] == "rtc-token"
    assert joined["app_id"] == "app"
    assert joined["ortc"]["iceParameters"]["candidates"]
    assert joined["ap_response"]["ticket"] == "edge-ticket"
    assert joined["aes_mode"] == "aes-256-gcm2"
    assert joined["aes_encrypt"] is True
    assert len(base64.b64decode(joined["aes_secret"])) == 128
    assert joined["aes_salt"] == agora_creds.encryption_kdf_salt
    print("Agora AP/join message: OK")

    # Gateway events: announce -> subscribe, crypt error -> fail + close.
    class _FakeAgoraPage:
        def __init__(self):
            self.posted = []

        def post_message(self, payload):
            self.posted.append(payload)

    fake_agora_page = _FakeAgoraPage()
    saved_generation = module._webcam_generation
    saved_camera_close = module.camera_close
    agora_closed = []
    module._webcam_generation = 7
    module.camera_close = lambda pid=None, timeout=20: (
        agora_closed.append(pid) or (True, "closed"))
    try:
        agora_session = module.AgoraSession(
            fake_agora_page, agora_creds, agora_offer, 7)
        agora_session._handle_event({
            "_type": "on_add_video_stream",
            "_message": {
                "uid": 9001, "ssrcId": 44, "rtxSsrcId": 45,
                "video": True, "cname": "printer",
            },
        })
        assert agora_session.streams[(9001, 44)]["ssrc"] == 44
        sent = [item for item in fake_agora_page.posted
                if item.get("type") == "webcam_agora_send"]
        assert sent, fake_agora_page.posted
        subscribe = json.loads(sent[0]["message"])
        assert subscribe["_type"] == "subscribe", subscribe
        assert subscribe["_message"]["stream_id"] == 9001, subscribe
        assert subscribe["_message"]["ssrcId"] == 44, subscribe

        agora_session._handle_event({
            "_type": "on_remove_video_stream",
            "_message": {"uid": 9001},
        })
        assert not agora_session.streams
        agora_session._handle_event({
            "_type": "on_crypt_error",
            "_message": {"code": 2028},
        })
        errors = [item for item in fake_agora_page.posted
                  if item.get("type") == "webcam"
                  and item.get("state") == "error"]
        assert errors, fake_agora_page.posted
        assert "encryption" in errors[-1]["error"].lower()
        assert agora_closed == [52074], agora_closed
        assert agora_session.closed

        # A superseded worker must not talk to the dashboard.
        stale_page = _FakeAgoraPage()
        stale = module.AgoraSession(
            stale_page, agora_creds, agora_offer, 6)
        stale._post({"type": "webcam", "state": "error"})
        assert not stale_page.posted
    finally:
        module._webcam_generation = saved_generation
        module.camera_close = saved_camera_close
    print("Agora gateway events: OK")

    # Full Agora orchestration without a network: the page relays a join
    # reply and a stream announcement, and the worker must answer with SDP.
    class _ScriptedAgoraPage:
        def __init__(self):
            self.posted = []
            self.session = None

        def post_message(self, payload):
            self.posted.append(payload)
            if payload.get("type") != "webcam_agora_join":
                return
            join = json.loads(payload["join"])
            self.session.feed(json.dumps({
                "_id": join["_id"],
                "_result": "success",
                "_message": {
                    "ortc": gateway_ortc,
                    "streams": [{
                        "uid": 9001, "ssrcId": 44, "rtxSsrcId": 45,
                        "video": True, "cname": "printer",
                    }],
                },
            }))

    scripted_page = _ScriptedAgoraPage()
    saved_choose = module._agora_choose_server
    saved_generation = module._webcam_generation
    module._agora_choose_server = lambda creds: ap_block
    module._webcam_generation = 7
    agora_worker = module.AgoraSession(
        scripted_page, agora_creds, agora_offer, 7)
    scripted_page.session = agora_worker
    try:
        agora_worker.start()
        answer_message = None
        deadline = time.time() + 8.0
        while time.time() < deadline:
            for item in scripted_page.posted:
                if item.get("type") == "webcam_agora_answer":
                    answer_message = item
                    break
            if answer_message:
                break
            time.sleep(0.02)
        assert answer_message is not None, scripted_page.posted
        assert answer_message["session"] == 7, answer_message
        for token in (
            "a=ice-lite", "a=setup:active", "a=ssrc:44",
            "a=ssrc-group:FID 44 45", "a=mid:0",
        ):
            assert token in answer_message["sdp"], (
                token, answer_message["sdp"])
        stream_messages = [
            json.loads(item["message"])
            for item in scripted_page.posted
            if item.get("type") == "webcam_agora_send"
        ]
        kinds = [item["_type"] for item in stream_messages]
        assert "set_client_role" in kinds, kinds
        assert "subscribe" in kinds, kinds
        subscribe = next(item for item in stream_messages
                         if item["_type"] == "subscribe")
        assert subscribe["_message"]["stream_id"] == 9001, subscribe
        assert subscribe["_message"]["ssrcId"] == 44, subscribe
        assert agora_worker.answer_sent is True
    finally:
        agora_worker.stop()
        agora_worker.join(timeout=3.0)
        module._agora_choose_server = saved_choose
        module._webcam_generation = saved_generation
    print("Agora worker orchestration: OK")

    # MQTT video publish: exact topic and payload shape.
    class _FakeInfo:
        rc = 0

    class _FakeMqtt:
        def __init__(self):
            self.calls = []

        def publish(self, topic, payload, qos=1):
            self.calls.append((topic, payload, qos))
            return _FakeInfo()

    fake_mqtt = _FakeMqtt()
    mqtt_service = module.MqttService()
    mqtt_service.running = True
    mqtt_service.client = fake_mqtt
    saved_connected = module.STATE.mqtt_connected
    saved_printer = module.STATE.printer
    module.STATE.mqtt_connected = True
    module.STATE.printer = {
        "id": 52074, "key": "camera-key", "machine_type": 20021,
    }
    try:
        ok, start_msgid = mqtt_service.publish_video("startCapture")
        assert ok, start_msgid
        ok, stop_msgid = mqtt_service.publish_video("stopCapture")
        assert ok, stop_msgid
        ok, message = mqtt_service.publish_video("reboot")
        assert not ok and "unknown camera action" in message.lower(), message
        module.STATE.mqtt_connected = False
        ok, message = mqtt_service.publish_video("startCapture")
        assert not ok and "not connected" in message.lower(), message
    finally:
        module.STATE.mqtt_connected = saved_connected
        module.STATE.printer = saved_printer
    topic, payload, qos = fake_mqtt.calls[0]
    assert topic == (
        "anycubic/anycubicCloud/v1/web/printer/20021/camera-key/video"), topic
    decoded = json.loads(payload)
    assert decoded["type"] == "video" and decoded["action"] == "startCapture"
    assert decoded["msgid"] == start_msgid and decoded["data"] is None
    assert decoded["timestamp"] > 0 and qos == 1
    assert json.loads(fake_mqtt.calls[1][1])["action"] == "stopCapture"
    print("MQTT video publish: OK", topic)

    # Full webcam_start/stop with a fake camera and MQTT service.
    class _FakeCameraService:
        running = True

        def __init__(self):
            self.published = []

        def publish_video(self, action):
            self.published.append(action)
            return True, "mqtt-" + action

    cam_service = _FakeCameraService()
    saved_service = module.STATE.mqtt_service
    saved_connected = module.STATE.mqtt_connected
    saved_printer = module.STATE.printer
    saved_camera_state = module.STATE.camera_state
    saved_camera_error = module.STATE.camera_error
    saved_camera_started = module.STATE.camera_started
    saved_open = module.camera_open
    saved_close = module.camera_close
    saved_arn = module._kvs_channel_arn
    saved_endpoint = module._kvs_wss_endpoint
    saved_session = module._webcam_session
    saved_in_flight = module._webcam_in_flight
    saved_generation = module._webcam_generation
    module.STATE.mqtt_service = cam_service
    module.STATE.mqtt_connected = True
    module.STATE.printer = {
        "id": 52074, "name": "Test Kobra", "key": "camera-key",
        "machine_type": 20021, "device_status": 1,
    }
    module.STATE.workbench_token = "smoke-token"
    module.STATE.token_exp = int(time.time()) + 3600
    module.STATE.camera_state = "idle"
    module.STATE.camera_error = ""
    module.STATE.camera_started = 0.0
    closed = []
    module.camera_open = lambda pid=None: ({
        "printer_id": 52074, "msgid": "open-msgid",
        "credentials": {
            "region": "us-east-2", "access_key": "AKIA-TEST",
            "secret_key": "secret", "session_token": "session",
        },
    }, "")
    module.camera_close = lambda pid=None, timeout=20: (
        closed.append(pid) or (True, "closed"))
    module._kvs_channel_arn = (
        lambda name, creds:
        "arn:aws:kinesisvideo:us-east-2:1:channel/camera-key/1")
    module._kvs_wss_endpoint = (
        lambda arn, creds:
        "wss://v-test.kinesisvideo.us-east-2.amazonaws.com")
    module._webcam_session = None
    module._webcam_in_flight = False
    module._webcam_generation = 0
    try:
        ok, message, config = module.webcam_start()
        assert ok, message
        assert module.STATE.camera_state == "connecting", module.STATE.camera_state
        assert cam_service.published == ["startCapture"], cam_service.published
        assert config["ice_servers"], config
        assert "X-Amz-ChannelARN=" in config["wss_url"], config["wss_url"]
        assert "X-Amz-Signature=" in config["wss_url"], config["wss_url"]
        assert "secret" not in config["wss_url"], config["wss_url"]
        status = module.build_status()
        assert status["camera"]["state"] == "connecting", status["camera"]
        module.webcam_mark_viewing()
        assert module.STATE.camera_state == "live", module.STATE.camera_state
        ok, message = module.webcam_stop()
        assert ok, message
        assert cam_service.published == ["startCapture", "stopCapture"], \
            cam_service.published
        assert module.STATE.camera_state == "idle", module.STATE.camera_state
        assert module._webcam_session is None
        assert closed, "camera_close was not called"
        print("webcam start/stop: OK")
    finally:
        module.STATE.mqtt_service = saved_service
        module.STATE.mqtt_connected = saved_connected
        module.STATE.printer = saved_printer
        module.STATE.camera_state = saved_camera_state
        module.STATE.camera_error = saved_camera_error
        module.STATE.camera_started = saved_camera_started
        module.camera_open = saved_open
        module.camera_close = saved_close
        module._kvs_channel_arn = saved_arn
        module._kvs_wss_endpoint = saved_endpoint
        module._webcam_session = saved_session
        module._webcam_in_flight = saved_in_flight
        module._webcam_generation = saved_generation

    # Agora start/stop: the order itself opens the stream, so no MQTT video
    # command and no KVS control-plane lookup may happen.
    agora_service = _FakeCameraService()
    saved_service = module.STATE.mqtt_service
    saved_connected = module.STATE.mqtt_connected
    saved_printer = module.STATE.printer
    saved_camera_state = module.STATE.camera_state
    saved_camera_error = module.STATE.camera_error
    saved_camera_started = module.STATE.camera_started
    saved_open = module.camera_open
    saved_close = module.camera_close
    saved_arn = module._kvs_channel_arn
    saved_endpoint = module._kvs_wss_endpoint
    saved_session = module._webcam_session
    saved_in_flight = module._webcam_in_flight
    saved_generation = module._webcam_generation
    module.STATE.mqtt_service = agora_service
    module.STATE.mqtt_connected = False
    module.STATE.printer = {
        "id": 52074, "name": "Test Kobra", "key": "camera-key",
        "machine_type": 20021, "device_status": 1,
    }
    module.STATE.workbench_token = "smoke-token"
    module.STATE.token_exp = int(time.time()) + 3600
    module.STATE.camera_state = "idle"
    module.STATE.camera_error = ""
    module.STATE.camera_started = 0.0
    agora_closed = []
    agora_page = object()
    module.camera_open = lambda pid=None: ({
        "printer_id": 52074, "msgid": "agora-msgid", "mode": "agora",
        "credentials": agora_creds,
    }, "")
    module.camera_close = lambda pid=None, timeout=20: (
        agora_closed.append(pid) or (True, "closed"))

    def kvs_must_not_run(*args, **kwargs):
        raise AssertionError("KVS must not be used for an Agora camera")

    module._kvs_channel_arn = kvs_must_not_run
    module._kvs_wss_endpoint = kvs_must_not_run
    module._webcam_session = None
    module._webcam_in_flight = False
    module._webcam_generation = 0
    try:
        ok, message, config = module.webcam_start(agora_page)
        assert ok, message
        assert config["mode"] == "agora", config
        assert config["first_frame_timeout"] == 45000, config
        assert module.STATE.camera_state == "connecting", \
            module.STATE.camera_state
        assert agora_service.published == [], agora_service.published
        assert module._webcam_session["mode"] == "agora"
        assert module._webcam_session["page"] is agora_page
        assert module._webcam_session["credentials"] is agora_creds

        ok, message = module.webcam_stop()
        assert ok, message
        assert agora_service.published == [], agora_service.published
        assert agora_closed == [52074], agora_closed
        assert module.STATE.camera_state == "idle", module.STATE.camera_state
        assert module._webcam_session is None
        print("Agora webcam start/stop: OK")
    finally:
        module.STATE.mqtt_service = saved_service
        module.STATE.mqtt_connected = saved_connected
        module.STATE.printer = saved_printer
        module.STATE.camera_state = saved_camera_state
        module.STATE.camera_error = saved_camera_error
        module.STATE.camera_started = saved_camera_started
        module.camera_open = saved_open
        module.camera_close = saved_close
        module._kvs_channel_arn = saved_arn
        module._kvs_wss_endpoint = saved_endpoint
        module._webcam_session = saved_session
        module._webcam_in_flight = saved_in_flight
        module._webcam_generation = saved_generation

    # Without monitoring, the camera refuses before any order is sent. With
    # monitoring but no MQTT socket, Agora still starts (no MQTT command is
    # involved), while a legacy KVS camera refuses.
    saved_service = module.STATE.mqtt_service
    saved_camera_state = module.STATE.camera_state
    saved_session = module._webcam_session
    saved_in_flight = module._webcam_in_flight
    saved_generation = module._webcam_generation
    saved_guard_printer = module.STATE.printer
    saved_guard_token = module.STATE.workbench_token
    saved_guard_exp = module.STATE.token_exp
    saved_guard_connected = module.STATE.mqtt_connected
    saved_guard_open = module.camera_open
    saved_guard_close = module.camera_close
    guard_mode = {"mode": "agora"}
    guard_closed = []
    module.camera_open = lambda pid=None: ({
        "printer_id": 52074, "msgid": "guard-msgid",
        "mode": guard_mode["mode"],
        "credentials": (
            object() if guard_mode["mode"] == "agora" else {
                "region": "us-east-2", "access_key": "AKIA-TEST",
                "secret_key": "secret", "session_token": "session",
            }),
    }, "")
    module.camera_close = lambda pid=None, timeout=20: (
        guard_closed.append(pid) or (True, "closed"))
    module.STATE.mqtt_service = type("S", (), {"running": True})()
    module.STATE.mqtt_connected = False
    module.STATE.printer = {
        "id": 52074, "key": "camera-key", "machine_type": 20021,
        "device_status": 1,
    }
    module.STATE.workbench_token = "smoke-token"
    module.STATE.token_exp = int(time.time()) + 3600
    module.STATE.camera_state = "idle"
    module._webcam_session = None
    module._webcam_in_flight = False
    module._webcam_generation = 0
    try:
        ok, message, config = module.webcam_start()
        assert ok and config["mode"] == "agora", (ok, message, config)
        assert module.STATE.camera_state == "connecting", \
            module.STATE.camera_state
        module.webcam_stop()

        guard_mode["mode"] = "kvs"
        ok, message, config = module.webcam_start()
        assert not ok and config is None, (ok, message, config)
        assert "not connected" in message.lower(), message
        assert module.STATE.camera_state == "error", module.STATE.camera_state

        module.STATE.mqtt_service = None
        ok, message, config = module.webcam_start()
        assert not ok and config is None, (ok, message, config)
        assert "monitoring" in message.lower(), message
        assert module.STATE.camera_state == "error", module.STATE.camera_state
    finally:
        module.STATE.mqtt_service = saved_service
        module.STATE.printer = saved_guard_printer
        module.STATE.workbench_token = saved_guard_token
        module.STATE.token_exp = saved_guard_exp
        module.STATE.mqtt_connected = saved_guard_connected
        module.STATE.camera_state = saved_camera_state
        module.camera_open = saved_guard_open
        module.camera_close = saved_guard_close
        module._webcam_session = saved_session
        module._webcam_in_flight = saved_in_flight
        module._webcam_generation = saved_generation
    print("webcam monitoring guard: OK")

    # Dashboard wiring: start/ready/stop/viewing messages and DOM ids.
    cam_page = module.AnycubicCloudPage()
    saved_start_fn = module.webcam_start
    saved_stop_fn = module.webcam_stop
    saved_page_camera_state = module.STATE.camera_state

    def cam_wait(kind, pred=None, timeout=3.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for msg in getattr(cam_page, "_posted_local", []):
                if (isinstance(msg, dict) and msg.get("type") == kind
                        and (pred is None or pred(msg))):
                    return msg
            time.sleep(0.02)
        return None

    module.webcam_start = lambda page=None: (
        True, "opened",
        {
            "mode": "kvs",
            "wss_url": "wss://example.invalid/?X-Amz-Signature=x",
            "ice_servers": [{"urls": "stun:stun.example:3478"}],
            "expires_in": 299,
        },
    )
    stopped = []
    module.webcam_stop = lambda: (stopped.append(True) or (True, "stopped"))
    saved_offer_fn = module.webcam_agora_offer
    saved_feed_fn = module.webcam_agora_feed
    try:
        cam_page.on_message({"type": "webcam_start"})
        ready = cam_wait("webcam", lambda m: m.get("state") == "ready",
                         timeout=5.0)
        assert ready is not None, "webcam_start did not post a viewer config"
        assert ready["mode"] == "kvs", ready
        assert ready["wss_url"].startswith("wss://example.invalid"), ready
        assert ready["ice_servers"], ready

        # Agora only sends the mode; the page creates the offer.
        module.webcam_start = lambda page=None: (
            True, "opened",
            {"mode": "agora", "first_frame_timeout": 45000},
        )
        cam_page._posted_local = []
        cam_page.on_message({"type": "webcam_start"})
        ready = cam_wait("webcam", lambda m: m.get("state") == "ready",
                         timeout=5.0)
        assert ready is not None, "Agora start did not post a viewer config"
        assert ready["mode"] == "agora", ready
        assert "wss_url" not in ready, ready

        offers = []
        frames = []
        module.webcam_agora_offer = lambda sdp: (
            offers.append(sdp) or (True, "started"))
        module.webcam_agora_feed = lambda data: frames.append(data)
        cam_page.on_message({"type": "webcam_agora_offer", "sdp": "v=0"})
        cam_page.on_message({"type": "webcam_agora_frame", "data": "{}"})
        assert offers == ["v=0"], offers
        assert frames == ["{}"], frames

        module.STATE.camera_state = "connecting"
        cam_page.on_message({"type": "webcam_viewing"})
        time.sleep(0.05)
        assert module.STATE.camera_state == "live", module.STATE.camera_state

        cam_page.on_message({"type": "webcam_stop"})
        deadline = time.time() + 3.0
        while time.time() < deadline and not stopped:
            time.sleep(0.02)
        assert stopped, "webcam_stop page message did not stop the camera"
    finally:
        module.webcam_start = saved_start_fn
        module.webcam_stop = saved_stop_fn
        module.webcam_agora_offer = saved_offer_fn
        module.webcam_agora_feed = saved_feed_fn
        module.STATE.camera_state = saved_page_camera_state

    ui = cam_page.get_ui()
    for element_id in (
        "camVideo", "camPlaceholder", "camBadge", "camStats", "camError",
        "btnCamStart", "btnCamReconnect", "btnCamStop",
    ):
        assert 'id="%s"' % element_id in ui, \
            "missing camera element %s" % element_id
    for token in (
        "RTCPeerConnection", "setCodecPreferences", "video/h264",
        "SDP_OFFER", "ICE_CANDIDATE", "ice_servers",
        "camConnectAgora", "webcam_agora_offer", "webcam_agora_frame",
        "webcam_agora_join", "webcam_agora_answer", "webcam_agora_send",
        "_type: \"ping\"",
    ):
        assert token in ui, "missing camera JavaScript token %s" % token
    assert module.KVS_STUN_SERVERS
    assert module.KVS_STUN_SERVERS[0]["urls"].startswith("stun:")
    print("dashboard camera wiring: OK")

    # JWT extraction accepts raw tokens, URLs and id_token=... fragments.
    jwt = ("eyJhbGciOiJSUzI1NiJ9."
           "eyJlbWFpbCI6ImFAYi5jIn0."
           "c2lnbmF0dXJl")
    assert module._extract_access_token(jwt) == jwt
    assert module._extract_access_token("id_token=" + jwt) == jwt
    assert module._extract_access_token(
        '"https://login.example/cb?access_token=%s&state=1"' % jwt) == jwt
    assert module._extract_access_token("  %s\n" % jwt) == jwt
    assert module._extract_access_token("not a token") == "not a token"
    print("token extraction: OK")

    # login_with_rs256 must not raise NameError; exchange is faked.
    saved_http = module._http_json
    saved_auth = (module.STATE.workbench_token, module.STATE.token_exp, module.STATE.email)

    def fake_login_http(method, path, body):
        assert path == module.LOGIN_PATH, path
        assert body.get("access_token") == jwt, body
        return {"code": 1, "data": {
            "token": "workbench-token",
            "user": {"user_email": "detected@example.com"},
        }}

    module._http_json = fake_login_http
    try:
        ok, message = module.login_with_rs256("id_token=" + jwt)
        assert ok, message
        assert module.STATE.workbench_token == "workbench-token"
        assert module.STATE.email == "detected@example.com"
        assert module.STATE.is_logged_in()
    finally:
        module._http_json = saved_http
        (module.STATE.workbench_token,
         module.STATE.token_exp,
         module.STATE.email) = saved_auth
        module.STATE.save()
    print("login exchange: OK")

    # state.json rewritten by another process is picked up without a restart.
    module.STATE.workbench_token = ""
    module.STATE.email = ""
    module.STATE.save()
    assert not module.STATE.is_logged_in()
    with open(module.STATE_PATH, "r", encoding="utf-8") as fh:
        disk_state = json.load(fh)
    disk_state["workbench_token"] = "external-token"
    disk_state["email"] = "external@example.com"
    disk_state["token_exp"] = int(time.time()) + 3600
    with open(module.STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(disk_state, fh)
    assert module.STATE.reload_if_changed() is True
    assert module.STATE.workbench_token == "external-token"
    assert module.STATE.email == "external@example.com"
    assert module.STATE.is_logged_in()
    assert module.STATE.reload_if_changed() is False
    print("external state reload: OK")

    # The login script must open a host window, not call the non-existent show_dialog().
    login = instances[1]
    saved_token = module.STATE.workbench_token
    module.STATE.workbench_token = ""      # force the signed-out path
    try:
        result = login.execute()
        assert orca_stub.ui.windows, "login script did not open a window"
        window = orca_stub.ui.windows[-1]
        window.submit({"token": ""})
        assert window.closed, "login window did not close on submit"
        print("login window: OK, result:", result.status)
    finally:
        module.STATE.workbench_token = saved_token

    # Simulate a Config-tab save: token stored, plugin picks it up on poll.
    token = os.environ.get("ANYCUBIC_RS256", "").strip()
    if token:
        page.save_config(json.dumps({"rs256_token": token, "email": ""}))
        ok, message = module.login_with_rs256(token)
        print("live login:", ok, "-", message)
        assert ok, message
        assert module.STATE.is_logged_in()
        assert module.STATE.email

        printers = module.get_printers()
        print("printers:", [(p.get("name"), p.get("machine_type"), p.get("key")) for p in printers])
        assert printers, "no printers returned"

        client_id, username, password = module.MqttService()._credentials(
            module.STATE.workbench_token, module.STATE.email)
        assert client_id.startswith("pcf_") and len(client_id) == 36
        assert username.startswith("user|pcf|%s|" % module.STATE.email)
        assert len(password) > 100
        print("mqtt credentials: OK", client_id)

        module.select_printer(printers[0])
        assert module.STATE.printer
        print("selected printer:", module.STATE.printer.get("name"))
    else:
        print("live login: skipped (set ANYCUBIC_RS256 to test the network path)")

    print("ALL CHECKS PASSED")


if __name__ == "__main__":
    sys.path.insert(0, HERE)
    main()
