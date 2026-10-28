# /// script
# requires-python = ">=3.12"
# dependencies = ["paho-mqtt>=2.0.0", "cryptography>=41.0.0"]
#
# [tool.orcaslicer.plugin]
# name = "Anycubic Cloud"
# description = "Anycubic Cloud device dashboard for OrcaSlicer: login, live printer status, G-code upload, confirmed print controls and a live webcam panel."
# author = "Anycubic Cloud Print Contributors"
# version = "0.7.3"
# ///
"""
Anycubic Cloud plugin for OrcaSlicer.

Provides:
  * A "Anycubic Cloud" main-window page (Pages capability) showing live printer
    status polled from the cloud MQTT broker.
  * Script capabilities (Plugins dialog -> Run):
      - "Anycubic Login"     : paste the RS256 access token, exchange it for a
                               workbench token, cache it locally.
      - "Select Printer"     : pick the Anycubic printer to monitor.
      - "Start Monitoring"   : connect to Anycubic MQTT and subscribe.
      - "Stop Monitoring"    : disconnect.
  * A custom Config tab (login form) via has_config_ui()/get_config_ui().
  * A "Print from folder" panel on the dashboard: lists G-code from a watched
    folder, uploads it to Anycubic Cloud, and (after explicit confirmation)
    starts the print with order 1 on the selected printer. Folder scanning runs
    on a background watcher; the dashboard only reads its cached snapshot.
  * A "Camera" panel on the dashboard: opens the printer's cloud camera with
    order 1001, asks the printer to start pushing video over MQTT, and plays the
    AWS Kinesis Video Streams WebRTC feed in the dashboard. Stop closes the
    stream with MQTT stopCapture and order 1002. The camera is monitoring-only;
    it never touches an active print.

Protocol details (verified):
  HTTP  : XX-Signature = MD5(KEY_A + ts + XX-Version + KEY_B + nonce + KEY_A)
  Login : POST /v3/public/loginWithAccessToken  {device_type, access_token, pc_id}
  MQTT  : clientId = pcf_md5(token)
          username = user|pcf|email|md5("pcf_"+md5(token)+password+"pcf_"+md5(token))
          password = base64(RSA_PKCS1v15(broker_pub, token))
          subscribe anycubic/anycubicCloud/v1/+/public/{machine_type}/{unionid}/#
  Upload: lockStorageSpace -> PUT preSignUrl -> profile/newUploadFile -> unlock
  Print : POST /work/operation/sendOrder {order_id:1, ...}
  Control: POST /work/operation/sendOrder {order_id:2|3|4, printer_id, project_id, ...}
  Camera : POST /work/operation/sendOrder {order_id:1001} -> temporary AWS STS
           credentials; MQTT video/startCapture -> KVS WebRTC master; viewer
           gets a SigV4-presigned WSS URL and plays H.264 in the dashboard.
           POST /work/operation/sendOrder {order_id:1002} closes the session.

Control commands are limited to the explicitly confirmed "Upload + start print"
and the confirmed Pause/Resume/Stop buttons on the dashboard. Every command is
re-checked against the cloud's active project immediately before it is sent,
so a stale dashboard cannot act on a job that has already finished. The camera
is strictly a viewer: its only printer-side messages are startCapture and
stopCapture.
"""

import base64
import hashlib
import hmac
import json
import os
import ssl
import threading
import time
import uuid

import orca


# ============================================================================
# Constants (verified from reverse engineering)
# ============================================================================

KEY_A = "f9b3528877c94d5c9c5af32245db46ef"
KEY_B = "0cf75926606049a3937f56b0373b99fb"
XX_VERSION = "2.0.0.3"
CLOUD_BASE = "https://cloud-universe.anycubic.com"
GATEWAY_PREFIX = "/p/p/workbench/api"
DEVICE_TYPE = "pcf"
IS_CN = "0"
LANGUAGE = "US"

MQTT_HOST = "mqtt-universe.anycubic.com"
MQTT_PORT = 8883
MQTT_SUB_TEMPLATE = "anycubic/anycubicCloud/v1/+/public/{machine_type}/{unionid}/#"
MQTT_VIDEO_TEMPLATE = "anycubic/anycubicCloud/v1/web/printer/{machine_type}/{unionid}/video"
# paho-mqtt passes source_address=("", 0) to socket.create_connection even when
# no bind address is configured, so the host's audit hook sees socket.bind.
# A first connect can be blocked while the permission prompt is open; retry
# until the user approves instead of giving up permanently.
MQTT_CONNECT_RETRY_MIN = 2.0
MQTT_CONNECT_RETRY_MAX = 30.0

LOGIN_PATH = "/v3/public/loginWithAccessToken"
PRINTERS_PATH = "/work/printer/getPrinters"
UPLOAD_LOCK_PATH = "/v2/cloud_storage/lockStorageSpace"
UPLOAD_UNLOCK_PATH = "/v2/cloud_storage/unlockStorageSpace"
UPLOAD_CLAIM_PATH = "/v2/profile/newUploadFile"
SEND_ORDER_PATH = "/work/operation/sendOrder"
PROJECTS_PATH = "/work/project/getProjects"

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(PLUGIN_DIR, "state.json")
TLS_DIR = os.path.join(PLUGIN_DIR, "tls")
CA_PATH = os.path.join(TLS_DIR, "ca.pem")
BROKER_PUB_PATH = os.path.join(TLS_DIR, "broker_pub.pem")

# The Orca plugin hub installs a single .py or .whl file, so a sibling tls/
# directory will not exist for hub-installed copies. Keep the PEMs embedded as
# the always-available fallback; the files above remain an override for local
# development.
CA_PEM = """-----BEGIN CERTIFICATE-----
MIIECDCCAvCgAwIBAgIBATANBgkqhkiG9w0BAQsFADCBmzELMAkGA1UEBhMCQ04x
EjAQBgNVBAgMCUd1YW5nZG9uZzERMA8GA1UEBwwIU2hlbnpoZW4xETAPBgNVBAoM
CEFueWN1YmljMREwDwYDVQQLDAhBbnljdWJpYzETMBEGA1UEAwwKQUMgUm9vdCBD
QTEqMCgGCSqGSIb3DQEJARYbYW55Y3ViaWNfY2xvdWRAYW55Y3ViaWMuY29tMCAX
DTIzMDcxMjA2NDgwNVoYDzIxMjMwNzEzMDY0ODA1WjCBmzELMAkGA1UEBhMCQ04x
EjAQBgNVBAgMCUd1YW5nZG9uZzERMA8GA1UEBwwIU2hlbnpoZW4xETAPBgNVBAoM
CEFueWN1YmljMREwDwYDVQQLDAhBbnljdWJpYzETMBEGA1UEAwwKQUMgUm9vdCBD
QTEqMCgGCSqGSIb3DQEJARYbYW55Y3ViaWNfY2xvdWRAYW55Y3ViaWMuY29tMIIB
IjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAn2Kpx1AzT2Df1kf1MImTdqCg
q5JDGdFTMw/Mx4TN6+oYUQyVUcSs8j+EPFlaCja7t9nXoNLrfS26PwdDzJEt+wVD
CdoWvNxuie+0w2Zk8lpRHn76z+waTONO3OLaneWe0xINNMtAqmuV0w2HSoNmvrLq
J7iO8moNeUO92fHUAnwkrNCtKeWpB80DLdVJBZRUeiL7KKH+7ud343KcfWLpC5cj
9RyZYVCEShwY9/tPN40Jg+jk43JiFbu5ruAMPuUWn5Iy/6ENnyBKk21uYxED3y/O
KNVJyl8+fZMgZriCzIP2YsU52O7nbr2iWytA1a1ZTIyiUPpGYQSYY8PSV7vwxwID
AQABo1MwUTAdBgNVHQ4EFgQUJZA6qRREX697ta0fYDWzSrYKxyIwHwYDVR0jBBgw
FoAUJZA6qRREX697ta0fYDWzSrYKxyIwDwYDVR0TAQH/BAUwAwEB/zANBgkqhkiG
9w0BAQsFAAOCAQEALPq1pVM6KjsJJx2Dk12JEN3AdYjj6oBKTBOYm1GvuuY+zN/a
ZTCuNADJQHg/UUypx5ZygJudIkDcLLmYPv99C+kNacbPIl6BqUQUEo4qNuxHqvCT
d48HMO6dngGY98pjBzHfHUH25LLAb0I/BqIx++XgZK/u7hea7xOkcBi11gpSplnj
Bzf3h7gl9iGr+FEAWmZAKVyNXZ9tMsf5AEJVgfaDG6UXyPKeMhO6zVC58quArL5r
LXVIJknBKbtnQ9n3qqExNyBWsvkQxgD+o07rZzdehnExZ5+LjeF6ktyP0e7BgFqW
mqBFWhzKUShFFw+6tjt9piDil2agec2b2JEEuA==
-----END CERTIFICATE-----
"""

BROKER_PUB_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAn2Kpx1AzT2Df1kf1MImT
dqCgq5JDGdFTMw/Mx4TN6+oYUQyVUcSs8j+EPFlaCja7t9nXoNLrfS26PwdDzJEt
+wVDCdoWvNxuie+0w2Zk8lpRHn76z+waTONO3OLaneWe0xINNMtAqmuV0w2HSoNm
vrLqJ7iO8moNeUO92fHUAnwkrNCtKeWpB80DLdVJBZRUeiL7KKH+7ud343KcfWLp
C5cj9RyZYVCEShwY9/tPN40Jg+jk43JiFbu5ruAMPuUWn5Iy/6ENnyBKk21uYxED
3y/OKNVJyl8+fZMgZriCzIP2YsU52O7nbr2iWytA1a1ZTIyiUPpGYQSYY8PSV7vw
xwIDAQAB
-----END PUBLIC KEY-----
"""

PLUGIN_VERSION = "0.7.3"

# G-code files are read from a local folder. The host grants file-read access
# per directory tree, so keep the allowed roots to the usual export folders.
WATCH_ROOTS = tuple(
    os.path.realpath(os.path.expanduser(path))
    for path in ("~/Downloads", "~/Desktop", "~/Documents")
)
DEFAULT_WATCH_DIR = os.path.join(os.path.expanduser("~"), "Downloads")
# Orca documents a "<folder>/*" grant as covering the folder itself, but its
# audit hook blocks a scan of the root unless the exact path is granted too.
# Request both so listing ~/Downloads is allowed.
WATCH_PERMISSIONS = []
for _root in WATCH_ROOTS:
    WATCH_PERMISSIONS.extend([_root, os.path.join(_root, "*")])
WATCH_EXTENSIONS = (".gcode", ".gco")
WATCH_FILE_LIMIT = 40
WATCH_CACHE_SECONDS = 5.0

# v0.6.1: directory enumeration can block indefinitely on macOS (TCC / file
# provider / iCloud). It must therefore never run on the UI thread. A single
# daemon watcher owns all filesystem access and publishes a cached snapshot;
# build_status() only copies that snapshot. Set WATCH_BACKGROUND = False in
# tests to use the old synchronous scanner.
WATCH_BACKGROUND = True
WATCH_SCAN_INTERVAL = 5.0
WATCH_STALL_SECONDS = 20.0

START_PRINT_RETRIES = 3
START_PRINT_RETRY_DELAY = 3.0

# status/report is published on change plus roughly every 5 minutes.
STATUS_INTERVAL = 300.0
STALE_AFTER = 2 * STATUS_INTERVAL

# Printer states that mean "do not start another job".
BUSY_STATES = {
    "printing", "working", "busy", "paused", "pausing", "resuming",
    "heating", "leveling", "preparing", "starting", "calibrating",
}

# Print-task states from /work/project/getProjects. 1=printing, 2=complete,
# 3=cancelled, 4=downloading, 5=checking, 6=preheating, 7=slicing.
ACTIVE_PROJECT_STATES = {1, 4, 5, 6, 7}
PAUSE_RESUME_PROJECT_STATE = 1

# Anycubic order IDs for the project control endpoint.
CONTROL_ORDERS = {"pause": 2, "resume": 3, "stop": 4}
CONTROL_VERBS = {"pause": "Pause", "resume": "Resume", "stop": "Stop"}

# The active-project record is fetched over HTTP, not MQTT. Polling it every
# few seconds keeps the controls accurate without hammering the cloud.
PROJECT_REFRESH_INTERVAL = 5.0

# --- Camera (verified 2026-10-27) -------------------------------------------
# Order 1001 returns temporary AWS STS credentials in data.token. The printer
# then publishes its WebRTC master on the KVS channel named after the printer
# key; a viewer signs a WSS URL for that channel and exchanges SDP/ICE with it.
# The printer only starts sending media after MQTT video/startCapture.
CAMERA_OPEN_ORDER = 1001
CAMERA_CLOSE_ORDER = 1002
KVS_SERVICE = "kinesisvideo"
KVS_DEFAULT_REGION = "us-east-2"
KVS_WSS_EXPIRES = 299
KVS_REGIONS = (
    "us-east-1", "us-east-2", "us-west-2", "eu-west-1", "ap-northeast-1",
)
# The KVS ICE endpoint is a per-channel r-*.kinesisvideo host that cannot be
# known before the camera is opened, and Orca grants URLs, not host wildcards.
# Public STUN is enough for the normal home-network case and keeps the Python
# side limited to the fixed regional KVS control-plane host.
KVS_STUN_SERVERS = (
    {"urls": "stun:stun.l.google.com:19302"},
    {"urls": "stun:stun1.l.google.com:19302"},
    {"urls": "stun:stun.cloudflare.com:3478"},
)
CAMERA_FIRST_FRAME_TIMEOUT = 20.0

# Orca grants URLs/socket addresses, not host wildcards. The regional KVS
# control-plane hosts are fixed, so request the regions the Anycubic service
# uses. (The r-*.kinesisvideo ICE host and v-*.kinesisvideo WSS host are
# discovered at runtime; the WSS connection is made by the dashboard WebView,
# and the plugin uses public STUN instead of the dynamic ICE endpoint.)
KVS_HTTP_PERMISSIONS = []
KVS_SOCKET_PERMISSIONS = []
for _kvs_region in KVS_REGIONS:
    _kvs_host = "kinesisvideo.%s.amazonaws.com" % _kvs_region
    KVS_HTTP_PERMISSIONS.extend([
        "https://" + _kvs_host,
        _kvs_host,
        _kvs_host + ":443",
        "('%s', 443)" % _kvs_host,
    ])
    KVS_SOCKET_PERMISSIONS.extend([
        "('%s', 443)" % _kvs_host,
        _kvs_host + ":443",
    ])


# ============================================================================
# Small helpers
# ============================================================================

def _now() -> float:
    return time.time()


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fmt_size(num: float) -> str:
    num = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return ("%d %s" % (num, unit)) if unit == "B" else ("%.1f %s" % (num, unit))
        num /= 1024.0
    return "%.1f GB" % num


def _jwt_payload(token: str) -> dict:
    """Decode (without verifying) the payload of a JWT."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode("ascii")))
    except Exception:
        return {}


def _jwt_exp(token: str) -> int:
    try:
        return int(_jwt_payload(token).get("exp", 0))
    except Exception:
        return 0


def _find_anycubic_log_token():
    """Search Anycubic Slicer Next logs for an RS256 token (id_token URL param)."""
    import glob, os, re
    log_dirs = [
        os.path.expanduser("~/Library/Application Support/AnycubicSlicerNext/log"),
        os.path.expanduser("~/Library/Application Support/Anycubic Slicer Next/log"),
        os.path.expanduser("~/.config/AnycubicSlicerNext/log"),
        os.path.expanduser("~/.config/Anycubic Slicer Next/log"),
    ]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            log_dirs.extend([
                os.path.join(appdata, "AnycubicSlicerNext", "log"),
                os.path.join(appdata, "Anycubic Slicer Next", "log"),
            ])
    token_re = re.compile(r"id_token=(eyJhbGciOiJSUzI1NiIsImtpZCI6ImFjX2FwcF9jcnQi[^&\s]{100,})")
    for log_dir in log_dirs:
        if not os.path.isdir(log_dir):
            continue
        for log_file in sorted(glob.glob(os.path.join(log_dir, "*.log")), key=os.path.getmtime, reverse=True)[:5]:
            try:
                with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                m = token_re.search(content)
                if m:
                    return m.group(1)
            except Exception:
                continue
    return None


def _anycubic_log_dir():
    """Return the Anycubic Slicer Next log directory, or None if not found."""
    import os
    candidates = [
        os.path.expanduser("~/Library/Application Support/AnycubicSlicerNext/log"),
        os.path.expanduser("~/Library/Application Support/Anycubic Slicer Next/log"),
        os.path.expanduser("~/.config/AnycubicSlicerNext/log"),
        os.path.expanduser("~/.config/Anycubic Slicer Next/log"),
    ]
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            candidates.extend([
                os.path.join(appdata, "AnycubicSlicerNext", "log"),
                os.path.join(appdata, "Anycubic Slicer Next", "log"),
            ])
    for d in candidates:
        if os.path.isdir(d):
            return d
    return None
    """Accept a raw JWT or a pasted JSON blob containing access_token/id_token."""
    text = (text or "").strip().strip('"').strip("'")
    if text.startswith("{"):
        try:
            blob = json.loads(text)
            for key in ("access_token", "id_token", "token"):
                val = blob.get(key)
                if isinstance(val, str) and val.count(".") == 2:
                    return val
        except Exception:
            pass
    return text


def _signature(ts: str, nonce: str, version: str = XX_VERSION) -> str:
    prehash = KEY_A + ts + version + KEY_B + nonce + KEY_A
    return hashlib.md5(prehash.encode("utf-8")).hexdigest().upper()


def _http_json(method: str, path: str, body=None, token: str = "", timeout: int = 20):
    """Perform a signed Anycubic cloud API request, return parsed JSON dict."""
    from urllib import request as urlrequest

    ts = str(int(_now() * 1000))
    nonce = uuid.uuid4().hex
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "Content-Type": "application/json; charset=UTF-8" if method != "GET"
        else "application/x-www-form-urlencoded",
        "XX-Device-Type": DEVICE_TYPE,
        "XX-IS-CN": IS_CN,
        "XX-LANGUAGE": LANGUAGE,
        "XX-Nonce": nonce,
        "XX-Signature": _signature(ts, nonce),
        "XX-Timestamp": ts,
        "XX-Version": XX_VERSION,
    }
    if token:
        headers["XX-Token"] = token

    data = None
    if body is not None and method != "GET":
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")

    req = urlrequest.Request(
        CLOUD_BASE + GATEWAY_PREFIX + path,
        data=data,
        headers=headers,
        method=method,
    )
    with urlrequest.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
    return json.loads(raw)


# ----------------------------------------------------------------------------
# AWS SigV4 (dependency-free)
# ----------------------------------------------------------------------------
# The camera uses two AWS APIs:
#   * Kinesis Video control plane (POST, fixed regional host) to look up the
#     channel ARN and the viewer WSS endpoint.
#   * A presigned WSS URL for the signaling channel. The URL contains the
#     temporary session token and must be generated in Python; the dashboard
#     only ever receives the signed URL, never the AWS secret key.

def _aws_quote(value, safe="-_.~"):
    from urllib.parse import quote
    return quote(str(value), safe=safe)


def _aws_canonical_query(params):
    return "&".join(
        "%s=%s" % (_aws_quote(key), _aws_quote(value))
        for key, value in sorted(params.items())
    )


def _aws_signing_key(secret_key, date_stamp, region, service):
    key = ("AWS4" + secret_key).encode("utf-8")
    for part in (date_stamp, region, service, "aws4_request"):
        key = hmac.new(key, part.encode("utf-8"), hashlib.sha256).digest()
    return key


def _aws_sigv4_headers(method, url, body, region, service, creds, now=None):
    """Return the headers for a SigV4-signed AWS REST-JSON request."""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    host = parts.netloc
    canonical_uri = _aws_quote(parts.path or "/", safe="/-_.~")
    canonical_query = ""
    payload = body or b""
    payload_hash = hashlib.sha256(payload).hexdigest()
    stamp = time.strftime("%Y%m%dT%H%M%SZ", now or time.gmtime())
    date_stamp = stamp[:8]
    headers = {
        "host": host,
        "x-amz-date": stamp,
        "x-amz-security-token": creds["session_token"],
    }
    if body is not None:
        headers["content-type"] = "application/json"
    signed_headers = sorted(headers)
    canonical_headers = "".join(
        "%s:%s\n" % (key, " ".join(str(headers[key]).split()))
        for key in signed_headers
    )
    canonical_request = "\n".join([
        method.upper(), canonical_uri, canonical_query,
        canonical_headers, ";".join(signed_headers), payload_hash,
    ])
    scope = "%s/%s/%s/aws4_request" % (date_stamp, region, service)
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", stamp, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signing_key = _aws_signing_key(
        creds["secret_key"], date_stamp, region, service)
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    headers["Authorization"] = (
        "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
        % (creds["access_key"], scope, ";".join(signed_headers), signature)
    )
    return headers


def _aws_presign_url(base_url, params, region, service, creds,
                     expires=KVS_WSS_EXPIRES, now=None):
    """Create a SigV4 presigned URL (used for the KVS WebSocket endpoint)."""
    from urllib.parse import urlsplit

    parts = urlsplit(base_url)
    host = parts.netloc
    path = parts.path or "/"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", now or time.gmtime())
    date_stamp = stamp[:8]
    scope = "%s/%s/%s/aws4_request" % (date_stamp, region, service)
    query = dict(params)
    query.update({
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": "%s/%s" % (creds["access_key"], scope),
        "X-Amz-Date": stamp,
        "X-Amz-Expires": str(int(expires)),
        "X-Amz-SignedHeaders": "host",
    })
    if creds.get("session_token"):
        query["X-Amz-Security-Token"] = creds["session_token"]
    canonical_query = _aws_canonical_query(query)
    canonical_request = "\n".join([
        "GET", path, canonical_query,
        "host:%s\n" % host, "host", hashlib.sha256(b"").hexdigest(),
    ])
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", stamp, scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signing_key = _aws_signing_key(
        creds["secret_key"], date_stamp, region, service)
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    query["X-Amz-Signature"] = signature
    return "%s://%s%s?%s" % (
        parts.scheme, host, path, _aws_canonical_query(query))


def _aws_request_json(region, path, body, creds, timeout=20):
    """POST a REST-JSON request to the Kinesis Video control plane."""
    from urllib import error as urlerror, request as urlrequest

    host = "kinesisvideo.%s.amazonaws.com" % region
    url = "https://%s%s" % (host, path)
    data = json.dumps(body, separators=(",", ":")).encode("utf-8")
    headers = _aws_sigv4_headers(
        "POST", url, data, region, KVS_SERVICE, creds)
    req = urlrequest.Request(url, data=data, headers=headers, method="POST")
    try:
        with urlrequest.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urlerror.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        raise RuntimeError(
            "AWS request failed (HTTP %s): %s"
            % (exc.code, detail or exc.reason))
    except Exception as exc:
        raise RuntimeError("AWS request failed: %s" % exc)
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        raise RuntimeError("AWS returned an unexpected response.")


def _camera_credentials(token_data):
    """Normalise the order-1001 credential blob into the AWS field names."""
    data = token_data if isinstance(token_data, dict) else {}
    region = str(data.get("region") or KVS_DEFAULT_REGION)
    access_key = str(data.get("tmpSecretId") or "")
    secret_key = str(data.get("tmpSecretKey") or "")
    session_token = str(data.get("sessionToken") or "")
    if not access_key or not secret_key or not session_token:
        raise RuntimeError(
            "The camera token response did not include AWS credentials.")
    return {
        "region": region,
        "access_key": access_key,
        "secret_key": secret_key,
        "session_token": session_token,
    }


def _kvs_channel_arn(channel_name, creds):
    response = _aws_request_json(
        creds["region"], "/describeSignalingChannel",
        {"ChannelName": channel_name}, creds)
    info = response.get("ChannelInfo") or {}
    arn = info.get("ChannelARN") or ""
    if not arn:
        raise RuntimeError("AWS did not return a signaling channel ARN.")
    return arn


def _kvs_wss_endpoint(channel_arn, creds):
    response = _aws_request_json(
        creds["region"], "/getSignalingChannelEndpoint",
        {
            "ChannelARN": channel_arn,
            "SingleMasterChannelEndpointConfiguration": {
                "Protocols": ["WSS"],
                "Role": "VIEWER",
            },
        },
        creds,
    )
    for entry in response.get("ResourceEndpointList") or []:
        if str(entry.get("Protocol") or "").upper() == "WSS":
            endpoint = entry.get("ResourceEndpoint") or ""
            if endpoint:
                return endpoint
    raise RuntimeError("AWS did not return a WSS signaling endpoint.")


# ============================================================================
# Module state (shared by all capabilities)
# ============================================================================

class PluginState:
    def __init__(self):
        self.lock = threading.RLock()
        self.pc_id = uuid.uuid4().hex.upper()
        self.workbench_token = ""
        self.token_exp = 0
        self.email = ""
        self.printer = None
        self.failed_token_sha = ""
        self.status_payload = None
        self.print_payload = None
        self.project = None          # active cloud project (id + pause state)
        self.project_ts = 0.0
        self.project_error = ""
        self.mqtt_service = None
        self.monitoring = False
        self.mqtt_connected = False
        self.mqtt_error = ""
        self.last_message_ts = 0.0
        self.camera_state = "idle"   # idle | starting | connecting | live | error
        self.camera_error = ""
        self.camera_started = 0.0
        self.video_payload = None
        self.video_ts = 0.0
        self.notice = ""
        self.notice_error = False
        self.recent_messages = []
        self.watch_dir = DEFAULT_WATCH_DIR
        self.upload = None
        self._load()

    # ---- persistence -------------------------------------------------------

    def _load(self):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return
        if not isinstance(data, dict):
            return
        self.pc_id = data.get("pc_id") or self.pc_id
        self.workbench_token = data.get("workbench_token") or ""
        self.token_exp = int(data.get("token_exp") or 0)
        self.email = data.get("email") or ""
        self.printer = data.get("printer") if isinstance(data.get("printer"), dict) else None
        self.failed_token_sha = data.get("failed_token_sha") or ""
        self.watch_dir = data.get("watch_dir") or DEFAULT_WATCH_DIR

    def save(self):
        with self.lock:
            data = {
                "pc_id": self.pc_id,
                "workbench_token": self.workbench_token,
                "token_exp": self.token_exp,
                "email": self.email,
                "printer": self.printer,
                "failed_token_sha": self.failed_token_sha,
                "watch_dir": self.watch_dir,
            }
        tmp = STATE_PATH + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            os.replace(tmp, STATE_PATH)
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass

    # ---- auth --------------------------------------------------------------

    def is_logged_in(self) -> bool:
        with self.lock:
            return bool(self.workbench_token) and (self.token_exp == 0 or _now() < self.token_exp - 60)

    def set_notice(self, text: str, error: bool = False):
        with self.lock:
            self.notice = text or ""
            self.notice_error = bool(error)

    def take_notice(self):
        with self.lock:
            text, err = self.notice, self.notice_error
            self.notice, self.notice_error = "", False
        return text, err

    # ---- upload progress ---------------------------------------------------

    def set_upload(self, value):
        with self.lock:
            self.upload = dict(value) if isinstance(value, dict) else None

    def patch_upload(self, **fields):
        with self.lock:
            upload = dict(self.upload or {})
            upload.update(fields)
            self.upload = upload
            return dict(upload)

    def upload_active(self) -> bool:
        with self.lock:
            upload = self.upload or {}
        return bool(upload) and not upload.get("done")


STATE = PluginState()


# ============================================================================
# Auth / cloud API
# ============================================================================

def login_with_rs256(rs256_token: str):
    """Exchange an RS256 access token for a workbench token. Returns (ok, message)."""
    global _projects_ts

    token = _extract_access_token(rs256_token)
    if not token or token.count(".") != 2:
        return False, "That does not look like a JWT. Paste the RS256 access token from the Anycubic Slicer Next logs."

    try:
        resp = _http_json(
            "POST",
            LOGIN_PATH,
            {"device_type": DEVICE_TYPE, "access_token": token, "pc_id": STATE.pc_id},
        )
    except Exception as exc:
        return False, "Login request failed: %s" % exc

    if resp.get("code") != 1:
        return False, "Login rejected (code %s): %s" % (resp.get("code"), resp.get("msg"))

    data = resp.get("data") or {}
    workbench = data.get("token") or ""
    if not workbench:
        return False, "Login succeeded but no token was returned."

    email = ""
    user = data.get("user") or {}
    if isinstance(user, dict):
        email = user.get("user_email") or ""
    if not email:
        email = _jwt_payload(token).get("email") or ""

    with STATE.lock:
        STATE.workbench_token = workbench
        STATE.token_exp = _jwt_exp(workbench) or int(_now() + 86400)
        STATE.email = email or STATE.email
        STATE.failed_token_sha = ""
        STATE.project = None
        STATE.project_ts = 0.0
        STATE.project_error = ""
    with _projects_lock:
        _projects_ts = 0.0
    STATE.save()
    return True, "Signed in as %s" % (STATE.email or "your Anycubic account")


def get_printers():
    """Return the account's FDM printers."""
    with STATE.lock:
        token = STATE.workbench_token
    if not token:
        raise RuntimeError("Not signed in.")

    body = {
        "page": 1, "limit": 1000, "is_farm": 0, "query_status": 0,
        "printer_id": 0, "machine_type": 0, "bulk_id": 0, "query_project": 0,
        "sort_by": 0, "type": "FDM", "label_name": None,
        "printer_ids": [],
        "ams_info": {"use_ams": False, "padding2": 0, "padding1": 0,
                     "padding": 0, "ams_box_mapping": []},
    }
    resp = _http_json("POST", PRINTERS_PATH, body, token=token)
    if resp.get("code") != 1:
        raise RuntimeError("getPrinters failed (code %s): %s" % (resp.get("code"), resp.get("msg")))
    data = resp.get("data")
    return data if isinstance(data, list) else []


def get_projects(limit=20):
    """Return the account's newest print tasks (cloud project records)."""
    with STATE.lock:
        token = STATE.workbench_token
    if not token:
        raise RuntimeError("Not signed in.")

    query = "%s?page=1&limit=%d" % (PROJECTS_PATH, max(1, int(limit)))
    resp = _http_json("GET", query, None, token=token)
    if resp.get("code") != 1:
        raise RuntimeError("getProjects failed (code %s): %s" % (resp.get("code"), resp.get("msg")))
    data = resp.get("data")
    return data if isinstance(data, list) else []


def _project_status(project):
    return _as_int(project.get("print_status")) if isinstance(project, dict) else None


def _project_active(project):
    return _project_status(project) in ACTIVE_PROJECT_STATES


def _project_paused(project):
    """A paused job keeps print_status=1 and sets pause to a non-zero value."""
    if not _project_active(project):
        return False
    return _as_int(project.get("pause")) not in (None, 0)


def _project_settings(project):
    """The project's ``settings`` field is a JSON string with layer details."""
    raw = project.get("settings") if isinstance(project, dict) else None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            value = json.loads(raw)
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _project_for_printer(project, printer_id):
    pid = _as_int(project.get("printer_id")) if isinstance(project, dict) else None
    wanted = _as_int(printer_id)
    return pid is not None and wanted is not None and pid == wanted


def find_active_project(projects, printer_id):
    """Newest active project for ``printer_id``, or None."""
    for project in projects or []:
        if _project_for_printer(project, printer_id) and _project_active(project):
            return project
    return None


def select_printer(printer: dict):
    global _projects_ts

    if not isinstance(printer, dict):
        return
    keep = {}
    for key in ("id", "key", "machine_type", "name", "model", "device_status",
                "is_printing", "machine_mac", "version", "parameter",
                "last_update_time", "reason", "ready_status"):
        if key in printer:
            keep[key] = printer[key]
    with STATE.lock:
        old_id = (STATE.printer or {}).get("id")
        STATE.printer = keep
        if old_id != keep.get("id"):
            STATE.project = None
            STATE.project_ts = 0.0
            STATE.project_error = ""
    with _projects_lock:
        if old_id != keep.get("id"):
            _projects_ts = 0.0
    STATE.save()


# The cloud's getPrinters record is the only source of the online/offline flag,
# and it is a snapshot: cached at login it would keep saying "offline" forever
# after the printer comes back. Re-fetch it in the background, at most every
# two minutes, so the dashboard and the MQTT badge agree.
REFRESH_INTERVAL = 120.0
_refresh_lock = threading.Lock()
_refresh_ts = 0.0
_refresh_thread = None


def refresh_printer(force: bool = False):
    global _refresh_ts, _refresh_thread

    with STATE.lock:
        printer = dict(STATE.printer or {})
        logged_in = STATE.is_logged_in()
    if not logged_in or not printer.get("id"):
        return

    now = _now()
    with _refresh_lock:
        if not force and (now - _refresh_ts) < REFRESH_INTERVAL:
            return
        if _refresh_thread is not None and _refresh_thread.is_alive():
            return
        _refresh_ts = now

        def worker():
            try:
                wanted = printer.get("id")
                for item in get_printers():
                    if item.get("id") == wanted:
                        select_printer(item)
                        return
            except Exception:
                pass

        _refresh_thread = threading.Thread(
            target=worker, name="anycubic-refresh", daemon=True)
        _refresh_thread.start()


# Active-project state is only needed for the control buttons. It is cached so
# build_status() never performs network I/O on the UI thread, and refreshed on
# a background thread at most every PROJECT_REFRESH_INTERVAL seconds.
_projects_lock = threading.Lock()
_projects_ts = 0.0
_projects_thread = None


def refresh_project(force: bool = False):
    global _projects_ts, _projects_thread

    with STATE.lock:
        printer = dict(STATE.printer or {})
        logged_in = STATE.is_logged_in()
    printer_id = printer.get("id")
    if not logged_in or not printer_id:
        with STATE.lock:
            STATE.project = None
            STATE.project_ts = 0.0
            STATE.project_error = ""
        return

    now = _now()
    with _projects_lock:
        if not force and (now - _projects_ts) < PROJECT_REFRESH_INTERVAL:
            return
        if _projects_thread is not None and _projects_thread.is_alive():
            return
        _projects_ts = now

        def worker():
            try:
                projects = get_projects()
            except Exception as exc:
                with STATE.lock:
                    STATE.project_error = str(exc)
                return
            with STATE.lock:
                wanted = (STATE.printer or {}).get("id")
            active = find_active_project(projects, wanted)
            with STATE.lock:
                STATE.project = dict(active) if active else None
                STATE.project_ts = _now()
                STATE.project_error = ""

        _projects_thread = threading.Thread(
            target=worker, name="anycubic-project", daemon=True)
        _projects_thread.start()


# ============================================================================
# MQTT service
# ============================================================================

class MqttService:
    def __init__(self):
        self.lock = threading.Lock()
        self.client = None
        self.thread = None
        self.running = False
        self.error = ""

    # ---- auth helpers ------------------------------------------------------

    def _credentials(self, token: str, email: str):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        try:
            with open(BROKER_PUB_PATH, "rb") as fh:
                public_key = serialization.load_pem_public_key(fh.read())
        except OSError:
            # Hub-installed single-file plugins have no tls/ directory.
            public_key = serialization.load_pem_public_key(
                BROKER_PUB_PEM.encode("ascii"))
        password = base64.b64encode(
            public_key.encrypt(token.encode("utf-8"), padding.PKCS1v15())
        ).decode("ascii")

        digest = hashlib.md5(token.encode("utf-8")).hexdigest()
        client_id = "pcf_" + digest
        user_md5 = hashlib.md5(
            ("pcf_" + digest + password + "pcf_" + digest).encode("utf-8")
        ).hexdigest()
        username = "user|pcf|%s|%s" % (email, user_md5)
        return client_id, username, password

    def _topic(self, printer: dict) -> str:
        machine_type = printer.get("machine_type", 20021)
        unionid = printer.get("key") or ""
        if not unionid:
            return ""
        return MQTT_SUB_TEMPLATE.format(machine_type=machine_type, unionid=unionid)

    def publish_video(self, action):
        """Ask the printer to start or stop pushing camera video.

        Returns ``(ok, msgid_or_error)``. This is the only MQTT write the
        plugin performs; everything else is monitoring.
        """
        action = str(action or "")
        if action not in ("startCapture", "stopCapture"):
            return False, "Unknown camera action: %s" % action
        with STATE.lock:
            printer = dict(STATE.printer or {})
            connected = STATE.mqtt_connected
        client = self.client
        if not self.running or not connected or client is None:
            return False, "MQTT is not connected. Start monitoring first."
        key = printer.get("key") or ""
        if not key:
            return False, "Select a printer first."
        topic = MQTT_VIDEO_TEMPLATE.format(
            machine_type=printer.get("machine_type", 20021), unionid=key)
        msgid = str(uuid.uuid4())
        payload = {
            "type": "video",
            "action": action,
            "timestamp": int(_now() * 1000),
            "msgid": msgid,
            "data": None,
        }
        try:
            info = client.publish(topic, json.dumps(payload), qos=1)
        except Exception as exc:
            return False, "Could not publish %s: %s" % (action, exc)
        rc = getattr(info, "rc", 0)
        if rc != 0:
            return False, "Could not publish %s (rc=%s)." % (action, rc)
        return True, msgid

    # ---- lifecycle ---------------------------------------------------------

    def start(self):
        with self.lock:
            if self.running:
                return True, "Monitoring already running."
            self.running = True
            self.error = ""
            self.thread = threading.Thread(target=self._run, name="anycubic-mqtt", daemon=True)
            self.thread.start()
        return True, "Connecting to Anycubic MQTT..."

    def stop(self):
        with self.lock:
            self.running = False
            client = self.client
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass
        with STATE.lock:
            STATE.monitoring = False
            STATE.mqtt_connected = False
        return True, "Monitoring stopped."

    def _run(self):
        try:
            with STATE.lock:
                token = STATE.workbench_token
                email = STATE.email or "user@example.com"
                printer = dict(STATE.printer or {})

            if not token or not printer or not printer.get("key"):
                raise RuntimeError("Sign in and select a printer before monitoring.")

            import paho.mqtt.client as mqtt

            client_id, username, password = self._credentials(token, email)

            try:
                client = mqtt.Client(
                    mqtt.CallbackAPIVersion.VERSION2,
                    client_id=client_id,
                    protocol=mqtt.MQTTv311,
                    clean_session=True,
                )
            except AttributeError:  # paho-mqtt 1.x
                client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311, clean_session=True)

            client.username_pw_set(username, password)
            client.on_connect = self._on_connect
            client.on_message = self._on_message
            client.on_disconnect = self._on_disconnect

            ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
            try:
                ctx.load_verify_locations(CA_PATH)
            except OSError:
                # Hub-installed single-file plugins have no tls/ directory.
                ctx.load_verify_locations(cadata=CA_PEM)
            # The broker certificate is signed with SHA1 (sha1WithRSAEncryption).
            # OpenSSL >= 1.1.1 omits SHA1 signature algorithms at its default
            # security level and the broker then answers with a fatal
            # handshake_failure alert. Lowering the level restores SHA1 in the
            # ClientHello. (LibreSSL sends SHA1 sigalgs by default, which is why
            # the macOS system Python connects without this.)
            try:
                ctx.set_ciphers("DEFAULT@SECLEVEL=0")
            except ssl.SSLError:
                pass
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            client.tls_set_context(ctx)

            with self.lock:
                self.client = client
            with STATE.lock:
                STATE.monitoring = True

            delay = MQTT_CONNECT_RETRY_MIN
            while self.running:
                try:
                    client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
                    break
                except OSError as exc:
                    # The host's audit hook reports a pending permission as an
                    # OSError. Keep the worker alive so approving the prompt can
                    # complete the connection without another Start Monitoring.
                    with STATE.lock:
                        STATE.mqtt_error = (
                            "Waiting for network permission from OrcaSlicer: %s" % exc)
                    if not self.running:
                        break
                    time.sleep(delay)
                    delay = min(delay * 2.0, MQTT_CONNECT_RETRY_MAX)
            if not self.running:
                return
            client.loop_forever(retry_first_connection=True)
        except Exception as exc:
            with STATE.lock:
                STATE.mqtt_error = str(exc)
                STATE.monitoring = False
                STATE.mqtt_connected = False
            with self.lock:
                self.running = False
        finally:
            with self.lock:
                self.client = None
            with STATE.lock:
                STATE.monitoring = False
                STATE.mqtt_connected = False

    # ---- paho callbacks ----------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        try:
            code = int(reason_code) if not isinstance(reason_code, int) else reason_code
        except Exception:
            code = 0
        if code == 0:
            with STATE.lock:
                STATE.mqtt_connected = True
                STATE.mqtt_error = ""
                printer = dict(STATE.printer or {})
            topic = self._topic(printer)
            if topic:
                client.subscribe(topic, qos=1)
        else:
            with STATE.lock:
                STATE.mqtt_error = "MQTT connection refused (rc=%s)" % code

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None):
        try:
            code = int(reason_code) if not isinstance(reason_code, int) else reason_code
        except Exception:
            code = 0
        with STATE.lock:
            STATE.mqtt_connected = False
            if code != 0 and self.running:
                STATE.mqtt_error = "MQTT disconnected (rc=%s), reconnecting..." % code

    def _on_message(self, client, userdata, msg):
        try:
            raw = msg.payload.decode("utf-8", "replace")
            try:
                payload = json.loads(raw)
            except Exception:
                payload = raw
            topic = msg.topic or ""
            with STATE.lock:
                STATE.last_message_ts = _now()
                STATE.mqtt_error = ""
                STATE.recent_messages.append({
                    "topic": topic,
                    "bytes": len(msg.payload or b""),
                    "ts": round(_now(), 1),
                })
                del STATE.recent_messages[:-10]
                if "status/report" in topic:
                    STATE.status_payload = payload
                elif "print/report" in topic:
                    STATE.print_payload = payload
                elif "video/report" in topic:
                    STATE.video_payload = payload
                    STATE.video_ts = _now()
        except Exception:
            pass


# ============================================================================
# Status formatting for the dashboard
# ============================================================================

def _walk(obj, depth=0):
    if depth > 8:
        return
    yield obj
    if isinstance(obj, dict):
        for value in obj.values():
            yield from _walk(value, depth + 1)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value, depth + 1)
    elif isinstance(obj, str):
        text = obj.strip()
        if text[:1] in "{[" or text[:1] == '"':
            try:
                decoded = json.loads(text)
            except Exception:
                return
            if decoded is not obj:
                yield from _walk(decoded, depth + 1)


def _find(payloads, keys):
    """Find the first non-empty value for the first matching key, in order."""
    for key in keys:
        for payload in payloads:
            if payload is None:
                continue
            for obj in _walk(payload):
                if isinstance(obj, dict) and key in obj:
                    value = obj.get(key)
                    if value not in (None, "", [], {}):
                        return value
    return None


def _as_float(value):
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, str):
            value = value.strip().rstrip("%")
        return float(value)
    except Exception:
        return None


def _as_int(value):
    try:
        if isinstance(value, bool):
            return None
        return int(value)
    except Exception:
        return None


def _progress_percent(payloads):
    value = _find(payloads, ["progress", "print_progress", "percent", "percentage", "rate"])
    num = _as_float(value)
    if num is None:
        return None
    if 0 <= num <= 1:
        num *= 100.0
    return round(num, 1)


# ============================================================================
# Watch folder (G-code source for upload)
# ============================================================================

_watch_lock = threading.Lock()
_watch_wake = threading.Event()
_watch_stop = threading.Event()
_watch_thread = None
_watch_state = {
    "dir": "",          # configured folder path used for the cached scan
    "resolved": "",     # realpath that was actually scanned
    "items": [],
    "error": "",
    "scanning": False,
    "scan_started": 0.0,
    "last_scan": 0.0,
    "last_ok": 0.0,
    "force": False,
}


def _inside_watch_roots(path):
    try:
        return any(path == root or path.startswith(root + os.sep) for root in WATCH_ROOTS)
    except Exception:
        return False


def watch_dir_status():
    """Return the configured watch folder without touching the filesystem.

    build_status() calls this on the UI thread, so it must stay pure string
    manipulation. The watcher thread verifies that the folder exists and can
    actually be read, and reports those problems through its snapshot.
    """
    with STATE.lock:
        raw = STATE.watch_dir or DEFAULT_WATCH_DIR
    try:
        path = os.path.normpath(os.path.expanduser(str(raw)))
    except Exception:
        path = DEFAULT_WATCH_DIR

    if not _inside_watch_roots(path):
        return {
            "path": path,
            "error": "The watch folder must be inside Downloads, Desktop or Documents, "
                     "because those are the folders the plugin is allowed to read.",
            "allowed": list(WATCH_ROOTS),
        }
    return {"path": path, "error": "", "allowed": list(WATCH_ROOTS)}


def _safe_gcode_path(path):
    """Resolve a user-supplied path, refusing anything outside the watch folder."""
    try:
        real = os.path.realpath(os.path.expanduser(str(path or "")))
    except Exception:
        return None
    status = watch_dir_status()
    if status["error"]:
        return None
    try:
        root = os.path.realpath(status["path"])
    except Exception:
        return None
    if not _inside_watch_roots(root):
        return None
    try:
        if os.path.commonpath([real, root]) != root:
            return None
    except ValueError:
        return None
    if not real.lower().endswith(WATCH_EXTENSIONS):
        return None
    if not os.path.isfile(real):
        return None
    return real


def _decorate_file(item, now):
    item = dict(item)
    item["age"] = round(max(0.0, now - item.get("mtime", now)), 1)
    item["size_text"] = _fmt_size(item.get("size", 0))
    return item


def _scan_watch_dir(status):
    """Enumerate the watch folder. Runs on the watcher thread only."""
    display = status["path"]
    if status["error"]:
        return display, [], status["error"]
    try:
        real = os.path.realpath(display)
    except Exception as exc:
        return display, [], "Could not resolve the watch folder: %s" % exc
    if not _inside_watch_roots(real):
        return real, [], ("The watch folder resolves outside Downloads, Desktop and "
                          "Documents, so the plugin will not read it.")
    try:
        if not os.path.isdir(real):
            return real, [], "That watch folder does not exist."
        items = []
        with os.scandir(real) as entries:
            for entry in entries:
                try:
                    if entry.name.startswith(".") or not entry.is_file(follow_symlinks=False):
                        continue
                    if not entry.name.lower().endswith(WATCH_EXTENSIONS):
                        continue
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                items.append({
                    "name": entry.name,
                    "path": os.path.join(real, entry.name),
                    "size": int(info.st_size),
                    "mtime": float(info.st_mtime),
                })
    except Exception as exc:
        return real, [], "Could not read the watch folder: %s" % exc

    items.sort(key=lambda item: item["mtime"], reverse=True)
    return real, items[:WATCH_FILE_LIMIT], ""


def _watch_scan_sync(force=False):
    """Synchronous scan used by tests and as a WATCH_BACKGROUND=False fallback."""
    now = _now()
    status = watch_dir_status()
    with _watch_lock:
        cached = (
            not force
            and _watch_state["dir"] == status["path"]
            and not _watch_state["scanning"]
            and (now - _watch_state["last_scan"]) < WATCH_CACHE_SECONDS
        )
        if cached:
            items = [dict(item) for item in _watch_state["items"]]
            error = _watch_state["error"]
            return [_decorate_file(item, now) for item in items], error
        _watch_state["dir"] = status["path"]
        _watch_state["scanning"] = True
        _watch_state["scan_started"] = now
        _watch_state["error"] = ""

    resolved, items, error = _scan_watch_dir(status)
    finished = _now()
    with _watch_lock:
        _watch_state["scanning"] = False
        _watch_state["resolved"] = resolved
        _watch_state["last_scan"] = finished
        _watch_state["items"] = [dict(item) for item in items]
        _watch_state["error"] = error
        if not error:
            _watch_state["last_ok"] = finished
    return [_decorate_file(item, finished) for item in items], error


def _watch_loop():
    """One daemon thread owns all watch-folder filesystem I/O."""
    while not _watch_stop.is_set():
        try:
            status = watch_dir_status()
            now = _now()
            with _watch_lock:
                force = bool(_watch_state["force"])
                last_scan = _watch_state["last_scan"]
                last_dir = _watch_state["dir"]
            changed = status["path"] != last_dir
            due = (
                force
                or changed
                or last_scan <= 0
                or (now - last_scan) >= WATCH_SCAN_INTERVAL
            )
            if not due:
                _watch_wake.wait(1.0)
                _watch_wake.clear()
                continue

            with _watch_lock:
                _watch_state["force"] = False
                _watch_state["dir"] = status["path"]
                _watch_state["scanning"] = True
                _watch_state["scan_started"] = _now()
                _watch_state["error"] = ""

            resolved, items, error = _scan_watch_dir(status)
            finished = _now()
            with _watch_lock:
                _watch_state["scanning"] = False
                _watch_state["resolved"] = resolved
                _watch_state["last_scan"] = finished
                _watch_state["items"] = [dict(item) for item in items]
                _watch_state["error"] = error
                if not error:
                    _watch_state["last_ok"] = finished
        except Exception as exc:
            with _watch_lock:
                _watch_state["scanning"] = False
                _watch_state["error"] = "Watch folder scan failed: %s" % exc
                _watch_state["last_scan"] = _now()

        # A force request arriving during the scan leaves force=True, so the
        # next pass rescans immediately after this wait is woken.
        _watch_wake.wait(WATCH_SCAN_INTERVAL)
        _watch_wake.clear()


def ensure_watch_thread():
    """Start the watcher once; safe to call repeatedly."""
    global _watch_thread
    if not WATCH_BACKGROUND:
        return None
    with _watch_lock:
        thread = _watch_thread
        if thread is not None and thread.is_alive():
            return thread
        _watch_stop.clear()
        thread = threading.Thread(target=_watch_loop, name="anycubic-watch", daemon=True)
        _watch_thread = thread
    thread.start()
    return thread


def stop_watch_thread():
    """Ask the watcher to stop. Never joins: it may be blocked in scandir()."""
    global _watch_thread
    _watch_stop.set()
    _watch_wake.set()
    with _watch_lock:
        _watch_thread = None


def request_watch_refresh(force=True):
    """Ask the watcher to rescan without doing any filesystem I/O here."""
    with _watch_lock:
        if force:
            _watch_state["force"] = True
    _watch_wake.set()
    ensure_watch_thread()


def watch_snapshot():
    """Return the cached watch-folder state. This never touches the filesystem."""
    status = watch_dir_status()
    now = _now()
    with _watch_lock:
        items = [dict(item) for item in _watch_state["items"]]
        state_dir = _watch_state["dir"]
        error = _watch_state["error"]
        scanning = bool(_watch_state["scanning"])
        scan_started = _watch_state["scan_started"]
        last_scan = _watch_state["last_scan"]

    # Never show files from a folder that is no longer configured.
    if state_dir != status["path"]:
        items = []
    if status["error"]:
        error = status["error"]

    stalled = bool(
        scanning and scan_started and (now - scan_started) > WATCH_STALL_SECONDS
    )
    if stalled:
        error = (
            "The watch-folder scan has been stuck for %d seconds. macOS may be "
            "waiting for file-access permission. Approve the prompt, then reopen "
            "the Anycubic Cloud tab; if it stays stuck, restart OrcaSlicer."
            % int(now - scan_started)
        )
    if stalled:
        mode = "stalled"
    elif scanning:
        mode = "scanning"
    elif status["error"] or error:
        mode = "error"
    elif last_scan:
        mode = "ready"
    else:
        mode = "idle"

    return {
        "path": status["path"],
        "error": error,
        "allowed": status["allowed"],
        "files": [_decorate_file(item, now) for item in items],
        "scanning": scanning,
        "stalled": stalled,
        "status": mode,
        "scan_age": round(now - last_scan, 1) if last_scan else None,
        "last_scan": last_scan or None,
    }


def watch_files(force=False):
    """Return ``(items, error)`` for the newest G-code files in the watch folder.

    With WATCH_BACKGROUND enabled this only reads the watcher's cached snapshot
    and (for force=True) wakes the watcher. It never blocks on the filesystem.
    """
    if WATCH_BACKGROUND:
        if force:
            request_watch_refresh(force=True)
        snapshot = watch_snapshot()
        return snapshot["files"], snapshot["error"]
    return _watch_scan_sync(force=force)


# ============================================================================
# Cloud upload (lock -> S3 PUT -> claim -> unlock)
# ============================================================================

class UploadError(RuntimeError):
    pass


class _UploadReader:
    """File wrapper so the S3 PUT can report progress to the dashboard."""

    def __init__(self, handle, size, on_bytes):
        self.handle = handle
        self.size = max(1, int(size))
        self.on_bytes = on_bytes
        self.sent = 0

    def read(self, amount=-1):
        chunk = self.handle.read(amount)
        if chunk:
            self.sent += len(chunk)
            try:
                self.on_bytes(self.sent, self.size)
            except Exception:
                pass
        return chunk

    def close(self):
        try:
            self.handle.close()
        except Exception:
            pass


def _put_presigned(url, path, size, on_progress=None):
    """PUT the file body to the signed S3 URL, reporting 0-100 percent."""
    from urllib import error as urlerror
    from urllib import request as urlrequest

    last = [0.0]

    def on_bytes(sent, total):
        if on_progress is None:
            return
        now = _now()
        if sent < total and (now - last[0]) < 0.4:
            return
        last[0] = now
        on_progress(min(100.0, sent * 100.0 / total))

    try:
        handle = open(path, "rb")
    except OSError as exc:
        raise UploadError("Could not open the file: %s" % exc)
    reader = _UploadReader(handle, size, on_bytes)
    req = urlrequest.Request(url, data=reader, method="PUT")
    req.add_header("Content-Length", str(int(size)))
    req.add_header("Content-Type", "application/octet-stream")
    try:
        with urlrequest.urlopen(req, timeout=600) as resp:
            try:
                resp.read(4096)
            except Exception:
                pass
            if resp.status not in (200, 201):
                raise UploadError("Cloud storage rejected the upload (HTTP %s)." % resp.status)
    except urlerror.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(300).decode("utf-8", "replace").strip()
        except Exception:
            pass
        raise UploadError("Cloud storage rejected the upload (HTTP %s)%s" % (
            exc.code, ": " + detail if detail else "."))
    except UploadError:
        raise
    except Exception as exc:
        raise UploadError("Upload failed: %s" % exc)
    finally:
        reader.close()

    if on_progress is not None:
        on_progress(100.0)


def upload_gcode(path, on_progress=None):
    """Upload one G-code file and return ``{file_id, name, size}``.

    ``on_progress(phase, percent)`` is called with phases lock/upload/claim/
    unlock/done. The cloud lock is always released, and a lock without a
    claimed file is deleted again.
    """
    def report(phase, percent):
        if on_progress is None:
            return
        try:
            on_progress(phase, max(0.0, min(100.0, float(percent))))
        except Exception:
            pass

    real = _safe_gcode_path(path)
    if real is None:
        raise UploadError("Choose a .gcode file inside the watch folder.")
    name = os.path.basename(real)
    try:
        size = int(os.path.getsize(real))
    except OSError as exc:
        raise UploadError("Could not read the file: %s" % exc)
    if size <= 0:
        raise UploadError("That G-code file is empty.")

    with STATE.lock:
        token = STATE.workbench_token
        logged_in = STATE.is_logged_in()
    if not logged_in or not token:
        raise UploadError("Sign in before uploading.")

    report("lock", 1.0)
    try:
        lock = _http_json(
            "POST", UPLOAD_LOCK_PATH,
            {"size": size, "name": name, "is_temp_file": 0},
            token=token, timeout=30,
        )
    except Exception as exc:
        raise UploadError("Could not reserve cloud storage: %s" % exc)
    if lock.get("code") != 1:
        raise UploadError("Could not reserve cloud storage: %s" % (
            lock.get("msg") or "code %s" % lock.get("code")))

    info = lock.get("data") if isinstance(lock.get("data"), dict) else {}
    lock_id = info.get("id")
    presigned = info.get("preSignUrl") or info.get("url")
    if not lock_id or not isinstance(presigned, str) or not presigned.startswith("http"):
        raise UploadError("Cloud storage did not return an upload URL.")

    file_id = None
    try:
        report("upload", 2.0)
        _put_presigned(
            presigned, real, size,
            lambda pct: report("upload", 2.0 + pct * 0.9),
        )
        report("claim", 93.0)
        try:
            claim = _http_json(
                "POST", UPLOAD_CLAIM_PATH,
                {"user_lock_space_id": lock_id},
                token=token, timeout=60,
            )
        except Exception as exc:
            raise UploadError("The upload finished but registering it failed: %s" % exc)
        if claim.get("code") != 1:
            raise UploadError("The upload finished but registering it failed: %s" % (
                claim.get("msg") or "code %s" % claim.get("code")))
        claimed = claim.get("data") if isinstance(claim.get("data"), dict) else {}
        file_id = claimed.get("id")
        if not file_id:
            raise UploadError("The cloud did not return a file id for the upload.")
    finally:
        report("unlock", 98.0)
        try:
            _http_json(
                "POST", UPLOAD_UNLOCK_PATH,
                {"id": lock_id, "is_delete_cos": 0 if file_id else 1},
                token=token, timeout=30,
            )
        except Exception:
            pass

    report("done", 100.0)
    return {"file_id": int(file_id), "name": name, "size": size}


# ============================================================================
# Start print (order 1, cloud file)
# ============================================================================

def _printer_printing(printer=None):
    """True when the printer reports an active or paused job.

    The cloud's ``is_printing`` is 1 = free, 2 = busy (not a boolean).
    """
    if isinstance(printer, dict):
        value = printer.get("is_printing")
        if value in (2, "2"):
            return True
        if isinstance(value, str) and value.strip().lower() in ("busy", "printing"):
            return True
    with STATE.lock:
        payloads = [STATE.print_payload, STATE.status_payload]
    state = _find(payloads, ["print_status", "state", "status"])
    if state is None or isinstance(state, bool):
        return False
    return str(state).strip().lower() in BUSY_STATES


def print_preflight(refresh=False):
    """Check that a new print may be started. Returns ``(ok, message)``."""
    with STATE.lock:
        printer = dict(STATE.printer or {})
        logged_in = STATE.is_logged_in()
    if not logged_in:
        return False, "Sign in before uploading to print."
    if not printer.get("id"):
        return False, "Select a printer before uploading to print."

    if refresh:
        try:
            fresh = select_printer_by_id(printer["id"])
        except Exception as exc:
            return False, "Could not check the printer: %s" % exc
        if fresh is not None:
            printer = fresh

    if printer.get("device_status") != 1:
        return False, "The printer is offline on Anycubic Cloud. Bring it online before starting a print."
    if _printer_printing(printer):
        return False, "A print is already in progress. This plugin will not interrupt it."
    return True, ""


def _start_print_body(file_id, printer_id):
    return {
        "order_id": 1,
        "printer_id": int(printer_id),
        "project_id": 0,
        "data": {
            "filetype": 0,
            "file_key": "",
            "file_name": "",
            "task_settings": {"ai_detect": 0, "camera_timelapse": 0},
            "file_id": int(file_id),
            "hollow_param": None,
            "is_delete_file": 0,
            "matrix": "",
            "project_type": 1,
            "punching_param": None,
            "slice_param": None,
            "slice_size": None,
            "template_id": 0,
        },
        "ams_info": None,
        "settings": None,
    }


def start_print(file_id, printer_id=None):
    """Start a cloud file with order 1. Returns ``(ok, message)``.

    The printer's current state is re-read first, so a job that started while
    the file was uploading is never interrupted. The cloud occasionally needs
    a moment to index a freshly uploaded file, so only ``No file found`` is
    retried (3 attempts, 3 seconds apart).
    """
    with STATE.lock:
        printer = dict(STATE.printer or {})
        token = STATE.workbench_token
        logged_in = STATE.is_logged_in()
    if not logged_in or not token:
        return False, "Sign in before starting a print."

    wanted = printer_id or printer.get("id")
    if not wanted:
        return False, "Select a printer before starting a print."
    try:
        fresh = select_printer_by_id(wanted)
    except Exception as exc:
        return False, "Could not check the printer: %s" % exc
    if fresh is None:
        return False, "That printer is no longer on this Anycubic account."
    printer = fresh

    if printer.get("device_status") != 1:
        return False, "The printer is offline on Anycubic Cloud."
    if _printer_printing(printer):
        return False, "A print is already in progress. This plugin will not interrupt it."

    name = printer.get("name") or "the printer"
    body = _start_print_body(file_id, printer.get("id"))
    last_message = ""
    for attempt in range(START_PRINT_RETRIES):
        try:
            resp = _http_json("POST", SEND_ORDER_PATH, body, token=token, timeout=30)
        except Exception as exc:
            return False, "Start print request failed: %s" % exc

        if resp.get("code") == 1:
            return True, "Print started on %s." % name
        last_message = str(resp.get("msg") or "code %s" % resp.get("code"))
        if last_message.strip() != "No file found" or attempt == START_PRINT_RETRIES - 1:
            break
        time.sleep(START_PRINT_RETRY_DELAY)

    return False, "The printer rejected the print request: %s" % last_message


# ============================================================================
# Print controls: pause / resume / stop (orders 2 / 3 / 4)
# ============================================================================

_control_lock = threading.Lock()
_control_in_flight = False


def _control_body(action, project_id, printer_id):
    return {
        "order_id": CONTROL_ORDERS[action],
        "printer_id": int(printer_id),
        "project_id": int(project_id),
        "data": None,
        "ams_info": None,
        "settings": None,
    }


def control_print(action, confirmed=False):
    """Pause, resume or stop the active print task. Returns ``(ok, message)``.

    The active project is re-read immediately before the order is sent, so a
    stale dashboard cannot pause or stop a job that has already finished.
    ``confirmed`` must be true: the caller is expected to have shown the user
    an irreversible-action prompt.
    """
    global _control_in_flight

    action = str(action or "").strip().lower()
    if action not in CONTROL_ORDERS:
        return False, "Unknown print control: %s" % action
    if not confirmed:
        return False, "Print controls require confirmation. Nothing was sent."

    with STATE.lock:
        printer = dict(STATE.printer or {})
        token = STATE.workbench_token
        logged_in = STATE.is_logged_in()
    if not logged_in or not token:
        return False, "Sign in before controlling a print."
    printer_id = printer.get("id")
    if not printer_id:
        return False, "Select a printer before controlling a print."
    device_status = _as_int(printer.get("device_status"))
    if device_status is not None and device_status != 1:
        return False, "The printer is offline on Anycubic Cloud."

    with _control_lock:
        if _control_in_flight:
            return False, "Another print control is already being sent."
        _control_in_flight = True
    try:
        try:
            projects = get_projects()
        except Exception as exc:
            return False, "Could not check the current job: %s" % exc

        project = find_active_project(projects, printer_id)
        if project is None:
            return False, "There is no active print job on this printer."
        project_id = _as_int(project.get("id"))
        if project_id is None:
            return False, "The cloud did not return a project id for the active job."

        status = _project_status(project)
        paused = _project_paused(project)
        if action in ("pause", "resume") and status != PAUSE_RESUME_PROJECT_STATE:
            return False, "Pause and resume are only available while the printer is printing."
        if action == "pause" and paused:
            return False, "The print is already paused."
        if action == "resume" and not paused:
            return False, "The print is not paused."

        body = _control_body(action, project_id, printer_id)
        try:
            resp = _http_json("POST", SEND_ORDER_PATH, body, token=token, timeout=30)
        except Exception as exc:
            return False, "%s request failed: %s" % (CONTROL_VERBS[action], exc)
        if resp.get("code") != 1:
            return False, "The printer rejected the %s request: %s" % (
                action, resp.get("msg") or "code %s" % resp.get("code"))

        # Patch the cache immediately so the dashboard reflects the command
        # without waiting for the next HTTP refresh.
        with STATE.lock:
            current = STATE.project
            if action == "stop":
                STATE.project = None
                STATE.project_ts = _now()
            elif isinstance(current, dict) and _as_int(current.get("id")) == project_id:
                current = dict(current)
                current["pause"] = 1 if action == "pause" else 0
                STATE.project = current
                STATE.project_ts = _now()
            STATE.project_error = ""

        name = printer.get("name") or "the printer"
        messages = {
            "pause": "Pause requested for %s. The printer will pause the job." % name,
            "resume": "Resume requested for %s. The printer will continue the job." % name,
            "stop": "Stop requested for %s. The job will be cancelled." % name,
        }
        return True, messages[action]
    finally:
        with _control_lock:
            _control_in_flight = False


def build_status():
    with STATE.lock:
        printer = dict(STATE.printer or {})
        status_payload = STATE.status_payload
        print_payload = STATE.print_payload
        logged_in = STATE.is_logged_in()
        email = STATE.email
        monitoring = STATE.monitoring
        connected = STATE.mqtt_connected
        mqtt_error = STATE.mqtt_error
        last_message = STATE.last_message_ts
        project = dict(STATE.project) if isinstance(STATE.project, dict) else None
        project_ts = STATE.project_ts
        project_error = STATE.project_error
        camera_state = STATE.camera_state
        camera_error = STATE.camera_error
        camera_started = STATE.camera_started
        video_ts = STATE.video_ts

    payloads = [status_payload, print_payload]
    # Fall back to the temps the cloud recorded in the printer list, so the
    # dashboard is not empty before the first status/report arrives.
    parameter = printer.get("parameter")
    temp_payloads = payloads + [parameter if isinstance(parameter, dict) else None]
    notice, notice_error = STATE.take_notice()
    watch = watch_snapshot()
    job_active = _project_active(project)
    job_paused = _project_paused(project)
    job_status = _project_status(project)
    job_settings = _project_settings(project)
    printing = _printer_printing(printer) or job_active
    with STATE.lock:
        upload = dict(STATE.upload) if isinstance(STATE.upload, dict) else None

    def one(keys, from_=None):
        return _find(from_ if from_ is not None else payloads, keys)

    nozzle = _as_float(one(["curr_nozzle_temp", "nozzle_temp", "nozzle_temperature"], temp_payloads))
    nozzle_target = _as_float(one(["target_nozzle_temp", "nozzle_target_temp", "target_nozzle"], temp_payloads))
    bed = _as_float(one(["curr_hotbed_temp", "bed_temp", "hotbed_temp"], temp_payloads))
    bed_target = _as_float(one(["target_hotbed_temp", "bed_target_temp", "target_hotbed"], temp_payloads))

    progress = _progress_percent(payloads)
    layer = one(["curr_layer", "current_layer", "layer", "layer_num"])
    print_state = one(["print_status", "state", "status", "device_status"])
    filename = one(["filename", "file_name", "gcode_name", "task_name", "model"])
    remaining = one(["remain_time", "remaining_time", "print_remain_time", "left_time"])

    job_file = (project.get("gcode_name") or project.get("name")
                or project.get("file_name") or "") if project else ""
    job_progress = _as_float(project.get("progress")) if project else None
    job_remaining = _as_float(project.get("remain_time")) if project else None
    if job_remaining is None or job_remaining <= 0:
        job_remaining = _as_float(job_settings.get("remain_time"))
    if job_remaining is not None and job_remaining <= 0:
        job_remaining = None

    # The HTTP project record fills gaps in the MQTT snapshot. MQTT values win
    # when both are present because status/report is the printer's own view.
    if progress is None and job_progress is not None:
        progress = round(job_progress, 1)
    if not filename and job_file:
        filename = job_file
    if remaining is None and job_remaining is not None:
        remaining = job_remaining
    if layer is None and project:
        job_layer = _as_int(job_settings.get("curr_layer"))
        if job_layer:
            layer = job_layer
    if job_paused:
        print_state = "paused"
    elif print_state is None and job_status is not None:
        print_state = {
            1: "printing", 4: "downloading", 5: "checking",
            6: "preheating", 7: "slicing",
        }.get(job_status, job_status)

    age = round(_now() - last_message, 1) if last_message else None
    # status/report is a ~5 minute heartbeat, not a stream; anything older than
    # two intervals is stale and must not be presented as live (design §3.5).
    stale = age is None or age > STALE_AFTER

    last_seen = printer.get("last_update_time")
    last_seen = round(last_seen / 1000.0, 0) if isinstance(last_seen, (int, float)) and last_seen else None

    online = printer.get("device_status") == 1
    control_busy = bool(_control_in_flight)
    can_control = bool(logged_in and online and job_active and not control_busy)
    project_age = round(_now() - project_ts, 1) if project_ts else None
    camera_age = round(_now() - camera_started, 1) if camera_started else None
    video_age = round(_now() - video_ts, 1) if video_ts else None

    raw = {}
    for name, payload in (("status", status_payload), ("print", print_payload)):
        if payload is None:
            continue
        try:
            text = json.dumps(payload, ensure_ascii=False)
        except Exception:
            text = str(payload)
        raw[name] = text[:3000]

    return {
        "type": "status",
        "login": logged_in,
        "email": email or "",
        "monitoring": monitoring,
        "printer": {
            "name": printer.get("name") or "",
            "model": printer.get("model") or "",
            "online": printer.get("device_status") == 1,
            "reason": printer.get("reason") or "",
            "last_seen": last_seen,
        },
        "mqtt": {
            "connected": connected,
            "error": mqtt_error,
            "last_message": age,
            "stale": stale,
        },
        "temps": {
            "nozzle": nozzle,
            "nozzle_target": nozzle_target,
            "bed": bed,
            "bed_target": bed_target,
        },
        "print": {
            "state": print_state,
            "progress": progress,
            "layer": layer,
            "file": filename,
            "remaining": remaining,
        },
        "printing": printing,
        "job": {
            "known": project is not None,
            "id": project.get("id") if project else None,
            "file": job_file,
            "active": job_active,
            "paused": job_paused,
            "status": job_status,
            "progress": job_progress,
            "remaining": job_remaining,
            "age": project_age,
            "error": project_error,
            "busy": control_busy,
            "can_pause": can_control and job_status == PAUSE_RESUME_PROJECT_STATE and not job_paused,
            "can_resume": can_control and job_paused,
            "can_stop": can_control,
        },
        "watch": {
            "dir": watch["path"],
            "error": watch["error"],
            "allowed": watch["allowed"],
            "files": watch["files"],
            "scanning": watch["scanning"],
            "stalled": watch["stalled"],
            "status": watch["status"],
            "scan_age": watch["scan_age"],
        },
        "camera": {
            "state": camera_state,
            "error": camera_error,
            "available": bool(logged_in and online and monitoring),
            "age": camera_age,
            "last_report": video_age,
        },
        "upload": upload,
        "notice": notice,
        "notice_error": notice_error,
        "raw": raw,
    }


def start_monitoring():
    with STATE.lock:
        if not STATE.is_logged_in():
            return False, "Sign in first: run the 'Anycubic Login' script or paste a token in the Config tab."
        if not STATE.printer:
            return False, "Select a printer first: run the 'Select Printer' script or use the dashboard button."
        existing = STATE.mqtt_service
    if existing is not None and existing.running:
        return True, "Monitoring already running."
    service = MqttService()
    with STATE.lock:
        STATE.mqtt_service = service
    return service.start()


def stop_monitoring():
    with STATE.lock:
        service = STATE.mqtt_service
    if service is None:
        return True, "Monitoring is not running."
    return service.stop()


# ============================================================================
# Camera: order 1001 / MQTT startCapture / KVS WebRTC viewer
# ============================================================================

_webcam_lock = threading.RLock()
_webcam_session = None
_webcam_in_flight = False
_webcam_stops = 0
_webcam_generation = 0


def camera_open(printer_id=None):
    """Open the printer camera and return its temporary AWS credentials.

    Returns ``(session, error)``. The order is read-only with respect to the
    print: it only makes the camera available to a KVS viewer.
    """
    with STATE.lock:
        token = STATE.workbench_token
        printer = dict(STATE.printer or {})
    if not token:
        return None, "Sign in first: the camera needs a cloud session."
    try:
        pid = int(printer_id or printer.get("id") or 0)
    except Exception:
        pid = 0
    if not pid:
        return None, "Select a printer first."
    try:
        response = _http_json(
            "POST", SEND_ORDER_PATH,
            {"order_id": CAMERA_OPEN_ORDER, "printer_id": pid},
            token=token, timeout=30,
        )
    except Exception as exc:
        return None, "Camera request failed: %s" % exc
    if response.get("code") != 1:
        return None, "The printer rejected the camera request: %s" % (
            response.get("msg") or "unknown error")
    data = response.get("data") or {}
    try:
        creds = _camera_credentials(data.get("token") or {})
    except Exception as exc:
        return None, str(exc)
    return {
        "printer_id": pid,
        "msgid": data.get("msgid") or "",
        "credentials": creds,
    }, ""


def camera_close(printer_id=None, timeout=20):
    """Close the printer camera session (order 1002). Best effort."""
    with STATE.lock:
        token = STATE.workbench_token
        printer = dict(STATE.printer or {})
    try:
        pid = int(printer_id or printer.get("id") or 0)
    except Exception:
        pid = 0
    if not token or not pid:
        return False, "No camera session to close."
    try:
        response = _http_json(
            "POST", SEND_ORDER_PATH,
            {"order_id": CAMERA_CLOSE_ORDER, "printer_id": pid},
            token=token, timeout=timeout,
        )
    except Exception as exc:
        return False, "Could not close the camera: %s" % exc
    if response.get("code") != 1:
        return False, "Could not close the camera: %s" % (
            response.get("msg") or "unknown error")
    return True, "Camera closed."


def webcam_start():
    """Open the camera, ask it to push, and return a viewer configuration.

    Returns ``(ok, message, config)``. ``config`` contains the presigned WSS
    URL and STUN servers for the dashboard; the AWS secret key never leaves
    the plugin process.
    """
    global _webcam_in_flight, _webcam_session, _webcam_generation

    with _webcam_lock:
        if _webcam_in_flight:
            return False, "The camera is already starting.", None
        _webcam_in_flight = True

    try:
        # A stop releases the printer camera with order 1002. Do not open a
        # new session until that request has finished, or the old close could
        # arrive after the new open and kill the new stream.
        deadline = time.monotonic() + 15.0
        while True:
            with _webcam_lock:
                stopping = _webcam_stops > 0
            if not stopping:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "The camera is still stopping. Try again in a moment.")
            time.sleep(0.1)

        with _webcam_lock:
            _webcam_generation += 1
            generation = _webcam_generation
            previous = _webcam_session
            _webcam_session = None

        with STATE.lock:
            logged_in = STATE.is_logged_in()
            printer = dict(STATE.printer or {})
            service = STATE.mqtt_service
            mqtt_connected = STATE.mqtt_connected
            state = STATE.camera_state
        if not logged_in:
            raise RuntimeError(
                "Sign in first: the camera needs a cloud session.")
        if not printer.get("id") or not printer.get("key"):
            raise RuntimeError("Select a printer first.")
        if service is None or not service.running:
            raise RuntimeError(
                "Start monitoring first. The camera needs the MQTT "
                "connection to ask the printer for video.")
        if not mqtt_connected:
            raise RuntimeError(
                "MQTT is not connected yet. Wait for the dashboard to go "
                "live, then start the camera.")

        with STATE.lock:
            STATE.camera_state = "starting"
            STATE.camera_error = ""
            STATE.camera_started = 0.0

        # Reconnecting replaces any previous camera session cleanly.
        if previous is not None or state in ("starting", "connecting", "live"):
            try:
                service.publish_video("stopCapture")
            except Exception:
                pass
            try:
                camera_close(printer.get("id"), timeout=10)
            except Exception:
                pass

        opened, error = camera_open(printer.get("id"))
        if not opened:
            raise RuntimeError(error)
        creds = opened["credentials"]
        try:
            channel_arn = _kvs_channel_arn(printer.get("key"), creds)
            endpoint = _kvs_wss_endpoint(channel_arn, creds)
            client_id = "viewer-" + uuid.uuid4().hex
            wss_url = _aws_presign_url(
                endpoint,
                {"X-Amz-ChannelARN": channel_arn,
                 "X-Amz-ClientId": client_id},
                creds["region"], KVS_SERVICE, creds,
            )
        except Exception:
            try:
                camera_close(printer.get("id"))
            except Exception:
                pass
            raise

        with _webcam_lock:
            cancelled = generation != _webcam_generation
        if cancelled:
            try:
                camera_close(printer.get("id"))
            except Exception:
                pass
            raise RuntimeError("Camera start was cancelled.")

        ok, message = service.publish_video("startCapture")
        if not ok:
            try:
                camera_close(printer.get("id"))
            except Exception:
                pass
            raise RuntimeError(
                "Could not ask the printer to start streaming: %s" % message)

        with _webcam_lock:
            cancelled = generation != _webcam_generation
            if not cancelled:
                _webcam_session = {
                    "printer_id": opened["printer_id"],
                    "order_msgid": opened["msgid"],
                    "started": _now(),
                    "channel_arn": channel_arn,
                    "client_id": client_id,
                }
                # Publish "connecting" while still holding the start lock so
                # a concurrent webcam_stop() cannot leave a stale state behind.
                with STATE.lock:
                    STATE.camera_state = "connecting"
                    STATE.camera_started = _now()
                    STATE.camera_error = ""
        if cancelled:
            try:
                service.publish_video("stopCapture")
            except Exception:
                pass
            try:
                camera_close(printer.get("id"))
            except Exception:
                pass
            raise RuntimeError("Camera start was cancelled.")

        return True, "Camera opened. Waiting for the first frame…", {
            "wss_url": wss_url,
            "ice_servers": [dict(item) for item in KVS_STUN_SERVERS],
            "expires_in": KVS_WSS_EXPIRES,
            "first_frame_timeout": int(CAMERA_FIRST_FRAME_TIMEOUT * 1000),
            "client_id": client_id,
        }
    except Exception as exc:
        with _webcam_lock:
            current = generation == _webcam_generation
            if current:
                _webcam_session = None
        if current:
            with STATE.lock:
                STATE.camera_state = "error"
                STATE.camera_error = str(exc)
                STATE.camera_started = 0.0
        return False, str(exc), None
    finally:
        with _webcam_lock:
            _webcam_in_flight = False


def webcam_stop():
    """Stop the video push and close the camera session. Idempotent."""
    global _webcam_session, _webcam_generation, _webcam_stops

    with _webcam_lock:
        _webcam_generation += 1
        generation = _webcam_generation
        _webcam_stops += 1
        session = _webcam_session
        _webcam_session = None
    try:
        with STATE.lock:
            printer = dict(STATE.printer or {})
            service = STATE.mqtt_service
            active = STATE.camera_state in ("starting", "connecting", "live")

        if not active and session is None:
            # Nothing to stop, but still make sure no stale camera error is
            # left behind after a failed start.
            with _webcam_lock:
                current = generation == _webcam_generation
            if current:
                with STATE.lock:
                    STATE.camera_state = "idle"
                    STATE.camera_error = ""
                    STATE.camera_started = 0.0
            return True, "The camera is not running."

        if service is not None and service.running:
            try:
                service.publish_video("stopCapture")
            except Exception:
                pass
        if printer.get("id"):
            try:
                camera_close(printer.get("id"), timeout=10)
            except Exception:
                pass

        with _webcam_lock:
            current = generation == _webcam_generation
        if current:
            with STATE.lock:
                STATE.camera_state = "idle"
                STATE.camera_error = ""
                STATE.camera_started = 0.0
        return True, "Camera stopped."
    finally:
        with _webcam_lock:
            _webcam_stops -= 1


def webcam_mark_viewing():
    """The dashboard decoded the first video frame."""
    with STATE.lock:
        if STATE.camera_state in ("starting", "connecting", "live"):
            STATE.camera_state = "live"
            STATE.camera_error = ""
            if not STATE.camera_started:
                STATE.camera_started = _now()


def webcam_active():
    with STATE.lock:
        return STATE.camera_state in ("starting", "connecting", "live")


# ============================================================================
# HTML pages
# ============================================================================

DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body { font-family: var(--orca-font, system-ui); margin: 0; color: var(--orca-fg, #222); background: var(--orca-bg, #fff); }
  .wrap { padding: 18px; max-width: 980px; }
  .row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
  .spacer { flex: 1; }
  h1 { font-size: 20px; margin: 0; }
  .muted { color: var(--orca-muted, #777); font-size: 13px; }
  .badge { padding: 3px 10px; border-radius: 11px; font-size: 12px; font-weight: 600; }
  .badge.ok { background: rgba(0,150,136,.15); color: #009688; }
  .badge.off { background: rgba(120,120,120,.15); color: var(--orca-muted, #777); }
  .badge.err { background: rgba(244,67,54,.15); color: #e53935; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-top: 16px; }
  .card { border: 1px solid var(--orca-border, #e3e3e3); border-radius: 10px; padding: 14px; background: var(--orca-bg, #fff); }
  .value { font-size: 26px; font-weight: 650; }
  .label { font-size: 11px; letter-spacing: .05em; text-transform: uppercase; color: var(--orca-muted, #777); margin-top: 4px; }
  .section { margin-top: 22px; font-size: 13px; font-weight: 650; text-transform: uppercase; letter-spacing: .05em; color: var(--orca-muted, #777); }
  button { padding: 7px 14px; border-radius: 7px; border: 1px solid var(--orca-border, #d5d5d5); background: var(--orca-bg, #fff); color: var(--orca-fg, #222); cursor: pointer; font-size: 13px; }
  button.primary { background: var(--orca-accent, #009688); border-color: var(--orca-accent, #009688); color: var(--orca-accent-fg, #fff); }
  button.danger { border-color: rgba(229,57,53,.6); color: #c62828; }
  button.danger-solid { background: #e53935; border-color: #e53935; color: #fff; }
  button.danger-solid:disabled { opacity: .5; }
  button:disabled { opacity: .5; cursor: default; }
  .bar { height: 10px; border-radius: 5px; background: var(--orca-border, #e8e8e8); overflow: hidden; margin-top: 8px; }
  .bar > div { height: 100%; width: 0%; background: var(--orca-accent, #009688); transition: width .3s ease; }
  .file { display: flex; align-items: center; gap: 10px; padding: 8px 0; border-bottom: 1px solid var(--orca-border, #eee); }
  .file:last-child { border-bottom: 0; }
  .file-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .file-meta { color: var(--orca-muted, #777); font-size: 12px; white-space: nowrap; }
  .file button { padding: 5px 10px; font-size: 12px; }
  .confirm { margin-top: 12px; padding: 12px; border: 1px solid rgba(255,152,0,.55); border-radius: 8px;
             background: rgba(255,152,0,.08); font-size: 13px; line-height: 1.45; }
  .confirm.stop { border-color: rgba(244,67,54,.55); background: rgba(244,67,54,.08); }
  .cam-wrap { position: relative; background: #000; border-radius: 8px; overflow: hidden;
              aspect-ratio: 4 / 3; max-height: 420px; display: flex; align-items: center; justify-content: center; }
  #camVideo { width: 100%; height: 100%; object-fit: contain; display: block; }
  .cam-placeholder { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
                     text-align: center; padding: 20px; color: #bbb; font-size: 13px; line-height: 1.45; }
  #camError { margin-top: 8px; padding: 9px 11px; border-radius: 7px; font-size: 12px;
              background: rgba(244,67,54,.1); color: #c62828; display: none; line-height: 1.4; }
  #notice { margin-top: 14px; padding: 10px 12px; border-radius: 8px; font-size: 13px; display: none; }
  #notice.info { display: block; background: rgba(0,150,136,.1); color: #00796b; }
  #notice.error { display: block; background: rgba(244,67,54,.1); color: #c62828; }
  details { margin-top: 18px; }
  pre { font-size: 11px; max-height: 220px; overflow: auto; background: var(--orca-border, #f5f5f5); padding: 10px; border-radius: 8px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="row">
    <div>
      <h1 id="printerName">No printer selected</h1>
      <div class="muted" id="printerModel"></div>
    </div>
    <div class="spacer"></div>
    <span id="conn" class="badge off">Offline</span>
  </div>

  <div class="row" style="margin-top:14px">
    <span class="muted" id="loginState">Checking sign-in…</span>
    <div class="spacer"></div>
    <button id="btnSelect">Select printer</button>
    <button id="btnStart" class="primary">Start monitoring</button>
    <button id="btnStop">Stop monitoring</button>
  </div>

  <div id="notice"></div>

  <div id="picker" class="card" style="margin-top:14px; display:none">
    <div class="row">
      <b>Choose a printer</b>
      <div class="spacer"></div>
      <button id="pickerClose">Close</button>
    </div>
    <div id="pickerList" style="margin-top:10px"></div>
  </div>

  <div class="section">Temperatures</div>
  <div class="cards">
    <div class="card">
      <div class="value"><span id="nozzle">—</span>°C</div>
      <div class="label">Nozzle</div>
      <div class="muted" style="margin-top:6px">Target <span id="nozzleTarget">—</span>°C</div>
    </div>
    <div class="card">
      <div class="value"><span id="bed">—</span>°C</div>
      <div class="label">Bed</div>
      <div class="muted" style="margin-top:6px">Target <span id="bedTarget">—</span>°C</div>
    </div>
    <div class="card">
      <div class="value" id="printState">—</div>
      <div class="label">Print state</div>
      <div class="muted" style="margin-top:6px">Layer <span id="layer">—</span></div>
    </div>
    <div class="card">
      <div class="value"><span id="progress">—</span>%</div>
      <div class="label">Progress</div>
      <div class="bar"><div id="bar"></div></div>
    </div>
  </div>

  <div class="section">Current job</div>
  <div class="card" style="margin-top:10px">
    <div class="row"><span class="muted">File</span><span id="file">—</span></div>
    <div class="row" style="margin-top:6px"><span class="muted">Remaining</span><span id="remaining">—</span></div>
    <div class="row" style="margin-top:6px"><span class="muted">Last data from printer</span><span id="lastMsg">—</span></div>
    <div class="row" style="margin-top:12px">
      <button id="btnPause">Pause</button>
      <button id="btnResume">Resume</button>
      <button id="btnStopPrint" class="danger">Stop</button>
      <span class="muted" id="jobHint">Controls activate while a job is running.</span>
    </div>
    <div id="controlConfirm" class="confirm" style="display:none">
      <div id="controlConfirmText"></div>
      <div class="row" style="margin-top:10px">
        <div class="spacer"></div>
        <button id="controlConfirmCancel">Cancel</button>
        <button id="controlConfirmGo" class="danger-solid">Stop print</button>
      </div>
    </div>
  </div>

  <div class="section">Camera</div>
  <div class="card" style="margin-top:10px">
    <div class="row">
      <b>Live view</b>
      <span id="camBadge" class="badge off">Camera off</span>
      <div class="spacer"></div>
      <button id="btnCamStart" class="primary">Start camera</button>
      <button id="btnCamReconnect">Reconnect</button>
      <button id="btnCamStop">Stop camera</button>
    </div>
    <div class="cam-wrap" style="margin-top:10px">
      <video id="camVideo" autoplay muted playsinline></video>
      <div id="camPlaceholder" class="cam-placeholder">Camera is off. Start the camera to view the live stream.</div>
    </div>
    <div class="row" style="margin-top:8px">
      <span class="muted" id="camStats"></span>
      <div class="spacer"></div>
      <span class="muted" id="camHint">Live view uses the printer's cloud camera and does not affect the print.</span>
    </div>
    <div id="camError"></div>
  </div>

  <div class="section">Print from folder</div>
  <div class="card" style="margin-top:10px">
    <div class="row">
      <div style="min-width:0">
        <div><b id="watchDir">—</b></div>
        <div class="muted" id="watchHint">Newest .gcode files in the watch folder.</div>
      </div>
      <div class="spacer"></div>
      <button id="btnRefreshFiles">Refresh</button>
    </div>
    <div id="fileList" style="margin-top:8px"><div class="muted">Loading files…</div></div>

    <div id="uploadBox" style="display:none; margin-top:12px">
      <div class="row">
        <span class="muted" id="uploadPhase">Uploading…</span>
        <div class="spacer"></div>
        <span class="muted" id="uploadPct">0%</span>
      </div>
      <div class="bar"><div id="uploadBar"></div></div>
    </div>

    <div id="confirmBox" class="confirm" style="display:none">
      <div id="confirmText"></div>
      <div class="row" style="margin-top:10px">
        <div class="spacer"></div>
        <button id="confirmCancel">Cancel</button>
        <button id="confirmGo" class="primary">Upload &amp; start print</button>
      </div>
    </div>
  </div>

  <div id="signinBlock">
    <div class="section">Sign-in &amp; configuration</div>
    <div class="card" style="margin-top:10px">
      <div class="muted">
        Paste the RS256 access token in this plugin's <b>Config</b> tab
        (File &gt; Plugins &gt; Anycubic Cloud &gt; Config), or run the
        <b>Anycubic Login</b> script from the Plugins dialog.
      </div>
    </div>
  </div>

  <details>
    <summary class="muted">Raw MQTT payloads</summary>
    <pre id="raw">—</pre>
  </details>
</div>

<script>
(function () {
  var B = window.orca;
  var lastStatus = null;
  var fileRows = [];
  var filesSig = null;
  var pending = null;
  var pendingControl = null;
  var currentUpload = null;
  var camPC = null;
  var camWS = null;
  var camTimer = null;
  var camState = "idle";
  var camPendingCandidates = [];

  function $(id) { return document.getElementById(id); }
  function set(id, value) {
    var el = $(id);
    if (!el) return;
    el.textContent = (value === null || value === undefined || value === "") ? "—" : value;
  }
  function showNotice(text, isError) {
    var el = $("notice");
    if (!text) { el.style.display = "none"; el.className = ""; el.textContent = ""; return; }
    el.textContent = text;
    el.className = isError ? "error" : "info";
    el.style.display = "block";
  }
  function age(sec) {
    if (sec === null || sec === undefined) return "—";
    if (sec < 90) return Math.round(sec) + "s ago";
    if (sec < 5400) return Math.round(sec / 60) + " min ago";
    return Math.round(sec / 3600) + " h ago";
  }
  function uploadActive() {
    return !!(currentUpload && !currentUpload.done);
  }
  function updateFileButtons() {
    var d = lastStatus || {};
    var hasPrinter = !!(d.printer && d.printer.name);
    var online = !!(d.printer && d.printer.online);
    var busy = !!(d.printing || uploadActive());
    var canUpload = !!d.login && !uploadActive();
    var canPrint = !!(d.login && hasPrinter && online && !d.printing) && !uploadActive();
    fileRows.forEach(function (row) {
      row.upload.disabled = !canUpload;
      row.print.disabled = !canPrint;
      row.print.title = !d.login ? "Sign in first"
        : !hasPrinter ? "Select a printer first"
        : !online ? "The printer is offline on Anycubic Cloud"
        : d.printing ? "A print is already in progress"
        : uploadActive() ? "An upload is already in progress" : "";
      row.upload.title = !d.login ? "Sign in first"
        : uploadActive() ? "An upload is already in progress" : "";
    });
    $("confirmGo").disabled = !canPrint || !pending;
    if (busy && pending && d.printing) {
      $("confirmGo").title = "A print is already in progress";
    } else {
      $("confirmGo").title = "";
    }
    updateJobControls();
  }
  function updateJobControls() {
    var d = lastStatus || {};
    var job = d.job || {};
    var busy = !!job.busy;
    $("btnPause").disabled = !(job.can_pause && !busy);
    $("btnResume").disabled = !(job.can_resume && !busy);
    $("btnStopPrint").disabled = !(job.can_stop && !busy);
    var hint;
    if (!d.login) {
      hint = "Sign in to use print controls.";
    } else if (!d.printer || !d.printer.name) {
      hint = "Select a printer first.";
    } else if (!d.printer.online) {
      hint = "The printer is offline on Anycubic Cloud.";
    } else if (busy) {
      hint = "Sending command…";
    } else if (job.active) {
      hint = job.paused ? "The printer reports this job as paused."
        : "Active job #" + (job.id === null || job.id === undefined ? "—" : job.id);
      if (job.error) hint += " · " + job.error;
    } else if (job.error) {
      hint = "Job status unavailable: " + job.error;
    } else {
      hint = "Controls activate while a job is running.";
    }
    $("jobHint").textContent = hint;
    if (pendingControl) {
      var stillAllowed = pendingControl === "pause" ? job.can_pause
        : pendingControl === "resume" ? job.can_resume : job.can_stop;
      if (!job.active || busy || !stillAllowed) hideControlConfirm();
    }
  }
  function showControlConfirm(action) {
    var d = lastStatus || {};
    var job = d.job || {};
    var allowed = action === "pause" ? job.can_pause
      : action === "resume" ? job.can_resume : job.can_stop;
    if (!job.active || job.busy || !allowed) return;
    pendingControl = action;
    var file = job.file || (d.print && d.print.file) || "the current job";
    var printer = (d.printer && d.printer.name) || "the printer";
    var text, label;
    if (action === "stop") {
      text = "Stop “" + file + "” on " + printer + " now? " +
        "The job will be cancelled and cannot be resumed.";
      label = "Stop print";
    } else if (action === "pause") {
      text = "Pause “" + file + "” on " + printer + "? " +
        "The printer will stop moving until you resume it.";
      label = "Pause print";
    } else {
      text = "Resume “" + file + "” on " + printer + "?";
      label = "Resume print";
    }
    $("controlConfirmText").textContent = text;
    var go = $("controlConfirmGo");
    go.textContent = label;
    go.className = action === "stop" ? "danger-solid" : "primary";
    var box = $("controlConfirm");
    box.className = action === "stop" ? "confirm stop" : "confirm";
    box.style.display = "block";
  }
  function hideControlConfirm() {
    pendingControl = null;
    $("controlConfirm").style.display = "none";
  }
  function renderFiles(watch) {
    watch = watch || {};
    var dir = watch.dir || "";
    var scanning = !!watch.scanning;
    var stalled = !!watch.stalled;
    $("watchDir").textContent = dir || "—";
    $("watchDir").title = dir || "";
    var hint = $("watchHint");
    if (stalled) {
      hint.textContent = watch.error ||
        "The watch-folder scan is stuck. macOS may be waiting for file-access permission.";
      hint.style.color = "#c62828";
    } else if (watch.error) {
      hint.textContent = watch.error;
      hint.style.color = "#c62828";
    } else if (scanning) {
      hint.textContent = "Scanning the watch folder…";
      hint.style.color = "";
    } else if (watch.status === "idle") {
      hint.textContent = "Waiting for the first watch-folder scan…";
      hint.style.color = "";
    } else if (dir) {
      var updated = (watch.scan_age === null || watch.scan_age === undefined)
        ? "" : " Updated " + age(watch.scan_age) + ".";
      hint.textContent = "Newest .gcode files in the watch folder." + updated;
      hint.style.color = "";
    }
    $("btnRefreshFiles").disabled = scanning;

    var items = watch.files || [];
    var sig = items.map(function (f) {
      return f.name + "|" + f.size + "|" + f.mtime;
    }).join("\\n");
    sig += "|s=" + (scanning ? 1 : 0);
    if (sig === filesSig) {
      updateFileButtons();
      return;
    }
    // While a scan is running keep the last good rows visible instead of
    // flashing "no files" every time the watcher starts a pass.
    if (scanning && !items.length && fileRows.length) {
      filesSig = sig;
      updateFileButtons();
      return;
    }
    filesSig = sig;
    var list = $("fileList");
    list.innerHTML = "";
    fileRows = [];
    if (!items.length) {
      list.innerHTML = scanning
        ? '<div class="muted">Scanning the watch folder…</div>'
        : watch.status === "idle"
          ? '<div class="muted">Waiting for the first watch-folder scan…</div>'
          : '<div class="muted">No .gcode files in this folder yet.</div>';
      return;
    }
    items.forEach(function (f) {
      var row = document.createElement("div");
      row.className = "file";

      var name = document.createElement("div");
      name.className = "file-name";
      name.textContent = f.name;
      name.title = f.path || f.name;

      var meta = document.createElement("div");
      meta.className = "file-meta";
      meta.textContent = (f.size_text || "") + " · " + age(f.age);

      var upload = document.createElement("button");
      upload.textContent = "Upload";
      upload.onclick = function () {
        B.postMessage({ type: "upload", path: f.path, start: false });
      };

      var print = document.createElement("button");
      print.className = "primary";
      print.textContent = "Upload + print";
      print.onclick = function () { showConfirm(f); };

      row.appendChild(name);
      row.appendChild(meta);
      row.appendChild(upload);
      row.appendChild(print);
      list.appendChild(row);
      fileRows.push({ item: f, upload: upload, print: print });
    });
    updateFileButtons();
  }
  function renderUpload(up) {
    currentUpload = up || null;
    var box = $("uploadBox");
    if (!up || (!up.phase && !up.done)) {
      box.style.display = "none";
      updateFileButtons();
      return;
    }
    box.style.display = "block";
    var pct = (up.percent === null || up.percent === undefined) ? 0 : up.percent;
    pct = Math.max(0, Math.min(100, pct));
    $("uploadBar").style.width = (up.done && up.ok !== false ? 100 : pct) + "%";
    $("uploadBar").style.background = up.phase === "error" ? "#e53935" : "";
    $("uploadPct").textContent = Math.round(up.done && up.ok !== false ? 100 : pct) + "%";
    var labels = {
      lock: "Reserving cloud storage",
      upload: "Uploading G-code",
      claim: "Registering cloud file",
      unlock: "Finishing upload",
      start: "Starting print",
      done: "Upload complete",
      error: "Upload failed"
    };
    var text = labels[up.phase] || "Working";
    if (up.name) text += " — " + up.name;
    $("uploadPhase").textContent = text;
    updateFileButtons();
  }
  function showConfirm(item) {
    var d = lastStatus || {};
    if (!item || !d.login) return;
    pending = item;
    var name = (d.printer && d.printer.name) || "the selected printer";
    $("confirmText").textContent =
      "Upload “" + item.name + "” and start printing on " + name + " now? " +
      "The printer starts immediately. You can pause or stop it from this " +
      "dashboard after it starts.";
    $("confirmBox").style.display = "block";
    updateFileButtons();
  }
  function hideConfirm() {
    pending = null;
    $("confirmBox").style.display = "none";
  }
  function renderPrinters(d) {
    var box = $("picker");
    box.style.display = "block";
    var list = $("pickerList");
    list.innerHTML = "";
    if (d.error) {
      list.innerHTML = '<div class="muted">' + d.error.replace(/</g, "&lt;") + "</div>";
      return;
    }
    if (!d.items || !d.items.length) {
      list.innerHTML = '<div class="muted">No FDM printers on this Anycubic account.</div>';
      return;
    }
    d.items.forEach(function (p) {
      var row = document.createElement("div");
      row.className = "row";
      row.style.cssText = "padding:8px;border:1px solid var(--orca-border,#e3e3e3);border-radius:8px;margin-bottom:6px;cursor:pointer";
      var badge = p.online
        ? '<span class="badge ok">Online</span>'
        : '<span class="badge off">Offline</span>';
      row.innerHTML = '<b>' + (p.name || "Printer").replace(/</g, "&lt;") + "</b>" +
                      '<span class="muted">' + (p.model || "") + "</span>" +
                      '<span class="spacer"></span>' + badge;
      row.onclick = function () {
        list.innerHTML = '<div class="muted">Selecting…</div>';
        B.postMessage({ type: "pick_printer", id: p.id });
      };
      list.appendChild(row);
    });
  }
  function camSetBadge(text, cls) {
    var badge = $("camBadge");
    badge.textContent = text;
    badge.className = "badge " + (cls || "off");
  }
  function camCleanup() {
    if (camTimer) { clearTimeout(camTimer); camTimer = null; }
    var pc = camPC;
    camPC = null;
    if (pc) { try { pc.close(); } catch (e) {} }
    var ws = camWS;
    camWS = null;
    if (ws) { try { ws.close(); } catch (e) {} }
    camPendingCandidates = [];
    var video = $("camVideo");
    try { video.pause(); } catch (e) {}
    try { video.srcObject = null; } catch (e) {}
  }
  function camIdle(text) {
    camState = "idle";
    camSetBadge("Camera off", "off");
    $("camPlaceholder").textContent =
      text || "Camera is off. Start the camera to view the live stream.";
    $("camPlaceholder").style.display = "flex";
    $("camStats").textContent = "";
    updateCameraControls();
  }
  function camFail(message) {
    var wasError = camState === "error";
    camCleanup();
    camState = "error";
    camSetBadge("Camera error", "err");
    $("camPlaceholder").textContent = "Camera unavailable.";
    $("camPlaceholder").style.display = "flex";
    $("camError").textContent = message || "The camera stream could not be opened.";
    $("camError").style.display = "block";
    updateCameraControls();
    if (!wasError) B.postMessage({ type: "webcam_failed" });
  }
  function camStart() {
    if (camState === "starting" || camState === "connecting") return;
    camCleanup();
    camState = "starting";
    camSetBadge("Opening camera…", "off");
    $("camPlaceholder").textContent = "Opening the printer camera…";
    $("camPlaceholder").style.display = "flex";
    $("camError").style.display = "none";
    $("camStats").textContent = "";
    updateCameraControls();
    B.postMessage({ type: "webcam_start" });
  }
  function camStop(silent) {
    var wasActive = camState !== "idle";
    camCleanup();
    camIdle("Camera is off. Start the camera to view the live stream.");
    $("camError").style.display = "none";
    if (!silent && wasActive) B.postMessage({ type: "webcam_stop" });
  }
  function camSendIce(candidate) {
    if (!camWS || camWS.readyState !== 1) return;
    try {
      camWS.send(JSON.stringify({
        action: "ICE_CANDIDATE",
        messagePayload: btoa(JSON.stringify(candidate))
      }));
    } catch (e) {}
  }
  async function camConnect(cfg) {
    camCleanup();
    camState = "connecting";
    camSetBadge("Connecting…", "off");
    $("camPlaceholder").textContent = "Connecting to the printer camera…";
    $("camPlaceholder").style.display = "flex";
    $("camError").style.display = "none";
    $("camStats").textContent = "";
    updateCameraControls();

    var pc = new RTCPeerConnection({
      iceServers: (cfg && cfg.ice_servers) || [],
      bundlePolicy: "max-bundle"
    });
    camPC = pc;
    var video = $("camVideo");
    video.onloadeddata = function () {
      if (camPC !== pc) return;
      if (camTimer) { clearTimeout(camTimer); camTimer = null; }
      camState = "live";
      camSetBadge("Live", "ok");
      $("camPlaceholder").style.display = "none";
      $("camStats").textContent = video.videoWidth + "×" + video.videoHeight;
      updateCameraControls();
      B.postMessage({ type: "webcam_viewing" });
    };
    pc.ontrack = function (ev) {
      if (camPC !== pc) return;
      var stream = null;
      if (ev.streams && ev.streams.length) stream = ev.streams[0];
      else {
        try { stream = new MediaStream([ev.track]); } catch (e) {}
      }
      if (!stream) return;
      video.srcObject = stream;
      var playing = video.play();
      if (playing && playing.catch) playing.catch(function () {});
    };
    pc.onconnectionstatechange = function () {
      if (camPC !== pc) return;
      if (pc.connectionState === "failed") {
        camFail("The camera connection failed. Check that the printer is online, then press Reconnect.");
      }
    };

    // KVS rejects the default multi-codec Chromium offer with an empty answer.
    // Restrict the transceiver to H.264, which is what the printer streams.
    try {
      var caps = RTCRtpReceiver.getCapabilities("video");
      var codecs = (caps && caps.codecs) ? caps.codecs : [];
      var h264 = codecs.filter(function (c) {
        return (c.mimeType || "").toLowerCase() === "video/h264";
      });
      var transceiver = pc.addTransceiver("video", { direction: "recvonly" });
      if (h264.length && transceiver.setCodecPreferences) {
        transceiver.setCodecPreferences(h264);
      }
    } catch (e) {
      try { pc.addTransceiver("video", { direction: "recvonly" }); } catch (e2) {}
    }

    var ws = new WebSocket(cfg.wss_url);
    camWS = ws;
    var offerSent = false;

    ws.onmessage = async function (ev) {
      if (camWS !== ws) return;
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      var kind = msg.messageType || msg.action;
      if (kind === "SDP_ANSWER") {
        try {
          var body = JSON.parse(atob(msg.messagePayload));
          await pc.setRemoteDescription({
            type: body.type || "answer",
            sdp: body.sdp
          });
        } catch (e) {
          camFail("Could not apply the camera answer: " + e.message);
        }
      } else if (kind === "ICE_CANDIDATE") {
        try {
          var candidate = JSON.parse(atob(msg.messagePayload));
          if (candidate && candidate.candidate) {
            await pc.addIceCandidate(candidate);
          }
        } catch (e) {}
      }
    };
    ws.onclose = function () {
      if (camWS !== ws) return;
      if (camState === "connecting") {
        camFail("The camera signaling connection closed. Press Reconnect.");
      }
    };
    ws.onerror = function () {
      if (camWS !== ws) return;
      if (camState === "connecting") {
        camFail("Could not reach the camera signaling service. Check the printer and this network.");
      }
    };

    try {
      await new Promise(function (resolve, reject) {
        var timer = setTimeout(function () {
          reject(new Error("signaling connection timed out"));
        }, 12000);
        ws.onopen = function () {
          clearTimeout(timer);
          resolve();
        };
      });
    } catch (e) {
      camFail("Could not open the camera signaling connection: " + e.message);
      return;
    }
    if (camWS !== ws) return;

    pc.onicecandidate = function (ev) {
      if (!ev.candidate) return;
      var candidate = ev.candidate.toJSON();
      if (offerSent) camSendIce(candidate);
      else camPendingCandidates.push(candidate);
    };

    var offer;
    try {
      offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
    } catch (e) {
      camFail("Could not create the camera offer: " + e.message);
      return;
    }
    await new Promise(function (resolve) {
      if (pc.iceGatheringState === "complete") return resolve();
      var done = false;
      function finish() {
        if (done) return;
        done = true;
        resolve();
      }
      pc.onicegatheringstatechange = function () {
        if (pc.iceGatheringState === "complete") finish();
      };
      setTimeout(finish, 8000);
    });
    if (camPC !== pc) return;

    try {
      offerSent = true;
      ws.send(JSON.stringify({
        action: "SDP_OFFER",
        messagePayload: btoa(unescape(encodeURIComponent(JSON.stringify({
          type: "offer",
          sdp: pc.localDescription.sdp
        }))))
      }));
    } catch (e) {
      camFail("Could not send the camera offer: " + e.message);
      return;
    }
    camPendingCandidates.forEach(camSendIce);
    camPendingCandidates = [];
    camTimer = setTimeout(function () {
      if (camPC === pc && camState !== "live") {
        camFail("No video arrived. The printer may be busy, the camera may be disconnected, or UDP may be blocked on this network.");
      }
    }, (cfg && cfg.first_frame_timeout) || 20000);
  }
  function renderWebcam(d) {
    if (!d) return;
    if (d.state === "ready") {
      camConnect(d).catch(function (e) {
        camFail("Camera setup failed: " + (e && e.message ? e.message : e));
      });
      return;
    }
    if (d.state === "error") {
      camFail(d.error || "The camera could not be started.");
      return;
    }
    if (d.state === "idle") {
      if (camState !== "idle") camIdle();
      return;
    }
    if (d.state === "starting" && camState === "idle") {
      camState = "starting";
      camSetBadge("Opening camera…", "off");
      $("camPlaceholder").textContent = "Opening the printer camera…";
      $("camPlaceholder").style.display = "flex";
      updateCameraControls();
    }
  }
  function updateCameraControls() {
    var d = lastStatus || {};
    var hasPrinter = !!(d.printer && d.printer.name);
    var online = !!(d.printer && d.printer.online);
    var mqtt = !!(d.mqtt && d.mqtt.connected);
    var active = camState === "starting" || camState === "connecting" || camState === "live";
    $("btnCamStart").disabled = !(d.login && hasPrinter && online && mqtt) || active;
    $("btnCamReconnect").disabled = !active && camState !== "error";
    $("btnCamStop").disabled = !active && camState !== "error";
    var hint;
    if (!d.login) hint = "Sign in to use the camera.";
    else if (!hasPrinter) hint = "Select a printer first.";
    else if (!online) hint = "The printer is offline on Anycubic Cloud.";
    else if (!mqtt) hint = "Start monitoring first: the camera opens over the printer's MQTT channel.";
    else if (camState === "live") hint = "Live view. The camera does not affect the print.";
    else if (camState === "starting" || camState === "connecting") hint = "Opening the camera…";
    else if (camState === "error") hint = "Camera unavailable. Check the printer and try again.";
    else hint = "Start the camera to view the printer.";
    $("camHint").textContent = hint;
  }
  function render(d) {
    if (d && d.type === "printers") {
      if (d.closed) { $("picker").style.display = "none"; return; }
      renderPrinters(d);
      return;
    }
    if (d && d.type === "files") {
      renderFiles({
        dir: d.dir, files: d.items, error: d.error,
        scanning: d.scanning, stalled: d.stalled, scan_age: d.scan_age
      });
      return;
    }
    if (d && d.type === "upload") {
      renderUpload(d);
      if (d.done) {
        if (d.ok !== false) hideConfirm();
        if (d.message) showNotice(d.message, d.ok === false);
      }
      return;
    }
    if (d && d.type === "webcam") {
      renderWebcam(d);
      return;
    }
    if (!d || d.type !== "status") {
      if (d && d.type === "notice") showNotice(d.text, !!d.error);
      return;
    }
    lastStatus = d;
    renderFiles(d.watch || {});
    renderUpload(d.upload || null);
    updateJobControls();
    updateCameraControls();

    var printer = d.printer || {};
    $("printerName").textContent = printer.name || "No printer selected";
    var modelText = printer.model || "";
    if (printer.name && !printer.online) modelText += (modelText ? " · " : "") + "offline on Anycubic Cloud";
    set("printerModel", modelText);

    var badge = $("conn");
    var mqtt = d.mqtt || {};
    var hasData = mqtt.last_message !== null && mqtt.last_message !== undefined;
    if (mqtt.error) { badge.textContent = "Error"; badge.className = "badge err"; }
    else if (mqtt.connected && hasData && !mqtt.stale) { badge.textContent = "Live"; badge.className = "badge ok"; }
    else if (mqtt.connected && hasData) { badge.textContent = "Stale"; badge.className = "badge off"; }
    else if (mqtt.connected) { badge.textContent = "Waiting for data"; badge.className = "badge off"; }
    else if (d.monitoring) { badge.textContent = "Connecting…"; badge.className = "badge off"; }
    else { badge.textContent = "Not monitoring"; badge.className = "badge off"; }

    if (d.login) $("loginState").textContent = "Signed in as " + (d.email || "Anycubic account");
    else $("loginState").textContent = "Not signed in";
    $("signinBlock").style.display = d.login ? "none" : "block";

    var temps = d.temps || {};
    set("nozzle", temps.nozzle);
    set("nozzleTarget", temps.nozzle_target);
    set("bed", temps.bed);
    set("bedTarget", temps.bed_target);

    var p = d.print || {};
    set("printState", p.state);
    set("layer", p.layer);
    set("progress", p.progress);
    set("file", p.file);
    set("remaining", p.remaining);
    $("bar").style.width = (p.progress === null || p.progress === undefined) ? "0%" :
      Math.max(0, Math.min(100, p.progress)) + "%";

    if (hasData) set("lastMsg", age(mqtt.last_message) + (mqtt.stale ? " (stale)" : ""));
    else set("lastMsg", "—");

    if (d.mqtt && d.mqtt.error) showNotice(d.mqtt.error, true);
    else if (d.notice) showNotice(d.notice, !!d.notice_error);
    else showNotice("", false);

    $("raw").textContent = d.raw && Object.keys(d.raw).length
      ? Object.keys(d.raw).map(function (k) { return "// " + k + "\\n" + d.raw[k]; }).join("\\n\\n")
      : "No MQTT data yet.";

    $("btnStart").disabled = !!d.monitoring;
    $("btnStop").disabled = !d.monitoring;
  }
  B.onMessage(render);

  $("btnStart").onclick = function () { B.postMessage({ type: "start_monitor" }); };
  $("btnStop").onclick = function () { B.postMessage({ type: "stop_monitor" }); };
  $("btnSelect").onclick = function () {
    var box = $("picker");
    box.style.display = "block";
    $("pickerList").innerHTML = '<div class="muted">Loading printers…</div>';
    B.postMessage({ type: "select_printer" });
  };
  $("pickerClose").onclick = function () { $("picker").style.display = "none"; };

  $("btnRefreshFiles").onclick = function () {
    $("watchHint").textContent = "Scanning the watch folder…";
    $("watchHint").style.color = "";
    $("btnRefreshFiles").disabled = true;
    B.postMessage({ type: "refresh_files" });
  };
  $("confirmCancel").onclick = hideConfirm;
  $("confirmGo").onclick = function () {
    if (!pending) return;
    var path = pending.path;
    var name = pending.name;
    hideConfirm();
    showNotice("Uploading " + name + " and starting the print…", false);
    B.postMessage({ type: "upload", path: path, start: true });
  };

  $("btnPause").onclick = function () { showControlConfirm("pause"); };
  $("btnResume").onclick = function () { showControlConfirm("resume"); };
  $("btnStopPrint").onclick = function () { showControlConfirm("stop"); };
  $("controlConfirmCancel").onclick = hideControlConfirm;
  $("controlConfirmGo").onclick = function () {
    if (!pendingControl) return;
    var action = pendingControl;
    hideControlConfirm();
    var label = action === "stop" ? "Stopping" : action === "pause" ? "Pausing" : "Resuming";
    showNotice(label + " the print…", false);
    B.postMessage({ type: action + "_print", confirm: true });
  };

  $("btnCamStart").onclick = camStart;
  $("btnCamReconnect").onclick = camStart;
  $("btnCamStop").onclick = function () { camStop(false); };

  window.addEventListener("beforeunload", function () {
    if (camState !== "idle") {
      camCleanup();
      B.postMessage({ type: "webcam_stop" });
    }
  });

  function poll() { B.postMessage({ type: "poll" }); }
  poll();
  setInterval(poll, 2000);
})();
</script>
</body>
</html>
"""

CONFIG_UI_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body { font-family: var(--orca-font, system-ui); margin: 0; padding: 14px; color: var(--orca-fg, #222); background: var(--orca-bg, #fff); }
  h3 { margin: 0 0 4px; font-size: 16px; }
  .muted { color: var(--orca-muted, #777); font-size: 12px; }
  label { display: block; margin-top: 14px; font-size: 12px; font-weight: 600; }
  textarea { width: 100%; box-sizing: border-box; margin-top: 6px; padding: 8px; border-radius: 6px;
             border: 1px solid var(--orca-border, #ccc); background: var(--orca-bg, #fff); color: var(--orca-fg, #222);
             font-family: ui-monospace, monospace; font-size: 11px; }
  input[type=text] { width: 100%; box-sizing: border-box; margin-top: 6px; padding: 8px; border-radius: 6px;
             border: 1px solid var(--orca-border, #ccc); background: var(--orca-bg, #fff); color: var(--orca-fg, #222);
             font-family: ui-monospace, monospace; font-size: 12px; }
  button { margin-top: 10px; padding: 7px 14px; border-radius: 7px; border: 1px solid var(--orca-border, #ccc);
           background: var(--orca-accent, #009688); color: var(--orca-accent-fg, #fff); cursor: pointer; font-size: 13px; }
  #status { margin-top: 12px; padding: 8px 10px; border-radius: 7px; font-size: 12px;
            background: var(--orca-border, #f2f2f2); }
  details { margin-top: 14px; font-size: 12px; }
  code { font-family: ui-monospace, monospace; }
</style>
</head>
<body>
  <h3>Anycubic Cloud</h3>
  <div class="muted">Sign in once. The workbench token is cached in the plugin folder.</div>

  <div id="status">Loading…</div>

  <label for="token">RS256 access token</label>
  <textarea id="token" rows="6" spellcheck="false"
    placeholder="Paste the RS256 JWT (starts with eyJhbGciOiJSUzI1NiIsImtpZCI6ImFjX2FwcF9jcnQi...)"></textarea>
  <button id="save">Save &amp; sign in</button>

  <label for="watch">G-code watch folder</label>
  <input id="watch" type="text" spellcheck="false" placeholder="~/Downloads">
  <button id="saveFolder">Save folder</button>
  <div class="muted" style="margin-top:6px">
    The plugin can only read files inside <b>Downloads</b>, <b>Desktop</b> or
    <b>Documents</b>. Leave this empty to use <code>~/Downloads</code>.
  </div>

  <details>
    <summary class="muted">Where do I get this token?</summary>
    <div class="muted" style="margin-top:8px; line-height:1.5">
      It is issued by Anycubic when you log in to <b>Anycubic Slicer Next</b>.
      Open the app and sign in, then find the token in
      <code>~/Library/Application Support/AnycubicSlicerNext/log/</code>
      (search for <code>eyJhbGciOiJSUzI1NiIsImtpZCI6ImFjX2FwcF9jcnQi</code>), or capture it
      from the app's login request with a proxy. The token is valid for about a year.
    </div>
  </details>

<script>
(function () {
  var B = window.orca;
  var statusEl = document.getElementById("status");
  var tokenEl = document.getElementById("token");
  var watchEl = document.getElementById("watch");

  function config() {
    try {
      var value = B.getConfig();
      if (typeof value === "string") return JSON.parse(value || "{}");
      return value || {};
    } catch (e) { return {}; }
  }

  function refresh() {
    var cfg = config();
    watchEl.value = cfg.watch_dir || "";
    watchEl.placeholder = "~/Downloads";
    if (cfg.logged_in && cfg.email) {
      statusEl.textContent = "Signed in as " + cfg.email;
      statusEl.style.color = "#00796b";
      tokenEl.placeholder = "Already signed in — paste a new token to replace it";
    } else if (cfg.email) {
      statusEl.textContent = "Last account: " + cfg.email + " — not currently signed in";
    } else {
      statusEl.textContent = "Not signed in yet.";
    }
  }

  document.getElementById("save").onclick = function () {
    var token = (tokenEl.value || "").trim();
    if (!token) {
      statusEl.textContent = "Paste a token first.";
      return;
    }
    var cfg = config();
    cfg.rs256_token = token;
    cfg.email = cfg.email || "";
    cfg.watch_dir = (watchEl.value || "").trim();
    B.saveConfig(cfg);
    statusEl.textContent = "Saved. Open the Anycubic Cloud tab — sign-in starts automatically.";
  };

  document.getElementById("saveFolder").onclick = function () {
    var cfg = config();
    cfg.watch_dir = (watchEl.value || "").trim();
    B.saveConfig(cfg);
    statusEl.textContent = cfg.watch_dir
      ? "Watch folder saved. The dashboard picks it up within a few seconds."
      : "Watch folder reset to ~/Downloads.";
  };

  refresh();
})();
</script>
</body>
</html>
"""

LOGIN_DIALOG_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  body { font-family: var(--orca-font, system-ui); margin: 0; padding: 16px; color: var(--orca-fg, #222); background: var(--orca-bg, #fff); }
  h3 { margin: 0 0 6px; font-size: 16px; }
  .muted { color: var(--orca-muted, #777); font-size: 12px; line-height: 1.5; }
  textarea { width: 100%; box-sizing: border-box; margin-top: 10px; padding: 8px; border-radius: 6px;
             border: 1px solid var(--orca-border, #ccc); background: var(--orca-bg, #fff); color: var(--orca-fg, #222);
             font-family: ui-monospace, monospace; font-size: 11px; }
  .row { display: flex; gap: 10px; justify-content: flex-end; margin-top: 12px; }
  button { padding: 7px 14px; border-radius: 7px; border: 1px solid var(--orca-border, #ccc);
           background: var(--orca-bg, #fff); color: var(--orca-fg, #222); cursor: pointer; font-size: 13px; }
  button.primary { background: var(--orca-accent, #009688); border-color: var(--orca-accent, #009688);
                   color: var(--orca-accent-fg, #fff); }
  code { font-family: ui-monospace, monospace; }
  .hint-row { display: flex; gap: 10px; justify-content: flex-start; margin-top: 8px; }
  .hint-row button { font-size: 11px; padding: 4px 10px; }
  #toast { position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%);
           padding: 10px 16px; border-radius: 8px; background: rgba(0,0,0,.85); color: #fff;
           font-size: 12px; display: none; z-index: 100; max-width: 90%; text-align: center; }
</style>
</head>
<body>
  <h3>Sign in to Anycubic Cloud</h3>
  <div class="muted">
    Paste the <b>RS256 access token</b> issued by Anycubic Slicer Next for your account.
    It is the JWT that starts with
    <code>eyJhbGciOiJSUzI1NiIsImtpZCI6ImFjX2FwcF9jcnQi</code>.
    You can also paste a whole JSON response containing <code>access_token</code>.
  </div>
  <textarea id="token" rows="7" spellcheck="false" placeholder="eyJhbGciOiJSUzI1NiIs..."></textarea>
  <div class="hint-row">
    <button id="btnOpenLogs">Open log folder</button>
  </div>
  <div class="row">
    <button id="cancel">Cancel</button>
    <button id="ok" class="primary">Sign in</button>
  </div>
  <div id="toast"></div>
<script>
(function () {
  var orca = window.orca;
  function submit() {
    var token = (document.getElementById("token").value || "").trim();
    if (!token) { document.getElementById("token").focus(); return; }
    orca.submit({ token: token });
  }
  function showToast(msg) {
    var t = document.getElementById("toast");
    t.textContent = msg;
    t.style.display = "block";
    setTimeout(function () { t.style.display = "none"; }, 5000);
  }
  document.getElementById("ok").onclick = submit;
  document.getElementById("cancel").onclick = function () { orca.submit({ token: "" }); };
  document.getElementById("btnOpenLogs").onclick = function () { orca.submit({ action: "open_logs" }); };
  window.addEventListener("message", function (ev) {
    var data = ev.data;
    if (!data || !data.type) return;
    if (data.type === "fill_token") {
      document.getElementById("token").value = data.token;
      showToast("Token filled from logs.");
    } else if (data.type === "toast") {
      showToast(data.message);
    }
  });
  document.getElementById("token").focus();
})();
</script>
</body>
</html>
"""

# ============================================================================
# Printer selection flow (shared by the dashboard picker and the script)
# ============================================================================

def list_printers():
    """The account's FDM printers as small, page-safe dicts."""
    out = []
    for printer in get_printers():
        out.append({
            "id": printer.get("id"),
            "name": printer.get("name") or "Printer",
            "model": printer.get("model") or str(printer.get("machine_type") or ""),
            "online": printer.get("device_status") == 1,
        })
    return out


def select_printer_by_id(printer_id):
    """Re-fetch the chosen printer and persist it. Returns the full record or None."""
    for printer in get_printers():
        if printer.get("id") == printer_id:
            select_printer(printer)
            return printer
    return None


def restart_monitoring_if_running(name):
    with STATE.lock:
        service = STATE.mqtt_service
    if service is not None and service.running:
        service.stop()
        return start_monitoring()
    return True, "Monitoring is not running."


# ============================================================================
# Async login (never block the UI thread)
# ============================================================================

_login_lock = threading.Lock()
_login_in_flight = False


def login_async(token: str) -> bool:
    """Exchange the token on a worker thread; result lands in STATE.notice."""
    global _login_in_flight
    with _login_lock:
        if _login_in_flight:
            return False
        _login_in_flight = True

    def worker():
        global _login_in_flight
        try:
            ok, message = login_with_rs256(token)
            STATE.set_notice(message, error=not ok)
        except Exception as exc:
            STATE.set_notice("Login failed: %s" % exc, error=True)
        finally:
            with _login_lock:
                _login_in_flight = False

    threading.Thread(target=worker, name="anycubic-login", daemon=True).start()
    return True


# Only one upload at a time: the cloud locks storage per file and the UI has a
# single progress panel.
_upload_lock = threading.Lock()
_upload_in_flight = False


# ============================================================================
# Capabilities
# ============================================================================

class AnycubicCloudPage(orca.pages.PagesPluginCapabilityBase):
    """Main-window 'Anycubic Cloud' tab: live printer dashboard."""

    _config_synced = False

    def get_name(self):
        return "Anycubic Cloud"

    def get_ui(self):
        return DASHBOARD_HTML

    # ---- configuration (Config tab) ---------------------------------------

    def get_default_config(self):
        return {
            "rs256_token": "",
            "email": "",
            "logged_in": False,
            "watch_dir": "",
        }

    def has_config_ui(self):
        return True

    def get_config_ui(self):
        return CONFIG_UI_HTML

    # ---- page messages -----------------------------------------------------

    def on_message(self, data):
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                return
        if not isinstance(data, dict):
            return

        kind = data.get("type")

        if kind == "poll":
            self._sync_config_token()
            if WATCH_BACKGROUND:
                try:
                    ensure_watch_thread()
                except Exception:
                    pass
            refresh_printer()
            refresh_project()
            self._post_status()
            return

        if kind == "start_monitor":
            ok, message = start_monitoring()
            STATE.set_notice(message, error=not ok)
            self._post_status()
            return

        if kind == "stop_monitor":
            ok, message = stop_monitoring()
            STATE.set_notice(message, error=not ok)
            self._post_status()
            return

        if kind == "select_printer":
            self._list_printers_async()
            return

        if kind == "pick_printer":
            printer_id = data.get("id")
            if printer_id is not None:
                self._pick_printer_async(printer_id)
            return

        if kind == "refresh_files":
            self._post_files(force=True)
            return

        if kind == "upload":
            self._upload_async(data.get("path") or "", bool(data.get("start")))
            return

        if kind in ("pause_print", "resume_print", "stop_print"):
            action = kind.split("_", 1)[0]
            if data.get("confirm") is not True:
                STATE.set_notice(
                    "%s requires confirmation. Nothing was sent." % CONTROL_VERBS[action],
                    error=True,
                )
                self._post_status()
                return
            self._control_async(action)
            return

        if kind in ("webcam_start", "webcam_reconnect"):
            self._webcam_async()
            return

        if kind in ("webcam_stop", "webcam_failed"):
            self._webcam_stop_async()
            return

        if kind == "webcam_viewing":
            webcam_mark_viewing()
            self._post_status()
            return

    # ---- async work (on_message runs on the UI thread: never block it) ------

    def _webcam_async(self):
        """Open the camera on a worker thread, then hand the viewer config to the page."""
        page = self

        def worker():
            try:
                ok, message, config = webcam_start()
            except Exception as exc:
                ok, message, config = False, "Camera failed: %s" % exc, None
            if ok and config:
                try:
                    page.post_message({
                        "type": "webcam",
                        "state": "ready",
                        "wss_url": config["wss_url"],
                        "ice_servers": config["ice_servers"],
                        "expires_in": config["expires_in"],
                        "first_frame_timeout": config.get(
                            "first_frame_timeout",
                            int(CAMERA_FIRST_FRAME_TIMEOUT * 1000)),
                        "message": message,
                    })
                except Exception:
                    pass
            else:
                STATE.set_notice(message, error=True)
                try:
                    page.post_message({
                        "type": "webcam",
                        "state": "error",
                        "error": message,
                    })
                except Exception:
                    pass
            try:
                page._post_status()
            except Exception:
                pass

        threading.Thread(
            target=worker, name="anycubic-webcam", daemon=True).start()

    def _webcam_stop_async(self):
        page = self

        def worker():
            try:
                webcam_stop()
            except Exception:
                pass
            try:
                page._post_status()
            except Exception:
                pass

        threading.Thread(
            target=worker, name="anycubic-webcam-stop", daemon=True).start()

    def _list_printers_async(self):
        page = self

        def worker():
            try:
                items = list_printers()
                page.post_message({"type": "printers", "items": items})
            except Exception as exc:
                page.post_message({"type": "printers", "items": [],
                                   "error": "Could not load printers: %s" % exc})

        threading.Thread(target=worker, name="anycubic-printers", daemon=True).start()

    def _pick_printer_async(self, printer_id):
        page = self

        def worker():
            try:
                printer = select_printer_by_id(printer_id)
            except Exception as exc:
                page.post_message({"type": "printers", "items": [],
                                   "error": "Could not load printers: %s" % exc})
                return
            if printer is None:
                page.post_message({"type": "printers", "items": [],
                                   "error": "That printer is no longer on this account."})
                return
            name = printer.get("name") or "printer"
            ok, message = restart_monitoring_if_running(name)
            STATE.set_notice("Selected %s. %s" % (name, message), error=not ok)
            page.post_message({"type": "printers", "items": [], "closed": True})
            page._post_status()

        threading.Thread(target=worker, name="anycubic-pick", daemon=True).start()

    def _post_files(self, force=False):
        try:
            if WATCH_BACKGROUND:
                if force:
                    request_watch_refresh(force=True)
                watch = watch_snapshot()
                items, error = watch["files"], watch["error"]
            else:
                items, error = watch_files(force=force)
                watch = watch_snapshot()
            self.post_message({
                "type": "files",
                "items": items,
                "dir": watch["path"],
                "error": error,
                "scanning": watch["scanning"],
                "stalled": watch["stalled"],
                "status": watch["status"],
                "scan_age": watch["scan_age"],
            })
        except Exception:
            pass

    def _upload_async(self, path, start):
        """Upload (and optionally print) on a worker thread."""
        global _upload_in_flight

        with _upload_lock:
            if _upload_in_flight:
                STATE.set_notice("An upload is already in progress.", error=True)
                self._post_status()
                return
            _upload_in_flight = True

        page = self
        name = os.path.basename(path or "") or "file"

        def progress(phase, percent):
            upload = STATE.patch_upload(
                name=name, phase=phase, percent=round(percent, 1),
                done=False, ok=None, message="", file_id=None,
            )
            try:
                page.post_message(dict(upload, type="upload"))
            except Exception:
                pass

        def finish(ok, message, phase, file_id=None, percent=100):
            STATE.patch_upload(
                name=name, phase=phase, percent=percent, done=True,
                ok=bool(ok), message=message, file_id=file_id,
            )
            STATE.set_notice(message, error=not ok)
            try:
                page.post_message({
                    "type": "upload", "name": name, "phase": phase,
                    "percent": percent, "done": True, "ok": bool(ok),
                    "message": message, "file_id": file_id,
                })
                page.post_message(build_status())
            except Exception:
                pass

        def worker():
            global _upload_in_flight
            try:
                if start:
                    ok, message = print_preflight(refresh=True)
                    if not ok:
                        finish(False, message, "error", percent=0)
                        return

                result = upload_gcode(path, progress)
                file_id = result["file_id"]
                if not start:
                    finish(
                        True,
                        "Uploaded %s to Anycubic Cloud (file #%s)." % (result["name"], file_id),
                        "done", file_id=file_id,
                    )
                    return

                STATE.patch_upload(name=result["name"], phase="start", percent=100, done=False)
                try:
                    page.post_message({
                        "type": "upload", "name": result["name"], "phase": "start",
                        "percent": 100, "done": False,
                    })
                except Exception:
                    pass

                ok, message = start_print(file_id)
                if ok:
                    finish(
                        True,
                        "Uploaded %s and started printing (cloud file #%s). %s" % (
                            result["name"], file_id, message),
                        "done", file_id=file_id,
                    )
                else:
                    finish(
                        False,
                        "Uploaded %s (cloud file #%s), but the print did not start: %s" % (
                            result["name"], file_id, message),
                        "error", file_id=file_id,
                    )
            except UploadError as exc:
                finish(False, "Could not upload %s: %s" % (name, exc), "error", percent=0)
            except Exception as exc:
                finish(False, "Upload failed: %s" % exc, "error", percent=0)
            finally:
                with _upload_lock:
                    _upload_in_flight = False

        threading.Thread(target=worker, name="anycubic-upload", daemon=True).start()

    def _control_async(self, action):
        """Send a pause/resume/stop order on a worker thread."""
        page = self

        def worker():
            try:
                ok, message = control_print(action, confirmed=True)
            except Exception as exc:
                ok = False
                message = "%s failed: %s" % (CONTROL_VERBS.get(action, "Control"), exc)
            STATE.set_notice(message, error=not ok)
            try:
                page.post_message(build_status())
            except Exception:
                pass

        threading.Thread(
            target=worker, name="anycubic-control", daemon=True).start()

    def _post_status(self):
        try:
            self.post_message(build_status())
        except Exception:
            pass

    def _sync_config_token(self):
        """Keep the Config tab in step with the cached session, and exchange a token if signed out."""
        try:
            config = json.loads(self.get_config() or "{}")
        except Exception:
            config = {}

        watch_dir = config.get("watch_dir")
        if isinstance(watch_dir, str):
            wanted = watch_dir.strip() or DEFAULT_WATCH_DIR
            with STATE.lock:
                changed = STATE.watch_dir != wanted
                STATE.watch_dir = wanted
            if changed:
                STATE.save()
                request_watch_refresh(force=True)

        if STATE.is_logged_in():
            if not self._config_synced:
                self._config_synced = True
                self._update_config_after_login()
            return

        self._config_synced = False
        token = (config.get("rs256_token") or "").strip()
        if not token:
            return

        token_sha = _sha256(token)
        with STATE.lock:
            if STATE.failed_token_sha == token_sha:
                return
            # Remember the attempt so the 2-second poll does not hammer the API.
            STATE.failed_token_sha = token_sha
        STATE.save()
        login_async(token)

    def _update_config_after_login(self):
        with STATE.lock:
            email = STATE.email
        try:
            config = json.loads(self.get_config() or "{}")
        except Exception:
            config = {}
        if config.get("email") == email and config.get("logged_in") is True:
            return
        config["email"] = email
        config["logged_in"] = True
        try:
            self.save_config(json.dumps(config))
        except Exception:
            pass

    # ---- lifecycle ---------------------------------------------------------

    def on_load(self):
        # Re-sync the Config tab with whatever session was cached on disk. Safe here:
        # the signed-out path hands the HTTP call to a worker thread.
        self._config_synced = False
        try:
            self._sync_config_token()
        except Exception:
            pass
        try:
            request_watch_refresh(force=True)
        except Exception:
            pass

    def on_unload(self):
        # Close the camera before disconnecting MQTT: stopCapture needs the
        # broker, and order 1002 needs HTTP. Publish stopCapture synchronously
        # (paho only queues it) so the printer stops pushing even if the
        # interpreter exits before the cleanup thread finishes.
        try:
            with STATE.lock:
                service = STATE.mqtt_service
                camera_on = STATE.camera_state in (
                    "starting", "connecting", "live")
            if camera_on and service is not None and service.running:
                service.publish_video("stopCapture")
        except Exception:
            pass

        def worker():
            try:
                webcam_stop()
            except Exception:
                pass
            try:
                stop_monitoring()
            except Exception:
                pass

        threading.Thread(
            target=worker, name="anycubic-unload", daemon=True).start()
        try:
            stop_watch_thread()
        except Exception:
            pass


class AnycubicLoginScript(orca.script.ScriptPluginCapabilityBase):
    """Plugins dialog -> Run -> paste RS256 token."""

    def get_name(self):
        return "Anycubic Login"

    def execute(self):
        ui = orca.host.ui
        if STATE.is_logged_in():
            ui.message(
                "Already signed in as %s.\n\nPaste a new token in the Config tab to switch accounts." % (STATE.email or "your account"),
                title="Anycubic Cloud",
                buttons="ok",
                icon="info",
            )
            return orca.ExecutionResult.success(message="Already signed in.")

        # Try to auto-detect token from logs BEFORE opening the dialog
        detected_token = _find_anycubic_log_token()

        holder = {}

        def on_submit(data):
            if not isinstance(data, dict):
                return
            token = (data.get("token") or "").strip()
            window = holder.get("window")
            if window is not None:
                try:
                    window.close()
                except Exception:
                    pass
            if token:
                login_async(token)

        try:
            # Pass detected token to dialog (pre-fills textarea if found)
            dialog_html = LOGIN_DIALOG_HTML
            if detected_token:
                # Inject the token into the HTML before the script runs
                inject = 'document.getElementById("token").value = %s; showToast("Token auto-detected from logs.");' % json.dumps(detected_token)
                dialog_html = dialog_html.replace(
                    'document.getElementById("token").focus();',
                    inject + '\n      document.getElementById("token").focus();'
                )

            holder["window"] = ui.create_window(
                html=dialog_html,
                title="Anycubic Cloud Login",
                width=680,
                height=470,
                on_submit=on_submit,
            )
        except Exception as exc:
            return orca.ExecutionResult.failure(
                orca.PluginResult.RecoverableError,
                "Could not open the login window: %s" % exc,
            )

        return orca.ExecutionResult.success(
            message="Paste your RS256 token in the login window that just opened." + (" (Auto-detected from logs)" if detected_token else ""),
        )


class AnycubicSelectPrinterScript(orca.script.ScriptPluginCapabilityBase):
    def get_name(self):
        return "Select Printer"

    def execute(self):
        if not STATE.is_logged_in():
            return orca.ExecutionResult.failure(
                orca.PluginResult.RecoverableError,
                "Sign in first: paste your token in the Config tab, then press Save.",
            )
        try:
            printers = list_printers()
        except Exception as exc:
            return orca.ExecutionResult.failure(
                orca.PluginResult.RecoverableError,
                "Could not load printers: %s" % exc,
            )
        if not printers:
            return orca.ExecutionResult.failure(
                orca.PluginResult.RecoverableError,
                "No FDM printers found on this Anycubic account.",
            )
        if len(printers) > 1:
            return orca.ExecutionResult.failure(
                orca.PluginResult.RecoverableError,
                "This account has %d printers. Open the 'Anycubic Cloud' tab and use "
                "'Select printer' to choose one." % len(printers),
            )

        printer = select_printer_by_id(printers[0]["id"])
        if printer is None:
            return orca.ExecutionResult.failure(
                orca.PluginResult.RecoverableError,
                "That printer is no longer on this account.",
            )
        name = printer.get("name") or "printer"
        ok, message = restart_monitoring_if_running(name)
        text = "Selected %s. %s" % (name, message)
        if not ok:
            return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError, text)
        return orca.ExecutionResult.success(message=text)


class AnycubicStartMonitorScript(orca.script.ScriptPluginCapabilityBase):
    def get_name(self):
        return "Start Monitoring"

    def execute(self):
        ok, message = start_monitoring()
        if not ok:
            return orca.ExecutionResult.failure(orca.PluginResult.RecoverableError, message)
        return orca.ExecutionResult.success(message=message)


class AnycubicStopMonitorScript(orca.script.ScriptPluginCapabilityBase):
    def get_name(self):
        return "Stop Monitoring"

    def execute(self):
        ok, message = stop_monitoring()
        return orca.ExecutionResult.success(message=message)


# ============================================================================
# Registration
# ============================================================================

@orca.plugin
class AnycubicCloudPlugin(orca.base):
    def register_capabilities(self):
        orca.request_permissions(
            fs_read=WATCH_PERMISSIONS,
            network_http=[
                "https://cloud-universe.anycubic.com",
                "cloud-universe.anycubic.com",
                "cloud-universe.anycubic.com:443",
                "('cloud-universe.anycubic.com', 443)",
                "https://workbentch.s3.us-east-2.amazonaws.com",
                "https://workbench.s3.us-east-2.amazonaws.com",
            ] + KVS_HTTP_PERMISSIONS,
            network_socket=[
                "('', 0)",
                "('mqtt-universe.anycubic.com', 8883)",
                "mqtt-universe.anycubic.com:8883",
                "('cloud-universe.anycubic.com', 443)",
            ] + KVS_SOCKET_PERMISSIONS,
            threading=True,
        )
        orca.register_capability(AnycubicCloudPage)
        orca.register_capability(AnycubicLoginScript)
        orca.register_capability(AnycubicSelectPrinterScript)
        orca.register_capability(AnycubicStartMonitorScript)
        orca.register_capability(AnycubicStopMonitorScript)
