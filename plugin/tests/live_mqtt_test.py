"""Live end-to-end test: login, printers, MQTT subscribe (read-only).

Run with the test venv:
    ANYCUBIC_RS256=$(cat token.txt) ./venv/bin/python tests/live_mqtt_test.py

The MQTT part only subscribes; it sends nothing to the printer.
"""

import importlib.util
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_PATH = os.path.join(os.path.dirname(HERE), "plugin.py")

sys.path.insert(0, HERE)
import orca_stub  # noqa: E402

sys.modules["orca"] = orca_stub


def load_plugin():
    spec = importlib.util.spec_from_file_location("anycubic_plugin_live", PLUGIN_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    token = os.environ.get("ANYCUBIC_RS256", "").strip()
    if not token:
        print("ANYCUBIC_RS256 is not set")
        return 1

    module = load_plugin()
    ok, message = module.login_with_rs256(token)
    print("login:", ok, "-", message)
    if not ok:
        return 1

    printers = module.get_printers()
    print("printers:", [(p.get("name"), p.get("machine_type"), p.get("id")) for p in printers])
    if not printers:
        return 1

    module.select_printer(printers[0])
    print("selected:", module.STATE.printer.get("name"), module.STATE.printer.get("key"))

    ok, message = module.start_monitoring()
    print("monitor:", ok, "-", message)

    deadline = time.time() + 20
    while time.time() < deadline and not module.STATE.mqtt_connected:
        time.sleep(0.5)

    print("connected:", module.STATE.mqtt_connected, "error:", module.STATE.mqtt_error or "-")
    if not module.STATE.mqtt_connected:
        module.stop_monitoring()
        return 1

    # Listen passively for live reports. The printer only publishes on change
    # or on a slow heartbeat, so an idle printer can be quiet for a while.
    listen = int(os.environ.get("LISTEN_SECONDS", "45"))
    print("listening %ss..." % listen)
    deadline = time.time() + listen
    while time.time() < deadline:
        time.sleep(1)
        if module.STATE.recent_messages:
            break

    status = module.STATE.status_payload
    print("status payload keys:", list(status.keys()) if isinstance(status, dict) else type(status).__name__)
    print("print payload present:", module.STATE.print_payload is not None)
    print("recent topics:")
    for entry in module.STATE.recent_messages:
        print("  %6.1f  %4d B  %s" % (entry["ts"], entry["bytes"], entry["topic"]))

    snapshot = module.build_status()
    print("dashboard snapshot:")
    print(json.dumps({k: snapshot[k] for k in ("login", "printer", "mqtt", "temps", "print")}, indent=2))

    module.stop_monitoring()
    print("stopped. ALL LIVE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
