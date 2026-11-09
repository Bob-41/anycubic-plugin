# Anycubic Cloud Plugin for OrcaSlicer

**Version:** 0.8.9
**Target:** Anycubic Kobra 2 Pro (and other Anycubic printers using the same cloud API)  
**Format:** Single-file PEP 723 Python plugin (`anycubic_cloud_any.py`)

---

## What it does

This plugin adds a **native dashboard** inside OrcaSlicer for managing Anycubic Cloud printers:

| Feature | Description |
|---------|-------------|
| **Live printer status** | Temperatures, print state, layer/progress via MQTT (300 s heartbeat) |
| **G-code upload** | Watched-folder panel (`~/Downloads` by default) → uploads to Anycubic Cloud → starts print after explicit confirmation |
| **Direct print from OrcaSlicer** | Local OctoPrint-compatible bridge on `127.0.0.1:5766`. Add it once as an **Octo/Klipper** physical printer, then **Print → Upload and Print**; the plugin uploads to Anycubic Cloud and starts the job. No export folder, no dashboard upload step. |
| **Print controls** | Pause / Resume / Stop with confirmation dialogs. Only enabled when a job is actually running. Uses HTTP `sendOrder` (orders 2/3/4), not MQTT. |
| **Webcam panel** | Opens the printer's cloud camera (order 1001) and plays its WebRTC feed. Newer firmware uses **Agora (shengwang)**; older firmware uses **AWS Kinesis Video Streams**. The plugin detects the transport from the response. Stop closes cleanly (order 1002, plus MQTT `stopCapture` on KVS). |

---

## Requirements

- OrcaSlicer with Python plugin support (macOS / Linux / Windows)
- Anycubic account (global region)
- Printer registered in Anycubic Cloud app
- OrcaSlicer permissions granted on first run:
  - Filesystem read: `~/Downloads`, `~/Desktop`, `~/Documents`
  - Network: `cloud-universe.anycubic.com`, S3, **`kinesisvideo.*.amazonaws.com`**, **`webrtc2-ap-web-*.agora.io`** / **`webrtc2-*.ap.sd-rtn.com`** (for camera)
  - MQTT socket: `mqtt-universe.anycubic.com:8883`
  - Local bridge socket: `127.0.0.1:5766` (direct print from OrcaSlicer)
- OrcaSlicer combines these grants into one confirmation when the plugin is first enabled.

---

## Install

1. Download `anycubic_cloud_any.py` (and verify `.sha256` if desired)
2. In OrcaSlicer: **Preferences → Plugins → Install from file** → select the `.py`
3. Restart OrcaSlicer
4. Open the **Anycubic Cloud** page (main window sidebar)
5. Click **Anycubic Login** → the plugin auto-detects the token from Anycubic Slicer Next logs and pre-fills it; paste it manually (or use **Open log folder**) if detection fails
6. **Select Printer** → pick your Kobra 2 Pro
7. **Start Monitoring**
8. Optional: set up **Direct print from OrcaSlicer** (see below) to send jobs without exporting to the watch folder.

---

## Usage

| Action | How |
|--------|-----|
| Direct print from OrcaSlicer | Configure the **Octo/Klipper** physical printer once (below) → **Print** |
| Upload & print | Dashboard → **Print from folder** → pick file → **Upload & start print** (confirm) |
| Change watch folder | Dashboard → **Print from folder** → **Change folder** (or the plugin's **Config** tab). Folders must be inside `~/Downloads`, `~/Desktop` or `~/Documents`. |
| Pause / Resume / Stop | Dashboard → **Current job** → buttons (confirmation required) |
| Webcam | Dashboard → **Camera** → **Start camera** → live view appears. **Stop camera** when done. |

---

## Direct print from OrcaSlicer (localhost bridge)

The plugin runs a small OctoPrint-compatible bridge on `127.0.0.1:5766`. OrcaSlicer can send a sliced job straight to it, so the watch folder and the dashboard upload button are no longer required.

1. Open the **Anycubic Cloud** page and wait for **Local bridge** to show **Listening**.
2. Copy the **API key** shown in that card (**Copy API key**).
3. In OrcaSlicer open **Printer settings → Physical Printer**, choose **Octo/Klipper** as the host type, and enter:
   - **Hostname:** `127.0.0.1:5766` (Orca hides the separate Port field for this host type, so include the port here)
   - **API key:** the key from step 2
4. Click **Test** — OrcaSlicer should report a working OctoPrint connection.
5. Press **Print**, then click **Upload and Print** in the send dialog. OrcaSlicer sends the G-code to the bridge; the plugin uploads it to Anycubic Cloud and starts it on the printer selected in the dashboard. (Plain **Upload** only stores the file in the cloud.)

Notes:

- The plugin must be loaded, signed in, and have a printer selected. The printer must be online, and the bridge refuses to start a job while another print is running.
- Both plain `.gcode` and Orca's optional `.gcode.3mf` bundle are accepted.
- The bridge listens on loopback only. The API key is generated once and stored in the plugin's `state.json`.
- The dashboard's folder panel still works; the bridge is an additional path.
- If the bridge card says **Not running**, read the error shown there: port `5766` may be in use, or the network permission may have been denied.
- The bridge does **not** expose pause/resume/stop. Those remain confirmed dashboard actions only.

---

## Security / Privacy

- Your Anycubic credentials **never leave OrcaSlicer**. The plugin exchanges the RS256 JWT for a workbench token locally.
- The local bridge binds to `127.0.0.1` only and requires the generated API key. It is not reachable from the LAN or the internet.
- AWS temporary credentials and Agora `rtc_token` / channel keys (from order 1001) stay in plugin memory. On KVS, only the **presigned WSS URL** reaches the dashboard. On Agora, the dashboard receives the edge websocket URL and the join message so the browser can be the WebRTC peer; the channel key is RSA-wrapped for Agora's edge.
- No telemetry, no external analytics, no auto-updates.

---

## Distribution artifact

```
dist/
├── anycubic_cloud_any.py      # ← upload this to Orca Plugin Hub
└── anycubic_cloud_any.py.sha256
```

The `_any` suffix is OrcaCloud's universal target. The plugin declares its dependencies (`paho-mqtt`, `cryptography`) in the header and uses only the standard library plus those packages, so the same file runs on macOS, Linux and Windows.

---

## Releasing

Every release is built from `master` and published by `.github/workflows/publish-orcacloud.yml`:

```bash
scripts/release.sh 0.8.3 "## v0.8.3
- What changed"
```

The script bumps the PEP 723 header and `PLUGIN_VERSION`, syncs both `dist/` artifacts and checksums, runs the source and packaged smoke tests, pushes, and creates the GitHub Release with `--target master`.

Always tag `master` HEAD. GitHub runs the workflow file from the tagged commit, so tagging an older commit uses that older workflow — the usual cause of a publish that "keeps failing" after a workflow fix.

---

## Verified live (2026-10-27)

- Upload → start print ✔
- Pause / Resume / Stop payloads ✔ (live command gated on explicit user consent)
- Camera (KVS, older printers): order 1001 → KVS channel ARN → viewer WSS → SigV4 presign → MQTT `startCapture` → **640×480 H.264 over STUN** ✔
- Camera (Agora, newer firmware): implemented in v0.8.3 with the corrected order-1001 shape; end-to-end verification was pending because the test printer was offline during development.

---

## License

MIT — see LICENSE (or add your preferred license).