# Disk CleanUp

<p align="center">
  <strong>High-performance, zero-network Windows disk reclamation utility and multi-volume storage analyzer.</strong>
</p>

<p align="center">
  <a href="https://github.com/103PU/disk-cleaner/releases/latest"><img src="https://img.shields.io/github/v/release/103PU/disk-cleaner?style=flat-square&color=blue" alt="Latest Release" /></a>
  <a href="https://github.com/103PU/disk-cleaner/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/103PU/disk-cleaner/ci.yml?branch=main&style=flat-square&label=CI" alt="CI Status" /></a>
  <img src="https://img.shields.io/badge/tests-959%20passed-brightgreen?style=flat-square" alt="959 Tests Passed" />
  <img src="https://img.shields.io/badge/platform-Windows%2010%20%7C%2011%20x64-0078D6?style=flat-square&logo=windows" alt="Platform: Windows x64" />
  <img src="https://img.shields.io/badge/python-3.12-3776AB?style=flat-square&logo=python" alt="Python 3.12" />
  <img src="https://img.shields.io/badge/shell-Microsoft%20Edge%20WebView2-0078D7?style=flat-square" alt="Shell: WebView2" />
  <img src="https://img.shields.io/badge/security-Zero%20Network%20Ports-success?style=flat-square" alt="Zero Network Exposure" />
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-green?style=flat-square" alt="License: MIT" /></a>
</p>

---

## 📖 Overview

Modern software engineering environments, AI agent frameworks, container runtimes, and compiler toolchains silently consume tens of gigabytes of disk space over time. Standard Windows utilities (`cleanmgr.exe` / Storage Sense) are oblivious to developer toolchains, virtual disk fragmentation, or volume shadow storage bloat.

**Disk CleanUp** is an enterprise-grade desktop utility specifically engineered for Windows developers, DevOps engineers, and power users. It safely identifies, visualizes, and purges developer tool caches, abandoned build directories, bloated Docker/WSL2 virtual disks (`.vhdx`), and unbounded Volume Shadow Copies — all without opening a single network port.

---

## ⚡ Key Highlights

- 🔒 **Zero Network Exposure (`SEC-01`)**: Completely in-process desktop architecture powered by `pywebview` and Microsoft Edge WebView2. Operates strictly over `file://` with an uncompromising Content Security Policy (`connect-src 'none'`, `font-src 'none'`). Zero open listening sockets, zero firewall prompts, immune to DNS rebinding and cross-origin attacks.
- 🎯 **49 Curated System & Developer Targets**: Granular support across 12 package managers and ecosystems (`uv`, `npm`, `pnpm`, `yarn`, `pip`, `Cargo`, `NuGet`, `Go`, `Gradle`, `Maven`, `Flutter`, `ccache`), browsers (`Chrome`, `Edge`, `Brave`), IDE logs, crash dumps, and Windows Update downloads.
- 🛡️ **4-Tier Defense-in-Depth Safety Model**: Every operation is strictly classified into `SAFE`, `REBUILDABLE`, `CAUTION`, or `DANGEROUS`. Irreversible actions require double-confirmation and are excluded from presets.
- 🧹 **Project Sweeper**: Deep-walks code workspaces (e.g., `E:\PROJECT`) to reclaim abandoned `node_modules`, `.venv`, and build targets (`dist`, `build`, `target`, `out`) older than configurable idle thresholds, while strictly protecting active repositories.
- 🗺️ **Interactive Multi-Volume Disk Explorer**: Real-time disk visualization featuring proportional SVG treemaps, breadcrumb navigation, and opaque-handle drill-down across all mounted drives (C:, D:, E:...).
- 🐳 **WSL2 & Docker VHDX Compaction**: Programmatic automation for graceful WSL2 instance shutdown and native `diskpart` compaction of dynamic `.vhdx` disks, reclaiming 15–25 GB in seconds.
- 💾 **Volume Shadow Service (VSS) Ceiling Management**: Visual inspector for restore points with storage ceiling controls (e.g., 2 GB or 10% maximum quota) preventing silent disk leaks after software installations.
- 🛑 **Chrome AI Weights Blocker**: Permanent filesystem-level directory lock (`+s +h +r` system attributes) blocking Google Chrome from silently downloading 4 GB Gemini Nano model weights into your user profile.
- 🌐 **Seamless Bilingual UI**: Instant live hot-switching between English and Vietnamese (Tiếng Việt) with 100% string key parity and zero page reload.

---

## 🚀 Installation & Quick Start

### Option 1: Standard Windows Installer (Recommended)
Download and run the official 64-bit setup executable from [GitHub Releases](https://github.com/103PU/disk-cleaner/releases/latest):
```text
DiskCleanUp-Setup-2.0.0-x64.exe (~13 MB)
```
- Installs to `%ProgramFiles%\Disk CleanUp` (or per-user directory for non-admin accounts).
- Automatically verifies Microsoft Edge WebView2 Runtime availability.
- Creates clean Start Menu shortcuts and uninstaller with diagnostic log preservation options.

### Option 2: Standalone Portable Bundle
Download the standalone archive from [GitHub Releases](https://github.com/103PU/disk-cleaner/releases/latest):
```text
DiskCleanUp-v2.0.0-windows-x64-portable.zip (~31 MB)
```
Extract and launch directly:
```cmd
DiskCleanUp\DiskCleanUp.exe
```

### Option 3: Run from Source (Development Mode)
Prerequisites: **Windows 10/11 x64**, **Python 3.12**, and **[uv](https://github.com/astral-sh/uv)**.

```powershell
# Clone the repository
git clone https://github.com/103PU/disk-cleaner.git
cd disk-cleaner

# Sync pinned dependencies
uv sync

# Launch the desktop application
uv run python -m adc

# Run in debug mode (enables WebView2 DevTools on F12)
uv run python -m adc --debug

# Run self-check headless diagnostic validation
uv run python -m adc --self-check
```

---

## 🏛️ Architecture & Security Model

Disk CleanUp enforces a layered architecture separating UI rendering, Win32 interop, argument validation, and disk manipulation primitives:

```text
┌────────────────────────────────────────────────────────────────────────┐
│                   Desktop Shell (Edge WebView2)                        │
│             file://index.html  (connect-src 'none', font-src 'none')   │
│   ┌────────────────────────────────────────────────────────────────┐   │
│   │  Views: Overview | Clean | Explorer | Sweeper | VSS | Schedule │   │
│   │  Components: Treemaps, DataTables, Modals, Terminal Console    │   │
│   └───────────────────────────────┬────────────────────────────────┘   │
└───────────────────────────────────┼────────────────────────────────────┘
                                    │ In-Process JavaScript-Python Bridge
┌───────────────────────────────────▼────────────────────────────────────┐
│                           adc.shell.bridge                             │
│   - Opaque random node_id handles (no raw filesystem paths in UI)      │
│   - Cryptographic single-use plan tokens (600s TTL)                    │
│   - PerMonitorV2 High-DPI & single-instance Win32 Mutex               │
│   - Process elevation handoff (RunAs Administrator)                    │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │ Strongly-Typed API Calls
┌───────────────────────────────────▼────────────────────────────────────┐
│                           adc.engine.*                                 │
│   catalogue | cleaner | explorer | sweeper | vssadmin | docker_vhdx   │
│   fsutil    | guard   | runner   | report  | platform_win              │
└────────────────────────────────────────────────────────────────────────┘
```

### Security Invariants

| Identifier | Security Principle | Implementation Guarantee |
| :--- | :--- | :--- |
| **SEC-01** | **Zero Network Ports** | No localhost HTTP/TCP daemon. Communication is completely in-process via Win32 WebView2 web-message channels. |
| **SEC-02** | **Handle Isolation** | Frontend DOM never receives or transmits raw filesystem paths for destructive actions; all references use randomized, ephemeral `node_id` tokens. |
| **SEC-03** | **Plan Token Exclusivity** | Every deletion strictly requires spending a single-use preview token minted during the preview step. Tampering with target IDs invalidates the token. |
| **SEC-04** | **Reparse Point Immunity** | The filesystem walker explicitly detects and refuses to traverse NTFS directory junctions or symbolic links, completely neutralizing recursive deletion loops. |
| **SEC-05** | **Path Guard Fences** | Hardcoded boundary assertions reject any request targeting critical system paths (`Windows`, `System32`, `Program Files`, user documents, or `.git` repositories). |
| **SEC-06** | **Strict CSP Enforcement** | Header policy `default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'none'; font-src 'none'` permanently eliminates script injection. |

---

## 🎯 Target Catalogue (49 Curated Targets)

Disk CleanUp organizes system and developer caches into four well-defined safety tiers:

### 1. Developer Ecosystems & Toolchains (Safe / Rebuildable)
- **Python**: `uv` cache (`uv cache clean`), `pip` cache, `pipenv`, virtualenv wheels, Poetry cache.
- **Node.js & JavaScript**: `npm` cache (`npm cache clean --force`), `pnpm` store (`pnpm store prune`), `yarn` cache (`yarn cache clean`), Bun cache.
- **Rust**: `Cargo` registry cache, git checkouts (`cargo clean`).
- **.NET & C#**: `NuGet` global-packages cache, NuGet HTTP cache, NuGet temporary scratch files.
- **Go**: `Go` build cache (`go clean -cache`), module download cache (`go clean -modcache`).
- **Java / JVM**: `Gradle` wrapper & cache, `Maven` `.m2/repository` download cache.
- **Mobile & Native**: `Flutter` engine & artifact cache, `ccache` compiler output cache.

### 2. Web Browsers & Developer IDEs (Safe / Caution)
- **Google Chrome**: Web Cache, Code Cache, GPUCache, Crashpad Dumps, Service Worker storage.
- **Microsoft Edge**: Edge Cache, EBWebView application data, GPUCache.
- **Brave Browser**: Brave Web Cache and temporary offline cache.
- **IDEs**: VS Code Cache & GPU Cache, JetBrains IDE system logs, Visual Studio telemetry scratch.

### 3. Windows System & Maintenance (Caution / Dangerous)
- **Windows Update**: `SoftwareDistribution\Download` staging directory.
- **System Logs**: CBS servicing logs, Component Based Servicing crash reports.
- **Diagnostics**: Memory crash dumps (`MEMORY.DMP`), minidump logs, WER error reports.
- **Storage**: Windows Delivery Optimization cache, `%TEMP%` scratch files, Windows Recycle Bin.
- **Virtualization**: Docker Desktop dynamic `ext4.vhdx` and WSL2 distribution disk compaction.
- **Restore Points**: Volume Shadow Service (VSS) snapshot inventory and quota adjustment.

---

## 🧹 Project Sweeper: Stale Workspace Cleaner

Developers frequently have dozens of cloned repositories containing forgotten `node_modules` or `.venv` folders that occupy hundreds of gigabytes.

```text
E:\PROJECT\
  ├── project-alpha/ (Modified 2 days ago)     ──> [PROTECTED: Actively developing]
  ├── client-portal/ (Modified 120 days ago)   ──> [OFFERED: node_modules (1.4 GB), .venv (850 MB)]
  └── archived-api/  (Modified 240 days ago)   ──> [OFFERED: target/ (4.2 GB), dist/ (320 MB)]
```

- **Manifest Validation**: A directory named `node_modules` is only considered if validated by a neighboring `package.json`; a `.venv` directory requires a valid `pyvenv.cfg`.
- **Active Project Shielding**: If any source file in the repository was touched within the configurable threshold (default: 30 days), the entire repository is protected from automated cleanup.
- **Pre-Execution Preview**: Generates an exact itemized bill of materials before presenting a confirmation dialog.

---

## 💻 Command Line Interface (CLI)

While Disk CleanUp is primarily an interactive desktop application, the executable provides headless CLI diagnostic switches:

```powershell
# Show version and metadata
DiskCleanUp.exe --version

# Run headless self-check diagnostics (validates WebView2, Win32 mutex, and elevation)
DiskCleanUp.exe --self-check

# Launch desktop GUI with Chromium DevTools enabled (F12)
DiskCleanUp.exe --debug
```

Example output of `--self-check`:
```text
renderer=edgechromium
webview2_runtime=148.0.3967.96
admin=True
single_instance=ok
http_server=none
dpi_per_monitor=True
ui=E:\PROJECT\antigravity-disk-cleaner\src\adc\ui
log=C:\Users\Administrator\AppData\Local\DiskCleanUp\logs\adc-20260911.log
```

---

## 🧪 Testing & Verification

Disk CleanUp adheres to strict software quality gates:

```powershell
# Run the automated test suite (959 unit & integration tests)
uv run pytest -q --tb=short

# Run Ruff linter and import sorting verification
uv run ruff check

# Run strict Mypy static type analysis across engine and shell boundaries
uv run mypy src/adc/engine src/adc/shell
```

### Release Build Pipeline
To produce clean standalone binaries and the Inno Setup installer locally:
```powershell
# Execute full build sequence (Tests -> PyInstaller -> Inno Setup -> SHA256 Checksums)
pwsh -File build.ps1 -Clean
```

Artifacts are output to:
- `dist/DiskCleanUp/` (Standalone executable bundle, ~31 MB)
- `dist/DiskCleanUp-Setup-2.0.0-x64.exe` (Windows Installer, ~13 MB)
- `dist/SHA256SUMS.txt` (SHA-256 integrity signatures)

---

## 📄 License & Acknowledgments

- **License**: Released under the [MIT License](LICENSE).
- **Core Dependencies**:
  - [pywebview](https://pywebview.flowrl.com/) — Desktop shell abstraction.
  - [pythonnet](https://pythonnet.github.io/) — Native Win32 CLR bridge.
  - [Microsoft Edge WebView2](https://developer.microsoft.com/en-us/microsoft-edge/webview2/) — Evergreen Chromium rendering engine.
  - [Inno Setup 6](https://jrsoftware.org/isinfo.php) — Professional Windows installer compilation.
