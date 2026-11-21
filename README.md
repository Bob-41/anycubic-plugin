# Anycubic Cloud for OrcaSlicer

**Live status, cloud uploads, confirmed print controls and the printer camera — inside OrcaSlicer.**

Unofficial community plugin for Anycubic's cloud printers. It adds an **Anycubic Cloud** tab to OrcaSlicer, signs in with the token from Anycubic Slicer Next, and drives the printer through the same cloud the mobile app uses.

| Version | Host | Platforms | Printers | License |
|---|---|---|---|---|
| 0.9.1 | OrcaSlicer 2.5+ | Windows, macOS, Linux | Anycubic FDM, global cloud | MIT |

## Overview

- **Live status** over MQTT: temperatures, state, layer, progress, times.
- **Printer picker** for every FDM printer on the account.
- **Upload and print** `.gcode` / `.gcode.3mf` from a watch folder (default `~/Downloads`) after confirmation.
- **Direct print** through an OctoPrint-compatible bridge on `127.0.0.1:5766`.
- **Pause / resume / stop**, each behind a confirmation dialog.
- **Camera** via Agora (newer firmware) or AWS Kinesis Video Streams (older).

Your password is never requested. The printer must stay on Anycubic Cloud, so **leave LAN Mode off** — LAN mode removes it from the cloud, the mobile app and this plugin.

## Requirements

- OrcaSlicer **2.5.0+** with Python plugin support.
- A **global** Anycubic account, with the printer bound to it and **online**.
- An **FDM** printer. Resin printers are not listed.
- Approve the permission prompt: `Downloads`/`Desktop`/`Documents`, `cloud-universe.anycubic.com` and S3, `mqtt-universe.anycubic.com:8883`, the Agora/KVS camera hosts, and `127.0.0.1:5766`.

## Install

1. **Plugins → Plugin Hub**, search **Anycubic Cloud**, then **Subscribe / Install**. Or download `anycubic_cloud_any.py` from [Releases](https://github.com/Bob-41/anycubic-plugin/releases/latest) and use **Plugins → Install from file**.
2. Accept the permissions. If no tab appears, restart OrcaSlicer.
3. `paho-mqtt` and `cryptography` install automatically on first load; the first start can take up to a minute.

## Sign in

The plugin needs the **RS256 access token** from Anycubic Slicer Next — not your password, and not the bridge API key.

1. Sign in to Anycubic Slicer Next and confirm your printer appears.
2. In OrcaSlicer open **Anycubic Cloud → Anycubic Login** (or **Config**).
3. The token is pre-filled from Slicer Next logs when found; otherwise paste it.
4. Click **Sign in → choose a printer**. Monitoring starts automatically when a printer is selected.

If detection fails, search the newest log for `eyJhbGciOiJSUzI1NiIs` and copy the whole token:

- Windows: `%APPDATA%\AnycubicSlicerNext\log`
- macOS: `~/Library/Application Support/AnycubicSlicerNext/log`
- Linux: `~/.config/AnycubicSlicerNext/log`

Newer Slicer Next builds may not log it; a process dump can recover your own token, but never share the dump. Tokens last about a year and stop working after sign-out or a password change.

## Using it

| Action | How |
|---|---|
| Status | **Anycubic Cloud** tab |
| Print from disk | **Print from folder** → file → **Upload & start print** → confirm |
| Direct print | Set up the bridge below, then **Print → Upload and Print** |
| Pause / resume / stop | **Current job** buttons, with confirmation |
| Camera | **Camera → Start camera**, then **Stop camera** when done |

The watch folder must be inside `Downloads`, `Desktop` or `Documents`.

### Direct print bridge

1. Wait for **Local bridge: Listening**, then click **Copy API key**.
2. Add a **Physical Printer** in OrcaSlicer: **Octo/Klipper**, hostname `127.0.0.1:5766`, paste the key.
3. Click **Test**, then **Print → Upload and Print**.

**Test only proves the bridge is reachable.** The plugin must also be signed in, with a printer selected and online, and no job running. Plain **Upload** only stores the file in the cloud.

**The OrcaSlicer Device tab is not the status view.** It shows a message pointing to the **Anycubic Cloud** tab for print progress, temperatures, and the camera. This local bridge only handles print uploads.

## Camera

- **Monitoring follows the selected printer.** The camera uses the cloud session that monitoring keeps alive.
- Close the camera in Anycubic Slicer Next and the mobile app: one viewer at a time.
- Agora or KVS is detected automatically. Press **Stop camera** when done.

## Troubleshooting

### "Plugin registration failed: TypeError: request_permissions()"

An early **OrcaSlicer 2.5.0-alpha** build accepts only `request_permissions(fs_read=...)`. The plugin also passes `network_http`, `network_socket` and `threading`, so registration aborts with *"incompatible function arguments"* and the plugin shows **Error**. Update OrcaSlicer to **2.5.0 final or 2.5.1** — the released API accepts all arguments. The alpha's plugin API is obsolete and not supported.

### `DLL load failed while importing _rust` (Windows)

This is an **OrcaSlicer packaging issue**, not an account problem. Orca's embedded Python 3.12 can ship `python312.dll` without `python3.dll`, which `cryptography`'s `_rust.pyd` imports. It breaks MQTT and the camera key exchange; the printer list uses plain HTTPS and still works.

With OrcaSlicer closed, check in PowerShell:

```powershell
$Orca = "C:\Program Files\OrcaSlicer"   # adjust for portable/per-user installs
$env:PYTHONPATH = "$env:APPDATA\OrcaSlicer\python\packages\cp312"
Test-Path "$Orca\python\python3.dll"
& "$Orca\python\python.exe" -c "import cryptography.hazmat.bindings._rust; print('RUST OK')"
```

If `Test-Path` prints `False`, download the official **CPython 3.12.x embeddable** zip ([x64](https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip) / [ARM64](https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-arm64.zip)) and copy only `python3.dll`:

```powershell
# Administrator PowerShell, OrcaSlicer closed. Use the arm64 URL on ARM64.
$Orca = "C:\Program Files\OrcaSlicer"
$zip = "$env:TEMP\python3-embed.zip"
Invoke-WebRequest "https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip" -OutFile $zip
Expand-Archive $zip "$env:TEMP\py312" -Force
Copy-Item "$env:TEMP\py312\python3.dll" "$Orca\python\python3.dll" -Force
Unblock-File "$Orca\python\python3.dll"
```

Re-run the check: it should print `RUST OK`. Restart OrcaSlicer.

> Do **not** rename `python312.dll` to `python3.dll`; they are different files and the fake one can crash Python. Use only the official python.org file.

An Orca update or reinstall can remove `python3.dll` again. If it is already present, install the latest **Microsoft Visual C++ 2015–2022 Redistributable** (x64) — `_rust.pyd` also needs `VCRUNTIME140.dll`.

### `ModuleNotFoundError: No module named 'cryptography'`

The dependency install was interrupted. Reinstall the plugin and let OrcaSlicer finish before closing it.

### "Not signed in" / token rejected

- Paste the **whole** token. It starts with `eyJ` and has two dots.
- Use the RS256 token, not the bridge API key.
- Take a fresh token from Slicer Next if it expired or you changed your password.
- To start over, delete `state.json` from the plugin folder, then sign in again.

### Printer list is empty

The list is HTTPS-only, so the `_rust` fix will not change it. Check:

1. You are signed in — the dashboard shows your email.
2. **LAN Mode is off** and the printer is bound to this account.
3. The printer is **FDM** and on the **global** cloud.
4. The printer is online in Anycubic Slicer Next.
5. Read the picker message:
   - **"No FDM printers on this Anycubic account."** → the cloud returned nothing. Fix the binding/LAN mode and re-login.
   - **"Could not load printers: ..."** → HTTP/auth error; the text after the colon is the cause.

### Status never updates

- Select an online printer; monitoring starts automatically.
- Approve the MQTT permission and allow outbound TCP **8883** to `mqtt-universe.anycubic.com`.
- VPNs, proxies and strict firewalls often block it.

### Camera will not start / black frame

- Select an online printer; monitoring starts automatically.
- Close the camera in Slicer Next and the mobile app.
- **Stop camera**, wait a few seconds, start again.
- Agora needs UDP; a firewall or VPN that blocks UDP connects without video.
- On Windows, fix the `_rust` error first.

### Local bridge says "Not running"

- Port **5766** is in use — close the other process and reopen the tab.
- OrcaSlicer blocked the port — re-enable the plugin and approve the permission.
- The bridge starts when the tab loads; restart OrcaSlicer after installing.

### Printing fails even though the bridge test passed

The test only checks the local connection. The plugin must be signed in, a printer selected and online, and no job running. The dashboard shows the real reason, e.g. *"The printer is offline on Anycubic Cloud."*

### Logs and bug reports

OrcaSlicer's Python log lives in `%APPDATA%\OrcaSlicer\log` (Windows), `~/Library/Application Support/OrcaSlicer/log` (macOS), or `~/.config/OrcaSlicer/log` (Linux). Include the plugin/Orca versions, OS, printer model and exact error.

**Redact:** RS256 token, workbench token, email, printer serial, bridge key, `state.json`. Never upload a process dump.

## Security and limitations

- The password is never stored. The RS256 token is exchanged for a workbench token cached in `state.json`.
- The bridge binds to `127.0.0.1` only and requires the generated key.
- No telemetry or third-party servers.
- FDM and global cloud only. Resin printers and China-region accounts are unsupported.
- Cloud-dependent: no cloud, no monitoring or printing. LAN Mode is intentionally not used.
- One camera viewer at a time. KVS is verified end-to-end; Agora depends on firmware and network.
- Unofficial API: Anycubic can change it, so updates may be required.

## License

MIT. Not affiliated with Anycubic or OrcaSlicer.
