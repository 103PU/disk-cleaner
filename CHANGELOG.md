# Changelog

All notable changes to **Disk CleanUp** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [2.0.0] - 2026-09-11

### 🚀 Major Architectural Rewrite
- **Native Desktop Shell**: Removed the legacy v1 localhost HTTP server (`http://127.0.0.1:8342`). Replaced with an in-process native desktop application powered by `pywebview` 5.4 and Microsoft Edge WebView2.
- **Zero-Network Architecture**: The application opens zero network sockets and binds to no listening ports, completely neutralizing the following vulnerabilities identified in the v1 security audit:
  - `SEC-01`: Unauthenticated CORS wildcard `/api/*` endpoint.
  - `SEC-02`: Arbitrary path execution via shell launch.
  - `SEC-03`: Localhost DNS rebinding attack surface.
- **Strict Content Security Policy**: Embedded HTML/JS environment operates under `connect-src 'none'`, `font-src 'none'`, and disables inline script execution.
- **Opaque Handle Safety**: Destructive APIs accept randomized opaque `node_id` tokens rather than raw filesystem paths, guarded by canonical path fence verifications (`guard.py`).
- **Two-Stage Plan/Execute Handshake**: Deletions strictly require redeeming a single-use preview token (`plan_token`) with a 600-second TTL.

### ✨ Added Features
- **49 Curated Cleanup Targets**: Expanded catalogue from v1's 14 targets to 49 comprehensive system, developer, and application cache targets across 4 risk tiers (`SAFE`, `REBUILDABLE`, `CAUTION`, `DANGEROUS`).
- **Project Sweeper**: Deep scans arbitrary user-selected roots (e.g. `E:\PROJECT`) for abandoned `node_modules`, `.venv`, and build artifacts (`dist`, `build`, `target`, `out`) older than a configurable idle threshold. Active projects modified within the threshold are strictly protected.
- **Multi-Volume Disk Explorer**: Real-time visual disk analyzer with interactive SVG treemaps and breadcrumb drill-down for any mounted partition (C:, D:, E:...).
- **VSS Restore Point Manager**: Dedicated management interface for Windows Volume Shadow Copies. Allows viewing snapshots, purging stale backups, and applying storage ceiling caps (e.g. 2 GB or 10%).
- **Docker / WSL2 VHDX Compaction**: Programmatic automation for shutting down WSL2 and executing `diskpart` compact commands on dynamic virtual hard disks.
- **Google Chrome AI Weights Blocker**: Permanent directory lock with `+s +h +r` system attributes preventing automatic 4 GB downloads of Gemini Nano `weights.bin`.
- **Bilingual Interface**: Seamless runtime switching between Vietnamese (`VI`) and English (`EN`) with 100% dictionary key symmetry and zero page reload.
- **High-DPI & Elevation Handoff**: Embedded Windows application manifest supporting `PerMonitorV2` High-DPI and seamless UAC elevation handoff.

### 📦 Packaging & Distribution
- **Standalone Portable Bundle**: PyInstaller 6.11 onedir distribution (`dist/DiskCleanUp/`) with a total footprint of only 31.49 MB (well under the 60 MB budget).
- **Windows Installer**: Inno Setup 6 installer (`dist/DiskCleanUp-Setup-2.0.0-x64.exe`, 12.7 MB) with WebView2 runtime detection and clean uninstaller support.
- **Automated Build Pipeline**: Single-command PowerShell build script (`build.ps1`) executing environment checks, pytest quality gate, PyInstaller bundle compilation, and Inno Setup installer generation with SHA256 checksums.


### 🧪 Quality & Reliability
- **Automated Test Suite**: 959 passing tests across 22 test modules running in under 18 seconds.
- **Static Analysis**: Clean `ruff` linting and strict `mypy` type checking across all 21 source engine and shell modules.
- **UI Asset Integrity**: Automated verification ensuring 0 missing i18n keys, 0 dead keys, 0 orphan CSS classes, and 0 dead stylesheet rules.

---

## [1.0.0] - 2026-07-26
- Initial v1 prototype using a local Python HTTP server and single-page web dashboard.
