# Anycubic Slicer Next — Cloud Protocol Reference (verified)

Consolidated reverse-engineering notes for building the OrcaSlicer plugin.
All live captures: Oct 25–27 2026, account `sankate@yahoo.com` (user_id 111128, global region).

## 1. HTTP API — hosts & signed headers

| Host | Role |
|---|---|
| `cloud-universe.anycubic.com` | global workbench API base, gateway path `/p/p/workbench/api/...` (CN variant `/p/t/workbench/api/...`), telemetry under `/j/p/buried/buried/...` |
| `api-cdp.anycubic.com` | SensorsAnalytics-compatible telemetry `POST /sa?project=production` (gzip `data_list`) |
| `workbentch.s3.us-east-2.amazonaws.com` | resource/config ZIP download via presigned AWS SigV4 URLs (bucket name literally "workbentch") |

Gateway: `APISIX/3.13.0`, `X-Powered-By: workbench-go`, per-request `X-Request-Id` (uuid).

### Request headers (exact set observed on the wire)
```
GET  /p/p/workbench/api/portal/index/reason HTTP/1.1
Host: cloud-universe.anycubic.com
Accept: */*
Accept-Encoding: identity
Content-Type:application/x-www-form-urlencoded      <- no space after colon on GETs
XX-Device-Type: pcf
XX-IS-CN: 0
XX-LANGUAGE: US
XX-Nonce: <random uuid v4 lowercase>
XX-Signature: <see formula>
XX-Timestamp: <epoch ms, 13 digits>
XX-Version: 2.0.0.3                                  <- app version string (part of signature!)
[XX-Token: <HS512 workbench JWT>]                    <- all calls after login
[Content-Type: application/json; charset=UTF-8]      <- POSTs with JSON body
[Content-Length: n]
```
- First boot request used `XX-Version: V3.0.0` (several endpoints just use `2.0.0.3` — server accepts any version as long as signature matches the header value).
- GET requests use `?`-terminated paths with empty query (e.g. `/v3/engine/getLatestVersion?`, `/v2/device/getPrintOption?`).
- HTTP method enum seen internally: `2 = GET`, `1 = POST`.

### XX-Signature — SOLVED ✔ (verified 27/27 live samples + independent replay accepted)
```
XX-Nonce     : random uuid "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
XX-Timestamp : epoch milliseconds as decimal string
XX-Version   : version string of this request's header
sig  = md5( KEY_A + ts + version + KEY_B + nonce + KEY_A )   (lowercase md5 input, UPPERCASE final hex)
KEY_A = "f9b3528877c94d5c9c5af32245db46ef"
KEY_B = "0cf75926606049a3937f56b0373b99fb"
```
- Both keys are static strings in `libcloud_sdk_cpp.dylib` (`strings` dump lines 8385/8386, adjacent).
- Captured prehash context (MD5_Update gated inside `BaseClient::SendRequest`):
  `f9b3528877c94d5c9c5af32245db46ef17929549569062.0.0.30cf75926606049a3937f56b0373b99fbd32ea01e-...-e63a5b02f9b3528877c94d5c9c5af32245db46ef`
- Server behavior validation: missing/incorrect signature+public params → `{"code":51000,"msg":"Request public parameter exception, missing mandatory public parameter"}`. Correctly signed replay → `code 1`/`200` success.
- Note: no body content participates in the signature.

### Boot HTTP flow (live, in order)
```
GET  /portal/index/reason                       (XX-Version V3.0.0 on first)
GET  /v3/engine/getLatestVersion?
POST /v3/public/loginWithAccessToken            {"device_type":"pcf","access_token":"<RS256 Casdoor JWT>","pc_id":"<pc_device_id hex32>"}
POST /farm/group/list                           {"page":1,"limit":100,"status":1,"group_type":1,"get_all":1}
POST /farm/message/list                         {"page":1,"limit":1,"status":0,"aggregate_mode":0,"update_status":0,"keyword":""}
POST /j/p/buried/buried/fdmOrcaReport           SigninFinish (telemetry)
GET  /user/public/getIpAddress
POST /portal/index/get_version                  {"platform":9,"version_code":2003}
POST /work/printer/getPrinters                  {"page":1,"limit":1000,"is_farm":0,"query_status":0,"printer_id":0,"machine_type":0,"bulk_id":0,"query_project":0,"sort_by":0,"type":"FDM","label_name":null,"printer_ids":[],"ams_info":{"use_ams":false,"padding2":0,"padding1":0,"padding":0,"ams_box_mapping":[]}}
GET  /v2/device/getPrintOption?
POST /v3/user/getSlicerConfig                   (Content-Length: 0!)
POST /j/p/buried/buried/fdmOrcaReport           AppStart
```

### Auth chain (fully replayed outside the app ✔)
1. Casdoor RS256 JWT (issuer `https://uc.maker.makeronline.com`, aud `b269a3136d26ac6488bb`, kid `ac_app_crt`, header `{"alg":"RS256","kid":"ac_app_crt","typ":"JWT"}`; claims: id/email/phone/email_verified/tokenType/scope/iss/sub/aud/exp 1797785110/nbf/iat/jti) — obtained by the app's account-login UI.
   - `POST /v3/public/loginWithAccessToken` `{"device_type":"pcf","access_token":<RS256 JWT>,"pc_id":"<hex32>"}` → TokenResponse (2416 B with full user profile).
2. `{"code":1,"msg":"Login successful","data":{"login_status":1,"token":"<HS512 workbench JWT>",...}}`
   - HS512 JWT payload: `{"user_id":111128,"create_time":...,"exp":<same exp as RS256>,"isCN":"0","mode":"prod"}` — used as `XX-Token` AND reused for MQTT.
3. `POST /work/printer/getPrinters` with XX-Token → printer list.

### getPrinters data model (live sample, printer offline)
```
id 52074, key "1e22021da407a52c640c277f6af031e1"   (= MQTT topic unionid!)
name "Anycubic Kobra 2 Pro", machine_type 20021, machine_mac "bc:fd:0c:62:32:3e"
device_status 2, reason "offline", is_printing 1, ready_status 0, status 1
machine_data {name, format "pw0Img", suffix "gcode", res_x 11520, res_y 5120, pixel 34.4, size_x/y/z 220/220/250, anti_max 8}
parameter {curr_hotbed_temp 57, curr_nozzle_temp 195}
version {firmware_version "3.1.5", need_update 0}, img https://cdn.anycubic-cloud.anycubic.com/device/kobra_2_pro_...png
```

### Device data APIs — LIVE VERIFIED ✔ (2026-10-25, signed replays from Python)
| Endpoint | Method | Notes |
|---|---|---|
| `GET /v2/printer/info?model_id=<machine_type>&type_function_id=0&id=<printer_id>` | GET | **Full printer snapshot**: `base` (firmware, mac, material, counts), `device_status`, `external_shelves` (spool: color/type), `temp_limit` + `free_temp_limit` (JSON strings: nozzle 0-260/185-260, hotbed 45-110), `parameter` (current temps), `tools[]` (stPrinterTool rows), `version` (fw update state), `quick_start_url`. Query keys alphabetical: `id, model_id, type_function_id`. |
| `GET /v2/printer/printersStatus` | GET | Compact live status for all printers: `available`, `device_status`, `reason`, `key`, `machine_type`. Sample reason: `unavailable reason:Slice file does not match machine type` (self-reported by printer). |
| `GET /v2/printer/tool?model_id=<mt>&type_function_id=<tfn>&id=<printer_id>` | GET | One tool row; response `{"code":1,"msg":"Connection successful","data":{"function_name","function_des","param":[]}}` — "Connection successful" ⇒ **live relay query to the printer** (source of the periodic `response` acks). Invalid tfn (≤0 or unknown for the model) → `code:0` "invalid PrinterToolReq.TypeFunctionId". |
| `GET /v2/device/getPrintOption?` | GET | Machine-type → optional-feature catalog (camera, auto_leveling, timelapse...). For 20021: `option: []` (no optional features). |
| `POST /work/gcode/infoFdm` | POST | `GetGcodeInfoFdmReq {id>0}` — cloud gcode file info (id required). |
| `GET /v5/printer/file/thumbnail/list`, `/v5/printer/properties/query` | — | **404 on pcf gateway** (mobile-app-only endpoints). |

### Tool model (drives the app's device-page buttons) — decoded
- `getPrinters[].type_function_ids` = [2, 22] for Kobra 2 Pro.
- `printer/info.tools[]` = `stPrinterTool { control, function_des, function_name, function_type, icon_url, id, model_id, param[], parent_id, show_place, status, typd_id(server typo of type_id), type_function_id }`.
- **For machine_type 20021 only ONE tool exists: `tfn=2 "Document Management" (control:0)`** — NO control-type tools ⇒ **the cloud app cannot jog/heat/LED this model** (matches UI: everything pre-print is grayed). tfn=22 rejected server-side even for this printer. During-print actions (pause/resume/stop/temp) belong to the print-task panel (separate API), not printerTool. → Plugin v1: **no remote control needed**; monitoring + file + start-print suffice.
- Remote control DOES exist for newer models (S1/Kobra X etc. catalog in getPrintOption) — plugin should render `printer/info.tools[]` generically so those printers get buttons automatically.

### Other CloudClient endpoints (static from `libcloud_sdk_cpp.dylib`, unverified live)
`/v3/printer/sendFile`, `/v5/mqtt/status/report`, `/v2/cloud_storage/{getPreSignUrlArrWithName,lockStorageSpace,unlockStorageSpace}`, `createBulkTask`, `requestBatchPrint`, `remotePrint`, `contrlPrinter` (sendToPrinters @sdk+0x9b638, remotePrint @0x9d480, contrlPrinter @0xeee7c, printerTool @0xefd84, login @0x179a38/0x179c38, accessToken @0x17a144), `requestSlicerInfo(uint)` → SlicerFileInfoResponse, `getMultiColorBoxInfo`, `getExternalShelves` (see nm inventory; full URL strings in `lcs_strings.txt`).
`printerTool` disassembly (arm64): query keys in alphabetical map order → `?id=<printer_id>&model_id=<model>&type_function_id=<tfn>`; sends via `BaseClient::SendRequest(UrlIndex, ...)` — full disasm saved at `/tmp/sdk_dis.txt`.

## 2. MQTT (broker `mqtts://mqtt-universe.anycubic.com:8883`)

### Auth (QUADRUPLE-verified; ★ third-party client accepted live ✔)
```
m    = md5hex(JWT)                                     # lowercase hex of HS512 workbench JWT
enc  = b64( RSA_PKCS1_encrypt(ca.crt_pub, JWT) )       # 256-byte block → 344 b64 chars
X    = "pcf_" + m + enc + "pcf_" + m
md5b = md5hex(X)
username  = "user|pcf|<email>|" + md5b
password  = enc
clientId  = "pcf_" + m                                 # ✔ VERIFIED: out2 = pcf_md5(JWT)
```
- ✔ 27/27 wire matches + formula reproduced by app on every connect (different b64 padding each time).
- ★ **CONNACK rc=0 + SUBACK + downlink received with a pure-python third-party client** (`/tmp/mqtt_standalone.py`, LibreSSL venv `/tmp/anycv-lib`) → the chain is complete.
- ⚠ **Stale JWT → CONNACK rc=5 (not authorized).** The broker decrypts `password`, validates the JWT live; tokens rotated by a later app login invalidate old ones (JWT exp unchanged 1797785110, yet dead). Plugin must (re)login before each MQTT boot.
- Username template in lib: `user|{0}|{1}|{2}`.
- Broker over Cloudflare Spectrum (172.65.173.145:8883).

### TLS specifics (broker picks hello by library!)
- Broker did NOT send CertificateRequest → **mTLS not required** for pcf MQTT (client cert `AnycubicSlicer.crt`/key bundled via `GetPems` is optional on this path).
- ✔ works: **LibreSSL** — macOS `/usr/bin/openssl` (3.3.6) negotiates TLS1.3 `AEAD-AES128-GCM-SHA256`; system python LibreSSL 2.8.3 → **TLS1.2 AES128-GCM-SHA256**.
- ✘ fails: **OpenSSL 4.x** ClientHello (Homebrew) → fatal alert 40 (TLS1.3) / alert 71 insufficient-security (TLS1.2); invariant to client cert, ALPN (mqtt/http/1.1/none), groups X25519 / X25519:P-256, app ciphersuite pinning, presence/absence of SNI.
- ✔ works: app's own stack = **OpenSSL 3.x** (libcommon_log) TLS1.3 `TLS_AES_256_GCM_SHA384`. Plugin guidance: use Paho-C linked with OpenSSL 3.x, or `cryptography`+`ssl` module of a python built against OpenSSL ≥3.1/<4.0, or LibreSSL ≥3.3.
- App's ctx config captured (v17): `SSL_CTX_set_ciphersuites TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256:TLS_AES_128_GCM_SHA256`, opts 0x82024054, ALPN http/1.1, verify mode=1.

### Topics — live-verified ✔
```
down SUB anycubic/anycubicCloud/v1/+/public/{machine_type}/{unionid}/#     ← captured on wire AND by CreateTopic hook
down SUB anycubic/anycubicCloud/v1/+/report                               (app adds these 3 narrower ones too)
down SUB anycubic/anycubicCloud/v1/+/ota/multiColorBox/+/report
down SUB anycubic/anycubicCloud/v1/+/print/report
up      anycubic/anycubicCloud/v1/slicer/printer/{}/{}/{}                  (static template; 3 slots TBD — GUI action)
up/down anycubic/anycubicCloud/v1/web/printer/{machine_type}/{unionid}/video   (camera startCapture/stopCapture + video/report)
```
- Single pattern `v1/+/public/20021/<unionid>/#` is sufficient to receive ALL downlink (response + status/report verified).
- `CreateTopic(char** out, PKc machine, S4 unionid, S4 extra, int mode)` @libcloud_mqtt+0x65f0 (mangled `CreateTopicEPPcPKcS4_S4_i`): with `a1="20021" a2=unionid a3="" a4=1` → out = the SUB pattern above; out array begins at `*out`.
- `PushMessage(HandlerRouter*, const char* a2, const char* a3, uint len)` @0x6238 — presumed send path (never fires when app idle; hook in place for GUI-command capture).
- `HandlerRouter::Router(PKc, PKc, uint)` @0x199b4 — dispatch (args recon pending; fired with junk layout in v17).

### Message envelope — LIVE VERIFIED ✔ (third-party subscriber, 2026-10-25)
```
Down:  <base>/printer/public/20021/<unionid>/response     ← {"msgid": "<uuid v4>"}   (printer ACKing cloud cmds; seen every ~30-90 s)
Down:  <base>/printer/public/20021/<unionid>/status/report ← CloudEventPack live sample:
       {"type":"status","action":"workReport","msgid":"<uuid>","state":"free","timestamp":1792958579434,"code":200,"msg":"","data":""}
```
- `state:"free"` heartbeat arrives **every exactly 300 s** (measured: 15:02:59 → 15:07:59).
- 15 downlink msgs in ~6.5 min while idle (2× heartbeat + 13× response-ack).
- note: `+/public/.../#` subscription receives both; MIXED `device/...` slots would also match the `+`.

### Message envelope (static, boost::pfr field-name symbols in `libcloud_mqtt.dylib`)
```
CloudEventPack { type, msgid, action, msg, state, timestamp, code }
```
47 `Report::*` structs with exact JSON field names (Python Mach-O symbol parse, 249 pfr symbols), notable:
- `tempature`: `curr_hotbed_temp, curr_nozzle_temp, target_hotbed_temp, target_nozzle_temp`
- `workReport` (PrintPrinting, 16 fields), `FileDetails` (19), `FileList`, `PrintDownloading` (19), `Slots`, `FeedStatus`, `MultiBoxInfoResponse` (12), `Settings`/`SettingsAdv`, `SourceInfo`, `ModelsProcessedInfo`
- latent names: `lastWill`, `onlineReport`, `autoOperation`
- Format string in lib: `recv topic:{}, payload:{}`, `unregister event: {} from topic:{}`

### Print-task flow — upload + start print VERIFIED LIVE ✔ (2026-10-27)
- File upload (v2, verified end-to-end from Python, `file_id 101639520`):
  1. `POST /v2/cloud_storage/lockStorageSpace` `{size, name, is_temp_file:0}` → `data.id` (lock id), `data.preSignUrl` (S3), `data.url`.
  2. `PUT <preSignUrl>` with the raw file bytes and `Content-Length`; no auth header (SigV4 is in the URL). `200`/empty body.
  3. `POST /v2/profile/newUploadFile` `{user_lock_space_id:<lock id>}` → `data.id` = cloud **file id**.
  4. `POST /v2/cloud_storage/unlockStorageSpace` `{id:<lock id>, is_delete_cos:0}` on success; `is_delete_cos:1` on failure (deletes the orphaned object).
- Start print (cloud file, no AMS): `POST /work/operation/sendOrder` with
  `{"order_id":1, "printer_id":<id>, "project_id":0, "data":{filetype:0, file_key:"", file_name:"", task_settings:{ai_detect:0,camera_timelapse:0}, file_id:<id>, hollow_param:null, is_delete_file:0, matrix:"", project_type:1, punching_param:null, slice_param:null, slice_size:null, template_id:0}, "ams_info":null, "settings":null}`.
  `code:1` = accepted. A freshly uploaded file can briefly return `msg:"No file found"`; retry the same order (3× / 3 s is enough).
- Print-task controls (v0.6.3): pause/resume/stop use the same `sendOrder` endpoint with order IDs **2/3/4** and
  `{"order_id":<2|3|4>, "printer_id":<id>, "project_id":<active project id>, "data":null, "ams_info":null, "settings":null}`.
  The active project comes from `GET /work/project/getProjects?page=1&limit=20`; filter by `printer_id` and `print_status` in `{1,4,5,6,7}` (newest first). A job is paused when `pause != 0` while `print_status == 1`. These are HTTP controls, not MQTT uplink commands.
- `/v3/printer/sendFile` (`send_file_to_printers_data {file_id, printer_ids[]}`) exists but is not needed for upload+print.
- `is_printing` in `getPrinters` is **1 = free, 2 = busy** (not a boolean); the plugin uses that plus MQTT `state` to refuse starting while a job is active.
- Structs: `send_file_to_printers_data { file_id, printer_ids[] }`; `RemotePrintSt { file_id, filetype, project_type }`; response `SendFileToPrintersResponse`; `PrintTaskResponse { taskid, user_id, printer_id, gcode_id, model, estimate, pause, progress, print_status, slice_status, project_type, create_time, end_time, reason }`.
- Printer control: `CloudClient::contrlPrinter(uint,uint,uint,const char*,int,AmsBoxMapping*,uint,bool)`; `stPrinterTool { type_id, model_id, type_function_id, parent_id, function_type, function_name, function_des, item, icon_url, param }`; slots via `/v2/printer/getExternalShelves` and `getMultiColorBoxInfo`.
- Other useful endpoints: `/work/gcode/infoFdm`, `/v2/device/save`, `/v5/machine_material/*`, `/farm/printer/createBulkTask`, `/farm/device/createBulkTask`, `/user/profile/userInfo`, `/v2/message/*`.

### Camera / KVS WebRTC — VERIFIED LIVE ✔ (2026-10-27)
The Kobra 2 Pro camera is an AWS Kinesis Video Streams (KVS) WebRTC master. There is no HTTP snapshot/MJPEG/HLS endpoint. The printer only pushes media after an MQTT `startCapture`.

- **Open** `POST /work/operation/sendOrder` `{"order_id":1001,"printer_id":52074}` →
  `data.msgid` (camera session id) and `data.token` = temporary AWS STS credentials:
  `{"region":"us-east-2","sessionToken":"...","tmpSecretId":"...","tmpSecretKey":"..."}`.
- **Channel** `POST https://kinesisvideo.us-east-2.amazonaws.com/describeSignalingChannel`
  `{"ChannelName":"1e22021da407a52c640c277f6af031e1"}` → `ChannelInfo.ChannelARN`
  (here `arn:aws:kinesisvideo:us-east-2:235858806732:channel/<printer-key>/1775696873326`), type `SINGLE_MASTER`, status `ACTIVE`.
- **Viewer endpoint** `POST .../getSignalingChannelEndpoint`
  `{"ChannelARN":"...","SingleMasterChannelEndpointConfiguration":{"Protocols":["WSS"],"Role":"VIEWER"}}`
  → `ResourceEndpointList[0].ResourceEndpoint` = `wss://v-dc297268.kinesisvideo.us-east-2.amazonaws.com` (per-channel host).
- Both control-plane calls are SigV4 (`kinesisvideo`, rest-json, `Content-Type: application/json`) signed with the STS credentials.
- **WSS signing** is SigV4 query presign with `X-Amz-ChannelARN`, `X-Amz-ClientId`, `X-Amz-Expires=299` plus the standard `X-Amz-Algorithm/Credential/Date/Security-Token/SignedHeaders=host`, ending in `X-Amz-Signature`. The KVS JS SDK fixture `fc268038...d6c92` matches the plugin's Python implementation exactly.
- **Start push** MQTT publish to
  `anycubic/anycubicCloud/v1/web/printer/20021/<unionid>/video`
  `{"type":"video","action":"startCapture","timestamp":<ms>,"msgid":"<uuid>","data":null}` (QoS 1).
  The printer replies on the same topic as `video/report`: `startCapture` → `initSuccess` → `pushStarted`.
- **Stop** MQTT `{"action":"stopCapture"}` → `video/report` `pushStopped`, then order **1002**
  `{"order_id":1002,"printer_id":<id>}` closes the camera session.
- **Viewer** sends `SDP_OFFER` and `ICE_CANDIDATE` over the presigned WSS; payloads are base64 JSON
  (`{"type":"offer","sdp":"..."}`). The master answers with `SDP_ANSWER` and its candidates.
- **H.264 is mandatory in the offer.** The default multi-codec Chromium offer gets an empty SDP answer.
  `RTCRtpTransceiver.setCodecPreferences()` must restrict the recvonly transceiver to `video/h264`.
- The offer must be sent within a few seconds of `startCapture`; the master only answers while the push is starting.
- **ICE:** the KVS ICE API endpoint is a per-channel `r-*.kinesisvideo` host discovered at runtime (the KVS `GetIceServerConfig` call returned HTTP 400 in testing). Public STUN alone worked live: host + srflx candidates, 640×480 H.264, 19 decoded frames / 258,992 bytes in ~4 s. No TURN is used.
- Verified viewer counters: 257 RTP packets, 19 frames, 640×480. `getPrintOption` for machine type 20021 returns `option: []`; camera presence is confirmed by `video/report`, not by the option list.
- Peripherals query order `1231` is ACKed but the printer never publishes `peripherie/report` on firmware 3.1.5; treat peripherals as unsupported.

### Plugin runtime: watch-folder scanning (v0.6.1) ✔
- The plugin never enumerates the watch folder on the OrcaSlicer UI thread. A single daemon watcher (`anycubic-watch`) owns all `isdir` / `scandir` / `stat` calls and republishes a cached snapshot; `build_status()` and the 2 s dashboard poll only copy that snapshot.
- Normal cadence is one scan every 5 s, plus an immediate scan when the Config watch folder changes or the dashboard Refresh button is pressed.
- If a scan has been in progress for more than 20 s, the snapshot is marked `stalled` and the UI explains that macOS may be waiting for file-access permission. The UI keeps the last good file list while scanning.
- Why: on 2026-10-27 `os.scandir(~/Downloads)` blocked the OrcaSlicer main thread (`os_scandir` → `open$NOCANCEL`) inside a WebView message callback and froze the whole app. A background thread plus cached snapshot prevents a TCC/file-provider stall from taking down the UI.

### Plugin runtime: MQTT permission recovery (v0.6.2) ✔
- OrcaSlicer audits socket operations before granting them. Observed denial: `[AUDIT BLOCKED] plugin=plugin event=socket.bind reason=audit permission required`.
- paho calls `socket.create_connection(..., source_address=("", 0))` on every connect, even when no bind address is configured, so the host sees a `socket.bind` before the `socket.connect`. The plugin now requests `"('', 0)"`, the broker name, and the broker IP forms in `network_socket` up front.
- A connect can still be denied while the permission dialog is open. The MQTT worker now retries `connect()` with 2 s → 30 s exponential backoff and keeps running instead of returning from `_run`. Once the user approves, the next attempt connects and the dashboard goes online without another Start Monitoring.
- Live confirmation: after approving the prompt, OrcaSlicer held `TCP 192.168.1.198:50556 -> 172.65.173.145:8883 (ESTABLISHED)` and the printer reported `device_status: 1`, `reason: "free"`.
- The persisted grants live in the plugin's `.install_state.json` under `permissions.network_socket`; a reinstall may prompt again, but recovery from a blocked first attempt is now automatic.

## 3. Conf file (deprioritized)
- `AnycubicSlicerNext.conf` (plaintext JSON + encrypted-only values):
  - obfuscated keys `8F38Vz7UeiZf7/ZcSK/y0Q==` (bool: bytes `f05dfc573ed47a265feff65c48aff2d1`)
  - `l1qde7GcMLvhaKcXOcWRLA==` (1232-byte blob = 77 AES blocks; key bytes `975a9d7bb19c30bbe168a71739c5912c`) — AES-CBC/ECB all key/IV combos + XOR families failed.
  - plaintext: `pc_device_id F6E69C68245D72FB9F06C260D5F53F5E` (telemetry pcId), `preset_folder 111128` (user id), `iot_environment 3`, region Global.
- Crypto lives in main binary (libcommon_encrypt not called at boot) — plugin does its own login instead; conf decryption still open (nice-to-have only).

## 4. Reverse-engineering environment (this machine)
- Only the ad-hoc-re-signed clone is spawnable: `/private/var/folders/.../opencode/anasl/AnycubicSlicerNext.app/Contents/MacOS/AnycubicSlicerNext` (orig: hardened runtime → SIGKILL on Frida).
- Critical offsets (arm64, current app version 2.0.0.3):
  - `libcloud_sdk_cpp.dylib`: `BaseClient::SendRequest` @0x11d84, `getSignature` @0x25b74 (NOT member-sret: x8 = returned string; args = this@x0, ts@x1, 0x0000, empty@x3), `sendToPrinters` @0x9b638, `Header::set` @0x192c14 (parses RESPONSE headers only), `HttpClient::SetUrl` @0x194a58
  - `libcommon_log.dylib`: `SSL_write` @0xb0650, `SSL_read` @0xb005c, `MD5_Update` @0x1a8718, `MD5_Final` @0x1a9238 (both exported; libcloud_sdk_cpp links libcommon_log) — plaintext HTTP over TLS visible exactly here
  - `libcloud_mqtt.dylib`: `CalculatePassword` @0x5138, publish/subscribe wrappers @0x1e2a8/0x1e51c
- Frida quoting: `readCString` overshoots NUL → use [cs()]; libc++ std::string via [ptr][size][cap] long form first byte = pointer LSB (long strings misread as SSO — byte0 even but big → must fall back to long-form deref).
- arm64 `std::string` return value = sret at `x8` (context.x8 in Frida hooks).
- v17 full-boot capture: session `a4bfdf3e-a983-4786-93d3-8431d4c8887d`, script `/Users/dhruvsharma/anycubic-plugin/fida_hooks17.js`, log `/tmp/anasl_capture17.log`; scripts dump to file themselves via libc fopen/fwrite (REA message buffer resets between polls). Frame 0x50 in MQTTW/MQTTR filters = HTTP bodies (SSL funcs shared) — filter by destination socket flow, not first byte alone.
- v23 old-version capture (1.4.1.1): session `ef2621bc-4379-4713-a055-5352816f29f3`, script `/Users/dhruvsharma/anycubic-plugin/fida_hooks23.js`, log `/tmp/anasl_capture23.log` — real symbols for `MQTTPacket_publish` (0x1aeac), `MQTTPacket_send_publish` (0x1c5f0), `setCallbacks` (0xfa0c), `MQTTAsync_connect` (0xdeac). Confirmed: **same protocol, no additional controls in old UI**.
- File inventory (test assets, /tmp): `mqtt_py_test.log` (third-party MQTT session), `mqtt_standalone.py`, `e2e_flow.py`+`e2e_refresh.log` (HTTP login replay), `anasl_tokens.json` (⚠ rotates — refresh before MQTT), venv `/tmp/anycv-lib` (LibreSSL python + paho 2.1 + cryptography), venv `/tmp/anycv` (OpenSSL4 python — unusable for this broker). Fixtures: `/tmp/printer_info.json`, `printers_status.json`, `printer_tool_tfn2.json`.

### Old build (1.4.1.1) critical offsets (arm64)
| Module | Symbol | Offset |
|---|---|---|
| libcloud_mqtt.dylib | `CloudMqtt::PushMessage` | 0x6744 |
| libcloud_mqtt.dylib | `CloudMqtt::CreateTopic` | 0x6ad0 |
| libcloud_mqtt.dylib | `CloudMqtt::CalculatePassword` | 0x5668 |
| libcloud_mqtt.dylib | `HandlerRouter::Router` | 0x1b6a8 |
| libmqtt_client.dylib | `Anycubic::MQTT::MQTTClient::publish` | 0xa768 |
| libmqtt_client.dylib | `Anycubic::MQTT::MQTTClient::subscribe` | 0xa9dc |
| libmqtt_client.dylib | `Anycubic::MQTT::MQTTClient::unsubscribe` | 0xab3c |
| libmqtt_client.dylib | `MQTTPacket_publish` | 0x1aeac |
| libmqtt_client.dylib | `MQTTPacket_send_publish` | 0x1c5f0 |
| libmqtt_client.dylib | `MQTTPacket_send_subscribe` | 0x1d33c |
| libmqtt_client.dylib | `MQTTPacket_send_connect` | 0x1ce34 |
| libmqtt_client.dylib | `MQTTAsync_connect` | 0xdeac |
| libmqtt_client.dylib | `MQTTAsync_create` | 0xdb74 |
| libmqtt_client.dylib | `MQTTAsync_setCallbacks` | 0xfa0c |
| libmqtt_client.dylib | `SSLSocket_putdatas` | 0x17c64 |
| libcommon_log.dylib | `SSL_write` | 0xb1714 |
| libcommon_log.dylib | `SSL_read` | 0xb1154 |
| libcommon_log.dylib | `MD5_Update` | 0x1a86f8 |
| libcommon_log.dylib | `MD5_Final` | 0x1a9224 |

## 5. Status / open items
- [x] MQTT auth formula (username/password/clientId) — verified on wire ×4 **and by third-party client CONNACK rc=0** ✔
- [x] MQTT TLS: works from LibreSSL 2.8.3–3.3.6 (TLS1.2/1.3) & app's OpenSSL 3.x; OpenSSL 4.x ClientHello rejected (alert 40/71) regardless of certs/ALPN/groups
- [x] MQTT downlink: `response` acks + `status/report` heartbeat (300 s cadence) received live
- [x] stale JWT → CONNACK rc=5 — relogin required before MQTT boot (plugin design constraint)
- [x] XX-Signature formula — verified 27/27 + independent replay accepted (HTTP 200/code 1 from self-built request)
- [x] Login chain: Casdoor RS256 → workbench HS512 token → printer list — replayed end-to-end from Python (`/tmp/e2e_flow.py`; fresh tokens saved)
- [x] Print-flow struct set (sendToPrinters/remotePrint/upload) — statically mapped
- [x] MQTT JSON schema (envelope + 47 Report structs) — static
- [x] DB/cloud topics observed (SUB patterns) — live; single `+/public/{machine}/{unionid}/#` suffices
- [x] CreateTopic (machine, unionid, extra, mode=1) → SUB pattern; uplink template `v1/slicer/printer/{}/{}/{}` needs slot values (requires live print)
- [x] Device data APIs (printer/info, printersStatus, printerTool) — live verified, read-only
- [x] **Control model for Kobra 2 Pro: no printerTool controls** — `printer/info.tools[]` = 1 entry (Document Management, control:0); tfn=22 rejected server-side; confirmed in both app v2.0.0.3 and v1.4.1.1. Pause/resume/stop during print use the **print-task API** (`sendOrder` orders 2/3/4, active `project_id`), not printerTool. v0.6.3 implements these as confirmation-gated HTTP controls; MQTT stays monitoring-only except for the camera's `startCapture`/`stopCapture` (§2 Camera).
- [x] **Camera / KVS WebRTC** — order 1001 → STS credentials → channel ARN + viewer WSS endpoint → presigned WSS → MQTT `startCapture`; 640×480 H.264 decoded live in Chromium over STUN; order 1002 closes the session ✔
- [x] **Upload flow** lockStorageSpace → S3 PUT → profile/newUploadFile → unlockStorageSpace — live, file id 101639520 created and deleted cleanly ✔
- [x] **Start-print order** sendOrder order 1, project_id 0, cloud file data — payload shape confirmed and **verified live** (2026-10-27)
- [x] First live `sendOrder` print — completed successfully on the Kobra 2 Pro; a second live print also ran ✔
- [ ] First live pause/resume/stop order — active-project lookup and payloads verified read-only; sending a control order needs explicit user consent (G6)
- [ ] richer downlink (tempature/feedStatus/multiColorBox events) — requires print activity
- [ ] conf-file decryption (optional)
