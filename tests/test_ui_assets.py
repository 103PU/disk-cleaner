"""Static gates on the UI assets. No browser, no pywebview -- just the files.

Every check in here exists because the thing it checks is invisible at review time
and silent at runtime. A CSP directive that was relaxed, a ``<style>`` block that
the CSP drops on the floor, a ``<use href="#icon-typo">`` that renders as nothing,
a colour token nudged two shades darker: none of them raise, none of them log, and
all of them ship.

The three that carry the most weight:

* **The CSP is asserted character for character.** It was measured in a real
  WebView2 148 window (see the comment at the top of ``index.html``), so it is
  evidence, not preference, and a well-meaning ``'unsafe-inline'`` added to make an
  inline style work would undo SEC-06 without a single visible symptom.
* **Contrast is recomputed, not trusted.** ``tokens.css`` used to state its ratios
  in a comment; a comment cannot fail. These tests run the WCAG 2.1 formula over
  the file, so P4's "every text colour clears 4.5:1" acceptance row is checked by
  the suite instead of by hand.
* **``opacity`` cannot be used to dim text.** A token can be measured against the
  page colour; ``opacity: .6`` composites against whatever happens to be behind it
  and cannot. The mechanical form of that rule is below: a static fractional
  opacity is banned, a 0-to-1 fade inside ``@keyframes`` is not.

Off-Windows this file is still meaningful -- it reads bytes and does arithmetic --
so unlike ``test_shell_window.py`` it carries no marker.
"""

from __future__ import annotations

import re
import struct
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[1] / "src" / "adc" / "ui"
INDEX = UI / "index.html"
CSS = UI / "css"
JS = UI / "js"

# The measured policy, verbatim from index.html. Written out here rather than
# imported from the file so that changing the file cannot change the expectation --
# that is the entire point of pinning it.
EXPECTED_CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'none'; "
    "connect-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "object-src 'none'"
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def css_files() -> list[Path]:
    return sorted(CSS.glob("*.css"))


def js_files() -> list[Path]:
    return sorted(JS.rglob("*.js"))


def strip_comments(text: str) -> str:
    """Drop ``/* ... */`` so a rule mentioned in prose is not read as a rule."""
    return re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)


def strip_js_comments(text: str) -> str:
    """Drop ``/* ... */`` and ``// ...`` for the same reason.

    Crude on purpose: a ``//`` inside a string literal would be eaten too. Nothing
    in this UI has a URL in a string -- there is no network -- so the crude version
    is safe here and a real tokeniser would be a dependency for nothing.
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"(?m)//.*$", " ", text)


def js_strings(text: str) -> list[str]:
    """Every single-quoted literal in a script, in source order.

    A walker rather than ``re.findall(r"'([^']+)'")`` because naive quote pairing
    desynchronises: the first double-quoted string holding an apostrophe -- and this
    UI has several -- makes every later pairing off by one, and the keys after it
    vanish silently. That is not hypothetical, it is how ``clean.cat.none`` went
    missing from the first extraction of this list.

    Comments are skipped here rather than pre-stripped, so a key quoted inside a
    ``/* ... */`` explanation is not counted as a key the UI asks for.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char == "/" and text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        elif char == "/" and text.startswith("//", i):
            end = text.find("\n", i)
            i = n if end < 0 else end + 1
        elif char in "'\"`":
            quote, j, buf = char, i + 1, []
            while j < n and text[j] != quote:
                if text[j] == "\\":
                    buf.append(text[j : j + 2])
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            if quote == "'":
                out.append("".join(buf))
            i = j + 1
        else:
            i += 1
    return out


def markup() -> str:
    """index.html with its comments removed.

    Not an optimisation -- a correctness requirement. The comments in that file
    explain the CSP by quoting the very constructs these tests ban, so a scan over
    the raw text finds ``<style>`` and a bare ``<script src>`` in the prose and
    fails on the documentation instead of on the markup.
    """
    return re.sub(r"<!--.*?-->", " ", read(INDEX), flags=re.DOTALL)



# ---------------------------------------------------------------------------
# SEC-06: the content security policy
# ---------------------------------------------------------------------------
def test_the_csp_is_exactly_the_policy_that_was_measured() -> None:
    """Character for character, including the order of the directives.

    An extra source in any directive is a hole; a missing directive falls back to
    ``default-src 'none'`` and is therefore safe but probably a mistake. Compared
    as a whole string so both show up.
    """
    html = markup()
    match = re.search(
        r'<meta\s+http-equiv="Content-Security-Policy"\s+content="([^"]+)"',
        html,
    )

    assert match is not None, "no CSP meta tag in index.html"
    assert " ".join(match.group(1).split()) == EXPECTED_CSP


@pytest.mark.parametrize(
    "forbidden",
    ["'unsafe-inline'", "'unsafe-eval'", "http:", "https:", "data: script", "*"],
)
def test_the_csp_never_grows_an_escape_hatch(forbidden: str) -> None:
    """The specific relaxations someone reaches for when an inline rule fails."""
    html = markup()
    csp = re.search(r'content="(default-src[^"]+)"', html)

    assert csp is not None
    assert forbidden not in csp.group(1)


def test_there_is_no_inline_style_block_because_it_would_do_nothing() -> None:
    """``style-src 'self'`` blocks ``<style>`` on this origin -- measured.

    So a ``<style>`` block here is not a style that works; it is a style that is
    silently dropped, which is worse than one that fails loudly.
    """
    assert "<style" not in markup().lower()


def test_every_script_tag_is_external_because_inline_script_is_blocked() -> None:
    """Same measurement, other half: an inline ``<script>`` never executes."""
    html = markup()
    tags = re.findall(r"<script\b([^>]*)>", html, flags=re.IGNORECASE)

    assert tags, "index.html loads no script at all"
    for attrs in tags:
        assert "src=" in attrs, f"inline <script{attrs}> cannot run under this CSP"
        assert "type=" not in attrs, "type=module is CORS-checked on file:// and fails"


# ---------------------------------------------------------------------------
# Everything the page asks for is on disk
# ---------------------------------------------------------------------------
def referenced_assets() -> list[str]:
    html = markup()
    return re.findall(r'<script\s+src="([^"]+)"', html) + re.findall(
        r'<link\s+rel="stylesheet"\s+href="([^"]+)"', html
    )


def test_the_page_references_at_least_the_files_p4_promised() -> None:
    """A guard on the guard: if the reference list itself shrank, the existence
    test below would pass by having nothing to check."""
    refs = referenced_assets()

    assert "js/app.js" in refs
    assert "css/tokens.css" in refs
    # Seven views on the rail, plus the shadow-copy manager, which is reached from the
    # Clean view's vss_manage row and never from a nav item (app.js EXTRA).
    assert len([r for r in refs if r.startswith("js/views/")]) == 8
    assert len([r for r in refs if r.startswith("js/locales/")]) == 2


@pytest.mark.parametrize("ref", referenced_assets())
def test_every_referenced_asset_exists(ref: str) -> None:
    """A missing ``<script src>`` on ``file://`` is a console line nobody sees and
    a view that is simply absent from the app."""
    assert (UI / ref).is_file(), f"index.html references missing {ref}"


def test_the_stylesheet_order_is_the_cascade_order() -> None:
    """tokens, then layout, then components, then views.

    Nothing in this UI uses ``!important``, so an override is resolved by source
    order at equal specificity -- which makes the ``<link>`` order load-bearing, not
    cosmetic. Reordering these two lines is how ``.ov__stats`` silently loses to
    ``.card__body`` and the overview grid collapses to one column.
    """
    sheets = [s for s in referenced_assets() if s.endswith(".css")]

    assert sheets == [
        "css/tokens.css",
        "css/layout.css",
        "css/components.css",
        "css/views.css",
    ], f"stylesheets out of cascade order: {sheets}"


def test_the_script_order_is_the_dependency_order() -> None:
    """locales before i18n, i18n/bridge/ui before the views, app.js last.

    Every file is a classic script with top-level registration, so load order *is*
    the dependency graph. app.js is the only one with side effects and it reads
    ADC.views, so it cannot run before the views have registered.
    """
    scripts = [s for s in referenced_assets() if s.endswith(".js")]
    at = scripts.index

    assert at("js/locales/vi.js") < at("js/i18n.js")
    assert at("js/locales/en.js") < at("js/i18n.js")
    assert at("js/i18n.js") < at("js/ui.js")
    assert at("js/bridge.js") < at("js/ui.js")
    assert at("js/ui.js") < min(at(s) for s in scripts if s.startswith("js/views/"))
    assert scripts[-1] == "js/app.js"


# ---------------------------------------------------------------------------
# The icon sprite
# ---------------------------------------------------------------------------
def sprite_symbols() -> set[str]:
    return set(re.findall(r'<symbol\s+id="([^"]+)"', markup()))


def icon_references() -> set[str]:
    """Every ``#icon-x`` the page or the JS asks for.

    Two forms, because there are two ways in: ``<use href="#icon-x">`` in the
    markup, and a bare ``'icon-x'`` string handed to ``ui.icon`` / ``ui.btn`` /
    ``ui.emptyState`` from a view.
    """
    found = set(re.findall(r'<use\s+href="#([^"]+)"', markup()))
    for path in js_files():
        found |= set(re.findall(r"['\"](icon-[a-z0-9-]+)['\"]", strip_js_comments(read(path))))
    return found


def test_the_sprite_defines_every_icon_that_is_asked_for() -> None:
    """A ``<use>`` naming an id that does not exist renders nothing at all -- no
    error, no fallback, just a hole where the icon was."""
    missing = sorted(icon_references() - sprite_symbols())

    assert missing == [], f"referenced but not in the sprite: {missing}"


def test_the_sprite_carries_no_dead_symbols() -> None:
    """The other direction: 25 symbols is ~4 KB of markup parsed at every launch,
    and an unused one is usually the survivor of a rename."""
    dead = sorted(sprite_symbols() - icon_references())

    assert dead == [], f"defined but never used: {dead}"


def test_every_risk_tier_has_its_own_shape_not_just_its_own_colour() -> None:
    """SPEC 7.3: colour is the third channel, never the only one. The four tiers
    therefore need four distinguishable symbols, and the same one twice would make
    two tiers identical to a colour-blind reader."""
    symbols = sprite_symbols()

    for tier in ("safe", "rebuildable", "caution", "dangerous"):
        assert f"icon-{tier}" in symbols

    bodies = {
        tier: re.search(
            rf'<symbol\s+id="icon-{tier}".*?</symbol>', markup(), flags=re.DOTALL
        )
        for tier in ("safe", "rebuildable", "caution", "dangerous")
    }
    shapes = {tier: m.group(0) for tier, m in bodies.items() if m}

    assert len(shapes) == 4
    assert len(set(shapes.values())) == 4, "two risk tiers draw the same shape"


# ---------------------------------------------------------------------------
# Contrast, computed rather than claimed
# ---------------------------------------------------------------------------
def hex_tokens() -> dict[str, str]:
    content = read(CSS / "tokens.css")
    root_match = re.search(r":root\s*\{([^}]+)\}", content)
    block = root_match.group(1) if root_match else content
    return dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", block))


def as_rgb(value: str) -> tuple[int, int, int]:
    return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))


def luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 2.1 relative luminance."""
    def channel(raw: int) -> float:
        v = raw / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = luminance(as_rgb(a)), luminance(as_rgb(b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# 4.5:1 is WCAG AA for normal text. --primary is listed at the 3:1 large-text floor
# and is documented as fills-and-headings only, which is why --primary-text exists.
FLOORS = {
    "text-main": 4.5,
    "text-muted": 4.5,
    "text-dim": 4.5,
    "primary-text": 4.5,
    "danger-text": 4.5,
    "secondary": 4.5,
    "success": 4.5,
    "warning": 4.5,
    "danger": 4.5,
    "primary": 3.0,
}


@pytest.mark.parametrize("token,floor", sorted(FLOORS.items()))
def test_every_foreground_token_clears_its_contrast_floor(token: str, floor: float) -> None:
    """P4's contrast acceptance row, as arithmetic instead of a screenshot."""
    tokens = hex_tokens()

    assert token in tokens, f"--{token} is gone from tokens.css"
    ratio = contrast(tokens[token], tokens["bg-color"])

    assert ratio >= floor, f"--{token} {tokens[token]} is {ratio:.2f}:1, needs {floor}:1"


def test_the_dimmest_permitted_text_is_still_readable() -> None:
    """--text-dim is the floor of the palette by definition, so if anything is
    going to slip under 4.5:1 it is this one. Named separately from the sweep above
    so a failure says which rule was broken."""
    tokens = hex_tokens()
    assert contrast(tokens["text-dim"], tokens["bg-color"]) >= 4.5


def test_dark_theme_tokens_clear_contrast_floor() -> None:
    """Verify dark mode tokens when present also clear their contrast floors."""
    content = read(CSS / "tokens.css")
    dark_match = re.search(r'\[data-theme="dark"\]\s*\{([^}]+)\}', content)
    assert dark_match is not None, "no [data-theme='dark'] in tokens.css"
    dark_tokens = dict(re.findall(r"--([a-z0-9-]+):\s*(#[0-9a-fA-F]{6})", dark_match.group(1)))
    bg = dark_tokens.get("bg-color", "#0b0f19")
    for token, floor in FLOORS.items():
        if token in dark_tokens:
            ratio = contrast(dark_tokens[token], bg)
            assert ratio >= floor, f"Dark theme --{token} is {ratio:.2f}:1, needs {floor}:1"


# ---------------------------------------------------------------------------
# opacity, and the other things that make a UI unmeasurable
# ---------------------------------------------------------------------------
OPACITY_RULE = re.compile(r"opacity\s*:\s*([0-9.]+)")


def keyframe_free(text: str) -> str:
    """Everything outside an ``@keyframes`` block.

    A fade animation is allowed to pass through 0.4 on its way from 0 to 1 -- it
    does not leave text dimmed. A static declaration does, and that is what the
    check below is for. The brace counter is enough because these files are
    hand-written and never minified.
    """
    out: list[str] = []
    i = 0
    for match in re.finditer(r"@keyframes[^{]*\{", text):
        out.append(text[i:match.start()])
        depth = 1
        j = match.end()
        while j < len(text) and depth:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
            j += 1
        i = j
    out.append(text[i:])
    return "".join(out)


@pytest.mark.parametrize("path", css_files(), ids=lambda p: p.name)
def test_no_stylesheet_dims_anything_with_a_static_opacity(path: Path) -> None:
    """The mechanical form of tokens.css's own rule.

    ``opacity: .6`` on a panel composites against whatever is behind it, so the
    resulting text colour is not knowable from the stylesheet and the contrast
    sweep above cannot see it. Use --text-muted or --text-dim. 0 and 1 are fine:
    they are visibility, not dimming.
    """
    body = keyframe_free(strip_comments(read(path)))
    offenders = [v for v in OPACITY_RULE.findall(body) if 0.0 < float(v) < 1.0]

    assert offenders == [], f"{path.name} dims with opacity: {offenders}"


@pytest.mark.parametrize("path", js_files(), ids=lambda p: p.name)
def test_no_script_writes_html_or_an_inline_style_attribute(path: Path) -> None:
    """Two habits this UI cannot afford.

    ``innerHTML`` with a folder name in it is an injection in a page that has the
    bridge attached; every component in ui.js writes textContent instead.
    ``setAttribute('style', ...)`` is blocked outright by the CSP while the CSSOM
    form (``node.style.width = ...``) is allowed, so the two look interchangeable
    and are not.
    """
    body = strip_js_comments(read(path))

    assert "innerHTML" not in body, f"{path.name} writes innerHTML"
    assert "outerHTML" not in body, f"{path.name} writes outerHTML"
    assert not re.search(r"""setAttribute\(\s*['"]style['"]""", body), (
        f"{path.name} sets a style attribute; assign to node.style.* instead"
    )
    assert not re.search(r"\.style\.opacity\s*=", body), (
        f"{path.name} dims with opacity; use a class and a colour token"
    )


@pytest.mark.parametrize("path", js_files(), ids=lambda p: p.name)
def test_no_script_can_reach_the_network(path: Path) -> None:
    """``connect-src 'none'`` means a fetch rejects with a TypeError at runtime.

    Which is a promise worth keeping on this side of the wall too: a call that can
    only ever fail is dead code that reads like a feature.
    """
    body = strip_js_comments(read(path))

    assert "XMLHttpRequest" not in body
    assert not re.search(r"\bfetch\s*\(", body), f"{path.name} calls fetch"
    assert not re.search(r"\bnew\s+WebSocket\b", body)
    assert not re.search(r"\bnew\s+EventSource\b", body)


@pytest.mark.parametrize("path", js_files(), ids=lambda p: p.name)
def test_no_script_uses_a_native_dialog(path: Path) -> None:
    """``window.confirm`` blocks the WebView2 message loop and cannot be styled,
    translated, or made to state the figure the user is approving. ui.confirm
    exists precisely so the destructive path can show real numbers."""
    body = strip_js_comments(read(path))

    for native in ("window.confirm(", "window.alert(", "window.prompt("):
        assert native not in body, f"{path.name} calls {native}"


# ---------------------------------------------------------------------------
# adc.ico
# ---------------------------------------------------------------------------
def test_the_window_icon_is_a_real_multi_size_ico() -> None:
    """``icon_path`` hands this file to the Win32 loader, which will simply show
    the default icon rather than complain if the container is malformed.

    16 px is the taskbar and the title bar; 256 px is what Explorer's large view
    and the installer use. Both have to be in there or Windows upscales the wrong
    one and the result looks like a blurry mistake.
    """
    ico = UI / "adc.ico"

    assert ico.is_file(), "src/adc/ui/adc.ico is missing"
    raw = ico.read_bytes()
    reserved, kind, count = struct.unpack("<HHH", raw[:6])

    assert (reserved, kind) == (0, 1)
    assert count >= 2

    sizes = set()
    for i in range(count):
        entry = struct.unpack("<BBBBHHII", raw[6 + 16 * i : 22 + 16 * i])
        width, _height, _colours, _pad, _planes, bits, length, offset = entry
        blob = raw[offset : offset + length]

        assert len(blob) == length, "an ICO entry points past the end of the file"
        assert bits == 32, "the mark has an alpha channel; 32bpp or it gets a black box"
        assert blob[:8] == b"\x89PNG\r\n\x1a\n", "PNG-in-ICO, per the generator"
        declared_w, declared_h = struct.unpack(">II", blob[16:24])

        assert declared_w == declared_h, "the icon is square"
        assert declared_w == (width or 256), "the directory entry and the PNG disagree"
        sizes.add(declared_w)

    assert {16, 256} <= sizes, f"needs 16 and 256; has {sorted(sizes)}"


# ---------------------------------------------------------------------------
# The dictionaries
# ---------------------------------------------------------------------------
# i18n.t() answers a missing key with the key itself (js/i18n.js:61-71), which is
# unmistakable in the window and completely silent in a test run -- nothing raises,
# nothing logs, the label just reads `history.empty.title`. Everything below exists
# to turn that into a failure here instead.
LOCALES = JS / "locales"

# Key-shaped string literals that are not dictionary keys. Empty, and expected to
# stay that way: script_keys() recognises a key by its shape rather than by which
# function it was passed to (see its docstring), so anything lowercase-dotted in a
# script is treated as a key. If a future literal collides with that shape -- a
# settings path, a dotted DOM id -- naming it here is the fix, not loosening the
# shape, because loosening the shape stops the check finding real keys.
NOT_A_KEY: frozenset[str] = frozenset()

# Every member each runtime-built family has to define, and where the list comes from.
# Two different kinds of source, and the difference matters: the engine enums are the
# full set of values that can arrive over the bridge, while the view allow-lists are
# narrower on purpose -- history.js:116 and overview.js:446 fold anything they do not
# recognise into 'unknown' rather than printing an engine token, so 'kind.' needs three
# entries and not the JobKind enum's four.
REQUIRED_FAMILY: dict[str, tuple[str, ...]] = {
    # targets.py Category
    "cat.": ("ai_ml", "app", "browser", "dev", "ide", "system", "temp", "vm"),
    # history.js:116 KIND / overview.js:446, plus the fold-to-unknown arm. 'explore' is
    # jobs.py JobKind's fourth value and reaches the dictionary through explorer.js:224 --
    # the busy toast names the job that is already running, and an explore is one of them.
    "kind.": ("scan", "clean", "explore", "unknown"),
    # jobs.py Level
    "log.level.": ("info", "warn", "error", "success"),
    # history.js:106 STATE covers all five jobs.py JobState values; overview.js:437
    # covers the three terminal ones. Both fold the rest into 'unknown'.
    "state.": ("pending", "running", "done", "failed", "cancelled", "unknown"),
    # settings.py PRESETS, plus the sentinel settings.js:297 adds for a hand-edited set
    "settings.preset.": ("safe", "deep", "custom"),
    # the same two presets as a filter label, plus clean.js:484's no-preset arm
    "clean.preset.": ("safe", "deep", "none"),
    # app.js:201-202 builds both 'nav.<view>' and 'nav.<view>.sub' for every route, which
    # is ORDER + EXTRA: the seven rail views and 'vss', which has a title and a subtitle
    # like the rest even though nothing in the rail links to it.
    "nav.": (
        "overview",
        "clean",
        "explorer",
        "projects",
        "schedule",
        "history",
        "settings",
        "vss",
        "overview.sub",
        "clean.sub",
        "explorer.sub",
        "projects.sub",
        "schedule.sub",
        "history.sub",
        "settings.sub",
        "vss.sub",
    ),
}

# One entry, one line: 'key': 'value',  -- double-quoted values accepted so an
# English apostrophe does not have to be escaped.
ENTRY = re.compile(
    r"""^[ \t]*'([^']+)'[ \t]*:[ \t]*(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")[ \t]*,?[ \t]*$"""
)

PLACEHOLDER = re.compile(r"\{(\w+)\}")

# What a dictionary key looks like: lowercase, dotted, at least two segments.
# This is a filter on the extractor, not a rule about the dictionary -- ui.js:176
# builds 'aria-label:' + o.label + ',title:' + o.label, and a regex looking for
# `label: '...'` finds `label:' + o.label + '` in the middle of that string and
# would otherwise report " + o.label + " as a missing key. The rule *about* the
# dictionary is test_every_key_is_named_the_way_the_extractor_expects, which stops
# this filter from hiding a real key that does not match.
KEY_SHAPE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+\Z")
PREFIX_SHAPE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*\.\Z")

# The dictionaries are written after the views (the views are what decide the key
# list). Until they land, every test in this section would raise FileNotFoundError
# and bury the one failure that matters -- which is
# test_every_referenced_asset_exists[js/locales/vi.js], already red above. So they
# skip instead, and the skip cannot hide anything that test does not already catch.
dictionaries_exist = pytest.mark.skipif(
    not (LOCALES / "vi.js").is_file() or not (LOCALES / "en.js").is_file(),
    reason="js/locales/*.js not written yet; the asset-existence test covers that",
)


def locale_body(lang: str) -> list[str]:
    """The lines between ``locales.<lang> = {`` and its closing brace.

    Sliced by text rather than parsed, because the point of the flatness test below
    is that this file *can* be read this way -- from Python, with no node on the
    box and no JSON to parse. ``dict()[key]`` in i18n.js is a flat lookup, so a
    nested object here would not be a different style, it would be unreachable.
    """
    text = read(LOCALES / f"{lang}.js")
    start = text.index("locales." + lang)
    open_brace = text.index("{", start)
    return text[open_brace + 1 : text.index("\n};", open_brace)].splitlines()


def locale(lang: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in locale_body(lang):
        match = ENTRY.match(line)
        if match:
            out[match.group(1)] = match.group(2) if match.group(2) is not None else match.group(3)
    return out


def duplicate_keys(lang: str) -> list[str]:
    seen: list[str] = []
    for line in locale_body(lang):
        match = ENTRY.match(line)
        if match:
            seen.append(match.group(1))
    return sorted({k for k in seen if seen.count(k) > 1})


def markup_keys() -> set[str]:
    """``data-i18n="k"`` plus each ``attr:key`` pair in ``data-i18n-attr``."""
    html = markup()
    keys = set(re.findall(r'data-i18n="([^"]+)"', html))
    for value in re.findall(r'data-i18n-attr="([^"]+)"', html):
        for pair in value.split(","):
            _, _, key = pair.partition(":")
            keys.add(key.strip())
    return keys


def script_keys() -> tuple[set[str], set[str], set[str]]:
    """Every key the JS asks for, split three ways.

    Returns ``(plain, counted, prefixes)``. ``counted`` is what ``tn()`` was called
    with -- satisfiable by the bare key *or* by ``.one``/``.other`` (i18n.js:76-89),
    so it cannot be checked with the same rule as the rest. ``prefixes`` is the
    concatenated form, ``i18n.t('kind.' + busy)``, where the key is only known at
    runtime; all that can be asserted from here is that the family exists.

    ``plain`` is taken from *every* key-shaped string literal in the file, not from
    ``t(...)`` and ``KEY_OPTIONS`` call sites. Matching call sites was the first
    attempt and it under-reported by 48 keys: the views also pass keys positionally
    (``sectionCard('settings.card.safety', ...)``), through a fallback
    (``i18n: o.titleKey || 'log.title'``), out of a lookup table (``RISK`` in
    ui.js:30-35), inside an array of bullets (the two P5 placeholders), and via a
    ternary into a variable (clean.js:422). Enumerating those forms is a losing
    game; recognising the *shape* of a key is not.

    The cost is that a key-shaped literal which is not a dictionary key would be
    reported as missing. ``NOT_A_KEY`` is where such a thing goes, and it is empty:
    across all 11 scripts every literal matching ``KEY_SHAPE`` is in fact a key. The
    two dictionaries are not among those 11 -- see the skip in the loop.
    """
    plain: set[str] = set()
    counted: set[str] = set()
    prefixes: set[str] = set()
    # Anchored on the '+' rather than on the caller, because both `t('cat.' + k)`
    # and `i18n: 'cat.' + k` are the same statement about the dictionary.
    concat = re.compile(r"'([a-z][a-z0-9_.]*\.)'\s*\+")
    call = re.compile(r"(?<![A-Za-z0-9_$])tn\(\s*'([^']+)'")

    for path in js_files():
        if path.parent == LOCALES:
            # A dictionary is not a caller. Every entry in it is a key-shaped literal,
            # so scanning these two files would put all 313 keys into `plain` -- which
            # changes nothing for the missing-key direction (a key cannot be missing
            # from the file that defines it) and quietly disables the other one:
            # test_the_dictionaries_carry_no_dead_keys would find every key reachable
            # through its own definition and could never fail. That is how the seven
            # explorer.soon.* keys outlived the placeholder view they belonged to.
            continue
        text = read(path)
        body = strip_js_comments(text)
        for key in concat.findall(body):
            if PREFIX_SHAPE.match(key):
                prefixes.add(key)
        for key in call.findall(body):
            if KEY_SHAPE.match(key):
                counted.add(key)
        for literal in js_strings(text):
            if KEY_SHAPE.match(literal):
                plain.add(literal)
            elif ":" in literal:
                # An 'attr:key' pair, the i18nAttr / data-i18n-attr form. The whole
                # literal cannot match KEY_SHAPE -- 'aria-label:log.label' has a dash
                # and a colon -- so the key has to be taken off the right of the colon.
                for pair in literal.split(","):
                    _, sep, key = pair.partition(":")
                    if sep and KEY_SHAPE.match(key.strip()):
                        plain.add(key.strip())
    plain -= NOT_A_KEY
    # A key reached only through its family is accounted for by `prefixes`; listing
    # it in `plain` as well would demand the exact runtime string.
    plain = {k for k in plain if not any(k.startswith(p) for p in prefixes)}
    return plain - counted, counted, prefixes


def satisfied(key: str, defined: set[str]) -> bool:
    """A ``tn()`` key is present if the base is, or if both plural arms are."""
    return key in defined or {key + ".one", key + ".other"} <= defined


@dictionaries_exist
def test_both_dictionaries_are_flat_because_the_lookup_is_flat() -> None:
    """Every line in the object is one entry, a comment, or blank.

    A nested object would parse as JS and be unreachable through ``dict()[key]``;
    a template literal would parse and then not match ENTRY, so this test is also
    what keeps the regex above honest -- it cannot silently skip a line it failed
    to understand.
    """
    for lang in ("vi", "en"):
        for number, line in enumerate(locale_body(lang), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith(("/*", "*", "//")):
                continue

            assert ENTRY.match(line), f"{lang}.js line {number} is not one flat entry: {line!r}"
            assert "`" not in line, f"{lang}.js line {number} uses a template literal"


@dictionaries_exist
def test_no_key_is_defined_twice_in_one_dictionary() -> None:
    """A duplicate in a JS object literal is not an error; the last one wins and the
    first is simply gone. Two translators editing the same file produce exactly
    this, and the symptom is a string that reverts for no visible reason."""
    for lang in ("vi", "en"):
        assert duplicate_keys(lang) == [], f"{lang}.js defines these twice: {duplicate_keys(lang)}"


@dictionaries_exist
def test_the_two_dictionaries_define_exactly_the_same_keys() -> None:
    """The whole point of the fallback chain is that it should never be used.

    A key present in vi and missing from en renders the *Vietnamese* string to an
    English-speaking user (i18n.js:61-71) -- not a crash, not a placeholder, just
    the wrong language in one label.
    """
    vi, en = set(locale("vi")), set(locale("en"))

    assert sorted(vi - en) == [], "in vi.js, missing from en.js"
    assert sorted(en - vi) == [], "in en.js, missing from vi.js"


@dictionaries_exist
def test_no_entry_is_blank() -> None:
    """An empty string is the one value that looks like a layout bug rather than a
    translation bug, so it gets chased in the CSS for an hour first."""
    for lang in ("vi", "en"):
        blank = sorted(k for k, v in locale(lang).items() if not v.strip())

        assert blank == [], f"{lang}.js has empty values for {blank}"


@dictionaries_exist
def test_every_key_is_named_the_way_the_extractor_expects() -> None:
    """The guard on KEY_SHAPE.

    script_keys() ignores anything that does not look like a dotted lowercase key,
    which is what keeps ui.js:176's concatenated aria-label out of the results. That
    filter would also swallow a genuinely odd key -- so the dictionary is required to
    contain only keys the filter would have kept, and the two cannot drift apart.
    """
    for lang in ("vi", "en"):
        odd = sorted(k for k in locale(lang) if not KEY_SHAPE.match(k))

        assert odd == [], f"{lang}.js keys the reference scan cannot match: {odd}"


@dictionaries_exist
def test_a_placeholder_in_one_language_is_a_placeholder_in_both() -> None:
    """``fill`` substitutes ``{name}`` and leaves an unknown brace verbatim
    (i18n.js:50-55). So a key whose vi text says ``{n} thư mục`` and whose en text
    says ``folders`` drops the number in English, and a key that says ``{count}``
    where the caller passes ``n`` renders the word ``{count}`` on screen."""
    vi, en = locale("vi"), locale("en")

    for key in sorted(set(vi) & set(en)):
        assert set(PLACEHOLDER.findall(vi[key])) == set(PLACEHOLDER.findall(en[key])), (
            f"{key} takes different placeholders in vi and en: "
            f"{vi[key]!r} vs {en[key]!r}"
        )


@dictionaries_exist
def test_every_key_the_markup_asks_for_is_defined() -> None:
    """index.html's own labels: the seven nav items, the admin banner, the language
    switch, and the fatal dialog. These are the strings on screen before any view
    has mounted, so a miss here is the first thing the user reads."""
    missing = sorted(markup_keys() - set(locale("vi")))

    assert missing == [], f"index.html asks for undefined keys: {missing}"


@dictionaries_exist
def test_every_key_the_scripts_ask_for_is_defined() -> None:
    """The other 300-odd. Split by call shape because ``tn`` resolves differently."""
    plain, counted, _ = script_keys()
    vi = set(locale("vi"))

    assert sorted(plain - vi) == [], "t()/ui.* option keys with no entry"
    assert sorted(k for k in counted if not satisfied(k, vi)) == [], (
        "tn() keys with neither a base entry nor a .one/.other pair"
    )


@dictionaries_exist
def test_every_runtime_built_key_has_its_family_defined() -> None:
    """``i18n.t('kind.' + busy)`` and ``'log.level.' + level`` cannot be resolved
    from here, but an empty family can: it means the prefix was renamed on one side
    only, and every label in that family silently becomes its own key."""
    _, _, prefixes = script_keys()
    vi = set(locale("vi"))

    for prefix in sorted(prefixes):
        family = sorted(k for k in vi if k.startswith(prefix))

        assert family, f"nothing in the dictionary starts with {prefix!r}"


@dictionaries_exist
@pytest.mark.parametrize("prefix,members", sorted(REQUIRED_FAMILY.items()))
def test_every_member_of_a_runtime_built_family_is_defined(
    prefix: str, members: tuple[str, ...]
) -> None:
    """The stronger half of the family check.

    A non-empty family passes the test above while still missing the one member the
    engine actually emits -- ``state.pending`` is the easy one to forget, because a
    job is only pending for a few milliseconds and the tag is the last thing anyone
    clicks through to. The member lists come from the engine's own enums and from the
    allow-lists the views gate on, both cited at REQUIRED_FAMILY.
    """
    vi = set(locale("vi"))
    missing = sorted(prefix + m for m in members if prefix + m not in vi)

    assert missing == [], f"{prefix!r} family is incomplete: {missing}"


@dictionaries_exist
def test_the_dictionaries_carry_no_dead_keys() -> None:
    """Reachability, the other way round.

    Prefix-aware, because a member of a runtime-built family is reached by a name
    this file never sees. A key nothing can reach is usually the survivor of a
    rename, and it is the reason a dictionary drifts to twice the size of the UI.
    """
    plain, counted, prefixes = script_keys()
    reachable = plain | markup_keys()
    for key in counted:
        reachable |= {key, key + ".one", key + ".other"}

    dead = sorted(
        key
        for key in locale("vi")
        if key not in reachable and not any(key.startswith(p) for p in prefixes)
    )

    assert dead == [], f"defined but unreachable: {dead}"


@dictionaries_exist
def test_each_dictionary_declares_the_language_it_is_for() -> None:
    """vi.js writing into ``locales.en`` would load, overwrite, and leave the app
    monolingual with no error anywhere."""
    for lang in ("vi", "en"):
        body = strip_js_comments(read(LOCALES / f"{lang}.js"))

        assert f"locales.{lang}" in body
        other = "en" if lang == "vi" else "vi"
        assert f"locales.{other}" not in body, f"{lang}.js also assigns locales.{other}"


# ---------------------------------------------------------------------------
# The stylesheets and the classes that ask for them
# ---------------------------------------------------------------------------
# The same failure as a missing icon or a missing key, in the third medium: a class
# with no rule behind it is not an error, it is an element with no padding sitting
# somewhere near where it was meant to be. And the reverse -- a rule for a class
# that was renamed -- is dead weight nobody notices, because the page still looks
# right.
CLASS_SHAPE = re.compile(r"[a-z][a-z0-9-]*(?:__[a-z0-9-]+)?(?:--[a-z0-9-]+)?\Z")

# Classes owned by the reset or by a browser feature rather than by a component, so
# they are legitimately defined without any element naming them in a class list.
NOT_A_COMPONENT = frozenset({"num", "mono", "sprite"})

# Every modifier each runtime-built family has to define, and where the value comes
# from. Same reasoning as REQUIRED_FAMILY on the i18n side: "the family is not empty"
# passes while the one modifier the engine actually emits is missing, and a missing
# modifier is invisible -- the element renders, in the neutral variant, so a dangerous
# row looks exactly like a safe one. That is a docs/02-SPEC.md 7.3 violation, not a
# cosmetic gap.
REQUIRED_MODIFIER: dict[str, tuple[str, ...]] = {
    # ui.js:158 o.variant, ui.js:160 o.size, plus the two literal layout modifiers
    "btn--": ("primary", "ghost", "danger", "bare", "sm", "block", "icon"),
    # explorer.js:620-623 tileKind(), which closes the vocabulary to three engine kinds
    # plus mapItems()'s synthetic remainder tile -- and 'open', pushed for a folder the
    # user can drill into. A missing kind rule is a black rectangle: an unstyled <rect>
    # paints black, so this is the one family where the modifier is not cosmetic.
    "dx__tile--": ("dir", "file", "link", "other", "open"),
    # ui.js:414, level is jobs.py Level
    "log__line--": ("info", "warn", "error", "success"),
    # ui.js:242, targets.py Risk plus riskBadge's fold-to-unknown arm and its compact form
    "risk--": ("safe", "rebuildable", "caution", "dangerous", "unknown", "compact"),
    # ui.js:220 o.tone
    "stat--": ("info", "success", "warn", "danger"),
    # ui.js:285 o.tone
    "tag--": ("info", "success", "warn", "danger"),
    # ui.js:549, kind defaults to 'info' and the icon switch names the other three
    "toast--": ("info", "warn", "error", "success"),
    # ui.js:326 o.risk is a targets.py Risk value; ui.js:325 adds the disabled state
    "trow--": ("safe", "rebuildable", "caution", "dangerous", "disabled"),
}

stylesheets_exist = pytest.mark.skipif(
    not all((CSS / name).is_file() for name in ("layout.css", "components.css", "views.css")),
    reason="css/{layout,components,views}.css not written yet; the existence test covers that",
)


def used_classes() -> tuple[set[str], set[str]]:
    """Every class the UI puts on an element, from the sites that actually set one.

    Returns ``(literal, families)``. A family is the prefix of a runtime-built
    modifier -- ``classes.push('btn--' + o.variant)`` yields the family ``btn--``;
    all that can be checked for it is that at least one member exists.

    Errs on the side of *missing* a class rather than inventing one, so it is the
    right input for "is this styled?" and the wrong input for "is this rule dead?".
    ``mentioned_classes`` is the other direction.
    """
    literal: set[str] = set()
    families: set[str] = set()

    def take(text: str) -> None:
        """Pull every quoted class list out of one expression."""
        for value, concat in re.findall(r"'([^']*)'(\s*\+)?", text):
            names = [n for n in value.split() if n]
            if concat and names:
                families.add(names.pop())
            literal.update(names)

    for value in re.findall(r'class="([^"]+)"', markup()):
        literal |= set(value.split())

    for path in js_files():
        body = strip_js_comments(read(path))
        # class: 'a b'   |   class: cond ? 'a b' : 'c'   |   class: 'a b--' + tone
        # Bounded at the comma: none of the three forms contains one, and without
        # the bound the scan runs on into `role: 'group'` and the icon name in the
        # child list on the same line.
        for match in re.finditer(r"\bclass\s*:([^,\n]*)", body):
            take(match.group(1))
        # var classes = ['btn', 'btn--' + variant]  and  classes.push('btn--block')
        for match in re.finditer(r"\bclasses\s*=\s*\[([^\]]*)\]", body):
            take(match.group(1))
        for match in re.finditer(r"\bclasses\.push\(([^;\n]*)", body):
            take(match.group(1))
        for value, concat in re.findall(
            r"classList\.(?:add|remove|toggle)\(\s*'([^']+)'(\s*\+)?", body
        ):
            (families if concat else literal).add(value)
        # icon(name, class) -- ui.js:135-139, the second argument is a class list
        for value in re.findall(r"\bicon\(\s*[^,()]+,\s*'([^']+)'", body):
            literal |= set(value.split())

    literal = {c for c in literal if CLASS_SHAPE.match(c)}
    families = {c for c in families if re.fullmatch(r"[a-z][a-z0-9-]*(?:__[a-z0-9-]+)?--", c)}
    return literal - families, families


def mentioned_classes() -> set[str]:
    """Every string anywhere in the JS that *could* be a class list.

    Deliberately generous: it cannot tell ``'card'`` the class from ``'card'`` the
    word, so it over-collects. That is the safe direction for the dead-rule test --
    a rule survives if anything at all still names it, and the thing that test is
    really hunting is the class that was renamed and now appears nowhere.

    Reads the literals through ``js_strings`` rather than a findall, so a
    double-quoted string carrying an apostrophe cannot shift the pairing and drop
    the class names after it -- which would turn "generous" into a false dead rule.
    """
    found, _ = used_classes()
    for path in js_files():
        for value in js_strings(read(path)):
            names = value.split()
            if names and all(CLASS_SHAPE.match(n) for n in names):
                found |= set(names)
    return found


def defined_classes() -> set[str]:
    """Every class a stylesheet writes a rule for, across all of them."""
    found: set[str] = set()
    for path in css_files():
        found |= set(re.findall(r"\.([a-z][a-z0-9_-]*)", strip_comments(read(path))))
    return {c for c in found if CLASS_SHAPE.match(c)}


@stylesheets_exist
def test_every_class_the_ui_puts_on_an_element_has_a_rule() -> None:
    """A class with no rule behind it is silent: the element renders, unstyled, in
    roughly the right place, and looks like a spacing bug rather than a typo."""
    literal, _ = used_classes()
    orphans = sorted(literal - defined_classes())

    assert orphans == [], f"used but never styled: {orphans}"


@stylesheets_exist
def test_every_runtime_built_modifier_has_at_least_one_rule() -> None:
    """``'toast toast--' + kind`` cannot be resolved from here, but an empty family
    can, and it means every toast in the app is rendering with no variant at all."""
    _, families = used_classes()
    defined = defined_classes()

    for family in sorted(families):
        members = sorted(c for c in defined if c.startswith(family))

        assert members, f"no stylesheet defines any {family}* modifier"


@stylesheets_exist
@pytest.mark.parametrize("family,members", sorted(REQUIRED_MODIFIER.items()))
def test_every_modifier_a_component_can_emit_has_a_rule(
    family: str, members: tuple[str, ...]
) -> None:
    """The stronger half. The test above is satisfied by one modifier out of seven;
    this one names the whole vocabulary each component can emit, so the tier that only
    appears on a dangerous target cannot be the one that was never styled."""
    defined = defined_classes()
    missing = sorted(family + m for m in members if family + m not in defined)

    assert missing == [], f"{family}* vocabulary is incomplete: {missing}"


@stylesheets_exist
def test_the_declared_modifier_vocabulary_covers_every_family_in_use() -> None:
    """A guard on the guard above: a new runtime-built family in the JS has to gain a
    REQUIRED_MODIFIER entry, or its members go unchecked while both family tests pass.
    """
    _, families = used_classes()
    undeclared = sorted(families - set(REQUIRED_MODIFIER))

    assert undeclared == [], f"runtime-built families with no declared vocabulary: {undeclared}"


@stylesheets_exist
def test_no_stylesheet_rule_is_dead() -> None:
    """The other direction. A rule for a class that was renamed still parses, still
    costs bytes, and is the reason a stylesheet doubles in size over a refactor.

    Judged against the generous scan, so a class this file cannot prove is a class
    gets the benefit of the doubt; a name that appears nowhere at all does not.
    """
    _, families = used_classes()
    dead = sorted(
        c
        for c in defined_classes() - mentioned_classes() - NOT_A_COMPONENT
        if not any(c.startswith(f) for f in families)
    )

    assert dead == [], f"styled but never used: {dead}"





