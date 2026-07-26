# Antigravity Disk Cleaner (ADC)

An interactive, high-end desktop-equivalent local web application designed to scan, analyze, and selectively clean developer caches, system temp files, VSS backup overhead, and virtual machine allocations on Windows systems.

ADC is particularly useful for developers using agentic AI models or intensive dev workflows that aggressively fill the C drive with log files, package caches, and system VM data.

---

## 🚀 Quick Start (Hướng Dẫn Chạy Nhanh)

1. Open the project root directory: `E:\PROJECT\antigravity-disk-cleaner`
2. Double-click the launcher script: **`run_cleaner.bat`**
3. Your default web browser will automatically open to the dashboard at: **`http://localhost:8342`**
4. Click **Scan System** to analyze current directory sizes, configure your options, and hit **Clean Selected**!

---

## 📂 Project Structure (Cấu Trúc Thư Mục Dự Án)

```text
antigravity-disk-cleaner/
├── run_cleaner.bat             # Root launcher script (starts server & opens browser)
├── README.md                   # Comprehensive application documentation (this file)
└── src/                        # Source files
    ├── cleaner_backend.py      # Multi-threaded Python API server (pure stdlib)
    └── cleaner_ui.html         # Premium dark-theme HTML/CSS/JS frontend UI
```

---

## 🛠️ Detailed Features (Các Tính Năng Chi Tiết)

### 1. Developer Caches (Dọn Dẹp Bộ Nhớ Đệm Lập Trình)
Safely deletes package caches for standard development platforms.
*   **uv Cache**: Purges `AppData\Local\uv\cache`.
*   **npm Cache**: Safely deletes `AppData\Local\npm-cache`.
*   **pnpm Store**: Performs local package cache optimization for pnpm packages.
*   **pip Cache**: Clears standard python pip wheels cache.
*   **NuGet Cache**: Deletes packages downloaded inside `.nuget\packages`.

### 2. Browser & IDE Caches (Dọn Dẹp Trình Duyệt & Code Editor)
*   **Chrome & Edge Browser Cache**: Deletes static cache resources and JS V8 engine Code Cache folders.
*   **VS Code Cache**: Safely removes editor logs, Cache, CachedData, and temporary Extension installer zip files (.vsix).

### 3. Windows System Deep Clean (Dọn Dẹp Hệ Thống Sâu)
*   **Windows Update Download Cache**: Clears downloaded installation packages in `SoftwareDistribution\Download`.
*   **Windows Delivery Optimization**: Clears local files shared with peer devices on the local network.
*   **Windows Event Logs**: Deletes Windows log folders (including CBS update logs that sometimes bloat to tens of GBs).
*   **Application Crash Dumps**: Deletes process crash memory log files.
*   **Recycle Bin**: Programmatically empties Windows Recycle Bin using PowerShell API with recursive fallback clean.

### 4. WSL2 / Docker Disk Compaction (Thu Nhỏ Đĩa Ảo Docker)
WSL2 dynamic disk files (`.vhdx`) grow automatically but never shrink on host Windows even after you delete container images inside Docker. 
*   **Action**: ADC cleanly shuts down active WSL instances (`wsl --shutdown`) and executes a programmatic Windows `diskpart` script to compact the virtual hard disk file back to its actual storage size. (Often shrinks files from **20+ GB to under 5 GB** instantly).

### 5. Google Chrome AI Block (Khóa Tệp AI Chrome 4GB)
Google Chrome automatically downloads local on-device Gemini Nano weights (`weights.bin` ~4 GB) in the background. 
*   **Action**: ADC deletes this file and creates a directory lock named `weights.bin` with restricted System (`+s`), Hidden (`+h`), and Read-only (`+r`) attributes. This permanently prevents Chrome from re-downloading the file. You can toggle the lock **ON** or **OFF** via the switch on the dashboard.

### 6. VSS Shadow Storage Limit (Giới Hạn Bộ Nhớ Phục Hồi Windows)
Windows Volume Shadow Service (VSS) creates restore points when installing database engines like Microsoft SQL Server, silently occupying 10-15% of your drive.
*   **Action**: ADC deletes inactive restore points and sets a hard upper ceiling limit of **2 GB** capacity using `vssadmin` commands to prevent future space leaks.

---

## ⚡ Interactive UI Features (Tính Năng Tương Tác Giao Diện)

*   **Presets (Chế Độ Chọn Nhanh)**:
    - **Safe Preset (Khuyên Dùng)**: Auto-ticks only 100% safe developer caches, browser caches, and temp directories.
    - **Deep Preset (Dọn Dẹp Sâu)**: Auto-ticks everything including Docker compaction, VSS limits, and Windows update caches.
    - **Clear All**: Instantly unchecks all boxes.
*   **Estimated Savings (Ước Tính Dung Lượng Dọn Dẹp)**: Shows in real-time how much space you will free up based on your selections before hitting Clean.
*   **Folder Explorer Shortcuts**: Clicking the `📂` explorer icon next to any path launches Windows File Explorer directly into that folder so you can verify the files.

---

## 🔒 Security & Performance Features (Tính Năng Bảo Mật & Hiệu Năng)

*   **Zero Dependencies**: Written purely using Python standard library modules (`http.server`, `urllib`, `subprocess`, `json`, `socketserver`) and vanilla HTML5/CSS3/JS. **No `pip install` required**, ensuring it runs without errors on any computer.
*   **Localhost Isolation**: The API server is bound explicitly to `127.0.0.1` (localhost). It does not listen on external network interfaces (`0.0.0.0`), preventing Windows Firewall prompts and blocking external network access.
*   **Threading Support**: Uses a multi-threaded server (`ThreadingHTTPServer`) so the UI stays fully responsive and never freezes, even while long deletion tasks or WSL compaction commands are executing.
*   **Reparse Point Safety**: The file system scanner automatically ignores symlinks and Windows junction points to prevent loops and protect system safety.

---

## 🩺 Troubleshooting & Failure Recovery (Hướng Dẫn Khắc Phục Sự Cố)

### 1. Port 8342 is Already in Use
If the port is blocked by another service, open `src/cleaner_backend.py` and modify the port value:
```python
PORT = 8342 # Change this value to another port (e.g. 8456)
```

### 2. Administrator Permissions Required for VSS/Diskpart
To shrink WSL2 disks or limit Volume Shadow copies, Windows require administrator rights. 
*   If you get an `Access Denied` error in the logs, right-click on `run_cleaner.bat` and select **Run as Administrator** (Chạy dưới quyền Quản trị viên).

---

## 🇻🇳 Tài liệu tiếng Việt - Tóm tắt

Ứng dụng **Antigravity Disk Cleaner (ADC)** giúp bạn tự động dọn dẹp và bảo vệ dung lượng ổ đĩa C khi làm việc với các hệ thống AI Agents và môi trường phát triển phần mềm vốn tạo ra cực kỳ nhiều rác.

### Các thành phần chính hỗ trợ dọn dẹp:
1. **Cache lập trình**: uv, npm, pnpm, pip, nuget.
2. **Trình duyệt & IDEs**: Cache và Code Cache của Chrome, Edge, VS Code.
3. **Bộ cài Windows Update & Logs**: `SoftwareDistribution\Download`, CBS logs, event logs, delivery optimization, crash dumps.
4. **Nén đĩa ảo Docker**: Đưa dung lượng đĩa ảo WSL2 từ 20GB về kích thước thực tế chỉ vài GB thông qua công cụ nén tự động.
5. **Khóa tải mô hình AI của Chrome**: Ngăn chặn trình duyệt tự động tải xuống file Gemini Nano weights nặng 4GB bằng tính năng gạt nút Lock trên giao diện.
6. **Giới hạn điểm khôi phục Windows VSS**: Tự động dọn dẹp restore points và áp dụng giới hạn không cho phép Windows tự tạo bản sao lưu vượt quá 2GB.
7. **Thùng rác**: Dọn sạch Recycle Bin bằng PowerShell API.

