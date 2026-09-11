# 03 — PLAN: Kế hoạch triển khai ADC v2.0

> Chín pha. Mỗi pha có **tiêu chí nghiệm thu kiểm được bằng lệnh hoặc bằng hành vi
> quan sát được**, không phải bằng lời. Pha P0 sửa lỗi bảo mật trên bản v1 đang chạy
> để app còn dùng được an toàn trong lúc v2 được xây.

---

## Bảng tổng quan

| Pha | Nội dung | Đầu ra chính | Trạng thái |
|-----|----------|--------------|------------|
| **P0** | Hotfix bảo mật + lỗi P0 trên v1 | v1 an toàn để dùng tạm | ✅ HOÀN THÀNH |
| **P1** | Tách engine + hạ tầng test | `src/adc/engine/`, pytest xanh | ✅ HOÀN THÀNH |
| **P2** | Catalogue target v2 | `targets.py` 49 target, resolver, strategy | ✅ HOÀN THÀNH |
| **P3** | Vỏ desktop + bridge + job model | cửa sổ WebView2 chạy được | ✅ HOÀN THÀNH |
| **P4** | UI v2 | 7 màn hình, i18n VI/EN | ✅ HOÀN THÀNH |
| **P5** | Tính năng mới | Explorer, Sweeper, VSS, Docker compact | ✅ HOÀN THÀNH |
| **P6** | Đóng gói | `dist/AntigravityDiskCleaner/` (31.5 MB) | ✅ HOÀN THÀNH |
| **P7** | Installer | `ADC-Setup-2.0.0-x64.exe` (12.7 MB) | ✅ HOÀN THÀNH |
| **P8** | QA + tài liệu + release | 959 test xanh, README v2, Runbook, Changelog | ✅ HOÀN THÀNH |

Ước lượng: P0 nửa buổi · P1 1 buổi · P2 1 buổi · P3 1 buổi · P4 1.5 buổi ·
P5 2 buổi · P6 nửa buổi · P7 nửa buổi · P8 1 buổi. Tổng ≈ **9 buổi làm việc**.

P0 → P1 → P2 → P3 → P4 phải tuần tự. P5 và P6/P7 chạy song song được sau P4.

---

## P0 — Hotfix: làm cho v1 an toàn (nửa buổi)

Mục tiêu: app hiện tại vẫn dùng được trong lúc v2 xây, nhưng không còn là lỗ bảo mật.

| # | File | Thay đổi |
|---|------|----------|
| 1 | `cleaner_backend.py:303-311` | Bỏ `Access-Control-Allow-Origin: *`. Sinh token ngẫu nhiên 32 byte lúc khởi động, in ra console; mọi endpoint đòi header `X-ADC-Token` khớp. `run_cleaner.bat` mở URL kèm `?token=…`. |
| 2 | `cleaner_backend.py` mọi handler | Kiểm `Host` phải là `127.0.0.1:8342` hoặc `localhost:8342`, không thì 403. |
| 3 | `cleaner_backend.py:585-605` | `/api/open-folder` **chỉ** nhận `key` (id category), tra path từ `TARGET_PATHS` phía server. Bỏ hẳn tham số `path`. Thay `os.startfile` bằng `subprocess.run(['explorer', path], shell=False)`. |
| 4 | `cleaner_backend.py:175`, `:282` | Thay `os.path.islink(...)` bằng helper `_is_reparse(entry_or_path)` dùng `st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT`. |
| 5 | `cleaner_backend.py:32-37` | `pnpm_cache` → chạy `pnpm store path`; nếu fail thì đánh dấu unavailable. **Không** trỏ vào `%LOCALAPPDATA%\pnpm`. |
| 6 | `cleaner_backend.py:52-75` | Chrome/Edge: liệt kê `Default` + `Profile *`, cộng `Cache` + `Code Cache` + `GPUCache`. |
| 7 | `cleaner_ui.html` | Thêm checkbox cho `chrome_code_cache`, `edge_code_cache`, `vscode_cached_data`, `vscode_vsixs`, `crash_dumps` (934 MB đang không dọn được). |
| 8 | `cleaner_ui.html:1182-1187` | `applyPreset('deep')` **loại** `vss_cleanup` và `docker_compact` khỏi tick-all. |
| 9 | `cleaner_ui.html:1202-1208` | Khai báo `USER_PROFILE`/`CHROME_AI_DIR` từ `/api/scan` đã cache, hoặc bỏ hẳn vì (3) đã chuyển sang truyền `key`. |
| 10 | `cleaner_backend.py:202-214` | Sửa dò `is_locked`: kiểm `os.path.isdir(os.path.join(root,'weights.bin'))` trên `dirs`, không phải `files`. |
| 11 | mới `.gitignore` | `__pycache__/`, `*.pyc`, `_spike*`, `dist/`, `build/`, `.venv*/` |
| 12 | git | `git rm --cached src/__pycache__/*.pyc` |
| 13 | `cleaner_ui.html:7-9` | Bỏ font CDN, dùng font stack hệ thống tạm thời |

**Nghiệm thu P0**

```bash
# 1. Không origin ngoài nào gọi được (phải trả 403)
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8342/api/clean \
  -H "Origin: https://evil.example" -H "Content-Type: application/json" -d '{}'
# 2. Thiếu token → 403
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8342/api/scan
# 3. open-folder không còn nhận path
curl -s -X POST http://localhost:8342/api/open-folder \
  -H "Content-Type: application/json" -d '{"path":"C:\\Windows\\System32\\calc.exe"}'
# 4. pnpm store trả đúng E:\.pnpm-store\v11
# 5. Chrome cache > 0 B (trước hotfix là 0 B với 9 profile)
# 6. git ls-files | grep pyc  → rỗng
```

---

## P1 — Tách engine + hạ tầng test (1 buổi)

Cấu trúc mới, `cleaner_backend.py` bị tách ra chứ không sửa tiếp:

```text
pyproject.toml                # uv, ruff, pytest, mypy
src/adc/__main__.py
src/adc/engine/{fsutil,volumes,guard,jobs,report,platform_win}.py
tests/{conftest.py,test_fsutil.py,test_guard.py,test_jobs.py}
tests/fixtures/make_tree.py   # dựng cây test + junction bằng mklink /J
```

Việc cụ thể:

1. `platform_win.py` — ctypes wrapper: `GetCompressedFileSizeW`,
   `SHFileOperationW`, `SHEmptyRecycleBinW`, `MoveFileExW`, `IsUserAnAdmin`,
   `ShellExecuteExW`, Restart Manager (`RmStartSession`/`RmGetList`),
   `SetProcessDpiAwarenessContext`.
2. `fsutil.walk_size()` theo §4.1 của SPEC.
3. `guard.py` theo §4.7 của SPEC — bất biến + chặn cứng.
4. `jobs.py` — Job/CancelToken/phase/progress/event.
5. `volumes.py` — liệt kê fixed volume.

**Nghiệm thu P1** — test phải chứng minh, không phải khẳng định:

| Test | Chứng minh gì |
|------|---------------|
| `test_fsutil_skips_junction` | Dựng `real/` + `link → real` bằng `mklink /J`, walk root: kích thước **không** bị đếm hai lần |
| `test_fsutil_self_referential_junction_terminates` | Dựng junction trỏ về cha (đúng mô hình `%LOCALAPPDATA%\Application Data`): walk kết thúc, không `RecursionError`, không timeout |
| `test_fsutil_single_stat_per_entry` | Monkeypatch đếm số lần `stat` = số entry (không phải 2×) |
| `test_fsutil_cancel_mid_walk` | Set cancel sau 100 entry → trả về trong < 200 ms, `truncated=True` |
| `test_fsutil_size_on_disk_sparse` | File sparse: `size_on_disk < logical_size` |
| `test_guard_rejects_volume_root` | `guard.check("C:\\")` raise |
| `test_guard_rejects_escape_via_junction` | Target root chứa junction ra ngoài → raise, target bị abort |
| `test_guard_rejects_windir` | `%SystemRoot%`, `System32`, `%ProgramFiles%` đều raise |

```bash
uv run pytest -q            # tất cả xanh
uv run ruff check src tests # sạch
uv run mypy src/adc/engine  # sạch
```

Ràng buộc: `engine/` **không import** `webview` và không biết gì về UI. Kiểm bằng
một test grep import.

---

## P2 — Catalogue target v2 (1 buổi)

1. `targets.py` — dataclass `Target`, enum `Category`/`Risk`, đăng ký ~40 target
   theo bảng §5 của SPEC.
2. `resolvers.py` — `StaticPath`, `GlobPath`, `ToolQuery` (cache 24 h),
   `ChromiumProfiles`.
3. `strategies.py` — `RecycleDelete`, `HardDelete`, `ToolCommand`, `WinNative`,
   `NoOp`.
4. `report.py` — đo before/after per-target.

**Nghiệm thu P2**

| Test | Chứng minh gì |
|------|---------------|
| `test_every_target_resolves_or_reports_unavailable` | Không target nào raise; target không có trên máy trả `unavailable`, **không** đoán path |
| `test_toolquery_pnpm_store` | `pnpm store path` được dùng; giá trị **không** phải `%LOCALAPPDATA%\pnpm` |
| `test_chromium_profiles_enumerates_all` | Trên máy đích tìm ra ≥ 9 profile Chrome |
| `test_no_target_uses_shell_true` | Grep toàn repo: 0 kết quả `shell=True` |
| `test_risk_tiers_sane` | `windows_logs`, `vss_manage`, `nuget_global` mang tầng đã định lại (CAUTION / DANGEROUS / REBUILDABLE) |
| `test_safe_preset_excludes_dangerous` | Preset Safe không chứa target nào tầng CAUTION/DANGEROUS |
| `test_recycle_is_default_for_userdata` | Mọi target dưới `%USERPROFILE%` mặc định `RecycleDelete` |

Nghiệm thu bằng hành vi: chạy scan trên máy đích, so với bảng số đo trong
`01-AUDIT.md` §6 — phải khớp trong sai số ±5 % cho các mục tĩnh.

---

## P3 — Vỏ desktop + bridge (1 buổi)

1. `src/adc/shell/window.py` — `webview.create_window(..., js_api=Bridge())`,
   `webview.start(gui='edgechromium')`. **Không** khởi tạo HTTP server nào.
2. `src/adc/shell/bridge.py` — đúng bộ method ở §3.1 của SPEC. Mọi method:
   validate input, bắt exception, trả `{ok, data|error}` — không để traceback rò ra JS.
3. Single-instance mutex; DPI awareness; icon; splash tối giản.
4. `admin_state()` / `admin_relaunch()` (ShellExecuteEx verb `runas`).
5. Ghi log ra file từ dòng đầu tiên.

**Nghiệm thu P3**

```bash
# Cửa sổ mở, renderer đúng là WebView2 (không phải MSHTML)
uv run python -m adc --self-check
#  → in: renderer=edgechromium, webview2_runtime=<version>, admin=<bool>, single_instance=ok

# KHÔNG có socket lắng nghe nào — đây là bằng chứng SEC-01/02/03 đã bị loại bỏ
netstat -ano | grep -i listen | grep <PID_của_adc>     # phải rỗng
```

Kiểm thủ công: mở app hai lần → lần hai focus cửa sổ cũ rồi thoát. Bấm "Khởi động
lại với quyền Administrator" → UAC hiện, app quay lại với badge admin.

---

## P4 — UI v2 (1.5 buổi)

```text
src/adc/ui/index.html            # CSP 'none'-mặc-định, không CDN, không inline
src/adc/ui/css/{tokens,layout,components}.css
src/adc/ui/js/{bridge,i18n,app}.js
src/adc/ui/js/views/*.js         # bảy view, một file một view
src/adc/ui/js/locales/{vi,en}.js # .js chứ không .json — xem ghi chú dưới
```

Hai chỗ lệch so với bản PLAN trước, cả hai do CSP đo được ở P4 (chi tiết trong
SPEC §7.2 và §8):

- **Không có `assets/fonts/`.** Outfit không có glyph tiếng Việt, và v1 thực tế
  vẫn đang render bằng Segoe UI Variable Text. Dùng font hệ thống + `font-src 'none'`.
- **`locales/{vi,en}.js`, không phải `src/adc/locales/{vi,en}.json`.** Với
  `connect-src 'none'` trên document `file://` thì `fetch()` một file JSON là không
  thể; từ điển phải nạp bằng `<script src>`.

Bảy view theo §7.2 của SPEC. Giữ design token hiện có.

**Nghiệm thu P4**

| Kiểm | Cách kiểm |
|------|-----------|
| Không gọi mạng | DevTools Network tab (WebView2 F12) sau khi dùng hết mọi view: 0 request ngoài |
| CSP có hiệu lực | Chèn thử `<img src="https://…">` → bị chặn, có lỗi CSP trong console |
| i18n | Đổi VI↔EN, mọi chuỗi đổi theo; không còn chuỗi hardcode (grep `data-i18n` phủ hết) |
| Progress + cancel | Quét `uv cache` (70 373 file): progress bar nhích, bấm Huỷ → dừng < 1 s |
| Dry-run | Bật dry-run, chạy Clean → report ghi `dry_run: true`, **0 file bị xoá** (kiểm bằng file count trước/sau) |
| Ước tính Docker | Ô `docker_vhdx_compact` hiện `est_note_vi`, không hiện 9.45 GB như số tiết kiệm |
| Admin gating | Chạy non-elevated: mọi mục `A` bị disable kèm tooltip, không mục nào fail im lặng |
| Truncated | Quét `WinSxS` với budget thấp → UI hiện `≥ …` chứ không phải số chính xác |
| Keyboard | Đi hết được UI bằng Tab, focus ring thấy rõ |
| Contrast | Kiểm tương phản ≥ 4.5:1 cho text thường |

---

## P5 — Tính năng mới (2 buổi)

| Thứ tự | Tính năng | Nghiệm thu |
|--------|-----------|-----------|
| 1 | Đa volume (§6.6) | Hiện đúng C 12.3 % / E 10.8 % / D 11.4 % free; Recycle Bin dọn theo từng volume |
| 2 | Disk Explorer (§6.2) | Quét `C:\`, đào xuống, tìm thấy `Chrome\User Data` 4.96 GB và `Docker\wsl\disk` 5.09 GB trong top-10 |
| 3 | VSS manager (§5) | **Đặt lại được** max shadow storage từ 2.00 GB về 10 %/UNBOUNDED — đường lùi cho BUG-09 |
| 4 | Project Sweeper (§6.3) | Chạy trên `E:\PROJECT`, liệt kê orphan `node_modules`/`.venv`/`__pycache__` kèm ngày sửa cuối, không tick sẵn |
| 5 | Docker/WSL an toàn (§5) | Precheck: Docker đang chạy → **chặn** kèm hướng dẫn (sửa BUG-11); script diskpart nằm trong `%TEMP%` (sửa BUG-10) |
| 6 | Restore point (§6.7) | Trước op DANGEROUS: tạo được, hoặc nói rõ vì sao không tạo được rồi buộc xác nhận lần hai |
| 7 | Duplicate finder (§6.4) | Ba pha, huỷ được giữa pha; không tick sẵn bản nào |
| 8 | Lịch tự động (§6.5) | Task xuất hiện trong Task Scheduler; **chỉ** chứa target SAFE; chạy headless ghi report |
| 9 | Lịch sử & xu hướng (§6.8) | Đọc `reports/*.json`, vẽ biểu đồ free space |

Test bắt buộc cho P5:

- `test_sweeper_never_touches_active_project` — project sửa trong 30 ngày không bị liệt kê.
- `test_schedule_rejects_non_safe_targets` — thêm target CAUTION vào task → raise.
- `test_docker_precheck_blocks_when_running` — mock Docker đang chạy → strategy abort.
- `test_duplicate_finder_no_false_positive` — hai file cùng size khác nội dung → không báo trùng.

---

## P6 — Đóng gói (nửa buổi)

1. `build/adc.spec`, `build/version_info.txt`, `build/adc.manifest`, `assets/adc.ico`.
2. `build.ps1`: venv 3.12 → `uv pip sync` → `pytest` → PyInstaller onedir.
3. Fail build nếu test đỏ.

**Nghiệm thu P6**

```bash
pwsh -File build.ps1 -SkipInstaller
# → dist/AntigravityDiskCleaner/AntigravityDiskCleaner.exe tồn tại
./dist/AntigravityDiskCleaner/AntigravityDiskCleaner.exe   # mở cửa sổ, KHÔNG có console
```

Kiểm: `sys.frozen == True` trong self-check (spike đã chứng minh pattern này chạy);
kích thước bundle ≤ 60 MB; `Microsoft.Web.WebView2.Core.dll` + `WebView2Loader.dll`
có trong `_internal/`; chạy được từ máy **chưa có Python** (test bằng cách đổi tên
tạm thời thư mục Python trên PATH, hoặc chạy trong Sandbox).

---

## P7 — Installer (nửa buổi)

1. `winget install JRSoftware.InnoSetup` (6.7.3).
2. `build/installer.iss` theo §9.2 của SPEC.
3. Bundle `MicrosoftEdgeWebview2Setup.exe` làm fallback.
4. `build.ps1` gọi `ISCC.exe`, xuất `dist/ADC-Setup-2.0.0-x64.exe` + `SHA256SUMS.txt`.

**Nghiệm thu P7** — chạy trong **Windows Sandbox** hoặc VM sạch, không phải trên máy chính:

| Kiểm | Kỳ vọng |
|------|---------|
| Cài per-machine | Vào `%ProgramFiles%\Antigravity Disk Cleaner`, shortcut Start Menu hoạt động |
| Add/Remove Programs | Có entry, đúng tên/version/publisher/icon |
| Cài per-user (không admin) | Vào `%LOCALAPPDATA%\Programs\…`, app chạy, mục admin bị disable |
| Máy không có WebView2 | Bootstrapper chạy, app mở được sau đó |
| Máy Windows < 19044 | Installer từ chối với thông báo rõ ràng |
| Upgrade 2.0.0 → 2.0.1 | In-place, không tạo entry thứ hai, config được giữ |
| Uninstall | Binary sạch, task Scheduler bị xoá, hỏi về log/report |
| Chạy khi app đang mở | `CloseApplications` phát hiện và nhắc |
| SmartScreen | **Có cảnh báo** (chưa ký) — đã ghi trong README |

---

## P8 — QA, tài liệu, release (1 buổi)

### Ma trận QA thủ công

| Trường hợp | Vì sao phải thử |
|-----------|-----------------|
| Chạy **không** admin | Đúng đường dùng phổ biến nhất; mục `A` phải disable, không fail im lặng |
| Chạy có admin | Toàn bộ tính năng |
| Chrome đang mở, 9 profile | File bị khoá → Restart Manager phải **nêu tên** "Chrome", không chỉ đếm |
| Docker Desktop đang chạy | Precheck phải chặn compact (BUG-11) |
| Huỷ giữa lúc quét `uv cache` (70 373 file) | Cancel thật, < 1 s |
| Huỷ giữa lúc xoá | Dừng an toàn, report ghi số đã xoá thật |
| Dry-run rồi Clean | Số trong plan khớp số trong report |
| Volume gần đầy (E: 5.3 GB free) | Không tự làm đầy thêm bằng temp/log của chính nó |
| Đường dẫn có dấu tiếng Việt + emoji | Không lỗi encoding |
| Đường dẫn > 260 ký tự | Xử lý được (prefix `\\?\`) |
| Junction tự trỏ về cha | Không treo (test P1 đã phủ, kiểm lại end-to-end) |
| Máy không có pnpm/uv/docker | Target báo `unavailable`, app không crash |
| DPI 100 % / 125 % / 150 % / đa màn hình khác DPI | Layout không vỡ (spike đã thấy scale 1.25×) |
| Reboot sau khi hẹn xoá `MOVEFILE_DELAY_UNTIL_REBOOT` | File thực sự mất |
| Task tự động chạy lúc không đăng nhập | Ghi report, không hiện cửa sổ |

### Tài liệu

- `README.md` viết lại: cài từ installer, ảnh chụp UI, giải thích **bốn tầng rủi
  ro**, cảnh báo SmartScreen, cách hoàn tác (Recycle Bin / restore point / reset VSS).
- `docs/04-RUNBOOK.md`: build, release, rollback.
- `CHANGELOG.md`: nêu rõ các lỗi P0 của v1 đã sửa, để người từng dùng v1 biết cần
  kiểm lại VSS max size trên máy mình.

### Release gate

Không commit / không push / không phân phối cho tới khi: `pytest` xanh, ma trận QA
đã chạy, installer đã test trong VM sạch, và người dùng phê duyệt manifest release
(xem `/release-check`).

---

## Sổ rủi ro

| Rủi ro | Xác suất | Ảnh hưởng | Giảm thiểu |
|--------|----------|-----------|-----------|
| pythonnet không theo kịp Python mới | Thấp (đã pin 3.12.13, **đã kiểm chứng**) | Chặn P3/P6 | Runtime bundle pin, không dùng Python hệ thống |
| PyInstaller bị AV báo false-positive | Trung bình | Người dùng không cài được | onedir (không onefile), công bố SHA256; ký số nếu có cert |
| Máy đích không có WebView2 | Thấp (evergreen, máy này có 148.0.3967.96) | App không mở | Bootstrapper bundle trong installer |
| Người dùng đã bị BUG-09 (VSS 2 GB) | **Đã xảy ra trên máy này** | System Restore vô hiệu | VSS manager (P5-3) + ghi trong CHANGELOG |
| Xoá quá tay ở tầng CAUTION | Trung bình | Mất state có ích | Dry-run mặc định, Recycle Bin mặc định, audit log |
| Quét cả ổ quá chậm | Trung bình | UX kém | Cache sqlite theo mtime, time budget, cancel, quét tăng dần |
| Không ký số → SmartScreen | **Chắc chắn** (không có signtool/cert) | Người dùng e ngại | Ghi rõ trong README + hướng dẫn ký về sau |
| Scope phình ra ở P5 | Trung bình | Trượt tiến độ | P5 độc lập với P6/P7; có thể ship v2.0 với P5 phần 1–3 rồi để 4–9 sang v2.1 |

---

## Đường đi sau v2.0

| Bản | Nội dung |
|-----|----------|
| v2.1 | Helper process elevated + named pipe (thay cho relaunch toàn app); phần còn lại của P5 |
| v2.2 | Auto-update có ký số; MSIX song song với Inno |
| v3.0 | Cân nhắc port vỏ sang .NET 10 + WinUI3 để có API Win32/Shell hạng nhất và một runtime duy nhất (xem so sánh §2 của SPEC — phương án B) |

---

## Ánh xạ sang six-gate

| Gate | Tương ứng |
|------|-----------|
| 1 Preflight | Đã xong: đo môi trường, đo dung lượng, spike công nghệ (SPEC §3.3) |
| 2 Evidence | Đã xong: `01-AUDIT.md`, mọi khẳng định neo `path:line` + số đo |
| 3 Mapping | Tài liệu này — mỗi pha một file, một thay đổi, tiêu chí kiểm được bằng lệnh |
| 4 Implement | P0 → P7 |
| 5 Verify | Khối nghiệm thu ở cuối mỗi pha + ma trận QA P8 |
| 6 Close | `CHANGELOG.md` + release manifest |

**Ngoài phạm vi tài liệu này**: không viết code, không commit, không cài Inno Setup,
không chạy `vssadmin`/`diskpart`/`dism` để "sửa" máy. Mọi thứ ở trên chờ phê duyệt
trước khi bắt tay vào P0.

