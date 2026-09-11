# 02 — SPEC: Antigravity Disk Cleaner v2.0 (Windows Desktop)

> Đặc tả sản phẩm + kỹ thuật cho bản nâng cấp từ "local web app" thành **ứng dụng
> Windows đóng gói, có installer**. Mọi lựa chọn công nghệ trong tài liệu này đã
> được **spike kiểm chứng trên máy đích** — xem §3.3.

---

## 1. Mục tiêu & phạm vi

### 1.1 Mục tiêu

1. **Một ứng dụng Windows thật**: cửa sổ native có icon riêng, Start Menu,
   uninstaller, entry trong Add/Remove Programs, single instance, DPI-aware.
2. **An toàn theo mặc định**: dry-run trước, phân tầng rủi ro rõ ràng, xoá vào
   Recycle Bin khi có thể, audit log mọi đường dẫn đã xoá, không có socket lắng nghe.
3. **Nói đúng sự thật về dung lượng**: đa volume, đa profile, size-on-disk, và báo
   cáo *thu hồi thật* theo từng mục — không phải ước tính.
4. **Giải quyết nguyên nhân, không chỉ triệu chứng**: tìm được "cái gì đang ăn ổ
   đĩa" (big file/folder explorer, orphan `node_modules`, duplicate), và dọn định kỳ
   tự động.
5. **Tiếng Việt là ngôn ngữ mặc định**, có công tắc EN.

### 1.2 Ngoài phạm vi (non-goals)

- Dọn registry, dọn driver, "one-click optimize everything" — rủi ro cao, lợi ích
  không kiểm chứng được.
- Telemetry, tài khoản, cloud sync. App **không gọi mạng** ngoài việc mở link tài
  liệu khi người dùng bấm.
- Chống malware, tối ưu RAM, tăng tốc game.
- Hỗ trợ Windows 8.1 / 7. Yêu cầu: **Windows 10 21H2 (build 19044) hoặc mới hơn**,
  x64. (Máy đích: 10.0.19045 ✔)

### 1.3 Nguyên tắc thiết kế

| Nguyên tắc | Áp dụng cụ thể |
|-----------|----------------|
| Safety-first | Dry-run bật mặc định lần đầu; DANGEROUS cần gõ xác nhận; tạo restore point trước op không hoàn tác được |
| Evidence-first | Mọi số đo kèm nguồn: path, file count, thời điểm quét, có bị truncate hay không |
| No surprise | Không có action nào chạy mà không hiện trong preview |
| Reversible-by-default | Recycle Bin là chiến lược xoá mặc định cho dữ liệu người dùng; hard-delete chỉ cho cache thuần |
| Offline | Zero network call. Font bundle local. |

---

## 2. So sánh phương án kiến trúc

Đã đo trên máy đích: Python 3.14.4, Node 24.16.0, .NET 10.0.302, **không có
cargo/go**, WebView2 Runtime **148.0.3967.96 đã cài**, `psutil` + `pywin32` có sẵn.

| Phương án | Giữ lại được gì | Kích thước | Rủi ro | Kết luận |
|-----------|-----------------|-----------:|--------|----------|
| **A. Python engine + pywebview/WebView2 + PyInstaller + Inno Setup** | 100 % engine Python, 100 % UI HTML/CSS | ~29 MB (đo được) | pythonnet phải khớp version Python → **giải quyết bằng cách pin runtime bundle ở 3.12** | ✅ **CHỌN** |
| B. Rewrite toàn bộ sang .NET 10 WPF/WinUI + WebView2 | UI HTML | ~25–40 MB self-contained | Port lại ~600 dòng logic; nhưng được Win32/Shell API hạng nhất | Đường nâng cấp v3, không phải v2 |
| C. Electron + Python sidecar | UI HTML | 150 MB+ | Hai runtime, hai lần đóng gói | ❌ |
| D. Tauri | UI HTML | ~10 MB | **Không có cargo/MSVC toolchain** trên máy | ❌ không khả thi |
| E. PySide6 / Qt | engine Python | ~60 MB | Bỏ toàn bộ UI HTML, viết lại bằng widget | ❌ mất công vô ích |
| F. Giữ browser, chỉ thêm installer | tất cả | nhỏ | **Không sửa được SEC-01/02/03** — vẫn là HTTP server mở cổng | ❌ |

Lý do quyết định phương án A, ngoài chi phí: chuyển từ HTTP sang **bridge trong
tiến trình** làm SEC-01, SEC-02, SEC-03 *biến mất theo kiến trúc* — không còn cổng
TCP, không còn CORS, không còn origin nào để tấn công. Đây không phải patch mà là
loại bỏ cả lớp lỗi.

### 2.1 Mô hình elevation

App cần admin cho: VSS, diskpart/Optimize-VHD, `SoftwareDistribution\Download`,
Delivery Optimization, DISM component cleanup, `powercfg /h off`, Prefetch,
`$PatchCache$`.

| Lựa chọn | Đánh giá |
|----------|----------|
| Manifest `requireAdministrator` toàn app | Đơn giản nhưng mọi thao tác file chạy elevated — blast radius lớn, và mất drag-drop |
| Helper process elevated + named pipe có secret | Đúng chuẩn nhất, nhưng là 1 module riêng |
| **Chạy unelevated; dò `IsUserAnAdmin`; disable mục admin-only; nút "Khởi động lại với quyền Administrator"** (`ShellExecuteEx` verb `runas`) | ✅ **v2.0** — đúng thực tế người dùng disk cleaner, đơn giản, không mất tính năng |

v2.0 dùng lựa chọn 3. Helper process là mục v2.1 trong `03-PLAN.md`.

---

## 3. Kiến trúc đích

### 3.1 Sơ đồ

```text
AntigravityDiskCleaner.exe                     (PyInstaller onedir, pythonw base)
│
├─ shell/                     pywebview 5.4, renderer = edgechromium
│    └─ webview.create_window(..., js_api=Bridge())      ← KHÔNG có HTTP server
│                                                          KHÔNG có cổng TCP
├─ bridge/                    lớp API duy nhất JS ↔ Python
│    ├─ @expose scan_start(volume_ids, target_ids) -> job_id
│    ├─ @expose job_poll(job_id) -> {phase, pct, events[], partial_results}
│    ├─ @expose job_cancel(job_id)
│    ├─ @expose clean_plan(selection) -> Plan  (dry-run, luôn gọi trước clean)
│    ├─ @expose clean_execute(plan_token) -> job_id
│    ├─ @expose reveal(target_id)          ← chỉ nhận target_id, KHÔNG nhận path
│    ├─ @expose settings_get/settings_set
│    └─ @expose admin_state / admin_relaunch
│
└─ engine/                    thuần Python, không phụ thuộc UI, có unit test
     ├─ fsutil.py      walker scandir + chặn reparse + size-on-disk + cancel
     ├─ volumes.py     liệt kê volume, free space, loại FS
     ├─ targets.py     catalogue khai báo (§4)
     ├─ resolvers.py   static | glob | tool-query | multi-profile
     ├─ strategies.py  recycle | hard-delete | tool-cmd | win-native
     ├─ guard.py       allowlist đường dẫn, canonicalise, chống escape
     ├─ jobs.py        Job/phase/progress/cancel/event log
     ├─ report.py      thu hồi thật theo mục, ghi JSON report
     ├─ explorer.py    big file/folder, orphan sweeper, duplicate finder
     ├─ schedule.py    Task Scheduler qua COM
     └─ platform_win.py  ctypes wrapper Win32/Shell API
```

### 3.2 Trạng thái trên đĩa

| Đường dẫn | Nội dung |
|-----------|----------|
| `%ProgramFiles%\Antigravity Disk Cleaner\` | binary (read-only) |
| `%APPDATA%\AntigravityDiskCleaner\config.json` | settings, ngôn ngữ, exclusion, preset custom |
| `%LOCALAPPDATA%\AntigravityDiskCleaner\logs\adc-YYYYMMDD.log` | audit log, rotate 7 ngày |
| `%LOCALAPPDATA%\AntigravityDiskCleaner\reports\<job_id>.json` | báo cáo từng lần dọn |
| `%LOCALAPPDATA%\AntigravityDiskCleaner\cache\scan.sqlite` | cache kết quả quét (invalidate theo mtime) |

### 3.3 Kết quả spike — đã kiểm chứng, không phải giả định

Chạy trên máy đích, sau đó đã dọn sạch artifact:

| Hạng mục | Kết quả |
|----------|---------|
| Runtime build | `uv venv --python 3.12` → CPython **3.12.13** OK |
| Dependency | `pywebview==5.4`, `pythonnet==3.1.0`, `pyinstaller==6.11.1` cài sạch, `import clr` OK |
| Renderer | `edgechromium` — **WebView2 thật**, không phải MSHTML fallback |
| Bridge JS→Python | ✅ method Python được gọi từ JavaScript trong WebView2 (`bridge_called_with: "from-js"`) |
| PyInstaller | onedir **29 MB**, launcher `.exe` 4.3 MB, `--windowed` (không console) |
| **Exe đã đóng gói** | ✅ chạy được, `frozen: True`, JS gọi bridge trả về số disk thật `{"total":135271542784,...}` |
| WebView2 interop | Tự bundle `Microsoft.Web.WebView2.Core.dll` + `WebView2Loader.dll` (x64/x86/arm64) |
| WebView2 Runtime máy đích | `148.0.3967.96` đã cài |
| Installer tool | `winget` có `JRSoftware.InnoSetup 6.7.3` (chưa cài — bước P7) |

Hai chi tiết phát hiện từ spike, phải xử lý trong implementation:

1. **DPI**: cửa sổ yêu cầu 520×340 mở ra 646×414 → scale 1.25×. Phải khai báo
   per-monitor DPI awareness trong manifest PyInstaller, và dùng đơn vị CSS
   tương đối.
2. **`evaluate_js` và Promise**: `window.evaluate_js()` không await Promise — một
   async IIFE trả về `{}`. Pattern đúng: JS ghi kết quả vào biến rồi Python đọc
   biến đó, hoặc dùng expression đồng bộ.

---

## 4. Engine spec

### 4.1 `fsutil.py` — walker

Thay thế `get_folder_size()` (`cleaner_backend.py:167`). Yêu cầu:

```python
def walk_size(root, *, cancel: CancelToken, on_progress=None,
              budget_s: float | None = None, size_on_disk: bool = True
              ) -> WalkResult:  # (bytes, files, dirs, truncated, denied[])
```

- Dùng `os.scandir` + **một** lần `entry.stat(follow_symlinks=False)` cho mỗi entry
  (v1 stat hai lần → BUG-13).
- **Chặn reparse point** bằng
  `st.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT`, có `os.path.isjunction()`
  làm kiểm tra thứ hai. Sửa BUG-01. Cấm dùng `os.path.islink()` một mình — bảng
  bằng chứng ở `01-AUDIT.md` §4 BUG-01.
- Stack tường minh (không đệ quy) → không tràn stack, huỷ được ngay.
- `size_on_disk=True` → `GetCompressedFileSizeW` cho file sparse/nén (sửa BUG-12);
  báo cáo **cả hai** con số khi chúng lệch > 5 %.
- Đếm và trả về danh sách đường dẫn `PermissionError` để UI hiển thị "N mục cần
  quyền Administrator".
- `budget_s` + `truncated` flag → không bao giờ treo vô hạn; UI phải hiển thị rõ
  khi kết quả bị cắt.
- Ghi vào `scan.sqlite`: `(root, mtime_ns, size, files, scanned_at)`; lần sau nếu
  `mtime_ns` không đổi thì dùng cache.

### 4.2 `targets.py` — catalogue khai báo

```python
@dataclass(frozen=True)
class Target:
    id: str
    name_vi: str;  name_en: str
    desc_vi: str;  desc_en: str
    category: Category          # DEV | BROWSER | IDE | AI_ML | SYSTEM | TEMP | VM | APP
    risk: Risk                  # SAFE | REBUILDABLE | CAUTION | DANGEROUS
    resolver: Resolver          # cách tìm ra đường dẫn thực
    strategy: Strategy          # cách dọn
    admin_required: bool = False
    min_age_hours: int = 0      # bỏ qua file mới hơn N giờ
    est_note_vi: str | None = None   # cảnh báo về độ chính xác của ước tính
    rebuild_cost_vi: str | None = None  # "tải lại ~2 GB khi restore lần sau"
```

### 4.3 Bốn tầng rủi ro — định nghĩa chặt

| Tầng | Định nghĩa | UI |
|------|-----------|-----|
| **SAFE** | Cache tái tạo tự động, không tốn băng thông đáng kể, không mất state người dùng | tick sẵn trong preset Safe |
| **REBUILDABLE** | Tái tạo được nhưng **tốn thời gian hoặc băng thông** (nuget 2.21 GB, uv 1.80 GB, playwright 683 MB) | không tick sẵn; hiện `rebuild_cost_vi` |
| **CAUTION** | Mất state có ích: log chẩn đoán, workspaceStorage, thumbnail, Prefetch | cần bấm mở nhóm; cảnh báo vàng |
| **DANGEROUS** | Không hoàn tác được / ảnh hưởng cấu hình hệ thống: VSS delete + resize, `powercfg /h off`, DISM component cleanup, VHDX compact | **phải gõ chữ xác nhận**; đề nghị tạo restore point trước |

Phân loại lại các mục v1 gán sai: `system_logs` SAFE→**CAUTION** (BUG-08),
`nuget_cache` SAFE→**REBUILDABLE**, `vss_cleanup` → **DANGEROUS** và **bỏ khỏi
preset Deep** (BUG-09).

### 4.4 Resolver — sửa BUG-02, BUG-03

| Loại | Dùng cho | Ví dụ |
|------|----------|-------|
| `StaticPath` | đường dẫn cố định | `%LOCALAPPDATA%\Temp` |
| `GlobPath` | nhiều match | `%LOCALAPPDATA%\Packages\*\LocalState\ext4.vhdx` |
| `ToolQuery` | hỏi chính công cụ, cache 24 h | `pnpm store path` → `E:\.pnpm-store\v11`; `npm config get cache`; `uv cache dir` |
| `ChromiumProfiles` | liệt kê `Default` + `Profile *` | 9 profile Chrome trên máy này; nhân với `Cache`, `Code Cache`, `GPUCache`, `Service Worker\CacheStorage`, `DawnGraphiteCache`, `DawnWebGPUCache` |

`ToolQuery` phải fail-safe: nếu công cụ không có trên PATH hoặc trả rác, target báo
`unavailable` chứ **không** fallback về đường dẫn đoán.

### 4.5 Strategy — cách dọn

| Strategy | Cơ chế | Dùng cho |
|----------|--------|----------|
| `RecycleDelete` | `SHFileOperationW` + `FOF_ALLOWUNDO \| FOF_NOCONFIRMATION \| FOF_SILENT` | **mặc định** cho mọi thứ thuộc profile người dùng |
| `HardDelete` | reset attribute → `DeleteFileW` / `RemoveDirectoryW`, bottom-up | cache thuần (npm `_cacache`, Code Cache) khi user tắt "dùng Recycle Bin" |
| `ToolCommand` | argv list, **không** `shell=True` | `npm cache clean --force`, `pnpm store prune`, `uv cache prune`, `dotnet nuget locals all --clear`, `docker system prune -f` |
| `WinNative` | API/CLI hệ thống | `SHEmptyRecycleBin`, `vssadmin`, `dism`, `Optimize-VHD`/`diskpart`, `powercfg /h off`, `cleanmgr /sagerun` |
| `NoOp/Advise` | chỉ báo cáo + hướng dẫn | `pagefile.sys`, `WinSxS` khi DISM báo không cần cleanup |

Ưu tiên `ToolCommand` hơn xoá thô ở những chỗ v1 làm sai:

- **uv**: xoá thô cache **không thực sự giải phóng** dung lượng nếu venv đang
  hardlink vào các file đó — data còn sống tới khi link cuối cùng mất. Phải dùng
  `uv cache prune` (bỏ entry không còn tham chiếu). Đây là lý do 1.80 GB "xoá" mà
  free space không tăng tương ứng.
- **pnpm**: `pnpm store prune`, và store thật ở `E:\.pnpm-store\v11` (BUG-02).
- **npm**: `npm cache clean --force` thay vì `rm -rf npm-cache`.
- **nuget**: `dotnet nuget locals http-cache --clear` (an toàn) tách khỏi
  `global-packages --clear` (REBUILDABLE, 2.21 GB).

### 4.6 File đang bị khoá

v1 chỉ đếm `locked_files` (`:289`) rồi báo con số. v2:

1. Dùng **Restart Manager** (`RmStartSession` / `RmRegisterResources` /
   `RmGetList`) để **nêu tên tiến trình** đang giữ file → UI hiện "Chrome đang giữ
   142 file, đóng Chrome rồi thử lại".
2. Tuỳ chọn (opt-in, không mặc định): `MoveFileExW(..., MOVEFILE_DELAY_UNTIL_REBOOT)`
   để xoá sau reboot.
3. Không bao giờ kill process thay người dùng.

### 4.7 `guard.py` — allowlist đường dẫn

Bất biến, kiểm tra trước **mọi** thao tác xoá:

1. Đường dẫn phải `os.path.realpath` được và nằm **trong** một root đã đăng ký của
   target đó. Escape khỏi root (qua `..`, symlink, junction) → **abort cả target**,
   ghi log ERROR.
2. Chặn cứng: gốc volume (`C:\`, `D:\`, `E:\`), `%SystemRoot%` và `%SystemRoot%\System32`,
   `%ProgramFiles%`, `%ProgramFiles(x86)%`, `%USERPROFILE%` trần, `%APPDATA%` trần,
   `%LOCALAPPDATA%` trần, thư mục cài chính ADC.
3. Từ chối mọi đường dẫn do UI truyền vào. Bridge chỉ nhận `target_id` — sửa SEC-02
   ở gốc: `reveal(target_id)` tra path từ catalogue phía Python, JS không bao giờ
   quyết định path.
4. Áp dụng exclusion list của người dùng sau cùng, có quyền phủ quyết.

### 4.8 Job model

Mọi thao tác dài là một `Job`:

```text
Job { id, kind: SCAN|PLAN|CLEAN|EXPLORE, phases[], current_phase, pct,
      events: [{ts, level, target_id, message_vi, message_en}],
      per_target: {target_id: {before, after, reclaimed, files_deleted,
                               files_locked, denied, skipped_reason}},
      cancel_requested, started_at, finished_at }
```

- Chạy trên thread riêng; UI poll `job_poll(job_id)` mỗi 250 ms (không stream, vì
  bridge pywebview là request/response).
- **Cancel thật**: `CancelToken` được kiểm tra ở mỗi entry trong walker và mỗi file
  trong deleter — sửa BUG-13.
- **Đo thu hồi thật**: `before` = size đo trước, `after` = size đo lại sau, per
  target. Đây là con số báo cáo, không phải ước tính. Với `docker_compact` thì
  `before/after` là size file VHDX → tự động sửa BUG-07.
- Ghi `reports/<job_id>.json` khi kết thúc.

---

## 5. Catalogue target v2

Dung lượng là số **đo thực tế trên máy đích** (2026-08-23). `A` = cần admin.

### DEV — package manager caches

| id | Đường dẫn / cách lấy | Đo được | Risk | Strategy |
|----|----------------------|--------:|------|----------|
| `uv_cache` | `uv cache dir` | 1.80 GB | REBUILDABLE | `uv cache prune` |
| `npm_cache` | `npm config get cache` | 545.95 MB | SAFE | `npm cache clean --force` |
| `pnpm_store` | `pnpm store path` → `E:\.pnpm-store\v11` | — | SAFE | `pnpm store prune` |
| `pip_cache` | `pip cache dir` | 5.97 KB | SAFE | `pip cache purge` |
| `nuget_http` | `dotnet nuget locals http-cache` | — | SAFE | ToolCommand |
| `nuget_global` | `~\.nuget\packages` | 2.21 GB | REBUILDABLE | ToolCommand |
| `yarn_cache` | `%LOCALAPPDATA%\Yarn\Cache` | không có | SAFE | HardDelete |
| `cargo_registry` | `~\.cargo\registry\{cache,src}` | không có | REBUILDABLE | HardDelete |
| `go_modcache` | `go env GOMODCACHE` | không có | REBUILDABLE | `go clean -modcache` |
| `gradle_caches` | `~\.gradle\caches` | không có | REBUILDABLE | HardDelete |
| `maven_repo` | `~\.m2\repository` | không có | REBUILDABLE | HardDelete |

### BROWSER — nhân theo profile (`ChromiumProfiles`)

| id | Đường dẫn | Đo được | Risk |
|----|-----------|--------:|------|
| `chrome_caches` | `Chrome\User Data\{Default,Profile *}\{Cache,Code Cache,GPUCache,Service Worker\CacheStorage,Dawn*Cache}` | tổng `User Data` = **4.96 GB** | SAFE |
| `edge_caches` | idem cho Edge (1 profile) | `User Data` = 401.09 MB | SAFE |
| `chrome_ai_weights` | `Chrome\User Data\OptGuideOnDeviceModel` | ~4 GB khi Chrome đã tải | SAFE + lock toggle |

### IDE / APP

| id | Đường dẫn | Đo được | Risk |
|----|-----------|--------:|------|
| `vscode_vsixs` | `%APPDATA%\Code\CachedExtensionVSIXs` | **720.80 MB** | SAFE |
| `vscode_cacheddata` | `%APPDATA%\Code\CachedData` | 84.53 MB | SAFE |
| `vscode_cache` | `%APPDATA%\Code\Cache` | 5.60 MB | SAFE |
| `vscode_workspacestorage` | `%APPDATA%\Code\User\workspaceStorage` | 335.42 MB | CAUTION |
| `vscode_logs` | `%APPDATA%\Code\logs` | 1011.90 KB | SAFE |
| `jetbrains_caches` | `%LOCALAPPDATA%\JetBrains\*\caches` | 1.02 GB | SAFE |
| `discord_cache` | `%APPDATA%\discord\{Cache,Code Cache,GPUCache}` | 239.24 MB | SAFE |
| `claude_projects` | `~\.claude\projects` | 22.78 MB | CAUTION (transcript) |

### AI / ML

| id | Đường dẫn | Đo được | Risk |
|----|-----------|--------:|------|
| `huggingface_cache` | `~\.cache\huggingface\hub` | 444.76 MB | REBUILDABLE |
| `ms_playwright` | `%LOCALAPPDATA%\ms-playwright` | 683.19 MB | REBUILDABLE |
| `ollama_models` | `~\.ollama\models` | không có | REBUILDABLE |
| `torch_hub` | `~\.cache\torch` | không có | REBUILDABLE |
| `puppeteer_cache` | `~\.cache\puppeteer` | không có | REBUILDABLE |

### TEMP

| id | Đường dẫn | Đo được | Risk | Ghi chú |
|----|-----------|--------:|------|---------|
| `user_temp` | `%LOCALAPPDATA%\Temp` | 40.95 MB | SAFE | `min_age_hours=24` — không xoá file app đang dùng |
| `system_temp` | `%SystemRoot%\Temp` | 3.96 MB | SAFE `A` | `min_age_hours=24` |
| `recycle_bin` | mọi volume | 2.65 MB | SAFE | `SHEmptyRecycleBin` cho **từng** volume (v1 chỉ `C:\$Recycle.Bin`) |
| `inetcache` | `%LOCALAPPDATA%\Microsoft\Windows\INetCache` | 166.53 KB | SAFE | |
| `thumbnail_cache` | `%LOCALAPPDATA%\Microsoft\Windows\Explorer` | 40.78 MB | CAUTION | thumbnail phải build lại |

### SYSTEM (đa số cần admin)

| id | Đường dẫn | Đo được | Risk |
|----|-----------|--------:|------|
| `windows_update_dl` | `%SystemRoot%\SoftwareDistribution\Download` | 1.21 MB | SAFE `A` |
| `delivery_optimization` | `…\DeliveryOptimization\Cache` | 576.49 MB | SAFE `A` |
| `windows_logs` | `%SystemRoot%\Logs` | 45.39 MB | **CAUTION** `A` (v1 gán SAFE — BUG-08) |
| `crash_dumps` | `%LOCALAPPDATA%\CrashDumps` | 44.56 MB | SAFE |
| `wer_reports` | `%LOCALAPPDATA%` + `%PROGRAMDATA%\Microsoft\Windows\WER` | 559.54 KB | SAFE `A` |
| `memory_dmp` | `%SystemRoot%\MEMORY.DMP`, `Minidump` | không có | SAFE `A` |
| `patch_cache` | `%SystemRoot%\Installer\$PatchCache$` | 59.83 MB | CAUTION `A` |
| `package_cache` | `%PROGRAMDATA%\Package Cache` | 446.97 MB | CAUTION `A` |
| `prefetch` | `%SystemRoot%\Prefetch` | 28.27 MB | CAUTION `A` |
| `windows_old` | `C:\Windows.old` | không có | CAUTION `A` |
| `component_store` | `DISM /Online /Cleanup-Image /StartComponentCleanup` | WinSxS ≥ 1.74 GB | **DANGEROUS** `A` |
| `hibernation` | `powercfg /h off` | đã tắt | **DANGEROUS** `A` |
| `pagefile_advise` | `C:\pagefile.sys` | 2.52 GB | NoOp/Advise |

### VM / VIRTUALIZATION

| id | Đường dẫn | Đo được | Risk |
|----|-----------|--------:|------|
| `docker_prune` | `docker system prune` (trước khi compact) | — | CAUTION |
| `docker_vhdx_compact` | `Docker\wsl\disk\docker_data.vhdx` (5.09 GB) + `DockerDesktop.vhdx` (4.36 GB) | 9.45 GB tổng file | **DANGEROUS** `A` |
| `wsl_distro_vhdx` | `%LOCALAPPDATA%\Packages\*\LocalState\ext4.vhdx` | `Packages` = 1.39 GB | **DANGEROUS** `A` |
| `vss_manage` | `vssadmin` | max hiện tại 2.00 GB, allocated 0 | **DANGEROUS** `A` |

`vss_manage` là màn hình riêng, không phải checkbox, gồm ba action tách biệt:
xem trạng thái · xoá shadow copy · **đặt lại giới hạn** (10 % hoặc UNBOUNDED) —
đường lùi cho BUG-09 đã xảy ra trên máy này.

`docker_vhdx_compact` bắt buộc precheck: Docker Desktop đã tắt? có container đang
chạy? (sửa BUG-11). Script diskpart ghi vào `%TEMP%` riêng, không ghi vào data dir
của Docker (sửa BUG-10). Ưu tiên `Optimize-VHD -Mode Full` nếu module Hyper-V có.

### 5.1 Tổng hợp

Catalogue v2 phủ **≈ 26 GB** đo được so với ~4 GB mà v1 hiển thị. Chi tiết bảng
đối chiếu ở `01-AUDIT.md` §6.

---

## 6. Tính năng mới

### 6.1 Dry-run / Plan preview — bắt buộc

`clean_execute()` **chỉ** nhận `plan_token` do `clean_plan()` phát ra. Không có
đường nào xoá file mà không qua plan. Plan hiển thị: từng target, đường dẫn thật đã
resolve, số file, dung lượng, strategy sẽ dùng, có vào Recycle Bin hay không, cần
admin hay không, và lý do skip nếu bị skip. Token hết hạn sau 5 phút hoặc khi
selection đổi.

Lần chạy đầu tiên: dry-run **bật mặc định**, có banner giải thích.

### 6.2 Disk Explorer — trả lời "cái gì đang ăn ổ đĩa"

Đây là tính năng mà v1 thiếu hoàn toàn và là lý do người dùng không biết 4.96 GB
Chrome nằm ở đâu.

- Chọn volume hoặc thư mục → quét có tiến độ, huỷ được.
- Bảng top-N thư mục và top-N file theo size-on-disk, đào xuống từng cấp
  (breadcrumb), sort/filter.
- Treemap đơn giản bằng SVG (không thư viện ngoài).
- Mỗi hàng: "Mở trong Explorer" (qua `reveal`, không nhận path từ JS).

### 6.3 Project Sweeper — dành riêng cho máy dev

Chọn một root (mặc định `E:\PROJECT`) rồi tìm:

| Loại | Pattern | Điều kiện |
|------|---------|-----------|
| orphan `node_modules` | thư mục `node_modules` | project không mở/không sửa > N ngày |
| `.venv` / `venv` | | idem |
| build artifacts | `dist`, `build`, `target`, `out`, `bin`, `obj` | có sibling manifest tương ứng |
| python caches | `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache` | luôn an toàn |
| `.next`, `.nuxt`, `.turbo`, `.parcel-cache` | | |

Lọc theo "sửa lần cuối > N ngày" (mặc định 30). Không bao giờ tự tick; luôn hiện
đường dẫn đầy đủ và ngày sửa cuối.

### 6.4 Duplicate finder

Ba pha để không hash cả ổ: nhóm theo size → hash 64 KB đầu+cuối → hash đầy đủ chỉ
với nhóm còn lại. Có huỷ giữa pha. Kết quả không tick sẵn bất kỳ bản nào.

### 6.5 Lên lịch tự động

Tạo Task Scheduler task (qua COM `Schedule.Service`, không gọi `schtasks` bằng
shell):

- Tần suất: hằng ngày / hằng tuần / khi free space < ngưỡng.
- **Chỉ cho phép target tầng SAFE** trong task tự động. CAUTION/DANGEROUS không bao
  giờ chạy không có người.
- Chạy `--headless --preset=<name>`, ghi report JSON, hiện toast thông báo.
- Task được xoá khi uninstall.

### 6.6 Đa volume

`volumes.py` liệt kê mọi fixed volume, hiện free/total/% cho từng ổ (máy này: C
12.3 %, E 10.8 %, D 11.4 %). Recycle Bin, temp, sweeper đều theo volume.

### 6.7 Restore point trước op DANGEROUS

Trước `component_store`, `hibernation`, `docker_vhdx_compact`, `vss_manage`: đề
nghị tạo checkpoint (`Checkpoint-Computer` / WMI `SystemRestore.CreateRestorePoint`).
Nếu System Restore đang bị tắt hoặc bị giới hạn (đúng tình trạng máy này), nói rõ
"không tạo được restore point vì …" và **buộc xác nhận lần hai**.

### 6.8 Lịch sử & xu hướng

Bảng các lần chạy trước (từ `reports/*.json`) + biểu đồ free space theo thời gian.
Giúp thấy mục nào lấp lại nhanh nhất → gợi ý đưa vào lịch tự động.

### 6.9 i18n

`locales/vi.json` (mặc định) + `locales/en.json`. Không hardcode chuỗi trong HTML;
tất cả qua `data-i18n`. Định danh kỹ thuật (đường dẫn, tên biến, tên lệnh) **không**
dịch.

---

## 7. UI spec

### 7.1 Cửa sổ

| Thuộc tính | Giá trị |
|-----------|---------|
| Kích thước | 1280 × 860, min 1040 × 700 |
| Frame | **native** (không frameless) — tin cậy hơn, snap/maximize/restore hoạt động đúng |
| DPI | per-monitor v2 awareness khai báo trong manifest (spike đo scale 1.25× → phải xử lý) |
| Title | `Antigravity Disk Cleaner` |
| Icon | `assets/adc.ico` (16/32/48/64/128/256) |
| Single instance | mutex tên `Global\AntigravityDiskCleaner`; instance thứ hai focus cửa sổ đang có rồi thoát |
| Tray | tuỳ chọn: thu nhỏ xuống tray, cảnh báo khi free space < ngưỡng |
| Console | không có (`pythonw` base, PyInstaller `--windowed`) |

### 7.2 Điều hướng — sidebar

```text
┌──────────────┬───────────────────────────────────────────────┐
│ ◈ ADC        │                                               │
│              │   [nội dung]                                  │
│ ▸ Tổng quan  │                                               │
│ ▸ Dọn dẹp    │                                               │
│ ▸ Khám phá ổ │                                               │
│ ▸ Dự án      │                                               │
│ ▸ Lịch trình │                                               │
│ ▸ Lịch sử    │                                               │
│ ▸ Cài đặt    │                                               │
│              │                                               │
│ ⚠ Chưa có    │                                               │
│   quyền admin│                                               │
│ [Khởi động   │                                               │
│  lại as admin]                                               │
└──────────────┴───────────────────────────────────────────────┘
```

Giữ nguyên design token hiện có (`--primary: #8b5cf6`, `--secondary: #06b6d4`,
`--bg-color: #0b0813`) — hệ màu này đang tốt, không cần đổi.

**Font: không bundle gì cả** (quyết định P4, thay cho "bundle `Outfit` + `Fira Code`
vào `assets/fonts/`" của bản SPEC trước). Lý do là kỹ thuật, không phải thẩm mỹ:
Outfit **không có glyph tiếng Việt**. Google Fonts chỉ phát hành subset `latin` và
`latin-ext` cho nó; `latin-ext` phủ `U+1E00–1E9F` và `U+1EF2–1EFF` nhưng **không**
phủ `U+1EA0–1EF1` — đúng vùng chứa ổ (U+1ED5), ọ (U+1ECD), ẹ (U+1EB9), ị (U+1ECB),
ế (U+1EBF). Bundle Outfit sẽ khiến mỗi từ tiếng Việt bị xé giữa hai font khác nhau.
Fira Code cũng không có subset `vietnamese`. Thực tế v1 **chưa bao giờ** render bằng
Outfit: font này không được cài trong `C:\Windows\Fonts`, nên `@import` từ CDN chỉ
tốn một request rồi fallback về Segoe UI Variable Text.

Vì vậy v2 dùng font hệ thống: `'Segoe UI Variable Text', 'Segoe UI', system-ui,
sans-serif` cho text và `Consolas, 'Cascadia Mono', monospace` cho console/số. Cách
này đóng SEC-06 chặt hơn bundle: CSP khai báo `font-src 'none'` — không có nguồn font
nào được phép, kể cả local, kể cả một lần sửa file sau này thêm `@font-face` vào.

Giữ nguyên design token hiện có, phần bổ sung duy nhất: thêm `--primary-text:
#a78bfa` (7.29:1 trên `--bg-color`) cho text nhỏ, vì `--primary` #8b5cf6 chỉ đạt
4.68:1 — qua ngưỡng 4.5:1 nhưng quá sát để dựa vào. Và **không dùng `opacity` cho
text**, để phép kiểm tương phản ở bảng nghiệm thu P4 cho ra một con số xác định.

### 7.3 Quy tắc trình bày rủi ro

- Tầng rủi ro **không** chỉ thể hiện bằng màu: mỗi tầng có icon + nhãn chữ
  (`An toàn` / `Tái tạo được` / `Cần chú ý` / `Nguy hiểm`). Bắt buộc cho khả năng
  truy cập và cho người dùng mù màu.
- Mục cần admin mà chưa có admin: disabled + tooltip "Cần quyền Administrator",
  không im lặng fail như v1.
- Mục có ước tính không chính xác (`docker_vhdx_compact`) hiện `est_note_vi` ngay
  cạnh số, ví dụ *"9.45 GB là kích thước file; thu hồi thực tế chỉ là phần trống
  bên trong"* — sửa BUG-07 ở tầng trình bày.
- Kết quả quét bị `truncated` hiển thị `≥ 1.74 GB` chứ không phải `1.74 GB`.

### 7.4 Accessibility

Điều hướng bằng bàn phím đầy đủ (Tab/Shift-Tab/Space/Enter), focus ring rõ,
`aria-label` cho mọi icon button, `role="log"` + `aria-live="polite"` cho console,
tỉ lệ tương phản ≥ 4.5:1 cho text thường. `prefers-reduced-motion` tắt animation
pulse.

### 7.5 Console / log

Giữ khung console (người dùng thích nó), nhưng: có filter theo level, có nút "Mở
file log", giới hạn 5 000 dòng trong DOM, và log đầy đủ vẫn ghi ra file (sửa thiếu
hụt #5).

---

## 8. Đặc tả bảo mật v2

| ID cũ | Cách v2 loại bỏ |
|-------|-----------------|
| SEC-01 (CORS `*`) | **Không còn HTTP server.** Không có cổng lắng nghe, không có origin, không có CORS. Kiểm chứng bằng `netstat -ano` sau khi app chạy: không có LISTEN nào của tiến trình ADC. |
| SEC-02 (`os.startfile` path tuỳ ý) | Bridge chỉ nhận `target_id`. `reveal()` tra path từ catalogue phía Python, kiểm qua `guard.py`, và dùng `explorer.exe /select,` với argv list — **không** `os.startfile`, không nhận path từ JS. |
| SEC-03 (DNS rebinding) | Không áp dụng — không có HTTP. |
| SEC-04 (`shell=True`) | Toàn bộ subprocess dùng argv list, `shell=False`, `creationflags=CREATE_NO_WINDOW`, có timeout. Lint rule chặn `shell=True` trong CI. |
| SEC-05 (đọc body) | Không áp dụng. |
| SEC-06 (font CDN) | **Không có font ngoài, cũng không bundle font.** Dùng font hệ thống (§7.2) và CSP `font-src 'none'` — không nguồn nào được phép. Cộng thêm `connect-src 'none'`: trang không gọi được mạng bằng bất cứ cách nào. |

Thêm mới:

- **CSP** trong `index.html`, đo thực nghiệm trên WebView2 148 với document `file://`:

  ```text
  default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:;
  font-src 'none'; connect-src 'none'; base-uri 'none'; form-action 'none';
  object-src 'none'
  ```

  Khác bản SPEC trước ở ba điểm, cả ba đều do đo được chứ không suy đoán:
  `default-src 'self'` → `'none'` (khai báo trắng, mọi directive phải tự nói ra);
  bỏ `'unsafe-inline'` khỏi `style-src` — trên `file://`, `'self'` **cho phép**
  `<link>` stylesheet ngoài và **chặn** `<style>` inline (thử cả với `!important`),
  nên toàn bộ CSS buộc phải nằm trong file rời; `font-src 'none'` thay cho việc
  bundle font. Inline `<script>` cũng bị chặn, ảnh `https://` bị chặn, `fetch()`
  ra mạng reject bằng `TypeError`.

  Hệ quả kỹ thuật bắt buộc, ghi lại vì nó trái với bảng file của PLAN: **từ điển
  i18n không thể là JSON.** `connect-src 'none'` cộng origin `file://` khiến
  `fetch('locales/vi.json')` không thực hiện được. Nên `vi`/`en` là file `.js`
  gán vào một registry toàn cục, nạp bằng `<script src>` — xem `js/locales/`.
- **Audit log** ghi mọi đường dẫn đã xoá kèm size và strategy. Không ghi nội dung file.
- **Không secret** trong config; app không có credential nào.
- **Không auto-update** trong v2.0 (không có kênh phân phối đã ký). Chỉ kiểm tra
  version khi người dùng bấm, và chỉ mở trang release trong browser.

### 8.1 Ký số — hạn chế đã biết

Máy đích **không có** `signtool.exe` (không có Windows SDK) và không có code-signing
certificate. Hệ quả: installer và exe **không được ký** → SmartScreen sẽ hiện cảnh
báo "Windows protected your PC" cho người dùng khác. Chấp nhận được cho phân phối nội
bộ. Nếu cần ký sau này: cài Windows SDK (`winget install Microsoft.WindowsSDK`),
lấy OV/EV code-signing cert, thêm bước `signtool sign /fd SHA256 /tr <TSA>` vào
`build.ps1`. Ghi rõ trong README để người dùng không bất ngờ.

---

## 9. Đóng gói & installer

### 9.1 PyInstaller

```text
build/adc.spec
  Analysis(['src/adc/__main__.py'], datas=[ui/, assets/, locales/])
  EXE(..., console=False, icon='assets/adc.ico',
      version='build/version_info.txt',       # ProductName/Version/Company/Copyright
      manifest='build/adc.manifest')          # per-monitor DPI, requestedExecutionLevel=asInvoker
  COLLECT(...)   # onedir — khởi động nhanh hơn onefile, dễ patch, AV ít false-positive hơn
```

Chọn **onedir** thay vì onefile: onefile giải nén vào `%TEMP%` mỗi lần chạy (chậm,
và trớ trêu là chính công cụ này lại dọn `%TEMP%`). Kích thước đo được từ spike:
**29 MB**.

Runtime bundle pin ở **Python 3.12.13** (`uv python install 3.12`) vì pythonnet 3.1.0
đã kiểm chứng hoạt động ở đó. Python 3.14 trên máy dev không ảnh hưởng — PyInstaller
đóng gói interpreter riêng.

### 9.2 Inno Setup 6

`winget install JRSoftware.InnoSetup` (6.7.3 có sẵn trong winget — đã kiểm tra).

| Directive | Giá trị | Lý do |
|-----------|---------|-------|
| `AppId` | GUID cố định | upgrade in-place, không tạo entry trùng |
| `AppName` / `AppVersion` / `AppPublisher` | ADC / 2.0.0 / (tên user) | hiện đúng trong Add/Remove Programs |
| `PrivilegesRequired` | `admin` | cài vào Program Files |
| `PrivilegesRequiredOverridesAllowed` | `dialog` | cho phép chọn cài per-user nếu không có admin |
| `DefaultDirName` | `{autopf}\Antigravity Disk Cleaner` | tự chọn `Program Files` hoặc `%LOCALAPPDATA%\Programs` |
| `ArchitecturesAllowed` / `Install...64BitMode` | `x64compatible` | |
| `MinVersion` | `10.0.19044` | chặn cài trên OS quá cũ |
| `Uninstallable` / `UninstallDisplayIcon` | yes / `{app}\AntigravityDiskCleaner.exe` | |
| `CloseApplications` | `yes` | phát hiện app đang chạy khi update |
| `SetupIconFile`, `WizardStyle=modern` | | |
| `LicenseFile`, `InfoBeforeFile` | cảnh báo về thao tác DANGEROUS | người dùng phải đọc trước khi cài |

Tasks: shortcut Desktop (mặc định off), "chạy khi đăng nhập" (off), "tạo task dọn
tự động hằng tuần" (off).

`[Code]` section phải làm:

1. **Kiểm tra WebView2 Runtime** qua registry key đã kiểm chứng trên máy này:
   `HKLM\SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}\pv`
   (máy đích: `148.0.3967.96`). Nếu thiếu → chạy `MicrosoftEdgeWebview2Setup.exe`
   (evergreen bootstrapper, ~2 MB) bundle kèm.
2. Khi uninstall: hỏi có xoá `%APPDATA%` / `%LOCALAPPDATA%` data không (mặc định
   **giữ** log và report).
3. Khi uninstall: **xoá Task Scheduler task** nếu đã tạo.
4. Không sửa PATH, không cài service, không thêm Run key trừ khi user tick.

### 9.3 Script build

`build.ps1` một lệnh: tạo venv 3.12 → `uv pip sync` → chạy test → PyInstaller →
`ISCC.exe` → xuất `dist/ADC-Setup-2.0.0-x64.exe` + `SHA256SUMS.txt`. Fail sớm nếu
test đỏ.

