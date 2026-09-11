# Disk CleanUp v2.0

> **Modern Windows Disk Reclamation Utility with WebView2 Desktop Shell**  
> An ultra-fast, multi-volume disk cleanup and maintenance tool designed for Windows 10/11. Built specifically to eliminate developer caches, build artifacts, orphan virtualenvs, WSL2/Docker bloated virtual disks, and unbounded VSS restore point storage.

---

## 🌟 Highlights of Version 2.0 (Những Điểm Nổi Bật)

- **Native Desktop Application**: Completely eliminated the v1 localhost HTTP server (`localhost:8342`). Built using `pywebview` on top of Microsoft Edge WebView2, communicating in-process via a type-checked Python-JS bridge loaded strictly over `file://` with strict Content Security Policy (`connect-src 'none'`, `font-src 'none'`).
- **Zero Network Exposure**: No open ports, no listening sockets, zero firewall popups, impervious to DNS rebinding or cross-origin script attacks.
- **49 Curated Targets**: Scans developer tool caches (uv, npm, pnpm, yarn, pip, Cargo, NuGet, Go, Gradle, Maven, Flutter, ccache), browser data, IDE logs, crash dumps, and Windows system update caches.
- **4-Tier Safety Model**: Targets categorized into `SAFE` (instant purge), `REBUILDABLE` (re-downloadable dependencies), `CAUTION` (user data or stateful tools), and `DANGEROUS` (irreversible, requires explicit double-confirmation).
- **Interactive Multi-Volume Disk Explorer**: Visualize disk space usage with dynamic SVG treemaps and drill down into subfolders on any mounted drive (C:, D:, E:...) using opaque handle navigation.
- **Project Sweeper**: Deep scan any directory tree (e.g., `E:\PROJECT`) for abandoned `node_modules`, `.venv`, and build folders (`dist`, `target`, `out`, `build`) based on idle age thresholds. Two-stage preview with single-use cryptographic plan tokens prevents accidental deletions.
- **Volume Shadow Service (VSS) Manager**: Visual inspector for Windows restore points with storage ceiling controls (prevents SQL Server / backup tools from consuming 15-20% of your disk).
- **WSL2 / Docker VHDX Compaction**: Automatic graceful shutdown and `diskpart` compaction of dynamic `.vhdx` disks, reclaiming tens of gigabytes.
- **Bilingual Interface**: Seamless live-switching between Vietnamese (Tiếng Việt) and English (EN) with zero reload.

---

## 🚀 Installation & Quick Start (Cài Đặt & Khởi Chạy)

### Option 1: Using the Installer (Khuyên Dùng)
Download and run the standalone installer:
```text
dist/DiskCleanUp-Setup-2.0.0-x64.exe
```
- Installs to `%ProgramFiles%\Disk CleanUp` (or per-user directory if non-admin).
- Automatically verifies or prompts for Microsoft Edge WebView2 Runtime.
- Creates Start Menu shortcut and optional Desktop shortcut.
- Clean uninstaller with prompt to preserve or delete diagnostic logs.

### Option 2: Standalone Portable Bundle
Run the executable directly without installation:
```text
dist\DiskCleanUp\DiskCleanUp.exe
```
Or launch via the root launcher script:
```cmd
run_cleaner.bat
```


### Option 3: Developer / Source Mode
```powershell
# Prerequisites: Python 3.12.x and uv
git clone <repo-url>
cd antigravity-disk-cleaner

# Sync dependencies and run
uv sync
uv run python -m adc
```

---

## 🛠️ Key Feature Modules (Các Tính Năng Cốt Lõi)

### 1. Dọn Dẹp Danh Mục (Clean View)
- Scans all 49 system and developer locations.
- Real-time preview with byte estimates, file counts, and lock status detection.
- Fast presets:
  - **An Toàn (Safe)**: 100% regenerable developer & browser caches.
  - **Mặc Định (Balanced)**: Safe + Rebuildable dependencies.
  - **Toàn Bộ (All)**: Full disk scan across all tiers.
- Two-stage execution: confirmation modal details exactly what will be removed before a single file is deleted.

### 2. Khám Phá Ổ Đĩa (Disk Explorer)
- Multi-volume selector with live drive capacity bars.
- Interactive SVG treemap showing proportional folder sizes.
- Breadcrumb navigation with sorting by size, item count, modified time, or name.
- Direct "Mở trong Explorer" (Reveal in Explorer) actions.

### 3. Dọn Dẹp Dự Án (Project Sweeper)
- Deep-walks any chosen root folder (defaulting to `E:\PROJECT`).
- Identifies forgotten directories by rule manifest validation (e.g. `node_modules` beside `package.json`, `.venv` with `pyvenv.cfg`).
- Filters by idle days (default: 30+ days untouched).
- Strictly protects active projects (modified within the threshold).
- Two-phase preview/execute workflow backed by 600-second plan tokens.

### 4. Quản Lý Bản Sao Bóng (VSS & Restore Points)
- Displays all active Volume Shadow Copies on each partition.
- Sets storage bounds (e.g. 2 GB or 10% ceiling) via programmatic Win32 vssadmin integration.
- Safely deletes stale snapshots without impacting disk integrity.

### 5. Khóa Tệp AI Chrome 4GB (Chrome AI Weights Blocker)
- Permanently blocks Google Chrome from silently downloading Gemini Nano model weights (`weights.bin` ~4 GB) into your user profile.
- Replaces target with an immutable filesystem directory lock with System (`+s`), Hidden (`+h`), and Read-only (`+r`) attributes.

---

## 🏗️ Architecture & Security Model (Kiến Trúc & Bảo Mật)

```text
┌────────────────────────────────────────────────────────┐
│             PyWebView (Edge WebView2)                  │
│       file://index.html  (connect-src: 'none')         │
│   ┌────────────────────────────────────────────────┐   │
│   │ UI Views: Overview | Clean | Explorer | Sweeper│   │
│   │ UI Components: Tables, Stats, Console, Modals  │   │
│   └───────────────────────┬────────────────────────┘   │
└───────────────────────────┼────────────────────────────┘
                            │ In-Process JS-Python Bridge
┌───────────────────────────▼────────────────────────────┐
│                    adc.shell.bridge                    │
│   - Opaque handles (node_id) & Plan Tokens (TTL 600s)  │
│   - Input sanitization & Path fences (no raw paths)    │
│   - Single-instance mutex & Admin elevation handoff    │
└───────────────────────────┬────────────────────────────┘
                            │
┌───────────────────────────▼────────────────────────────┐
│                    adc.engine.*                        │
│   catalogue | cleaner | explorer | sweeper | vssadmin  │
│   docker    | shadow  | guard    | runner  | win32     │
└────────────────────────────────────────────────────────┘
```

- **SEC-01 (No Network)**: Zero HTTP/TCP servers. Everything runs in-process via Win32 WebView2 interop.
- **SEC-02 (Handle Isolation)**: Frontend never passes raw filesystem paths to destructive endpoints. All operations reference ephemeral, randomized `node_id` handles verified by `guard.py`.
- **SEC-03 (Token Exclusivity)**: Execution requires spending a single-use preview token minted during the preview step. Tampering with parameters or selection invalidates the token.
- **SEC-04 (Junction/Reparse Safety)**: Filesystem scanner explicitly refuses to follow directory junctions or symbolic links, avoiding recursive delete traps.

---

## 🧪 Testing & Verification (Kiểm Thử & Đóng Gói)

### Running the Test Suite
```powershell
# Run full unit & integration tests (959 tests)
uv run pytest -q --tb=short

# Run static linting and type checks
uv run ruff check
uv run mypy src/adc/engine src/adc/shell
```

### Automated Build Pipeline
```powershell
# Build standalone bundle + Inno Setup installer
pwsh -File build.ps1

# Build PyInstaller bundle only (skip Inno Setup)
pwsh -File build.ps1 -SkipInstaller
```
The build artifacts are output to:
- `dist/DiskCleanUp/` (standalone executable bundle ~31 MB)
- `dist/DiskCleanUp-Setup-2.0.0-x64.exe` (full Windows installer ~13 MB)
- `dist/SHA256SUMS.txt` (cryptographic checksums)

---

## 🇻🇳 Tài Liệu Tiếng Việt (Tóm Tắt)

Ứng dụng **Disk CleanUp v2.0** giúp bạn tự động phân tích, dọn dẹp và bảo vệ dung lượng ổ đĩa Windows (đặc biệt là ổ C:) khi làm việc với các hệ thống AI Agents, Docker/WSL2 và môi trường phát triển phần mềm vốn tạo ra lượng lớn bộ nhớ đệm và file tạm.

### Các Tính Năng Đột Phá Trên v2.0:
1. **Ứng Dụng Desktop Độc Lập**: Hoàn toàn không mở cổng mạng localhost (`no HTTP server`), giao diện WebView2 mượt mà, khởi động tức thì, có file cài đặt riêng (`DiskCleanUp-Setup-2.0.0-x64.exe`).
2. **Khám Phá Ổ Đĩa (Disk Explorer)**: Bản đồ nhiệt Treemap trực quan cho tất cả các ổ đĩa C:, D:, E:..., duyệt thư mục không giới hạn độ sâu.
3. **Dọn Dẹp Dự Án (Project Sweeper)**: Quét cây thư mục dự án (như `E:\PROJECT`) để tìm các thư mục `node_modules`, `.venv`, `dist`, `build` bị bỏ quên lâu ngày mà không đụng vào các dự án đang phát triển tích cực.
4. **Quản Lý Điểm Khôi Phục VSS**: Đặt giới hạn trần 2 GB hoặc 10% cho Volume Shadow Copies, ngăn ngừa việc Windows tự động phình to ổ cứng sau khi cài đặt SQL Server hoặc phần mềm hệ thống.
5. **Nén Đĩa Ảo WSL2 / Docker**: Tự động tắt WSL2 và gọi lệnh `diskpart` nén file `.vhdx`, thu hồi ngay lập tức từ 15–20 GB dung lượng thực tế.
6. **Khóa Tệp AI Chrome 4GB**: Tạo thư mục khoá có thuộc tính bảo vệ hệ thống ngăn chặn Chrome tự động tải file mô hình Gemini Nano 4GB vào profile.

7. **Bảo Vệ Đa Tầng**: 4 mức độ rủi ro (`SAFE`, `REBUILDABLE`, `CAUTION`, `DANGEROUS`), mã xác thực xoá dùng 1 lần (TTL 600s), không bao giờ xoá nhầm thư mục quan trọng.

---

## 📄 License & Credits

- **License**: MIT License.
- **Authors**: Antigravity Contributors (2026).
- **Core Dependencies**: [pywebview](https://pywebview.flowrl.com/), [pythonnet](https://pythonnet.github.io/), Microsoft Edge WebView2.


