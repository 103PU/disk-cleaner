r"""The 49 targets, declared rather than coded (docs/02-SPEC.md 5).

This module is data. Each row names a **resolver** -- how to find the real paths
on *this* machine -- and a **strategy** -- how to clean them. Neither is invented
here and no row contains an ``if``. v1 mixed the path, the measurement, the risk
label and the delete into one function per target (``src/cleaner_backend.py``),
which made a wrong path and a wrong risk tier the same kind of edit and made
neither testable without deleting something.

Three rules hold across the whole table:

* **A missing tool or a missing directory is not a target.** Availability is the
  resolver's answer, and a resolver reports ``unavailable`` with a reason instead
  of guessing (BUG-02, BUG-03). Several rows resolve to nothing on this machine;
  that is the correct answer, not a hole in the catalogue.
* **The risk tier is SPEC 4.3's definition, not v1's optimism.** ``windows_logs``
  is CAUTION (BUG-08), the nuget rows are split so the 2.21 GB half is
  REBUILDABLE, and everything irreversible or system-configuration-changing is
  DANGEROUS and outside every preset (BUG-09).
* **Recycle unless recycling would be pointless.** SPEC 4.5 makes
  ``RecycleDelete`` the default for the user profile because it is the only undo
  ADC can offer. It is not used where the bytes are pure machine-regenerable
  cache measured in gigabytes: moving 4.96 GB of Chrome cache into the Recycle
  Bin frees nothing until the bin is emptied, and the bin is then holding data
  nobody would ever restore.

Two decisions the SPEC left open, made here and worth knowing:

* **Presets.** No SPEC table defines them. ``safe`` is SAFE only; ``deep`` adds
  REBUILDABLE and CAUTION and **never** DANGEROUS. Scheduled runs (SPEC 6.5) are
  narrower still: SAFE, and nothing that needs its own screen.
* **``chrome_ai_weights`` is REBUILDABLE, not SAFE.** SPEC 5 labels it SAFE while
  SPEC 4.3 defines REBUILDABLE as exactly this case -- rebuilding costs bandwidth
  (~4 GB re-download). The tier definition wins, which means the row is not
  ticked by default and shows its rebuild cost.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Final

from .resolvers import (
    AnyOf,
    ChromiumProfiles,
    GlobPath,
    RecycleBins,
    Resolution,
    Resolver,
    StaticPath,
    Systemic,
    ToolPresence,
    ToolQuery,
)
from .strategies import Advise, HardDelete, RecycleDelete, Strategy, ToolCommand, WinNative


class Category(str, Enum):
    """The UI's grouping. ``str`` so it serialises across the bridge as itself."""

    DEV = "dev"
    BROWSER = "browser"
    IDE = "ide"
    AI_ML = "ai_ml"
    SYSTEM = "system"
    TEMP = "temp"
    VM = "vm"
    APP = "app"


class Risk(str, Enum):
    """SPEC 4.3. The order is the order the UI presents them in."""

    SAFE = "safe"
    REBUILDABLE = "rebuildable"
    CAUTION = "caution"
    DANGEROUS = "dangerous"


@dataclass(frozen=True)
class Target:
    """One catalogue row (SPEC 4.2).

    ``admin_required`` is declared per row rather than derived from the strategy:
    ``WinNative`` needs administrator for two of its three operations and not for
    emptying the Recycle Bin, so the strategy class cannot answer for the row.
    ``tests/test_targets.py`` checks the two never disagree.

    The ``_en`` halves of ``est_note`` and ``rebuild_cost`` are an addition to
    SPEC 4.2, which lists only ``_vi``. Every other user-visible string in the
    dataclass is a pair, and a warning that stays Vietnamese in the English UI is
    a defect P4 would have to come back and fix.
    """

    id: str
    name_vi: str
    name_en: str
    desc_vi: str
    desc_en: str
    category: Category
    risk: Risk
    resolver: Resolver
    strategy: Strategy
    admin_required: bool = False
    min_age_hours: int = 0
    est_note_vi: str | None = None
    est_note_en: str | None = None
    rebuild_cost_vi: str | None = None
    rebuild_cost_en: str | None = None
    # Not a checkbox: it gets its own screen because the operation needs
    # prechecks or separate actions (``vss_manage``, ``docker_vhdx_compact``).
    standalone: bool = False

    def resolve(self) -> Resolution:
        """Ask the resolver. Never raises; an absent target answers unavailable."""
        return self.resolver.resolve()

    @property
    def reversible(self) -> bool:
        return self.strategy.reversible

    def as_dict(self) -> dict[str, Any]:
        """What the bridge sends the UI. No paths: those come from a resolve."""
        return {
            "id": self.id,
            "name": {"vi": self.name_vi, "en": self.name_en},
            "desc": {"vi": self.desc_vi, "en": self.desc_en},
            "category": self.category.value,
            "risk": self.risk.value,
            "resolver": self.resolver.kind,
            "strategy": self.strategy.kind,
            "reversible": self.reversible,
            "admin_required": self.admin_required,
            "min_age_hours": self.min_age_hours,
            "est_note": {"vi": self.est_note_vi, "en": self.est_note_en},
            "rebuild_cost": {"vi": self.rebuild_cost_vi, "en": self.rebuild_cost_en},
            "standalone": self.standalone,
        }


# ---------------------------------------------------------------------------
# DEV -- package manager caches (SPEC 5, "DEV")
#
# Four of these are ToolCommand rather than a raw delete, and that is the whole
# reason the section exists twice over: uv hardlinks its cache into live venvs,
# so deleting the files frees nothing until the last link goes (BUG-07), and
# npm/pnpm keep an index that a raw delete leaves lying about its contents.
# ---------------------------------------------------------------------------
_DEV: Final[tuple[Target, ...]] = (
    Target(
        id="uv_cache", category=Category.DEV, risk=Risk.REBUILDABLE,
        name_vi="Cache uv (Python)", name_en="uv cache (Python)",
        desc_vi="Wheel và source uv đã tải. Dùng prune, không xoá thô.",
        desc_en="uv's downloaded wheels and sources; pruned, never raw-deleted.",
        resolver=ToolQuery(("uv", "cache", "dir")),
        strategy=ToolCommand(("uv", "cache", "prune")),
        est_note_vi="venv hardlink vào đây: chỗ trống có thể tăng ít hơn.",
        est_note_en="Live venvs hardlink into this; free space may rise by less.",
        rebuild_cost_vi="Lần cài kế tiếp tải lại ~1.8 GB.",
        rebuild_cost_en="The next install re-downloads ~1.8 GB.",
    ),
    Target(
        id="npm_cache", category=Category.DEV, risk=Risk.SAFE,
        name_vi="Cache npm", name_en="npm cache",
        desc_vi="Gói npm đã tải (_cacache). npm tự tải lại khi cần.",
        desc_en="npm's _cacache of downloaded packages; refetched on demand.",
        resolver=ToolQuery(("npm", "config", "get", "cache")),
        strategy=ToolCommand(("npm", "cache", "clean", "--force")),
    ),
    Target(
        id="pnpm_store", category=Category.DEV, risk=Risk.SAFE,
        name_vi="Store pnpm", name_en="pnpm store",
        desc_vi="Store thật do pnpm khai báo, có thể nằm ở ổ khác.",
        desc_en="The store pnpm itself reports, which can be on another volume.",
        resolver=ToolQuery(("pnpm", "store", "path")),
        strategy=ToolCommand(("pnpm", "store", "prune")),
        est_note_vi="v1 đoán %LOCALAPPDATA%\\pnpm nên báo 0 B (BUG-02).",
        est_note_en="v1 guessed %LOCALAPPDATA%\\pnpm and reported 0 B (BUG-02).",
    ),
    Target(
        id="pip_cache", category=Category.DEV, risk=Risk.SAFE,
        name_vi="Cache pip", name_en="pip cache",
        desc_vi="Wheel pip đã build và tải về.",
        desc_en="Wheels pip has built or downloaded.",
        resolver=ToolQuery(("pip", "cache", "dir")),
        strategy=ToolCommand(("pip", "cache", "purge")),
    ),
    Target(
        id="nuget_http", category=Category.DEV, risk=Risk.SAFE,
        name_vi="Cache HTTP NuGet", name_en="NuGet HTTP cache",
        desc_vi="Phản hồi HTTP đã cache của NuGet, tách khỏi gói global.",
        desc_en="NuGet's cached HTTP responses, kept apart from global-packages.",
        resolver=StaticPath(r"%LOCALAPPDATA%\NuGet\v3-cache"),
        strategy=ToolCommand(("dotnet", "nuget", "locals", "http-cache", "--clear")),
    ),
    Target(
        id="nuget_global", category=Category.DEV, risk=Risk.REBUILDABLE,
        name_vi="Gói NuGet global", name_en="NuGet global packages",
        desc_vi="Gói đã giải nén dùng chung cho mọi project .NET.",
        desc_en="Extracted packages shared by every .NET project on the machine.",
        resolver=StaticPath(r"~\.nuget\packages"),
        strategy=ToolCommand(("dotnet", "nuget", "locals", "global-packages", "--clear")),
        rebuild_cost_vi="Build .NET kế tiếp restore lại ~2.2 GB.",
        rebuild_cost_en="The next .NET build restores ~2.2 GB.",
    ),
    Target(
        id="yarn_cache", category=Category.DEV, risk=Risk.SAFE,
        name_vi="Cache Yarn", name_en="Yarn cache",
        desc_vi="Gói Yarn đã tải. Không có trên máy này.",
        desc_en="Yarn's downloaded packages. Not present on this machine.",
        resolver=StaticPath(r"%LOCALAPPDATA%\Yarn\Cache"),
        strategy=HardDelete(),
    ),
    Target(
        id="cargo_registry", category=Category.DEV, risk=Risk.REBUILDABLE,
        name_vi="Registry Cargo (Rust)", name_en="Cargo registry (Rust)",
        desc_vi="Crate đã tải và giải nén. Giữ nguyên thư mục bin và .crates.",
        desc_en="Downloaded and unpacked crates; bin/ and .crates are left alone.",
        resolver=AnyOf((
            StaticPath(r"~\.cargo\registry\cache"),
            StaticPath(r"~\.cargo\registry\src"),
        )),
        strategy=HardDelete(),
        rebuild_cost_vi="cargo build kế tiếp tải lại crate.",
        rebuild_cost_en="The next cargo build re-downloads the crates.",
    ),
    Target(
        id="go_modcache", category=Category.DEV, risk=Risk.REBUILDABLE,
        name_vi="Cache module Go", name_en="Go module cache",
        desc_vi="Module Go đã tải. Xoá qua go clean vì file để read-only.",
        desc_en="Downloaded Go modules; cleaned via go clean, as they are read-only.",
        resolver=ToolQuery(("go", "env", "GOMODCACHE")),
        strategy=ToolCommand(("go", "clean", "-modcache")),
        rebuild_cost_vi="go build kế tiếp tải lại module.",
        rebuild_cost_en="The next go build re-downloads the modules.",
    ),
    Target(
        id="gradle_caches", category=Category.DEV, risk=Risk.REBUILDABLE,
        name_vi="Cache Gradle", name_en="Gradle caches",
        desc_vi="Dependency và build cache của Gradle.",
        desc_en="Gradle's dependency and build caches.",
        resolver=StaticPath(r"~\.gradle\caches"),
        strategy=HardDelete(),
        rebuild_cost_vi="Build Gradle kế tiếp tải lại dependency.",
        rebuild_cost_en="The next Gradle build re-resolves its dependencies.",
    ),
    Target(
        id="maven_repo", category=Category.DEV, risk=Risk.REBUILDABLE,
        name_vi="Repository Maven", name_en="Maven repository",
        desc_vi="Artifact Maven đã tải về ~\\.m2.",
        desc_en="Artifacts Maven has downloaded into ~\\.m2.",
        resolver=StaticPath(r"~\.m2\repository"),
        strategy=HardDelete(),
        rebuild_cost_vi="Build Maven kế tiếp tải lại artifact.",
        rebuild_cost_en="The next Maven build re-downloads the artifacts.",
    ),
)

# ---------------------------------------------------------------------------
# BROWSER -- multiplied by profile (SPEC 5, "BROWSER")
#
# The v1 number for Chrome was a fraction of the real one because it looked at
# ``Default`` alone; there are nine profiles here. HardDelete, not Recycle: these
# are gigabytes of pure cache, and the Recycle Bin would hold every byte of it
# without freeing any space until it is emptied.
# ---------------------------------------------------------------------------
_BROWSER: Final[tuple[Target, ...]] = (
    Target(
        id="chrome_caches", category=Category.BROWSER, risk=Risk.SAFE,
        name_vi="Cache Chrome (mọi profile)", name_en="Chrome caches (all profiles)",
        desc_vi="Cache HTTP, code, GPU và Service Worker của từng profile.",
        desc_en="HTTP, code, GPU and Service Worker caches of every profile.",
        resolver=ChromiumProfiles(r"%LOCALAPPDATA%\Google\Chrome\User Data"),
        strategy=HardDelete(),
        est_note_vi="Đóng Chrome trước khi dọn, nếu không nhiều file bị khoá.",
        est_note_en="Close Chrome first, or many files will be locked.",
    ),
    Target(
        id="edge_caches", category=Category.BROWSER, risk=Risk.SAFE,
        name_vi="Cache Edge (mọi profile)", name_en="Edge caches (all profiles)",
        desc_vi="Cùng bộ cache như Chrome, cho từng profile Edge.",
        desc_en="The same cache set as Chrome, per Edge profile.",
        resolver=ChromiumProfiles(r"%LOCALAPPDATA%\Microsoft\Edge\User Data"),
        strategy=HardDelete(),
        est_note_vi="WebView2 dùng chung runtime với Edge; đóng Edge trước.",
        est_note_en="WebView2 shares the Edge runtime; close Edge first.",
    ),
    Target(
        id="chrome_ai_weights", category=Category.BROWSER, risk=Risk.REBUILDABLE,
        name_vi="Model AI on-device của Chrome", name_en="Chrome on-device AI model",
        desc_vi="Trọng số model Chrome tải để chạy AI ngay trên máy.",
        desc_en="Model weights Chrome downloads to run AI locally.",
        resolver=StaticPath(r"%LOCALAPPDATA%\Google\Chrome\User Data\OptGuideOnDeviceModel"),
        strategy=HardDelete(),
        est_note_vi="Chrome sẽ tải lại nếu tính năng AI vẫn bật.",
        est_note_en="Chrome downloads it again while the AI feature stays on.",
        rebuild_cost_vi="Tải lại ~4 GB.",
        rebuild_cost_en="Re-downloads ~4 GB.",
    ),
)

# ---------------------------------------------------------------------------
# IDE / APP (SPEC 5, "IDE / APP")
# ---------------------------------------------------------------------------
_IDE: Final[tuple[Target, ...]] = (
    Target(
        id="vscode_vsixs", category=Category.IDE, risk=Risk.SAFE,
        name_vi="VSIX extension đã cache (VS Code)", name_en="Cached extension VSIXs (VS Code)",
        desc_vi="Bản .vsix tải về khi cài extension, giữ lại sau khi cài xong.",
        desc_en="The .vsix downloads kept after an extension is already installed.",
        resolver=StaticPath(r"%APPDATA%\Code\CachedExtensionVSIXs"),
        strategy=RecycleDelete(),
    ),
    Target(
        id="vscode_cacheddata", category=Category.IDE, risk=Risk.SAFE,
        name_vi="CachedData (VS Code)", name_en="CachedData (VS Code)",
        desc_vi="Bytecode V8 đã cache theo từng bản VS Code.",
        desc_en="V8 bytecode cached per VS Code build.",
        resolver=StaticPath(r"%APPDATA%\Code\CachedData"),
        strategy=HardDelete(),
    ),
    Target(
        id="vscode_cache", category=Category.IDE, risk=Risk.SAFE,
        name_vi="Cache HTTP (VS Code)", name_en="HTTP cache (VS Code)",
        desc_vi="Cache mạng của Electron trong VS Code.",
        desc_en="VS Code's Electron network cache.",
        resolver=StaticPath(r"%APPDATA%\Code\Cache"),
        strategy=HardDelete(),
    ),
    Target(
        id="vscode_workspacestorage", category=Category.IDE, risk=Risk.CAUTION,
        name_vi="workspaceStorage (VS Code)", name_en="workspaceStorage (VS Code)",
        desc_vi="State theo từng workspace: file đang mở, undo, dữ liệu extension.",
        desc_en="Per-workspace state: open editors, undo history, extension data.",
        resolver=StaticPath(r"%APPDATA%\Code\User\workspaceStorage"),
        strategy=RecycleDelete(),
        est_note_vi="Mất layout và state extension của các workspace đã mở.",
        est_note_en="Loses the layout and extension state of every workspace.",
    ),
    Target(
        id="vscode_logs", category=Category.IDE, risk=Risk.SAFE,
        name_vi="Log VS Code", name_en="VS Code logs",
        desc_vi="Log theo phiên của VS Code và các extension host.",
        desc_en="Per-session logs from VS Code and its extension hosts.",
        resolver=StaticPath(r"%APPDATA%\Code\logs"),
        strategy=RecycleDelete(),
        min_age_hours=24,
    ),
    Target(
        id="jetbrains_caches", category=Category.IDE, risk=Risk.SAFE,
        name_vi="Cache JetBrains", name_en="JetBrains caches",
        desc_vi="Index và cache của từng IDE JetBrains đã cài.",
        desc_en="Indexes and caches of each installed JetBrains IDE.",
        resolver=GlobPath(r"%LOCALAPPDATA%\JetBrains\*\caches", dirs_only=True),
        strategy=HardDelete(),
        est_note_vi="IDE index lại lần mở kế tiếp, mất vài phút.",
        est_note_en="The IDE re-indexes on next launch, which takes minutes.",
    ),
    Target(
        id="discord_cache", category=Category.APP, risk=Risk.SAFE,
        name_vi="Cache Discord", name_en="Discord cache",
        desc_vi="Cache HTTP, code và GPU của client Discord.",
        desc_en="The Discord client's HTTP, code and GPU caches.",
        resolver=AnyOf((
            StaticPath(r"%APPDATA%\discord\Cache"),
            StaticPath(r"%APPDATA%\discord\Code Cache"),
            StaticPath(r"%APPDATA%\discord\GPUCache"),
        )),
        strategy=HardDelete(),
    ),
    Target(
        id="claude_projects", category=Category.APP, risk=Risk.CAUTION,
        name_vi="Transcript project Claude Code", name_en="Claude Code project transcripts",
        desc_vi="Bản ghi hội thoại và memory theo project. Không tái tạo được.",
        desc_en="Per-project conversation transcripts and memory; not regenerable.",
        resolver=StaticPath(r"~\.claude\projects"),
        strategy=RecycleDelete(),
        min_age_hours=24,
        est_note_vi="Giữ lại file mới hơn 24 h để không cắt phiên đang chạy.",
        est_note_en="Files newer than 24 h are kept so a live session survives.",
    ),
)

# ---------------------------------------------------------------------------
# AI / ML (SPEC 5, "AI / ML")
#
# Every row here is REBUILDABLE by SPEC 4.3's definition: the bytes come back
# only over the network, and they are large. None is ticked by default.
# ---------------------------------------------------------------------------
_AI_ML: Final[tuple[Target, ...]] = (
    Target(
        id="huggingface_cache", category=Category.AI_ML, risk=Risk.REBUILDABLE,
        name_vi="Cache Hugging Face hub", name_en="Hugging Face hub cache",
        desc_vi="Model và dataset đã tải từ Hugging Face.",
        desc_en="Models and datasets downloaded from Hugging Face.",
        resolver=StaticPath(r"~\.cache\huggingface\hub"),
        strategy=HardDelete(),
        rebuild_cost_vi="Lần dùng kế tiếp tải lại model.",
        rebuild_cost_en="The next run re-downloads the models.",
    ),
    Target(
        id="ms_playwright", category=Category.AI_ML, risk=Risk.REBUILDABLE,
        name_vi="Browser Playwright", name_en="Playwright browsers",
        desc_vi="Chromium, Firefox và WebKit do Playwright tải về.",
        desc_en="The Chromium, Firefox and WebKit builds Playwright downloads.",
        resolver=StaticPath(r"%LOCALAPPDATA%\ms-playwright"),
        strategy=HardDelete(),
        rebuild_cost_vi="Cần chạy lại playwright install (~680 MB).",
        rebuild_cost_en="Needs playwright install again (~680 MB).",
    ),
    Target(
        id="ollama_models", category=Category.AI_ML, risk=Risk.REBUILDABLE,
        name_vi="Model Ollama", name_en="Ollama models",
        desc_vi="Blob model LLM đã pull về máy.",
        desc_en="LLM model blobs pulled onto this machine.",
        resolver=StaticPath(r"~\.ollama\models"),
        strategy=HardDelete(),
        rebuild_cost_vi="Phải ollama pull lại từng model.",
        rebuild_cost_en="Each model has to be pulled again.",
    ),
    Target(
        id="torch_hub", category=Category.AI_ML, risk=Risk.REBUILDABLE,
        name_vi="Cache PyTorch hub", name_en="PyTorch hub cache",
        desc_vi="Checkpoint và model torch.hub đã tải.",
        desc_en="Checkpoints and models torch.hub has downloaded.",
        resolver=StaticPath(r"~\.cache\torch"),
        strategy=HardDelete(),
        rebuild_cost_vi="Lần load kế tiếp tải lại checkpoint.",
        rebuild_cost_en="The next load re-downloads the checkpoints.",
    ),
    Target(
        id="puppeteer_cache", category=Category.AI_ML, risk=Risk.REBUILDABLE,
        name_vi="Cache Puppeteer", name_en="Puppeteer cache",
        desc_vi="Bản Chrome for Testing do Puppeteer tải.",
        desc_en="The Chrome for Testing builds Puppeteer downloads.",
        resolver=StaticPath(r"~\.cache\puppeteer"),
        strategy=HardDelete(),
        rebuild_cost_vi="Lần cài kế tiếp tải lại browser.",
        rebuild_cost_en="The next install re-downloads the browser.",
    ),
)

# ---------------------------------------------------------------------------
# TEMP (SPEC 5, "TEMP")
#
# ``min_age_hours=24`` on both temp directories is the whole safety story here: a
# running installer's working files live in %TEMP%, and deleting them mid-install
# breaks the install. Age is measured per file, not per directory.
# ---------------------------------------------------------------------------
_TEMP: Final[tuple[Target, ...]] = (
    Target(
        id="user_temp", category=Category.TEMP, risk=Risk.SAFE,
        name_vi="Temp người dùng", name_en="User temp",
        desc_vi="File tạm của mọi app chạy dưới tài khoản này.",
        desc_en="Scratch files from every app running as this user.",
        resolver=StaticPath(r"%LOCALAPPDATA%\Temp"),
        strategy=HardDelete(),
        min_age_hours=24,
        est_note_vi="File mới hơn 24 h được giữ: có thể đang được dùng.",
        est_note_en="Files newer than 24 h are kept; they may be in use.",
    ),
    Target(
        id="system_temp", category=Category.TEMP, risk=Risk.SAFE,
        name_vi="Temp hệ thống", name_en="System temp",
        desc_vi="File tạm của service và của installer chạy dưới SYSTEM.",
        desc_en="Scratch files from services and installers running as SYSTEM.",
        resolver=StaticPath(r"%SystemRoot%\Temp"),
        strategy=HardDelete(),
        admin_required=True,
        min_age_hours=24,
    ),
    Target(
        id="recycle_bin", category=Category.TEMP, risk=Risk.SAFE,
        name_vi="Thùng rác (mọi ổ)", name_en="Recycle Bin (all volumes)",
        desc_vi="Dọn thùng rác trên từng ổ cố định, không chỉ ổ C.",
        desc_en="Empties the bin on every fixed volume, not just C: (BUG-04).",
        resolver=RecycleBins(),
        strategy=WinNative("empty_recycle_bin"),
        est_note_vi="Dọn thùng rác là không hoàn tác được.",
        est_note_en="Emptying the bin cannot be undone.",
    ),
    Target(
        id="inetcache", category=Category.TEMP, risk=Risk.SAFE,
        name_vi="Cache Internet (WinINET)", name_en="Internet cache (WinINET)",
        desc_vi="Cache HTTP của Explorer, IE và các app dùng WinINET.",
        desc_en="The HTTP cache of Explorer, IE and anything using WinINET.",
        resolver=StaticPath(r"%LOCALAPPDATA%\Microsoft\Windows\INetCache"),
        strategy=HardDelete(),
    ),
    Target(
        id="thumbnail_cache", category=Category.TEMP, risk=Risk.CAUTION,
        name_vi="Cache thumbnail và icon", name_en="Thumbnail and icon cache",
        desc_vi="thumbcache_*.db và iconcache_*.db của Explorer.",
        desc_en="Explorer's thumbcache_*.db and iconcache_*.db databases.",
        resolver=StaticPath(r"%LOCALAPPDATA%\Microsoft\Windows\Explorer"),
        strategy=HardDelete(),
        est_note_vi="Explorer đang giữ các file này; thường phải khởi động lại.",
        est_note_en="Explorer holds these open; it usually has to be restarted.",
        rebuild_cost_vi="Thumbnail dựng lại dần khi mở lại từng thư mục.",
        rebuild_cost_en="Thumbnails rebuild gradually as folders are reopened.",
    ),
)

# ---------------------------------------------------------------------------
# SYSTEM (SPEC 5, "SYSTEM")
#
# Most of this section needs administrator, and the tiers are where v1 was most
# wrong: ``windows_logs`` was SAFE (BUG-08) although it holds the only record of
# a failed servicing operation, and the three rows that change machine
# configuration rather than delete cache are DANGEROUS.
# ---------------------------------------------------------------------------
_SYSTEM: Final[tuple[Target, ...]] = (
    Target(
        id="windows_update_dl", category=Category.SYSTEM, risk=Risk.SAFE,
        name_vi="Tải về của Windows Update", name_en="Windows Update downloads",
        desc_vi="Payload update đã cài xong, Windows tải lại được nếu cần.",
        desc_en="Update payloads already installed; Windows refetches if needed.",
        resolver=StaticPath(r"%SystemRoot%\SoftwareDistribution\Download"),
        strategy=HardDelete(),
        admin_required=True,
    ),
    Target(
        id="delivery_optimization", category=Category.SYSTEM, risk=Risk.SAFE,
        name_vi="Cache Delivery Optimization", name_en="Delivery Optimization cache",
        desc_vi="Phần update Windows chia sẻ trong mạng LAN.",
        desc_en="Update chunks Windows keeps to share on the local network.",
        resolver=StaticPath(
            r"%SystemRoot%\ServiceProfiles\NetworkService\AppData\Local"
            r"\Microsoft\Windows\DeliveryOptimization\Cache"
        ),
        strategy=HardDelete(),
        admin_required=True,
    ),
    Target(
        id="windows_logs", category=Category.SYSTEM, risk=Risk.CAUTION,
        name_vi="Log Windows", name_en="Windows logs",
        desc_vi="CBS, DISM, log servicing — chứng cứ duy nhất khi update lỗi.",
        desc_en="CBS, DISM and servicing logs: the only record of a failed update.",
        resolver=StaticPath(r"%SystemRoot%\Logs"),
        strategy=RecycleDelete(),
        admin_required=True,
        min_age_hours=24,
        est_note_vi="v1 gán SAFE (BUG-08); mất log là mất đường chẩn đoán.",
        est_note_en="v1 called this SAFE (BUG-08); losing it loses the diagnosis.",
    ),
    Target(
        id="crash_dumps", category=Category.SYSTEM, risk=Risk.SAFE,
        name_vi="Crash dump ứng dụng", name_en="Application crash dumps",
        desc_vi="Dump do app trong profile này crash sinh ra.",
        desc_en="Dumps written when an app in this profile crashed.",
        resolver=StaticPath(r"%LOCALAPPDATA%\CrashDumps"),
        strategy=RecycleDelete(),
    ),
    Target(
        id="wer_reports", category=Category.SYSTEM, risk=Risk.SAFE,
        name_vi="Báo cáo lỗi Windows (WER)", name_en="Windows Error Reporting queue",
        desc_vi="Báo cáo lỗi đang chờ gửi, ở cả profile và ProgramData.",
        desc_en="Queued error reports, in both the profile and ProgramData.",
        resolver=AnyOf((
            StaticPath(r"%LOCALAPPDATA%\Microsoft\Windows\WER"),
            StaticPath(r"%PROGRAMDATA%\Microsoft\Windows\WER"),
        )),
        strategy=HardDelete(),
        admin_required=True,
    ),
    Target(
        id="memory_dmp", category=Category.SYSTEM, risk=Risk.SAFE,
        name_vi="Dump kernel (MEMORY.DMP)", name_en="Kernel dumps (MEMORY.DMP)",
        desc_vi="Dump toàn bộ bộ nhớ và minidump sau khi máy dừng đột ngột.",
        desc_en="Full memory dumps and minidumps left after a bug check.",
        resolver=AnyOf((
            StaticPath(r"%SystemRoot%\MEMORY.DMP"),
            StaticPath(r"%SystemRoot%\Minidump"),
        )),
        strategy=HardDelete(),
        admin_required=True,
    ),
    Target(
        id="patch_cache", category=Category.SYSTEM, risk=Risk.CAUTION,
        name_vi="Patch cache của Windows Installer", name_en="Windows Installer patch cache",
        desc_vi="Bản lưu để sửa hoặc gỡ MSI mà không cần file gốc.",
        desc_en="Baselines that let MSI repair or uninstall without the original.",
        resolver=StaticPath(r"%SystemRoot%\Installer\$PatchCache$"),
        strategy=HardDelete(),
        admin_required=True,
        est_note_vi="Sau khi xoá, sửa hoặc gỡ MSI có thể đòi file cài gốc.",
        est_note_en="Afterwards an MSI repair may ask for the original installer.",
    ),
    Target(
        id="package_cache", category=Category.SYSTEM, risk=Risk.CAUTION,
        name_vi="Package Cache (bootstrapper)", name_en="Package Cache (bootstrapper)",
        desc_vi="Bản cài Visual Studio và VC++ redistributable giữ lại để sửa.",
        desc_en="Visual Studio and VC++ redistributable payloads kept for repair.",
        resolver=StaticPath(r"%PROGRAMDATA%\Package Cache"),
        strategy=HardDelete(),
        admin_required=True,
        est_note_vi="Sửa hoặc gỡ Visual Studio sau đó sẽ đòi tải lại.",
        est_note_en="A later Visual Studio repair will need to download again.",
    ),
    Target(
        id="prefetch", category=Category.SYSTEM, risk=Risk.CAUTION,
        name_vi="Prefetch", name_en="Prefetch",
        desc_vi="Dữ liệu Windows dùng để mở app nhanh hơn.",
        desc_en="The data Windows uses to start applications faster.",
        resolver=StaticPath(r"%SystemRoot%\Prefetch"),
        strategy=HardDelete(),
        admin_required=True,
        rebuild_cost_vi="App mở chậm hơn vài lần đầu cho tới khi dựng lại.",
        rebuild_cost_en="Apps start slower for a few launches while it rebuilds.",
    ),
    Target(
        id="windows_old", category=Category.SYSTEM, risk=Risk.CAUTION,
        name_vi="Windows.old", name_en="Windows.old",
        desc_vi="Bản Windows cũ giữ lại sau khi nâng cấp phiên bản.",
        desc_en="The previous Windows installation kept after an upgrade.",
        resolver=StaticPath(r"%SystemDrive%\Windows.old"),
        strategy=HardDelete(),
        admin_required=True,
        est_note_vi="Xoá xong là không quay về bản Windows trước được nữa.",
        est_note_en="Once removed, rolling back to the previous build is gone.",
    ),
    Target(
        id="component_store", category=Category.SYSTEM, risk=Risk.DANGEROUS,
        name_vi="Dọn component store (WinSxS)", name_en="Component store cleanup (WinSxS)",
        desc_vi="DISM bỏ bản component cũ. Không hoàn tác, chạy khá lâu.",
        desc_en="DISM drops superseded components. Not undoable, and slow.",
        resolver=Systemic("component_store"),
        strategy=WinNative("dism_component_cleanup"),
        admin_required=True,
        est_note_vi="Không đo trước được; DISM tự báo dung lượng thu hồi.",
        est_note_en="Cannot be measured up front; DISM reports what it reclaimed.",
    ),
    Target(
        id="hibernation", category=Category.SYSTEM, risk=Risk.DANGEROUS,
        name_vi="Tắt hibernation (hiberfil.sys)", name_en="Disable hibernation (hiberfil.sys)",
        desc_vi="Đổi cấu hình máy: tắt ngủ đông và cả Fast Startup.",
        desc_en="Changes machine configuration: no hibernate, no Fast Startup.",
        resolver=Systemic("hibernation"),
        strategy=WinNative("hibernate_off"),
        admin_required=True,
        est_note_vi="Bật lại bằng powercfg /hibernate on.",
        est_note_en="Reversed with powercfg /hibernate on.",
    ),
    Target(
        id="pagefile_advise", category=Category.SYSTEM, risk=Risk.CAUTION,
        name_vi="Pagefile (chỉ tư vấn)", name_en="Pagefile (advice only)",
        desc_vi="ADC không bao giờ xoá pagefile; chỉ báo size và cách đổi.",
        desc_en="ADC never deletes the pagefile; it reports size and how to change it.",
        resolver=StaticPath(r"%SystemDrive%\pagefile.sys"),
        strategy=Advise(
            reason_vi="Đổi pagefile qua System Properties, không xoá file.",
            reason_en="Resize the pagefile in System Properties; never delete it.",
        ),
        admin_required=True,
        est_note_vi="Tắt pagefile làm crash dump không ghi được và app lớn dễ hết bộ nhớ.",
        est_note_en="Disabling it breaks crash dumps and starves large apps of memory.",
    ),
)

# ---------------------------------------------------------------------------
# VM / VIRTUALIZATION (SPEC 5, "VM / VIRTUALIZATION")
#
# Three of these four are ``Advise`` on purpose. Compacting a VHDX and resizing
# VSS storage need prechecks and a screen of their own -- is Docker Desktop
# stopped, is a container running (BUG-11), where does the diskpart script go
# (BUG-10) -- and P5 builds those. Until then the honest answer is the size, the
# real path, and the instruction; a checkbox that silently ran diskpart would be
# exactly the class of surprise BUG-09 already inflicted on this machine.
# ---------------------------------------------------------------------------
_VM: Final[tuple[Target, ...]] = (
    Target(
        id="docker_prune", category=Category.VM, risk=Risk.CAUTION,
        name_vi="Dọn rác Docker", name_en="Docker prune",
        desc_vi="Bỏ container đã dừng, image mồ côi, network và build cache.",
        desc_en="Removes stopped containers, dangling images, networks, build cache.",
        resolver=ToolPresence("docker"),
        strategy=ToolCommand(("docker", "system", "prune", "-f")),
        est_note_vi="Docker tự báo số thu hồi; file VHDX chưa nhỏ lại.",
        est_note_en="Docker reports its own reclaim; the VHDX does not shrink yet.",
        rebuild_cost_vi="Build kế tiếp mất cache, phải build lại từ đầu.",
        rebuild_cost_en="The next build has no cache and starts from scratch.",
    ),
    Target(
        id="docker_vhdx_compact", category=Category.VM, risk=Risk.DANGEROUS,
        name_vi="Nén VHDX của Docker", name_en="Compact Docker VHDX",
        desc_vi="Ổ ảo Docker giữ nguyên size sau khi xoá dữ liệu bên trong.",
        desc_en="Docker's virtual disks keep their size after data inside is freed.",
        resolver=AnyOf((
            StaticPath(r"%LOCALAPPDATA%\Docker\wsl\disk\docker_data.vhdx"),
            StaticPath(r"%LOCALAPPDATA%\Docker\wsl\main\ext4.vhdx"),
            StaticPath(r"%PROGRAMDATA%\DockerDesktop\vm-data\DockerDesktop.vhdx"),
        )),
        strategy=Advise(
            reason_vi="Cần tắt Docker Desktop trước; màn hình riêng ở P5.",
            reason_en="Needs Docker Desktop stopped; its own screen lands in P5.",
            handled_by="docker_prune",
        ),
        admin_required=True,
        standalone=True,
        est_note_vi="Số hiện ra là size file VHDX, không phải phần thu hồi được.",
        est_note_en="The number shown is the VHDX file size, not the reclaimable part.",
    ),
    Target(
        id="wsl_distro_vhdx", category=Category.VM, risk=Risk.DANGEROUS,
        name_vi="Nén VHDX của distro WSL", name_en="Compact WSL distro VHDX",
        desc_vi="ext4.vhdx của distro cài từ Store, cũng không tự nhỏ lại.",
        desc_en="The ext4.vhdx of a Store-installed distro; also never shrinks.",
        resolver=GlobPath(r"%LOCALAPPDATA%\Packages\*\LocalState\ext4.vhdx"),
        strategy=Advise(
            reason_vi="Cần wsl --shutdown rồi nén; màn hình riêng ở P5.",
            reason_en="Needs wsl --shutdown then a compact; own screen in P5.",
        ),
        admin_required=True,
        standalone=True,
        est_note_vi="Nén sai lúc distro đang chạy có thể làm hỏng filesystem ext4 bên trong.",
        est_note_en="Compacting while the distro runs can corrupt the ext4 filesystem inside.",
    ),
    Target(
        id="vss_manage", category=Category.VM, risk=Risk.DANGEROUS,
        name_vi="Shadow copy / System Restore", name_en="Shadow copies / System Restore",
        desc_vi="Xem, xoá shadow copy và đặt lại giới hạn dung lượng.",
        desc_en="View and delete shadow copies, and reset the storage limit.",
        resolver=Systemic("vss"),
        strategy=Advise(
            reason_vi="Ba hành động tách biệt, không phải một checkbox (BUG-09).",
            reason_en="Three separate actions, not one checkbox (BUG-09).",
        ),
        admin_required=True,
        standalone=True,
        est_note_vi="Trên máy này giới hạn còn 2.00 GB do v1 đặt lại.",
        est_note_en="On this machine the limit is still 2.00 GB, set by v1.",
    ),
)


# ---------------------------------------------------------------------------
# The catalogue, and the only ways to reach it
# ---------------------------------------------------------------------------
def _index(rows: tuple[Target, ...]) -> dict[str, Target]:
    """``id -> Target``, refusing a duplicate id rather than dropping a row.

    A dict comprehension would silently keep the last of two rows sharing an id,
    and the catalogue would then be one target short with nothing to show it.
    """
    out: dict[str, Target] = {}
    for row in rows:
        if row.id in out:
            raise ValueError(f"duplicate target id in the catalogue: {row.id}")
        out[row.id] = row
    return out


_CATALOG: Final[tuple[Target, ...]] = _DEV + _BROWSER + _IDE + _AI_ML + _TEMP + _SYSTEM + _VM
_BY_ID: Final[dict[str, Target]] = _index(_CATALOG)

# Presets are a decision this module makes, not a SPEC table (see the module
# docstring). DANGEROUS is absent from both by construction, which is the
# structural half of the BUG-09 fix -- ``vss_cleanup`` could not be dragged back
# into Deep without editing this literal.
PRESETS: Final[dict[str, tuple[Risk, ...]]] = {
    "safe": (Risk.SAFE,),
    "deep": (Risk.SAFE, Risk.REBUILDABLE, Risk.CAUTION),
}


def catalog() -> tuple[Target, ...]:
    """Every target, in catalogue order. Nothing is resolved by this call."""
    return _CATALOG


def ids() -> tuple[str, ...]:
    return tuple(_BY_ID)


def by_id(target_id: str) -> Target:
    """The target with this id, or ``KeyError``.

    The bridge's only path from a UI string to a set of real paths (SEC-02): the
    UI names a target, Python owns the path.
    """
    return _BY_ID[target_id]


def find(target_id: str) -> Target | None:
    """``by_id`` for a caller that treats an unknown id as an ordinary answer."""
    return _BY_ID.get(target_id)


def by_category(category: Category) -> tuple[Target, ...]:
    return tuple(row for row in _CATALOG if row.category is category)


def by_risk(risk: Risk) -> tuple[Target, ...]:
    return tuple(row for row in _CATALOG if row.risk is risk)


def in_preset(target: Target, name: str) -> bool:
    """Whether *target* is ticked by the named preset.

    ``standalone`` rows are excluded whatever their tier: a preset is a set of
    checkboxes, and those rows are not checkboxes.
    """
    risks = PRESETS[name]
    return not target.standalone and target.risk in risks


def preset(name: str) -> tuple[Target, ...]:
    """The targets a preset ticks, or ``KeyError`` naming the valid presets."""
    if name not in PRESETS:
        raise KeyError(f"unknown preset {name!r}; expected one of {sorted(PRESETS)}")
    return tuple(row for row in _CATALOG if in_preset(row, name))


def schedulable() -> tuple[Target, ...]:
    """What an unattended run may touch (SPEC 6.5): SAFE, and nothing else.

    Narrower than the Safe preset in intent even where it matches in extension
    today: a scheduled run has nobody watching it, so a tier that asks the user
    to weigh a rebuild cost has no business in one.
    """
    return tuple(
        row for row in _CATALOG if row.risk is Risk.SAFE and not row.standalone
    )


def catalog_as_dicts() -> list[dict[str, Any]]:
    """The catalogue as the bridge sends it: metadata only, still no paths."""
    return [row.as_dict() for row in _CATALOG]
