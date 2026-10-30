# OrcaSlicer Plugin: "Anycubic Cloud Print" Design Doc (v2)

Status: implemented through v0.7.0 · 2026-10-27
Basis: protocol facts verified in `mqtt_auth_protocol.md`. Everything else is marked ⚠ (static, unverified) or OPEN (undecided) and is gated behind user-approved live tests.

## 0. Pre-conditions (resolve BEFORE building)

| # | Question | Why it blocks | How to resolve |
|---|----------|---------------|----------------|
| P1 | Does OrcaSlicer expose a plugin/extension loader that supports a native or script plugin with UI? | The whole architecture depends on it. | Run the spike first. If it fails, v1 becomes a standalone companion app. |
| P2 | How does the plugin legitimately obtain the RS256 Casdoor JWT? | F1 cannot work without it. | Decide on an own-login window or OAuth-style flow. Do NOT read the official app's token cache. |
| P3 | Does a plugin re-login (own pc_id) invalidate the official app's session, and vice versa? | If yes, the plugin and the official app kick each other out. | Test with both logged in. Document the result. |
| P4 | Licensing: OrcaSlicer is AGPL-3.0. Can a plugin be distributed under the intended license? | Distribution and linking constraints. | Check before writing code. |
| P5 | ToS and key redistribution: the plugin spoofs client version `2.0.0.3`, uses extracted signing constants (KEY_A/KEY_B), and bundles `cloud_mqtt_pubk.pem`. | Takedown and account-ban risk for users. | Decide whether to ship keys, make users supply them, or accept and disclose the risk. |
| P6 | Region: does the `IS-CN` header imply different endpoints for CN accounts? | v1 covers the global gateway only. | State "global only" in the UI, or verify CN. |

## 1. Goal & feature matrix

| F | Feature | Mechanism | Status |
|---|---------|-----------|--------|
| F1 | Account login | `POST /v3/public/loginWithAccessToken` (RS256 JWT → HS512 workbench token) | Verified. Token acquisition: see P2 |
| F2 | Printer list / snapshot | `POST /work/printer/getPrinters` | Verified |
| F3 | Status stream | MQTT SUB `anycubic/anycubicCloud/v1/+/public/{machine_type}/{unionid}/#`. `status/report` arrives about every 300 s, plus `response` acks | Verified. NOT real-time (see §3.5) |
| F4 | Signed HTTP | XX-headers + `md5(KEY_A+ts+ver+KEY_B+nonce+KEY_A)` | Verified |
| F5 | G-code → cloud upload | Watched folder → `/v2/cloud_storage/lockStorageSpace` → S3 `PUT` → `/v2/profile/newUploadFile` → `unlockStorageSpace` | **Verified live** (2026-10-27) |
| F6 | Start print | `sendOrder` order 1, cloud `file_id`, `project_id 0` | **Verified live** (2026-10-27); safety-gated (§3.6) |
| F7 | Task progress | MQTT `status/report` plus active-project `progress` from `getProjects` | Partial — heartbeat plus 5 s project refresh while the dashboard is open |
| F8 | Pause/resume/stop | `POST /work/operation/sendOrder` order 2/3/4 with the active `project_id`. Active job from `GET /work/project/getProjects`; paused when `pause != 0` and `print_status == 1`. Dashboard controls are confirmation-gated (§3.7) | **Implemented**; live command test pending explicit user consent (G6) |
| F9 | Token lifecycle | JWT exp ≈ Oct 2027, but rotated server-side on re-login. Relogin before MQTT boot. On rc=5, relogin once, then reconnect | Verified. Interaction with official app: see P3 |
| F10 | Webcam | order 1001 (`order_id` string + `shengwang_rtc_support`) → Agora `shengwang` (no MQTT command) or legacy KVS STS → `describeSignalingChannel` / `getSignalingChannelEndpoint` → SigV4-presigned WSS URL → MQTT `video/startCapture` → browser WebRTC (H.264). Stop: order 1002 (+ MQTT `stopCapture` on KVS). AMS still n/a for the Kobra 2 Pro | KVS **verified live** (2026-10-27): 640×480 H.264 over STUN; panel v0.7.0. Agora implemented v0.8.3, live order response pending printer online (§3.8). Orca WebView playback gated on G7 |

**Honest v1 scope:** monitoring works (F1–F4, F9). Upload and start print are verified live (F5, F6). Pause/resume/stop are implemented against the confirmed project-control order shape (F8), but the first live command is still gated on G6 (explicit consent, attended printer). F7 remains heartbeat-limited between project refreshes. The webcam protocol is verified end-to-end in Chromium (F10); the same code path inside Orca's WebView is not yet confirmed.

## 2. Architecture

(Keep the v1 diagram. Components: AuthSvc, AnycubicHttp, CloudApi, MqttSvc, UploadWorker, StateStore, UI.)

- Form factor is decided by spike P1: loadable plugin, wx sidecar dialog, or standalone companion.
- Single thread-safe `StateStore` (printer snapshot, last temps, last-heartbeat timestamp). The UI is event-driven via a wx event queue.

## 3. Implementation notes

### 3.1 HTTP layer
- Base: `https://cloud-universe.anycubic.com`, gateway prefix `/p/p/workbench/api/...`
- Per request: `ts = now_ms`, `nonce = uuid4`, `sig = md5(KEY_A+ts+version+KEY_B+nonce+KEY_A).upper()`, `version = "2.0.0.3"`
- Headers copied exactly from the wire: Accept, Content-Type (`application/x-www-form-urlencoded` for GET with no extra space, JSON for POST), XX-Device-Type, XX-IS-CN, XX-LANGUAGE, XX-Nonce, XX-Signature, XX-Timestamp, XX-Version, XX-Token.
- `Accept-Encoding: identity`. **OPEN:** v1 had an unintelligible note here, so confirm the reason (likely to avoid compressed bodies).
- Responses are `{"code":1,"data":...}`. `code != 1` is surfaced to the user. `51000` = signature/public-param error: recompute once, then fail.
- No telemetry/"buried" endpoints are called. Final decision.

### 3.2 Auth
- Per P2: the plugin uses its own login flow. It never reads the official app's token store.
- Plugin has its own persistent `pc_id` (random uuid32, per install).
- Tokens are stored via the OS secret store (Windows DPAPI/Credential Manager, macOS Keychain, Linux libsecret). Never plaintext on disk and never in logs.
- Re-login policy: on MQTT rc=5 or HTTP auth failure, re-login once. A second failure within 60 s stops and shows an error with no loop. (The v1 "token-age > N min" placeholder is dropped. Replace it only if P3 testing shows a need.)

### 3.3 MQTT layer
- Host `mqtts://mqtt-universe.anycubic.com:8883`, TLS 1.2–1.3, MQTT v3.1.1, keepalive 30 (verified), clean_session=1.
  **OPEN:** v1 said "CRLF connections", which is unclear. Confirm the intended meaning.
- Credentials per protocol doc §2. Password = base64(RSA-PKCS1(jwt)) using the embedded CA public key. **OPEN:** v1 also mentioned "OUR HMAC" and cut off mid-sentence. HMAC appears nowhere else in the doc, so confirm whether it's needed.
- SUB `anycubic/anycubicCloud/v1/+/public/{machine_type}/{unionid}/#` at QoS 1.
- Handler: `status/report` → StateStore. `response` → match pending command by msgid.
- Reconnect uses exponential backoff with jitter (1 s → 60 s cap) and a hard stop after N consecutive rc=5 failures.
- **v0.6.2 host-permission recovery:** OrcaSlicer audits `socket.bind` and `socket.connect`, and paho always passes `source_address=("", 0)` to `socket.create_connection`, even with no configured bind address. The permission request therefore includes `"('', 0)"` up front next to the broker host/port. If a connect is denied while the permission prompt is still open, the MQTT worker retries with 2 s → 30 s exponential backoff instead of exiting. Approving the prompt is then enough to finish connecting; the user does not need to press Start Monitoring again. Symptom without this: `[AUDIT BLOCKED] event=socket.bind reason=audit permission required`, then a dead worker and an empty dashboard.
- TLS: the broker rejects OpenSSL 4.x ClientHello (alert 40/71). **OPEN:** test OpenSSL 3.x on Win/macOS/Linux. If the plugin runs inside Orca's process, check for symbol/ABI conflicts with Orca's own OpenSSL, and prefer a statically linked or isolated TLS stack. On handshake failure, show a clear error naming the TLS version.

### 3.4 Upload pipeline (F5, verified live)
G-code from the watched folder → `lockStorageSpace {size, name, is_temp_file:0}` → S3 `PUT <preSignUrl>` (raw bytes, Content-Length, no auth header) → `newUploadFile {user_lock_space_id}` → `unlockStorageSpace {id, is_delete_cos:0}`. On any failure unlock with `is_delete_cos:1` so the cloud does not keep an orphaned object.
- The plugin does not slice; OrcaSlicer already writes `.gcode` files. It watches `~/Downloads` by default and offers the newest files.
- Upload progress comes from the S3 `PUT` body reader; phases are lock/upload/claim/unlock.
- Only one upload runs at a time. There is no cancel button yet; the presigned URL is valid for 30 min, and the plugin re-requests a lock for every attempt.
- **v0.6.1 watcher:** directory enumeration never runs on the UI thread. One daemon thread (`anycubic-watch`) scans the configured folder every 5 s or on demand, and `build_status()` / page polling only copy a cached snapshot. A scan still running after 20 s is reported as `stalled`, with a message pointing at macOS file-access permission. The dashboard shows `scanning`, `stalled` and `scan_age` states and keeps the last good file rows during a refresh. This was added after `os.scandir(~/Downloads)` blocked the OrcaSlicer main thread inside a WebView message callback and froze the app.
- **v0.6.3 watch permission fix:** OrcaSlicer documents `<root>/*` as granting the folder itself and its whole tree, but its audit hook still blocked `os.scandir(~/Downloads)` when only `~/Downloads/*` was granted, producing an `[AUDIT] block path=.../Downloads` line every 5 s. The plugin now requests both `<root>` and `<root>/*` for each watch root. The extra exact-folder grants may trigger a one-time permission prompt on the first launch after upgrading.

### 3.5 Staleness
`status/report` is about every 300 s, so the UI must show "last updated N s ago" and mark data stale after about 2× the interval. Do not label F3 "live" in the UI.

### 3.5.1 Distribution (Orca Plugin Hub)
- The hub installs one entry file per plugin (`.py` or `.whl`); sibling files are not copied. The upload artifact is `dist/anycubic_cloud_any.py`: a single-file PEP 723 plugin with the CA certificate and broker public key embedded as `CA_PEM` / `BROKER_PUB_PEM`.
- The `tls/` directory remains an optional local override. If `ca.pem` / `broker_pub.pem` are absent (hub install), the plugin uses the embedded copies for TLS verification and MQTT password encryption.
- The `_any.py` suffix marks the artifact as universal. Dependencies are declared in the PEP 723 block: `paho-mqtt>=2.0.0`, `cryptography>=41.0.0`.
- Installed hub copies write their `state.json` next to the installed entry file, same as a side-loaded plugin.

### 3.6 Start-print safety (F6)
A remote start must:
1. Require an explicit confirmation: the dashboard shows the printer and file name and states that the printer starts immediately. After it starts, it can be paused or stopped from the same dashboard.
2. Refuse unless the printer is online (`device_status == 1`) and reports idle (`is_printing != 2`, MQTT state not busy/paused, and no active cloud project). The guard runs both before the upload and again immediately before `sendOrder`, so a job that starts mid-upload is not interrupted.
3. Keep Upload and Upload + print separate buttons. A plain upload never starts a print.
4. Re-check the printer state on the worker thread, never on the UI thread.

### 3.7 Pause / resume / stop safety (F8)
- These are print-task controls over HTTP, not MQTT: `POST /work/operation/sendOrder` with `order_id` 2 (pause), 3 (resume) or 4 (stop), plus `printer_id`, the active `project_id`, and `data`/`ams_info`/`settings` set to `null`.
- The active task comes from `GET /work/project/getProjects?page=1&limit=20`. Filter by the selected `printer_id` and an active `print_status` (1 printing, 4 downloading, 5 checking, 6 preheating, 7 slicing); the API returns newest first. The result is cached and refreshed on a background thread at most every 5 s, so `build_status()` never performs network I/O.
- A job is paused when `pause != 0` while `print_status == 1`. Pause/resume are refused outside `print_status == 1`, pause is refused when already paused, and resume is refused when not paused.
- The cached project is re-fetched immediately before every order. A stale dashboard therefore cannot act on a job that has already finished.
- The dashboard shows Pause, Resume and Stop only as enabled when the active job and printer state allow them. Every action opens an inline confirmation; Stop explicitly warns that the job will be cancelled. The page message must include `confirm: true`; a missing confirmation is rejected server-side before any request.
- Stop uses order 4 (`STOP_PRINT`). The force-stop order 44 is not exposed.
- Commands run on a worker thread with a single in-flight guard. The cached project is patched optimistically after an accepted order and reconciled by the next refresh.
- Nothing here runs automatically. A print is only paused or stopped by an explicit user click.

### 3.8 Webcam (F10, KVS verified live 2026-10-27; Agora added 2026-10-30)
The cloud camera has two transports. Newer firmware publishes through Agora ("shengwang") SD-RTN; older firmware publishes an AWS Kinesis Video Streams (KVS) WebRTC master on a channel named after the printer key. Neither is HTTP/HLS: there is no snapshot or MJPEG URL. Playback needs a signaling exchange and, once connected, a UDP media path.

Sequence implemented by the plugin:
1. `POST /work/operation/sendOrder {"order_id":"1001","printer_id":<id>,"shengwang_rtc_support":true}`. The order id must be a string and the flag top-level; without them the cloud answers "Video service upgraded. Update the slicer to enable." `data.msgid` is the camera session id. `data.shengwang` selects the Agora path; `data.token` contains temporary AWS STS credentials (`region`, `sessionToken`, `tmpSecretId`, `tmpSecretKey`) for the legacy KVS path. Credentials stay in Python memory.
2. `POST https://kinesisvideo.<region>.amazonaws.com/describeSignalingChannel` `{"ChannelName":<printer key>}` (SigV4, rest-json) → channel ARN.
3. `POST .../getSignalingChannelEndpoint` with `{ChannelARN, SingleMasterChannelEndpointConfiguration:{Protocols:["WSS"], Role:"VIEWER"}}` → the viewer WSS host (e.g. `wss://v-xxxx.kinesisvideo.us-east-2.amazonaws.com`).
4. Python SigV4-presigns that WSS URL with `X-Amz-ChannelARN`, `X-Amz-ClientId` and `X-Amz-Expires=299`. Only this URL is posted to the dashboard; the AWS secret key never leaves the plugin.
5. MQTT publish `anycubic/anycubicCloud/v1/web/printer/{machine_type}/{unionid}/video` `{"type":"video","action":"startCapture","timestamp":<ms>,"msgid":"<uuid>","data":null}`. The printer answers on `video/report` (`initSuccess`, `pushStarted`), which the dashboard uses as a secondary health signal.
6. The dashboard opens the WSS URL and sends `SDP_OFFER` / `ICE_CANDIDATE`; the answer arrives as base64 JSON in `messagePayload`. The offer must be restricted to H.264 with `RTCRtpTransceiver.setCodecPreferences`; the default multi-codec Chromium offer gets an empty SDP answer.
7. Stop publishes `stopCapture` and sends order 1002. Closing the dashboard also stops the camera before MQTT is disconnected.

Agora path (replaces steps 2–6; no MQTT command is used):
1. Python POSTs the channel and `rtc_token` to Agora's access point (`https://webrtc2-ap-web-1.agora.io/api/v2/transpond/webrtc?v=2`, with three backups) and receives the edge address, ticket and per-edge DTLS fingerprint.
2. Python turns the browser's SDP offer into Agora's ORTC form, wraps the channel AES key with the SDK's RSA-OAEP public key, and builds `join_v3`.
3. The dashboard opens the edge websocket (`wss://<ip-with-dashes>.edge.agora.io:<port>`) because Orca's plugin sandbox cannot grant the dynamic `*.edge.agora.io` host. Every gateway frame is relayed to Python, which subscribes to the announced video SSRC and returns the SDP answer. The browser remains the WebRTC peer; Python never carries media.
4. Pings run every 3 s from the dashboard. A gateway crypt error, token expiry, lost media path or socket close fails the panel and closes order 1002.

Details:
- **STUN-only ICE.** The KVS ICE endpoint is a per-channel `r-*.kinesisvideo` host that is unknown until the camera is open. Orca grants URL and socket targets, not host wildcards, so the plugin does not call the KVS ICE API. Public STUN (`stun.l.google.com:19302`, `stun1.l.google.com`, `stun.cloudflare.com:3478`) is enough on a normal home network and was verified live; TURN fallback is not implemented. Symmetric NAT/CGNAT networks will fail with a clear "No video arrived" message after 20 s.
- **Permissions.** `network_http` and `network_socket` include the five fixed regional `kinesisvideo.<region>.amazonaws.com` control-plane hosts plus the four fixed Agora access-point domains. The WSS media hosts (AWS `v-*`/`r-*` and Agora `*.edge.agora.io`) are contacted by the dashboard WebView, not by Python, so they need no plugin grant.
- **Version note.** v0.7.0 added the panel; v0.8.3 adds Agora and the corrected order-1001 shape. The camera is viewer-only and never touches the active print.
- **Failure handling.** Any failure after order 1001 closes the order (`1002`) and publishes `stopCapture` where possible (KVS only). Start/stop use an in-flight guard plus a generation counter so a cancelled or superseded start cannot leave a camera session open. A superseded Agora worker cannot post frames or close the replacement session. `on_unload` publishes `stopCapture` synchronously, then finishes order 1002 and the MQTT disconnect on a worker thread.
- **Open:** OrcaSlicer's WebView must support `RTCPeerConnection`, `RTCRtpReceiver.getCapabilities`, WSS connections to AWS/Agora and outbound STUN/UDP. This is verified in Chromium but not yet inside Orca (G7). If the WebView blocks either WSS host, playback needs a native bridge and this design must be revisited. The live Agora order-1001 response is untested because the printer was offline during development.

## 4. Device integration options
- A. Virtual device profile: best UX, needs deep Orca internals.
- B. Sidecar dialog + status tab: v1 fallback.
- C. Folder-watch scheduler: rejected (compatibility hack).
Choice is made after spike P1.

## 5. Test gates
- G0 spike: plugin loads in Orca (P1), and the P3 session-interaction test is done.
- G1 signed-request self-test (`/portal/index/reason` returns code 1).
- G2 login + getPrinters replay.
- G3 MQTT connect/SUB/downlink.
- G4 first upload + `sendOrder` → printer acknowledges the job. **Requires explicit user consent and an attended, online printer.** Upload, start print and the first live print are verified (2026-10-27).
- G6 first live pause/resume/stop through the dashboard. **Requires explicit user consent; pause/resume/stop interrupt the current print.** The active-project lookup and control eligibility are verified read-only, but no control order has been sent yet.
- G7 webcam inside OrcaSlicer's WebView: confirms `RTCPeerConnection`, `setCodecPreferences`, the AWS WSS connection and STUN/UDP media. The full sequence (orders 1001/1002, MQTT `startCapture`/`stopCapture`, KVS signaling, 640×480 H.264 decode) is verified live in Chromium; only the host WebView is unverified.
- G5 removed (not needed for v1).

Test scripts (`e2e_flow.py`, `mqtt_standalone.py`) must live in the repo, not `/tmp`.
Captured fixtures must be scrubbed (tokens, unionids, device IDs, pc_id) before committing.

## 6. Work plan
1. G0 spike (P1, P3).
2. Resolve P2, P4, P5, P6.
3. HTTP module (AuthSvc, signed client, F1/F2), unit-testable against scrubbed fixtures.
4. MqttSvc, with parity to `mqtt_standalone.py`, plus backoff.
5. UI: printer selector and status card with staleness.
6. ⚠ Gated: F5–F7 after G4 passes.