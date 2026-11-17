# Evonet — Evonic Workplace Connector

Evonet is a lightweight Go binary that runs on any device and connects it to an Evonic server via WebSocket. It enables agents to execute commands on that device without requiring SSH access, port forwarding, or a public IP address — similar to a reverse-tunnel, but purpose-built for agent workloads.

## How It Works

```
Agent (Evonic) ──── WebSocket (outbound) ──── Evonet (your device)
                                                     │
                                              executes commands
                                                 locally
```

Evonet makes an outbound connection to the Evonic server and waits for JSON-RPC requests (bash, python, file read/write). Results are sent back over the same connection. Because the connection is outbound from the device, no firewall rules or port forwarding are needed.

## Getting the Binary

### Download from the Evonic UI (easiest)

On a Workplace's detail page, click one of the platform buttons under **Download Pre-configured Binary**. The downloaded binary has your server URL and credentials pre-embedded — just run it, no pairing step needed.

```bash
chmod +x evonet-linux-amd64
./evonet-linux-amd64 run
```

### Build from source

Requires Go 1.21+. All build outputs go into `dist/`.

```bash
cd evonet/
make build-all       # all platforms → dist/

make build-linux     # dist/evonet-linux-amd64
make build-macos     # dist/evonet-darwin-arm64 + dist/evonet-darwin-amd64
make build-windows   # dist/evonet-windows-amd64.exe
make build           # current platform → dist/evonet
```

To make platform binaries available for download in the Evonic UI, run `make build-all` on the server — the UI reads from `evonet/dist/`.

### Release archives (GitHub Releases)

Every platform has a stable archive name, so a release page can be refreshed by re-uploading the same assets. All archives are written to `dist/`.

| Asset | Contains | Make target |
|-------|----------|-------------|
| `evonet-linux-amd64.zip` | `evonet-linux-amd64` (CLI with GUI) | `make release-linux-amd64` |
| `evonet-linux-arm64.zip` | `evonet-linux-arm64` (headless CLI) | `make release-linux-arm64` |
| `evonet-macos-arm64.zip` | `evonet-darwin-arm64` (headless CLI) | `make release-macos-arm64` |
| `evonet-macos-amd64.zip` | `evonet-darwin-amd64` (headless CLI) | `make release-macos-amd64` |
| `evonet-windows-amd64.zip` | `evonet-windows-amd64.exe` (GUI) | `make release-windows-amd64` |
| `evonet-macos-arm64-app.zip` | `Evonet.app` bundle (macOS only) | `make release-gui-macos` |
| `checksums.txt` | SHA-256 for every archive built | `make release-checksums` |

```bash
make release             # all 5 CLI archives + dist/checksums.txt
make release-all         # make release + the macOS GUI bundle (run on a Mac)
make release-macos       # just the two macOS CLI archives

make release-linux-amd64 # single platform, builds the binary then the archive
```

`checksums.txt` covers every archive present in `dist/`, so build the GUI archive before the checksums if you want it included — `make release-all` does this in the right order:

```bash
make release-all
cd dist && shasum -a 256 -c checksums.txt
```

Upload the archives and `checksums.txt` to the matching GitHub Release:

```bash
gh release create v1.2.8 dist/evonet-*.zip dist/checksums.txt \
  --repo anvie/evonic --title "Evonet v1.2.8" --generate-notes
```

The macOS GUI archive contains an ad-hoc-signed `Evonet.app`; it is not Developer ID signed or notarized, so users must right-click → Open on first launch (see the Gatekeeper note below).


### Building GUI binaries (Windows & macOS)

The Windows and macOS binaries include a desktop GUI — a log window with a Stop button — so non-technical users can run Evonet by double-clicking without opening a terminal. Linux builds are always headless (no GUI, no extra dependencies).

#### macOS (GUI)

**Option A — build on a Mac** (easiest):

```bash
# Install Xcode command line tools if not already installed
xcode-select --install

cd evonet/
CGO_ENABLED=1 GOOS=darwin GOARCH=arm64 go build -trimpath -ldflags="-s -w" \
  -o dist/evonet-darwin-arm64 .

# For Intel Macs:
CGO_ENABLED=1 GOOS=darwin GOARCH=amd64 go build -trimpath -ldflags="-s -w" \
  -o dist/evonet-darwin-amd64 .
```

**Option B — cross-compile from Linux via fyne-cross** (requires Docker):

```bash
go install github.com/fyne-io/fyne-cross@latest
make build-gui-macos
```

> **macOS Gatekeeper note:** Binaries built outside the Mac App Store may be blocked on first run.
> Right-click → Open → Open to bypass the warning, or sign/notarize the binary for distribution.

#### Windows (GUI)

**Option A — cross-compile from Linux** (requires `mingw-w64`):

```bash
sudo apt-get install mingw-w64
make build-gui-windows-native   # → dist/evonet-windows-amd64.exe
```

**Option B — cross-compile via fyne-cross** (requires Docker):

```bash
go install github.com/fyne-io/fyne-cross@latest
make build-gui-windows
```

The Windows binary is built with `-H windowsgui` so no console window appears when double-clicking.

#### Using the GUI

When a pre-configured binary (embedded config) is double-clicked on Windows or macOS, a window opens automatically:

- **Log area** — shows connection events and activity in real time
- **Stop button** — cleanly disconnects and closes the window

To run in headless mode on a desktop OS (e.g. as a service), pass `--no-gui`:

```bash
./evonet-darwin-arm64 --no-gui
```

## Pairing

Pairing is an alternative to downloading a pre-configured binary. Use it when you want to install Evonet manually and register the device yourself.

**Step 1** — In the Evonic UI, open the Workplace's detail page and click **Generate Pairing Code**. A 6-character code (e.g., `X7KQ2M`) is shown with a 5-minute countdown.

**Step 2** — On the target device, run:

```bash
evonet pair --code X7KQ2M --server https://your-evonic-server.com
```

Credentials are saved to `~/.evonet/config.yaml`.

**Step 3** — Start the connection:

```bash
evonet run   # auto-reconnect (recommended)
evonet start # foreground, exits on disconnect
```

## Auto-register with a Join Token

Instead of creating a workplace in the UI first, an admin can enable **Evonet Join Token** in
**System Settings**. Any Evonet that presents the token gets a workplace created automatically.
Re-joining from the same device reuses its workplace (the device id is stored in
`~/.config/evonet/device_id`). Regenerating the token does not disconnect workplaces that already joined.

```bash
evonet join --server https://your-evonic-server.com --token ejt_xxx [--name my-node] [--workdir /srv/work]
evonet run
```

### One-line install

Downloads the right release binary, verifies its checksum, joins, and installs a systemd (Linux) or
launchd (macOS) service:

```bash
curl -fsSL https://raw.githubusercontent.com/anvie/evonic/main/evonet/scripts/install.sh \
  | sh -s -- --server https://your-evonic-server.com --token ejt_xxx
```

Missing values are prompted for when a terminal is available. For automation pass `--yes` and set
`EVONET_SERVER` / `EVONET_JOIN_TOKEN` (keeps the token out of the process list). Other options:
`--name`, `--workdir`, `--version <tag>`, `--no-service`. For many nodes see
[`docs/ansible/install-evonet.yml`](docs/ansible/install-evonet.yml).

## Commands

| Command | Description |
|---------|-------------|
| `evonet join --token <TOKEN> --server <URL>` | Auto-register using the server's join token |
| `evonet pair --code <CODE> --server <URL>` | Pair with an Evonic server |
| `evonet start` | Connect (foreground, exits on disconnect) |
| `evonet run` | Connect with automatic reconnection |
| `evonet status` | Show current pairing status |
| `evonet unpair` | Clear credentials |

### Options for `start` / `run`

| Flag | Description |
|------|-------------|
| `--config <path>` | Path to `config.yaml` (default: `~/.evonet/config.yaml`) |
| `--server <url>` | Override server URL |
| `--token <token>` | Override connector token |
| `--workdir <path>` | Override working directory |

## Configuration

Config is loaded in priority order (highest wins):

1. **CLI flags** — `--server`, `--token`, `--workdir`
2. **`~/.evonet/config.yaml`** — written by `evonet pair`
3. **Embedded config** — JSON appended to the binary at build/download time

### `~/.evonet/config.yaml` format

```yaml
server_url: https://your-evonic-server.com
connector_token: ect_abc123...
home_id: home_xyz
home_name: My Server
work_dir: /home/user/workspace
ws_port: 8081
```

### Embedded config format (JSON)

Binaries downloaded from the Evonic UI have this appended automatically. When building manually, use `scripts/embed_config.sh`:

```bash
make embed BINARY=dist/evonet-linux-amd64 CONFIG=myconfig.json
```

```json
{
  "server_url": "https://your-evonic-server.com",
  "connector_token": "ect_abc123...",
  "home_id": "home_xyz",
  "home_name": "My Server",
  "work_dir": "/home/user/workspace",
  "ws_port": 8081
}
```

The config is appended after a magic marker (`\x00\x00EVONET_CFG\x00\x00`) and can still be overridden by `~/.evonet/config.yaml` or CLI flags.

## Running as a Service

### systemd (Linux)

```ini
# /etc/systemd/system/evonet.service
[Unit]
Description=Evonet Workplace Connector
After=network.target

[Service]
ExecStart=/usr/local/bin/evonet run
Restart=on-failure
RestartSec=5
User=ubuntu

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now evonet
```

### Launchd (macOS)

```xml
<!-- ~/Library/LaunchAgents/com.evonic.evonet.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.evonic.evonet</string>
  <key>ProgramArguments</key>
  <array><string>/usr/local/bin/evonet</string><string>run</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.evonic.evonet.plist
```

## Supported Operations

When connected, agents can use these operations through a Workplace:

| Operation | Description |
|-----------|-------------|
| `exec_bash` | Execute a bash script, returns stdout/stderr/exit_code |
| `exec_python` | Execute a Python script |
| `read_file` | Read a file from the device filesystem |
| `write_file` | Write a file to the device filesystem |

All operations respect the configured `work_dir` as the working directory.

## Reconnection Behavior

`evonet run` uses exponential backoff (1 s → 30 s max) when the connection is lost or the server is unreachable. It logs connection events to stdout.

`evonet start` exits immediately on disconnect — suitable for external supervisors.

## Security

- The connector token is a permanent secret — keep `~/.evonet/config.yaml` owner-readable only (`chmod 600`).
- Pairing codes expire after 5 minutes (configurable via `CONNECTOR_PAIRING_CODE_TTL` on the server).
- All traffic travels over the authenticated WebSocket connection — no additional per-request auth.
- `exec_bash` and `exec_python` run with the OS privileges of the `evonet` process — use a least-privilege user.

## Project Structure

```
evonet/
├── dist/                          pre-built binaries (git-ignored)
│   ├── evonet-linux-amd64
│   ├── evonet-darwin-arm64
│   ├── evonet-darwin-amd64
│   └── evonet-windows-amd64.exe
├── main.go                        CLI entrypoint
├── cmd/
│   ├── pair.go                    evonet pair
│   └── start.go                   evonet start / run / status / unpair
├── internal/
│   ├── config/
│   │   ├── config.go              Config struct, Load(), Save(), ApplyCLI()
│   │   └── embedded.go            Read config embedded in binary after magic marker
│   ├── gui/
│   │   ├── gui.go                 Fyne window — log area + stop button (Windows/macOS only)
│   │   ├── logwriter.go           io.Writer adapter: log → Fyne widget (Windows/macOS only)
│   │   └── gui_stub.go            No-op stub for Linux builds
│   ├── ws/
│   │   └── client.go              WebSocket client (gorilla/websocket), auto-reconnect
│   └── executor/
│       ├── executor.go            JSON-RPC dispatcher
│       ├── bash.go                exec_bash handler
│       ├── python.go              exec_python handler
│       └── file.go                read_file / write_file handlers
├── Makefile                       build targets (outputs to dist/)
└── scripts/
    └── embed_config.sh            append config to an existing binary
```
