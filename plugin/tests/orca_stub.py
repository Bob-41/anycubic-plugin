"""Minimal stand-in for OrcaSlicer's embedded `orca` module.

Used only to smoke-test the plugin outside the app: import, registration,
capability instantiation and the pure-Python logic paths.
"""

import json
import types
import sys


class PythonPluginBase:
    _config = "{}"

    # host-provided config API
    def get_config(self):
        return self._config

    def save_config(self, value):
        if isinstance(value, dict):
            value = json.dumps(value)
        self._config = value
        return True

    def get_config_version(self):
        return ""

    def get_default_config(self):
        return {}

    def has_config_ui(self):
        return False

    def get_config_ui(self):
        return ""

    # pages API
    def post_message(self, data):
        posted = getattr(self, "_posted_local", None)
        if posted is None:
            posted = []
            self._posted_local = posted
        posted.append(data)


class ScriptPluginCapabilityBase(PythonPluginBase):
    def execute(self):
        raise NotImplementedError


class PagesPluginCapabilityBase(PythonPluginBase):
    def get_ui(self):
        raise NotImplementedError

    def on_message(self, data):
        pass


class ExecutionResult:
    def __init__(self, status, message="", data=""):
        self.status = status
        self.message = message
        self.data = data

    @staticmethod
    def success(message="", data=""):
        return ExecutionResult("Success", message, data)

    @staticmethod
    def skipped(message=""):
        return ExecutionResult("Skipped", message)

    @staticmethod
    def failure(status, message, data=""):
        return ExecutionResult(status, message, data)

    def __repr__(self):
        return "ExecutionResult(%s, %r)" % (self.status, self.message)


class PluginResult:
    Success = "Success"
    Skipped = "Skipped"
    RecoverableError = "RecoverableError"
    FatalError = "FatalError"


class FakeWindow:
    def __init__(self, on_submit=None):
        self.on_submit = on_submit
        self.posted = []
        self.closed = False

    def post(self, data):
        self.posted.append(data)

    def close(self):
        self.closed = True

    def is_open(self):
        return not self.closed

    def submit(self, data):
        if self.on_submit:
            self.on_submit(data)


class _Ui:
    WINDOW_MODELESS = 0
    WINDOW_MODAL = 1
    PD_APP_MODAL = 1
    PD_AUTO_HIDE = 2

    def __init__(self):
        self.calls = []
        self.windows = []

    def message(self, text, title="", buttons="ok", icon="info"):
        self.calls.append(("message", title, text, icon))
        return "ok"

    def create_window(self, html="", title="", width=0, height=0, on_message=None,
                      on_close=None, style=0, on_submit=None):
        self.calls.append(("create_window", title, len(html), width, height, style))
        window = FakeWindow(on_submit=on_submit)
        self.windows.append(window)
        return window


registered = []
permissions = []


def register_capability(cls):
    registered.append(cls)


def request_permissions(**kwargs):
    permissions.append(kwargs)


def plugin(cls):
    cls.__orca_plugin__ = True
    return cls


class base:
    def register_capabilities(self):
        raise NotImplementedError


ui = _Ui()

orca = sys.modules[__name__]
orca.PythonPluginBase = PythonPluginBase
orca.base = base
orca.plugin = plugin
orca.register_capability = register_capability
orca.request_permissions = request_permissions
orca.ExecutionResult = ExecutionResult
orca.PluginResult = PluginResult
orca.script = types.SimpleNamespace(ScriptPluginCapabilityBase=ScriptPluginCapabilityBase)
orca.pages = types.SimpleNamespace(PagesPluginCapabilityBase=PagesPluginCapabilityBase)
orca.host = types.SimpleNamespace(ui=ui)
