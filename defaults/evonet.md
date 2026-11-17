---
description: Evonet — lightweight Go connector that links remote devices to Evonic via WebSocket tunnel
---

# Evonet

Evonet is a lightweight Go binary that runs on any device and connects it to an Evonic server via outbound WebSocket. Agents can then execute commands (bash, Python, file I/O) on that device without SSH, port forwarding, or a public IP.

## How It Works

```
Agent (Evonic) ———— WebSocket (outbound) ———— Evonet (your device)
                                                    │
                                             executes commands
                                                  locally
```

Evonet initiates an outbound connection to the Evonic server and waits for JSON-RPC requests. Results return over the same connection.

## Directory

```
evonet/                     # Source code & build tooling
  dist/                     # Pre-built binaries (git-ignored)
    evonet-linux-amd64
    evonet-darwin-arm64
    evonet-darwin-amd64
    evonet-windows-amd64.exe
  main.go                   # CLI entrypoint
  cmd/pair.go               # evonet pair
  cmd/start.go              # evonet start / run / status / unpair
  internal/
    config/                 # Config loading (embedded → YAML → CLI flags)
    ws/client.go            # WebSocket client with auto-reconnect
    executor/               # JSON-RPC dispatcher + bash/python/file handlers
    gui/                    # Fyne desktop GUI (Windows/macOS)
  Makefile                  # Build targets
  scripts/embed_config.sh   # Append embedded config to binary
```

## Building

Requires Go 1.21+.

```bash
cd evonet/
make build-all       # all platforms → dist/
make build-linux     # dist/evonet-linux-amd64
make build-macos     # dist/evonet-darwin-arm64 + dist/evonet-darwin-amd64 (headless)
make build-windows   # dist/evonet-windows-amd64.exe (headless)
make build           # current platform → dist/evonet
```

The UI serves binaries from `evonet/dist/` — run `make build-all` on the server to populate download options.

### GUI Builds (Windows & macOS)

- **macOS**: Build on a Mac with `CGO_ENABLED=1 GOOS=darwin GOARCH=arm64 go build ...`, or cross-compile via `make build-gui-macos` (requires fyne-cross + Docker).
- **Windows**: Cross-compile from Linux with `make build-gui-windows-native` (requires mingw-w64), or via `make build-gui-windows` (requires fyne-cross + Docker).

Linux builds are always headless.

## Commands

| Command | Description |
|---------|-------------|
| `evonet join --token <TOKEN> --server <URL>` | Auto-register with the join token from System Settings (creates the workplace) |
| `evonet pair --code <CODE> --server <URL>` | Pair with an Evonic server |
| `evonet start` | Connect (foreground, exits on disconnect) |
| `evonet run` | Connect with auto-reconnect (recommended) |
| `evonet status` | Show pairing status |
| `evonet unpair` | Clear credentials |

Options for `start`/`run`: `--config <path>`, `--server <url>`, `--token <token>`, `--workdir <path>`, `--no-gui`.

## Configuration Priority (highest wins)

1. CLI flags (`--server`, `--token`, `--workdir`)
2. `~/.evonet/config.yaml` (written by `evonet pair`)
3. Embedded config (JSON appended to binary)

## Creating a Tunnel Workplace & Connecting Evonet

### Via Web UI

1. Go to **Workplaces** → **Create Workplace**
2. Select type **Tunnel**, give it a name, set workspace path in config
3. Open the workplace detail page

#### Option A — Download pre-configured binary (easiest)

1. On the detail page, click a platform button under **Download Pre-configured Binary**
2. The downloaded binary has server URL + credentials pre-embedded
3. Run it:

```bash
chmod +x evonet-linux-amd64
./evonet-linux-amd64 run
```

Double-click on Windows/macOS to launch with GUI.

#### Option B — Pair manually

1. Click **Generate Pairing Code** on the detail page (6-char code, 5-minute expiry)
2. On the target device:

```bash
evonet pair --code X7KQ2M --server https://your-evonic-server.com
evonet run
```

### Via API

**Create**:
```json
POST /api/workplaces
{
  "name": "My Server",
  "type": "tunnel",
  "config": { "workspace_path": "/home/user/workspace" }
}
```

**Generate pairing code**:
```
POST /api/workplaces/<id>/pairing-code
```

**Download pre-configured binary**:
```
GET /api/workplaces/<id>/download-binary?platform=linux-amd64
```

**Unpair**:
```
DELETE /api/workplaces/<id>/connector
```

## Running as a Service

**systemd** (`/etc/systemd/system/evonet.service`):
```ini
[Unit]
Description=Evonet Workplace Connector
After=network.target

[Service]
ExecStart=/usr/local/bin/evonet run
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now evonet
```

**launchd** (macOS `~/Library/LaunchAgents/com.evonic.evonet.plist`): launch `evonet run` with `RunAtLoad` and `KeepAlive`.

## Supported Operations

| Operation | Description |
|-----------|-------------|
| `exec_bash` | Execute a bash script |
| `exec_python` | Execute a Python script |
| `read_file` | Read a file from the device |
| `write_file` | Write a file to the device |

All respect the configured `work_dir`. Communication is JSON-RPC over WebSocket.

## Reconnection

`evonet run` uses exponential backoff (1s → 30s max) on disconnect. `evonet start` exits immediately.

## Security

- Connector token is a permanent secret — `chmod 600 ~/.evonet/config.yaml`
- Pairing codes expire after 5 minutes (configurable via `CONNECTOR_PAIRING_CODE_TTL`)
- All traffic over authenticated WebSocket
- `exec_bash`/`exec_python` run with the OS privileges of the evonet process
