# 01 — AUDIT: Phân tích sâu hiện trạng ADC v1

> Tài liệu này ghi lại kết quả **đo thực tế** trên máy `Windows 10 Pro 19045` ngày
> 2026-08-23, không phải suy đoán. Mọi khẳng định về code đều neo vào `path:line`.
> Mọi con số dung lượng đều lấy từ một probe `os.scandir` có chặn reparse-point,
> chạy trong session **elevated** (`IsUserAnAdmin: True`).

---

## 1. Ảnh chụp hệ thống (baseline đo được)

| Volume | Total | Used | Free | % Free |
|--------|------:|-----:|-----:|-------:|
| `C:\`  | 126.0 GB | 110.5 GB | 15.5 GB | **12.3 %** |
| `E:\`  | 48.7 GB  | 43.4 GB  | 5.3 GB  | **10.8 %** |
| `D:\`  | 63.2 GB  | 56.0 GB  | 7.2 GB  | **11.4 %** |

Cả **ba** volume đều dưới ngưỡng 13 % free. ADC v1 chỉ nhìn `C:\`
(`cleaner_backend.py:251`), nên hai ổ còn lại — trong đó `E:\` là project root và
chứa `E:\.pnpm-store\v11` — hoàn toàn vô hình với ứng dụng.

Ghi chú: free space dao động vài GB giữa các lần đo do temp churn. Con số trên là
lần đo đầu, dùng làm baseline.

---

## 2. Kiến trúc hiện tại

```text
run_cleaner.bat
  └─ start /B python src\cleaner_backend.py
       └─ ThreadingHTTPServer  127.0.0.1:8342          ← cleaner_backend.py:611-612
            ├─ GET  /                → trả cleaner_ui.html          (:317)
            ├─ GET  /api/status      → shutil.disk_usage("C:\\")    (:331)
            ├─ GET  /api/scan        → os.walk toàn bộ TARGET_PATHS  (:341)
            ├─ POST /api/clean       → xoá / vssadmin / diskpart     (:405)
            ├─ POST /api/chrome-lock → tạo/xoá thư mục khoá          (:515)
            └─ POST /api/open-folder → os.startfile(path)            (:585)
  └─ start http://localhost:8342   → mở trong browser mặc định
```

Đặc điểm quyết định mọi vấn đề bên dưới: **UI chạy trong browser của người dùng,
giao tiếp với một HTTP server có quyền xoá file qua một cổng TCP mở.**

---

## 3. Lỗi bảo mật — P0

### SEC-01 · Bất kỳ website nào cũng gọi được `/api/clean`

`send_cors_headers()` đặt `Access-Control-Allow-Origin: *`
(`cleaner_backend.py:303-306`) và `do_OPTIONS()` trả 200 kèm
`Access-Control-Allow-Headers: Content-Type` (`:308-311`). Nghĩa là preflight cho
một `POST application/json` **thành công** từ mọi origin. Không có token, không có
session, không kiểm tra `Origin`.

Hệ quả cụ thể: một trang web bất kỳ mà người dùng đang mở tab có thể chạy

```js
fetch('http://localhost:8342/api/clean', {
  method: 'POST', headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({categories: ['system_logs','user_temp','system_temp',
                                     'vss_cleanup','docker_compact'],
                        options: {vss_limit_2gb: true}})
})
```

→ xoá `C:\Windows\Logs`, xoá toàn bộ temp, **xoá sạch mọi restore point**, hạ
shadow storage xuống 2 GB, `wsl --shutdown` rồi `diskpart compact` lên VHDX Docker.
Không có xác nhận nào ở phía server.

### SEC-02 · `/api/open-folder` = thực thi file tuỳ ý

`:600` gọi `os.startfile(folder_path)` với `folder_path` lấy nguyên từ JSON body
(`:589`), chỉ kiểm tra `os.path.exists`. `os.startfile` là `ShellExecute` — trỏ vào
`.exe`, `.bat`, `.ps1`, `.lnk`, `.msi` là **chạy** nó. Kết hợp với SEC-01, một
trang web có thể thực thi file local trên máy người dùng. Đây là lỗi nặng nhất.

### SEC-03 · Không kiểm tra header `Host` → DNS rebinding

Server bind `127.0.0.1` (`:611`) nhưng không xác thực `Host`. Một domain phân giải
về `127.0.0.1` sau TTL ngắn sẽ vượt qua same-origin policy và nói chuyện trực tiếp
với API.

### SEC-04 · `shell=True` với đường dẫn nội suy chuỗi

`:470` `diskpart /s "{script_path}"`, `:558` `attrib +r +h +s "{target}"`,
`:568` `attrib -r -h -s "{target}"`. Đường dẫn xuất phát từ biến môi trường nên rủi
ro thực tế thấp, nhưng đây là pattern sai: phải dùng argv list và bỏ `shell=True`.

### SEC-05 · Đọc body không phòng vệ

`int(self.headers['Content-Length'])` tại `:406`, `:517`, `:587` — thiếu header là
`TypeError` → 500 và stack trace. Không giới hạn kích thước body.

### SEC-06 · UI nạp font từ CDN ngoài

`cleaner_ui.html:7-9` `preconnect` + `@import` tới `fonts.googleapis.com` /
`fonts.gstatic.com`. Một tiện ích dọn ổ đĩa offline không nên gọi mạng: rò rỉ tín
hiệu sử dụng, và mất font/hỏng layout khi máy không có internet.

---

## 4. Lỗi logic & rủi ro mất dữ liệu

### BUG-01 · Bộ chặn junction **không hoạt động** — P0

`get_folder_size()` (`:175`) và `clean_target_folder()` (`:282`) đều lọc thư mục
bằng `os.path.islink()`. Trên NTFS, `islink()` trả **False** cho junction. Đo thực
tế trên máy này (Python 3.14.4):

| Đường dẫn | `islink()` | `isjunction()` | reparse attr |
|-----------|:----------:|:--------------:|:------------:|
| `%LOCALAPPDATA%\Application Data` | **False** | True | True |
| `%LOCALAPPDATA%\History` | **False** | True | True |
| `C:\ProgramData\Application Data` | **False** | True | True |
| `C:\Documents and Settings` | **False** | True | True |
| `%USERPROFILE%\My Documents` | **False** | True | True |
| `%LOCALAPPDATA%\Microsoft\Windows\Temporary Internet Files` | **False** | True | True |

`%LOCALAPPDATA%\Application Data` là junction **trỏ về chính `%LOCALAPPDATA%`** —
đệ quy vô hạn cho tới khi chạm `MAX_PATH`. Hôm nay chưa nổ vì mọi target đều nằm
một cấp *dưới* `AppData\Local`, nhưng nó sẽ nổ ngay khi thêm chức năng "quét cả ổ /
tìm thư mục nặng". Nghiêm trọng hơn: nếu `clean_target_folder` đi vào một junction,
nó xoá nội dung ở **đích** của link, không phải link.

Cách đúng: `entry.stat(follow_symlinks=False).st_file_attributes &
FILE_ATTRIBUTE_REPARSE_POINT`, hoặc `os.path.isjunction()` (Python ≥ 3.12) kết hợp
`islink()`.

### BUG-02 · Đường dẫn pnpm store sai — có thể phá cài đặt pnpm — P0

`:32-37` khai báo store là `%LOCALAPPDATA%\pnpm`. Đo được: **0 B / 0 file**.
`pnpm store path` trả về `E:\.pnpm-store\v11`. `%LOCALAPPDATA%\pnpm` là **pnpm
HOME** — nơi chứa chính binary `pnpm` và các global tool. Nếu thư mục đó có nội
dung, `clean_target_folder` sẽ xoá luôn cài đặt pnpm. Hiện tại tính năng này thu
hồi được **0 byte** và mang rủi ro phá công cụ.

Cách đúng: hỏi `pnpm store path` lúc runtime, và dùng `pnpm store prune` chứ không
xoá thô.

### BUG-03 · Chrome: chỉ nhìn profile `Default` — bỏ sót ~5 GB — P0

`:52-63` hardcode `User Data\Default\Cache`. Máy này có **9 profile**:
`Default, Profile 1, Profile 2, Profile 3, Profile 4, Profile 5, Profile 7,
Profile 9, Profile 13`.

| Mục | UI hiển thị | Thực tế |
|-----|------------:|--------:|
| `Default\Cache` | **0 B** | 0 B |
| `Default\Code Cache` | không hiển thị | 84.44 MB |
| Toàn bộ `Chrome\User Data` | — | **4.96 GB / 48 985 file** |

Người dùng thấy "Google Chrome Cache: 0 B" trong khi gần 5 GB đang nằm đó. Đây là
khoảng bỏ sót lớn nhất của v1.

### BUG-04 · Năm category được quét nhưng **không bao giờ dọn được** — P1

`TARGET_PATHS` có 19 key. UI chỉ gửi được 16 key (`data-key` trong
`cleaner_ui.html`). Ba key không có checkbox nào trỏ tới, cộng hai key bị nhãn UI
gộp sai:

| Key trong backend | Dung lượng đo được | UI có gửi? |
|-------------------|-------------------:|:----------:|
| `vscode_vsixs` | **720.80 MB** | ❌ |
| `vscode_cached_data` | 84.53 MB | ❌ |
| `chrome_code_cache` | 84.44 MB | ❌ |
| `crash_dumps` | 44.56 MB | ❌ |
| `edge_code_cache` | 0 B | ❌ |
| **Tổng chết** | **934.33 MB** | |

Tệ hơn, nhãn `chk_system_logs` ghi *"Windows Event Logs & Crash Dumps"*
(`cleaner_ui.html:881`) nhưng `data-key="system_logs"` — `crash_dumps` **không bao
giờ** được gửi. Nhãn nói sai sự thật.

Tương tự, ô "VS Code Editors Cache" mô tả *"extension objects, VSIXs, and editor
workspaces cache"* (`:831`) nhưng chỉ hiển thị và chỉ dọn `Code\Cache` = 5.60 MB,
bỏ qua 805 MB `CachedData` + `CachedExtensionVSIXs`.

### BUG-05 · Trạng thái LOCKED của Chrome AI không bao giờ phát hiện được — P1

`check_chrome_ai_status()` (`:202-214`) lặp trên `files` của `os.walk` rồi hỏi
`os.path.isdir(fp)`. `os.walk` **không bao giờ** đặt thư mục vào list `files`, nên
nhánh `is_locked = True` là code chết. Badge trên UI sẽ luôn báo `UNLOCKED` ngay cả
khi thư mục khoá `weights.bin` đang tồn tại — đúng cái nó có nhiệm vụ báo.

### BUG-06 · Ba nút "mở thư mục" ném `ReferenceError` — P1

`cleaner_ui.html:1202` dùng `USER_PROFILE`, `:1208` dùng `CHROME_AI_DIR`. Không
biến nào được khai báo ở phía JS. Kết quả: nút 📂 của `temp_files`,
`docker_compact`, `chrome_ai` chết. Ngoài ra với mọi key khác, `openFolder()`
(`:1211-1215`) gọi lại **`/api/scan` đầy đủ** chỉ để tra một đường dẫn — trên máy
này là quét lại hơn 100 000 file. `:1198` còn dựa vào biến global `event` ngầm
(chỉ Chrome, đã deprecated).

### BUG-07 · Ước tính tiết kiệm của Docker sai về bản chất — P1

`calculateEstimate()` (`:1161-1171`) cộng `sizesCache['docker_compact']` =
**toàn bộ** kích thước VHDX (5.09 GB + 4.36 GB = **9.45 GB**). Compaction chỉ thu
hồi phần *chưa dùng* bên trong ổ ảo. UI hứa 9.45 GB, thực tế có thể chỉ vài trăm MB.

### BUG-08 · `C:\Windows\Logs` bị gán nhãn "safe" và tick sẵn — P1

`:112-117` trỏ vào toàn bộ `%SystemRoot%\Logs` (45.39 MB / 306 file), UI gán
`class="option-safe"` và `checked` (`cleaner_ui.html:879`). Xoá sạch cây này phá
lịch sử chẩn đoán CBS/DISM/WindowsUpdate và cần quyền admin. Không sai chí tử,
nhưng "safe + tick sẵn" là phân loại rủi ro sai.

### BUG-09 · "Deep Preset" xoá sạch System Restore, không có đường lùi — P0

`applyPreset('deep')` tick **tất cả** checkbox (`cleaner_ui.html:1182-1187`), bao
gồm `vss_cleanup`, và `chkVssLimit` mặc định `checked` (`:939`). Nhấn Clean sẽ chạy
`vssadmin delete shadows /all /quiet` (`:486`) rồi
`vssadmin resize shadowstorage /maxsize=2GB` (`:492`).

Bằng chứng việc này **đã xảy ra** trên máy này:

```text
For volume: (C:)\\?\Volume{cbd6257a-...}\
  Used Shadow Copy Storage space:      0 bytes (0%)
  Allocated Shadow Copy Storage space: 0 bytes (0%)
  Maximum Shadow Copy Storage space:   2.00 GB (1%)
```

Trần 2 GB khiến restore point bị Windows purge liên tục — System Restore coi như
vô hiệu. Không có nút nào trong UI để xem hay hoàn tác thiết lập này.

### BUG-10 · Script diskpart ghi vào thư mục dữ liệu của Docker — P2

`:462` tạo `diskpart_compact.txt` **ngay cạnh file VHDX**, tức trong
`%LOCALAPPDATA%\Docker\wsl\disk` hoặc `C:\ProgramData\DockerDesktop\vm-data`. Ghi
rác vào data dir của Docker và cần quyền ghi ở đó. Phải dùng thư mục temp riêng.

### BUG-11 · `wsl --shutdown` không kiểm tra container đang chạy — P1

`:455` tắt WSL vô điều kiện. Container đang chạy bị kill; workload chưa commit có
thể mất dữ liệu. Cần dò trạng thái Docker/WSL và cảnh báo trước.

### BUG-12 · Dùng logical size thay vì size-on-disk — P2

`os.path.getsize()` (`:180`, `:374`) trả `st_size`. Với file sparse (VHDX), file nén
NTFS, hoặc volume có dedup, dung lượng thu hồi thực tế khác con số hiển thị. Cần
`GetCompressedFileSizeW` cho báo cáo chính xác.

### BUG-13 · Scan chặn, không tiến độ, không huỷ được — P1

`/api/scan` (`:341`) chạy toàn bộ `os.walk` **đồng bộ trong request handler**. Khối
lượng thực tế trên máy này:

| Target | File count |
|--------|-----------:|
| `uv\cache` | **70 373** |
| `Chrome\User Data` | 48 985 |
| `WinSxS` | ≥ 11 677 (probe hết 6 s time-budget) |
| `.nuget\packages` | 10 800 |
| `JetBrains` | 5 091 |

Mỗi file bị `stat` **hai lần** (`os.walk` + `os.path.getsize`). Không có progress,
không có cancel, không có cache, không có timeout. UI chỉ đổi chữ nút thành
"Scanning..." rồi treo. Đây là nguyên nhân trực tiếp của cảm giác "app bị đứng".

### BUG-14 → BUG-18 · Nhóm lỗi nhỏ

| ID | Vị trí | Vấn đề |
|----|--------|--------|
| BUG-14 | `:154-155` | `ThreadingTCPServer` không set `daemon_threads`/`allow_reuse_address`; Ctrl-C để lại thread treo |
| BUG-15 | `:11`, `:611` | Port 8342 hardcode; nếu bận thì server crash kèm traceback nhưng `.bat` vẫn mở browser → người dùng thấy trang lỗi không hiểu vì sao |
| BUG-16 | `run_cleaner.bat:9` | Gọi `python` trần; trên Windows rất thường trỏ vào Store alias và fail im lặng |
| BUG-17 | `cleaner_ui.html:190` vs `:1062` | CSS `stroke-dasharray: 440` còn JS set `377` (2πr, r=60). Giá trị CSS là code chết |
| BUG-18 | `src/__pycache__/*.pyc` | `.pyc` đã staged vào git; repo không có `.gitignore` |

---

## 5. Thiếu hụt về mặt "ứng dụng"

Đây là phần trả lời trực tiếp cho yêu cầu *"nâng cấp thành một Windows application
có giao diện và cài đặt được"*.

| # | Thiếu | Hệ quả |
|---|-------|--------|
| 1 | Không có **dry-run** | Công cụ xoá vĩnh viễn mà không có chế độ xem trước. Với một tool destructive, đây là thiếu sót số một. |
| 2 | Không có tuỳ chọn **đưa vào Recycle Bin** | Mọi thao tác không thể hoàn tác. Không dùng `SHFileOperation` + `FOF_ALLOWUNDO`. |
| 3 | Không nhận biết **quyền admin** | VSS, diskpart, WU cache, DISM đều cần admin. App chỉ ghi "Access Denied" vào log rồi đi tiếp. `run_cleaner.bat` không elevate. |
| 4 | Không có **báo cáo thu hồi thật** | Chỉ so free space tổng trước/sau (`:508`) rồi rescan. Không có "mục X thu hồi được Y byte". |
| 5 | Không có **lịch sử / log file** | Console log mất khi refresh trang. Không có audit trail của những gì đã bị xoá. |
| 6 | Không có **exclusion / whitelist** | Không có cách bảo vệ một đường dẫn cụ thể. |
| 7 | Không có **danh tính app** | Chạy trong browser: có address bar, có tab, taskbar là icon Chrome. Không icon riêng, không single-instance guard, không tray. |
| 8 | Không có **installer / uninstaller** | Không vào Start Menu, không có entry Add/Remove Programs, không version info, không auto-update path. Người dùng phải biết đường dẫn `.bat`. |
| 9 | **UI chỉ có tiếng Anh** | Người dùng viết tiếng Việt; README song ngữ nhưng UI thì không. |
| 10 | **Không có test nào** | 0 unit test, 0 integration test cho code xoá file. |
| 11 | Không **đa volume** | Chỉ `C:\` trong khi cả `D:\` và `E:\` đều dưới 12 % free. |
| 12 | Không có **auto-clean định kỳ** | Máy này bị lấp lại liên tục; dọn tay một lần không giải quyết nguyên nhân. |

---

## 6. Cơ hội bị bỏ sót — bảng đo đầy đủ

ADC v1 *hiển thị* khoảng 4 GB. Đo thực tế cho thấy **≈ 26 GB** có thể thu hồi hoặc
nén được. Cột "v1" cho biết v1 có nhìn thấy mục đó hay không.

| Mục | Đo được | v1 | Ghi chú |
|-----|--------:|:--:|---------|
| Docker VHDX (`docker_data` + `DockerDesktop`) | **9.45 GB** | ⚠️ | ước tính sai (BUG-07) |
| `Chrome\User Data` (9 profile) | **4.96 GB** | ❌ | v1 báo 0 B (BUG-03) |
| `.nuget\packages` | 2.21 GB | ✅ | tick sẵn, nhưng rebuild rất chậm |
| `uv\cache` | 1.80 GB | ✅ | 70 373 file; cần `uv cache prune` |
| `WinSxS` component store | ≥ 1.74 GB | ❌ | cần `DISM /StartComponentCleanup` |
| `%LOCALAPPDATA%\Packages` (WSL/Store) | 1.39 GB | ❌ | chứa `ext4.vhdx` của distro |
| `JetBrains` caches | 1.02 GB | ❌ | |
| `Code\CachedExtensionVSIXs` | 720.80 MB | ⚠️ | scanned, không dọn được (BUG-04) |
| `ms-playwright` browsers | 683.19 MB | ❌ | |
| Delivery Optimization | 576.49 MB | ✅ | |
| `npm-cache` | 545.95 MB | ✅ | nên dùng `npm cache clean --force` |
| `ProgramData\Package Cache` | 446.97 MB | ❌ | MSI/VS installer cache |
| `.cache\huggingface` | 444.76 MB | ❌ | |
| `Code\User\workspaceStorage` | 335.42 MB | ❌ | |
| `discord\Cache` | 239.24 MB | ❌ | |
| `Windows\Installer\$PatchCache$` | 59.83 MB | ❌ | |
| `Windows\Logs` | 45.39 MB | ✅ | phân loại rủi ro sai (BUG-08) |
| `CrashDumps` | 44.56 MB | ⚠️ | scanned, không dọn được (BUG-04) |
| `Explorer` thumbnail cache | 40.78 MB | ❌ | |
| `user_temp` | 40.95 MB | ✅ | |
| `Prefetch` | 28.27 MB | ❌ | |
| `.claude\projects` transcripts | 22.78 MB | ❌ | |
| `pagefile.sys` | 2.52 GB | ❌ | quản lý qua sizing, không xoá |
| `E:\.pnpm-store\v11` | chưa đo | ❌ | v1 trỏ sai chỗ (BUG-02) |

Đã kiểm tra và **không tồn tại** trên máy này (nhưng nên có trong catalogue để
portable): `Yarn\Cache`, `.cargo\registry`, `go\pkg\mod`, `.gradle\caches`,
`.m2\repository`, `.ollama\models`, `.cache\torch`, `.cache\puppeteer`,
`C:\Windows.old`, `hiberfil.sys` (hibernation đã tắt), `MEMORY.DMP`.

---

## 7. Tổng kết ưu tiên

| Mức | ID |
|-----|----|
| **P0 — sửa trước khi dùng tiếp** | SEC-01, SEC-02, SEC-03, BUG-01, BUG-02, BUG-03, BUG-09 |
| **P1 — sai chức năng thấy được** | SEC-05, BUG-04, BUG-05, BUG-06, BUG-07, BUG-08, BUG-11, BUG-13 |
| **P2 — chất lượng / chính xác** | SEC-04, SEC-06, BUG-10, BUG-12, BUG-14…BUG-18 |

Kết luận: v1 vừa **không an toàn** (SEC-01/02 cho phép web bên ngoài xoá file và
thực thi file local), vừa **báo cáo sai** (bỏ sót ~5 GB Chrome, 934 MB category
chết, ước tính Docker sai), vừa **không phải một ứng dụng Windows** (không installer,
không danh tính, không quyền admin, không dry-run). Cả ba nhóm đều được giải quyết
bởi cùng một quyết định kiến trúc: bỏ HTTP server, chuyển sang cửa sổ native với
bridge trong tiến trình. Xem `02-SPEC.md`.

