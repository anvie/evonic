# Windows Installation Guide

Evonic runs natively on Windows 10/11 using Windows Terminal or PowerShell — no WSL, no Docker, no containers required.

---

## Prerequisites

| Tool | Minimum | Download |
|------|---------|---------|
| Python | 3.8+ | [python.org](https://www.python.org/downloads/) — check **"Add Python to PATH"** |
| Git | any | [git-scm.com](https://git-scm.com/download/win) |
| uv _(recommended)_ | any | [docs.astral.sh/uv](https://docs.astral.sh/uv/getting-started/installation/) |

---

## Option A — One-liner (GitHub release)

Once the project is published to GitHub, open **Windows Terminal** and run:

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
irm https://evonic.dev/install.ps1 | iex
```

---

## Option B — Install from local source (dev / pre-release)

If you have the source cloned locally, use the `-SourcePath` flag:

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
.\install.ps1 -SourcePath "C:\path\to\evonic"
```

The installer will:

1. Check Python 3.8+ and Git are available
2. Copy source files to `%USERPROFILE%\.evonic`
3. Create a Python virtual environment at `%USERPROFILE%\.evonic\venv`
4. Install all dependencies (uses `uv` automatically when available)
5. Create `%USERPROFILE%\.evonic\bin\evonic.bat` CLI wrapper
6. Add `%USERPROFILE%\.evonic\bin` to your user PATH
7. Launch the interactive first-time setup wizard

After setup completes, open a **new terminal** and run from anywhere:

```powershell
evonic start
```

---

## Option C — Manual installation

```powershell
# 1. Get the source
git clone https://github.com/anvie/evonic "$env:USERPROFILE\.evonic"
cd "$env:USERPROFILE\.evonic"

# 2. Create virtual environment
#    Recommended: uv (--seed ensures pip is included)
uv venv venv --python python --seed
#    Alternative: standard Python
# python -m venv venv

# 3. Install dependencies
#    Recommended: uv (faster)
uv pip install -r requirements.txt --python venv
#    Alternative: standard pip
# .\venv\Scripts\pip install -r requirements.txt

# 4. First-time setup wizard
.\evonic.bat setup

# 5. Start the server
.\evonic.bat start
```

Open `http://localhost:8080`.

---

## Option D — Run directly from the cloned repo

No installer needed. From the repo root:

```powershell
cd C:\path\to\evonic

# Create venv and install deps
uv venv venv --python python --seed
uv pip install -r requirements.txt --python venv

# Run
.\evonic.bat setup
.\evonic.bat start
```

`evonic.bat` in the repo root automatically uses `venv\Scripts\python.exe` when present.

---

## CLI reference

After the installer adds `evonic` to your PATH, use `evonic` from any terminal. From inside the repo, use `.\evonic.bat`.

| Linux / macOS | Windows (global) | Windows (repo) |
|---|---|---|
| `./evonic setup` | `evonic setup` | `.\evonic.bat setup` |
| `./evonic start` | `evonic start` | `.\evonic.bat start` |
| `./evonic start -d` | `evonic start -d` | `.\evonic.bat start -d` |
| `./evonic stop` | `evonic stop` | `.\evonic.bat stop` |
| `./evonic status` | `evonic status` | `.\evonic.bat status` |
| `./evonic doctor` | `evonic doctor` | `.\evonic.bat doctor` |
| `./evonic agent list` | `evonic agent list` | `.\evonic.bat agent list` |
| `./evonic skill list` | `evonic skill list` | `.\evonic.bat skill list` |
| `./evonic plugin list` | `evonic plugin list` | `.\evonic.bat plugin list` |

---

## Configuration

Run `evonic setup` for the interactive wizard, or edit `.env` directly:

```powershell
copy "$env:USERPROFILE\.evonic\.env.example" "$env:USERPROFILE\.evonic\.env"
notepad "$env:USERPROFILE\.evonic\.env"
```

Minimum required settings:

```env
LLM_API_KEY=your-api-key-here
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=qwen/qwen3-8b
SECRET_KEY=any-random-string
```

---

## Troubleshooting

### Script execution policy error

PowerShell blocks local scripts by default. Allow scripts for the current session only:

```powershell
Set-ExecutionPolicy Bypass -Scope Process -Force
.\install.ps1 -SourcePath "C:\path\to\evonic"
```

### `evonic` not recognized after install

The installer updates your **user** PATH. Open a **new** terminal window for the change to take effect.

To add it manually:

```powershell
$binDir = "$env:USERPROFILE\.evonic\bin"
[Environment]::SetEnvironmentVariable("Path", "$([Environment]::GetEnvironmentVariable('Path','User'));$binDir", "User")
```

Then open a new terminal.

### `No module named pip` during install

This happens when `uv venv` creates a minimal venv without pip. The installer uses `--seed` to fix this automatically. If you hit it manually, add the flag:

```powershell
uv venv venv --python python --seed
```

### Garbled or missing characters in terminal

`evonic.bat` sets `chcp 65001` (UTF-8) automatically. If characters still look wrong, make sure you are using **Windows Terminal** (not the legacy `cmd.exe` window) with a font that supports Unicode (e.g. Cascadia Code, Consolas).

### `python` not found or points to Windows Store

Install Python from [python.org](https://www.python.org/downloads/), check "Add Python to PATH", and restart your terminal. Verify:

```powershell
python --version
```

### Port already in use

```powershell
# Pass a different port at startup
evonic start --port 9090

# Or set it permanently in .env
# PORT=9090
```

---

## Reinstall / Uninstall

```powershell
# Full reinstall from local source
.\install.ps1 -SourcePath "C:\path\to\evonic"

# Remove everything
Remove-Item -Recurse -Force "$env:USERPROFILE\.evonic"

# Remove from PATH: Settings > System > About > Advanced system settings > Environment Variables
```
