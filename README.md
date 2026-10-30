# Anycubic Cloud Plugin for OrcaSlicer

**Version:** 0.8.3
**Target:** Anycubic Kobra 2 Pro (and other Anycubic printers using the same cloud API)  
**Format:** Single-file PEP 723 Python plugin (`anycubic_cloud_any.py`)

---

## What it does

This plugin adds a **native dashboard** inside OrcaSlicer for managing Anycubic Cloud printers:

| Feature | Description |
|---------|-------------|
| **Live printer status** | Temperatures, print state, layer/progress via MQTT (300 s heartbeat) |
| **G-code upload** | Watched-folder panel (`~/Downloads` by default) → uploads to Anycubic Cloud → starts print after explicit confirmation |
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

---

## Install

1. Download `anycubic_cloud_any.py` (and verify `.sha256` if desired)
2. In OrcaSlicer: **Preferences → Plugins → Install from file** → select the `.py`
3. Restart OrcaSlicer
4. Open the **Anycubic Cloud** page (main window sidebar)
5. Click **Anycubic Login** → the plugin auto-detects the token from Anycubic Slicer Next logs and pre-fills it; paste it manually (or use **Open log folder**) if detection fails
6. **Select Printer** → pick your Kobra 2 Pro
7. **Start Monitoring**

---

## Usage

| Action | How |
|--------|-----|
| Upload & print | Dashboard → **Print from folder** → pick file → **Upload & start print** (confirm) |
| Pause / Resume / Stop | Dashboard → **Current job** → buttons (confirmation required) |
| Webcam | Dashboard → **Camera** → **Start camera** → live view appears. **Stop camera** when done. |

---

## Security / Privacy

- Your Anycubic credentials **never leave OrcaSlicer**. The plugin exchanges the RS256 JWT for a workbench token locally.
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