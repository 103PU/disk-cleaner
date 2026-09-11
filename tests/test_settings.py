r"""Settings tests. The module's own claim is that ``load()`` never raises.

A damaged, partial, or hand-edited ``config.json`` is the least trusted input
the engine has, so every field is validated independently and a bad value
degrades to the default rather than raising. The tests below are organised
around that claim and its two supporting rules: exclusions can only narrow
what ``Guard`` refuses (never widen a root), and the write path is atomic so a
reboot mid-save cannot brick the file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from adc.engine import settings as st
from adc.engine.targets import PRESETS


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
def test_defaults_match_the_spec_values() -> None:
    """SPEC 3.2's stated defaults, checked as data rather than assumed."""
    d = st.DEFAULTS

    assert d.language == "vi"
    assert d.preset == "safe"
    assert d.exclusions == ()
    assert d.size_on_disk is True
    assert d.min_age_hours == 0
    assert d.scan_budget_s is None
    assert d.confirm_dangerous is True
    assert d.restore_point_before_dangerous is True
    assert d.volume_ids == ()


def test_defaults_preset_is_a_real_preset() -> None:
    """A default that ``targets.preset()`` would reject is a defect on its own."""
    assert st.DEFAULTS.preset in PRESETS


def test_defaults_is_not_mutated_by_a_round_trip(data_dir: Path) -> None:
    """``DEFAULTS`` is a module-level constant; a careless ``save``/``load`` that
    mutated it in place would corrupt every other caller in the process.
    """
    before = st.DEFAULTS

    st.save(st.Settings(language="en", min_age_hours=48))
    st.load()

    assert st.DEFAULTS is before
    assert st.DEFAULTS.language == "vi"
    assert st.DEFAULTS.min_age_hours == 0


def test_settings_is_frozen() -> None:
    """The docstring's promise: a caller cannot rewrite a field in place."""
    with pytest.raises(AttributeError):
        st.DEFAULTS.language = "en"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Round-trip: save() then load()
# ---------------------------------------------------------------------------
def test_save_then_load_round_trips(data_dir: Path) -> None:
    original = st.Settings(
        language="en",
        preset="deep",
        exclusions=("C:\\Users\\pu\\Documents",),
        size_on_disk=False,
        min_age_hours=72,
        scan_budget_s=120.0,
        confirm_dangerous=False,
        restore_point_before_dangerous=False,
        volume_ids=("C", "E"),
    )

    assert st.save(original) is True
    assert st.load() == original


def test_round_trip_survives_non_ascii_exclusions(data_dir: Path) -> None:
    """Vietnamese path segments must not become escaped garbage or vanish."""
    original = st.Settings(exclusions=(r"C:\Người dùng\Tệp tin",))

    st.save(original)
    reloaded = st.load()

    assert reloaded.exclusions == original.exclusions
    raw = (data_dir / "config.json").read_text(encoding="utf-8")
    assert "Người dùng" in raw, "ensure_ascii=False must not have been dropped"


def test_round_trip_survives_windows_backslash_paths(data_dir: Path) -> None:
    original = st.Settings(exclusions=(r"C:\Program Files\Some App\Cache",))

    st.save(original)

    assert st.load().exclusions == original.exclusions


def test_round_trip_preserves_a_none_scan_budget(data_dir: Path) -> None:
    st.save(st.Settings(scan_budget_s=None))

    assert st.load().scan_budget_s is None


def test_load_of_a_fresh_profile_is_defaults(data_dir: Path) -> None:
    """No file has been written yet -- first run, not an error."""
    assert not (data_dir / "config.json").exists()
    assert st.load() == st.DEFAULTS


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------
def test_save_leaves_no_leftover_temp_file(data_dir: Path) -> None:
    assert st.save(st.Settings(language="en")) is True

    names = [p.name for p in data_dir.iterdir()]
    assert names == ["config.json"]
    assert not any(name.endswith(".tmp") for name in names)


def test_save_creates_the_roaming_directory(data_dir: Path) -> None:
    """``ensure(roaming_dir())`` must run before the write, not be assumed."""
    assert not data_dir.exists()

    assert st.save(st.Settings()) is True

    assert data_dir.is_dir()
    assert (data_dir / "config.json").is_file()


def test_save_returns_false_when_the_profile_is_not_writable(data_dir: Path) -> None:
    """A file sitting where the directory needs to be: ``ensure`` cannot mkdir over it.

    ``save`` must report ``False`` rather than raise, and it must clean up its
    own temp file rather than leaving a half-written ``.config.json.tmp``
    beside the blocker.
    """
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    data_dir.write_text("i am a file, not a directory", encoding="utf-8")

    ok = st.save(st.Settings(language="en"))

    assert ok is False


def test_save_writes_the_current_schema_version(data_dir: Path) -> None:
    st.save(st.Settings())

    raw = json.loads((data_dir / "config.json").read_text(encoding="utf-8"))
    assert raw["schema_version"] == st.SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Corrupt / partial input -- load() never raises
# ---------------------------------------------------------------------------
def test_load_of_invalid_json_is_defaults(data_dir: Path) -> None:
    data_dir.mkdir(parents=True)
    (data_dir / "config.json").write_text("{not valid json", encoding="utf-8")

    assert st.load() == st.DEFAULTS


def test_load_of_an_empty_file_is_defaults(data_dir: Path) -> None:
    data_dir.mkdir(parents=True)
    (data_dir / "config.json").touch()

    assert st.load() == st.DEFAULTS


def test_load_of_a_json_array_is_defaults(data_dir: Path) -> None:
    """The file is meant to hold one object; an array is not that shape."""
    data_dir.mkdir(parents=True)
    (data_dir / "config.json").write_text("[1, 2, 3]", encoding="utf-8")

    assert st.load() == st.DEFAULTS


def test_from_dict_of_a_non_mapping_is_defaults() -> None:
    for raw in ([1, 2, 3], "just a string", 42, None, True):
        assert st.Settings.from_dict(raw) == st.DEFAULTS


def test_from_dict_ignores_unknown_extra_keys() -> None:
    """A newer config talking to an older engine must not fail on an extra key."""
    result = st.Settings.from_dict({"language": "en", "some_future_field": {"x": 1}})

    assert result.language == "en"
    assert result == st.Settings(language="en")


def test_from_dict_coerces_a_wrong_typed_field_per_field() -> None:
    """One bad key costs one field, not the whole object."""
    result = st.Settings.from_dict(
        {
            "language": "en",
            "preset": 123,
            "size_on_disk": "not a bool",
            "min_age_hours": "not a number",
            "confirm_dangerous": [],
        }
    )

    assert result.language == "en"
    assert result.preset == st.DEFAULTS.preset
    assert result.size_on_disk == st.DEFAULTS.size_on_disk
    assert result.min_age_hours == st.DEFAULTS.min_age_hours
    assert result.confirm_dangerous == st.DEFAULTS.confirm_dangerous


def test_load_survives_a_file_with_a_wrong_typed_field(data_dir: Path) -> None:
    data_dir.mkdir(parents=True)
    (data_dir / "config.json").write_text(
        json.dumps({"schema_version": 1, "min_age_hours": "lots"}), encoding="utf-8"
    )

    result = st.load()

    assert result.min_age_hours == st.DEFAULTS.min_age_hours


# ---------------------------------------------------------------------------
# Numeric / bounded fields
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (-1, 0),
        (-1_000_000, 0),
        (0, 0),
        (24, 24),
        (st.MAX_MIN_AGE_HOURS, st.MAX_MIN_AGE_HOURS),
        (st.MAX_MIN_AGE_HOURS + 1, st.MAX_MIN_AGE_HOURS),
        (10**9, st.MAX_MIN_AGE_HOURS),
        ("48", 48),
        ("not a number", 0),
        (None, 0),
        (True, 0),  # bool is not accepted even though it is an int subclass
        ([1, 2], 0),
    ],
)
def test_min_age_hours_is_clamped(raw: object, expected: int) -> None:
    assert st.Settings.from_dict({"min_age_hours": raw}).min_age_hours == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        (0, None),  # zero means "no budget"
        (-5, None),
        (30.0, 30.0),
        (st.MAX_SCAN_BUDGET_S, st.MAX_SCAN_BUDGET_S),
        (st.MAX_SCAN_BUDGET_S * 10, st.MAX_SCAN_BUDGET_S),
        ("120", 120.0),
        ("not a number", None),
        (True, None),
        ([1], None),
    ],
)
def test_scan_budget_s_is_clamped(raw: object, expected: float | None) -> None:
    assert st.Settings.from_dict({"scan_budget_s": raw}).scan_budget_s == expected


def test_min_age_hours_infinity_does_not_raise_out_of_load() -> None:
    """``int(inf)`` raises ``OverflowError``, which ``_as_int`` now catches.

    Regression guard: before settings.py:64 listed ``OverflowError``, this escaped
    ``from_dict`` and broke the module's "load() has no failure mode" rule.
    """
    result = st.Settings.from_dict({"min_age_hours": float("inf")})

    assert result.min_age_hours == st.DEFAULTS.min_age_hours


def test_min_age_hours_nan_does_not_raise_out_of_load() -> None:
    """``nan`` takes a different path than ``inf``: ``int(nan)`` raises
    ``ValueError`` where ``int(inf)`` raises ``OverflowError``. Kept beside the
    ``inf`` case (settings.py:60-67) because the two non-finite floats reach the
    fallback through different exceptions, and only one of them was ever caught.
    """
    result = st.Settings.from_dict({"min_age_hours": float("nan")})

    assert result.min_age_hours == st.DEFAULTS.min_age_hours


def test_load_of_infinity_in_the_file_does_not_raise(data_dir: Path) -> None:
    """``json.load`` accepts the non-standard ``Infinity``/``NaN`` tokens, so a
    hand-edited or corrupted file can carry one straight into ``from_dict``.

    This is the end-to-end half of the ``inf`` case above: the path a real user
    reaches is a file on disk, not a dict literal.
    """
    data_dir.mkdir(parents=True)
    (data_dir / "config.json").write_text('{"min_age_hours": Infinity}', encoding="utf-8")

    result = st.load()

    assert result.min_age_hours == st.DEFAULTS.min_age_hours


@pytest.mark.parametrize("raw", ["vi", "en"])
def test_language_accepts_every_declared_language(raw: str) -> None:
    assert st.Settings.from_dict({"language": raw}).language == raw


def test_language_rejects_anything_not_declared() -> None:
    assert st.Settings.from_dict({"language": "fr"}).language == st.DEFAULTS.language
    assert st.Settings.from_dict({"language": "VI"}).language == st.DEFAULTS.language


def test_preset_rejects_a_name_not_in_the_catalogue() -> None:
    assert st.Settings.from_dict({"preset": "nonexistent"}).preset == st.DEFAULTS.preset


@pytest.mark.parametrize("name", list(PRESETS))
def test_preset_accepts_every_real_preset_name(name: str) -> None:
    assert st.Settings.from_dict({"preset": name}).preset == name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, True),
        (False, False),
        ("true", True),
        ("false", False),
        ("1", True),
        ("0", False),
        ("yes", True),
        ("no", False),
        ("On", True),
        ("Off", False),
        ("maybe", True),  # falls back to whatever the field's default is
        (1, True),  # not a bool, not a recognised string -> fallback
        (None, True),
    ],
)
def test_bool_fields_coerce_familiar_strings(raw: object, expected: bool) -> None:
    assert st.Settings.from_dict({"confirm_dangerous": raw}).confirm_dangerous == expected


# ---------------------------------------------------------------------------
# Exclusions -- normalisation, dedup, bounds (the "only narrows" rule)
# ---------------------------------------------------------------------------
def test_exclusions_drops_relative_paths() -> None:
    """A relative path is dropped, not resolved against ADC's own cwd."""
    result = st.clean_exclusions(["Documents\\Foo", "..\\Escape"])

    assert result == ()


def test_exclusions_drops_wildcards_and_nul() -> None:
    result = st.clean_exclusions(["C:\\Temp\\*", "C:\\Temp\\?", "C:\\Temp\\\0evil"])

    assert result == ()


def test_exclusions_drops_non_string_and_blank_entries() -> None:
    result = st.clean_exclusions([123, None, "", "   ", "\t"])

    assert result == ()


def test_exclusions_drops_entries_over_the_length_cap() -> None:
    too_long = "C:\\" + ("a" * st.MAX_PATH_LEN)

    assert st.clean_exclusions([too_long]) == ()
    just_ok = "C:\\" + ("a" * (st.MAX_PATH_LEN - 3))
    assert st.clean_exclusions([just_ok]) == (os.path.normpath(just_ok),)


def test_exclusions_normalises_slashes_and_dot_segments() -> None:
    result = st.clean_exclusions(["C:/Foo/Bar", "C:\\Foo\\..\\Foo\\Bar"])

    assert result == ("C:\\Foo\\Bar",)


def test_exclusions_dedups_case_insensitively_keeping_first_seen() -> None:
    result = st.clean_exclusions(["C:\\Foo\\Bar", "c:\\foo\\bar", "C:\\FOO\\BAR"])

    assert result == ("C:\\Foo\\Bar",)


def test_exclusions_strips_surrounding_quotes_and_whitespace() -> None:
    result = st.clean_exclusions(['  "C:\\Foo\\Bar"  '])

    assert result == ("C:\\Foo\\Bar",)


def test_exclusions_are_bounded_at_max_exclusions() -> None:
    many = [f"C:\\Dir{i}" for i in range(st.MAX_EXCLUSIONS + 20)]

    result = st.clean_exclusions(many)

    assert len(result) == st.MAX_EXCLUSIONS
    assert result[0] == "C:\\Dir0"
    assert result[-1] == f"C:\\Dir{st.MAX_EXCLUSIONS - 1}"


def test_exclusions_of_a_non_list_is_empty() -> None:
    assert st.clean_exclusions("C:\\Foo") == ()
    assert st.clean_exclusions({"C:\\Foo"}) == ()
    assert st.clean_exclusions(None) == ()


def test_exclusions_survive_a_round_trip_unchanged_in_meaning(data_dir: Path) -> None:
    original = st.Settings(exclusions=st.clean_exclusions(["C:/Foo/Bar", "C:\\Baz"]))

    st.save(original)
    reloaded = st.load()

    assert reloaded.exclusions == original.exclusions
    assert reloaded.exclusions == ("C:\\Foo\\Bar", "C:\\Baz")


# ---------------------------------------------------------------------------
# volume_ids
# ---------------------------------------------------------------------------
def test_volume_ids_accepts_bare_letters() -> None:
    result = st.Settings.from_dict({"volume_ids": ["c", "E", " f "]})

    assert result.volume_ids == ("C", "E", "F")


def test_volume_ids_strips_drive_punctuation() -> None:
    result = st.Settings.from_dict({"volume_ids": ["C:", "D:\\", "E:/"]})

    assert result.volume_ids == ("C", "D", "E")


def test_volume_ids_rejects_anything_not_a_single_letter() -> None:
    result = st.Settings.from_dict({"volume_ids": ["CC", "1", "", "*", None, 5]})

    assert result.volume_ids == ()


def test_volume_ids_dedups_case_insensitively() -> None:
    result = st.Settings.from_dict({"volume_ids": ["c", "C", "c:"]})

    assert result.volume_ids == ("C",)


# ---------------------------------------------------------------------------
# patched() -- the settings_set path from the UI
# ---------------------------------------------------------------------------
def test_patched_applies_only_recognised_fields() -> None:
    result = st.DEFAULTS.patched({"language": "en", "made_up_field": "x"})

    assert result.language == "en"
    assert not hasattr(result, "made_up_field")


def test_patched_of_a_non_mapping_returns_self_unchanged() -> None:
    assert st.DEFAULTS.patched("not a mapping") == st.DEFAULTS
    assert st.DEFAULTS.patched(None) == st.DEFAULTS
    assert st.DEFAULTS.patched([1, 2]) == st.DEFAULTS


def test_patched_revalidates_through_from_dict() -> None:
    """A value from the UI deserves no more trust than one off the disk."""
    result = st.DEFAULTS.patched({"preset": "not-a-real-preset", "min_age_hours": -5})

    assert result.preset == st.DEFAULTS.preset
    assert result.min_age_hours == 0


def test_patched_leaves_unmentioned_fields_alone() -> None:
    base = st.Settings(language="en", min_age_hours=48)

    result = base.patched({"preset": "deep"})

    assert result.language == "en"
    assert result.min_age_hours == 48
    assert result.preset == "deep"


def test_patched_does_not_mutate_the_original() -> None:
    base = st.Settings(language="vi")

    base.patched({"language": "en"})

    assert base.language == "vi"


# ---------------------------------------------------------------------------
# update() -- read, merge, write, return
# ---------------------------------------------------------------------------
def test_update_persists_the_merged_result(data_dir: Path) -> None:
    st.save(st.Settings(language="vi", min_age_hours=24))

    result = st.update({"min_age_hours": 96})

    assert result.min_age_hours == 96
    assert result.language == "vi"
    assert st.load() == result


def test_update_from_a_fresh_profile_starts_from_defaults(data_dir: Path) -> None:
    result = st.update({"language": "en"})

    assert result.language == "en"
    assert result == st.Settings(language="en")


# ---------------------------------------------------------------------------
# ADC_DATA_DIR override honoured
# ---------------------------------------------------------------------------
def test_config_file_lives_under_the_data_dir_override(data_dir: Path) -> None:
    from adc.engine.paths import config_file

    st.save(st.Settings())

    assert config_file() == data_dir / "config.json"
    assert config_file().is_file()


def test_save_never_touches_outside_the_override(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt and braces: a real ``APPDATA`` set alongside the override must be
    ignored, so a test run can never write into the real profile.
    """
    monkeypatch.setenv("APPDATA", str(data_dir.parent / "should-not-be-used"))

    st.save(st.Settings(language="en"))

    assert not (data_dir.parent / "should-not-be-used").exists()
    assert (data_dir / "config.json").is_file()
