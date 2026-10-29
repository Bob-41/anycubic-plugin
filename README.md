# Anycubic Cloud Plugin for OrcaSlicer

**Version:** 0.8.1  
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
| **Webcam panel** | Opens the printer's cloud camera (order 1001), streams via **AWS Kinesis Video Streams WebRTC** (H.264, 640×480), plays in the dashboard. Stop closes cleanly (MQTT `stopCapture` + order 1002). |

---

## Requirements

- OrcaSlicer with Python plugin support (macOS / Linux / Windows)
- Anycubic account (global region)
- Printer registered in Anycubic Cloud app
- OrcaSlicer permissions granted on first run:
  - Filesystem read: `~/Downloads`, `~/Desktop`, `~/Documents`
  - Network: `cloud-universe.anycubic.com`, S3, **`kinesisvideo.*.amazonaws.com`** (for camera)
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
- AWS temporary credentials (from order 1001) stay in plugin memory; only the **presigned WSS URL** is sent to the dashboard WebView.
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

## Verified live (2026-10-27)

- Upload → start print ✔
- Pause / Resume / Stop payloads ✔ (live command gated on explicit user consent)
- Camera: order 1001 → KVS channel ARN → viewer WSS → SigV4 presign → MQTT `startCapture` → **640×480 H.264 over STUN** ✔

---

## License

MIT — see LICENSE (or add your preferred license).