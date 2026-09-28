"""Tests for c_lord.version — pure version helpers + release logic.

These are pure-logic functions (90%+ coverage target per CLAUDE.md).
The article format being implemented is ``v1.4.0-b599631-20251203`` —
semver tag + short commit + commit date (https://qiita.com/yousan/items/cffa19f67f225097127d).
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from c_lord.version import (
    STALE_BUILD_DAYS,
    _baked_commit_date,
    _installed_version,
    build_date,
    bump_version,
    detect_bump_level,
    extract_changelog_section,
    format_version_string,
    label_with_age,
    parse_local_version,
    runtime_version,
    stale_build_age,
)


class TestFormatVersionString:
    def test_full_article_format(self) -> None:
        assert format_version_string("1.4.0", "599631", "20251203") == "v1.4.0-b599631-20251203"

    def test_base_only(self) -> None:
        assert format_version_string("1.4.0", None, None) == "v1.4.0"

    def test_commit_without_date(self) -> None:
        assert format_version_string("1.4.0", "599631", None) == "v1.4.0-b599631"

    def test_date_without_commit(self) -> None:
        assert format_version_string("1.4.0", None, "20251203") == "v1.4.0-20251203"

    def test_strips_leading_v_from_base(self) -> None:
        assert format_version_string("v1.4.0", None, None) == "v1.4.0"

    def test_empty_strings_treated_as_missing(self) -> None:
        assert format_version_string("1.4.0", "", "") == "v1.4.0"


class TestParseLocalVersion:
    def test_clean_tag(self) -> None:
        assert parse_local_version("1.4.0") == ("1.4.0", None, None)

    def test_dev_with_commit_and_date(self) -> None:
        assert parse_local_version("1.4.1.dev3+g599631.d20251203") == (
            "1.4.1",
            "599631",
            "20251203",
        )

    def test_dev_with_commit_no_date(self) -> None:
        assert parse_local_version("1.4.1.dev3+g599631") == ("1.4.1", "599631", None)

    def test_leading_v_stripped(self) -> None:
        assert parse_local_version("v1.4.0") == ("1.4.0", None, None)

    def test_garbage_returns_raw_base(self) -> None:
        base, commit, date = parse_local_version("not-a-version")
        assert base == "not-a-version"
        assert commit is None
        assert date is None


class TestBumpVersion:
    def test_patch(self) -> None:
        assert bump_version("1.4.0", "patch") == "1.4.1"

    def test_minor_resets_patch(self) -> None:
        assert bump_version("1.4.3", "minor") == "1.5.0"

    def test_major_resets_minor_and_patch(self) -> None:
        assert bump_version("1.4.3", "major") == "2.0.0"

    def test_strips_leading_v(self) -> None:
        assert bump_version("v1.4.0", "patch") == "1.4.1"

    def test_invalid_level_raises(self) -> None:
        with pytest.raises(ValueError):
            bump_version("1.4.0", "nope")  # type: ignore[arg-type]

    def test_invalid_current_raises(self) -> None:
        with pytest.raises(ValueError):
            bump_version("1.4", "patch")


class TestDetectBumpLevel:
    def test_major(self) -> None:
        assert detect_bump_level("feat: huge change [major]") == "major"

    def test_minor(self) -> None:
        assert detect_bump_level("feat: new thing [minor]") == "minor"

    def test_release_alias_is_minor(self) -> None:
        assert detect_bump_level("release: v1.5.0 [release]") == "minor"

    def test_default_is_patch(self) -> None:
        assert detect_bump_level("fix: small bug") == "patch"

    def test_major_takes_precedence_over_minor(self) -> None:
        assert detect_bump_level("[minor] [major]") == "major"

    def test_case_insensitive(self) -> None:
        assert detect_bump_level("Big one [MAJOR]") == "major"


_CHANGELOG = """\
# Changelog

## [Unreleased]

## [1.4.0] - 2026-02-22

### Added
- Feature A
- Feature B

## [1.3.0] - 2026-02-20

### Added
- Old feature
"""


class TestExtractChangelogSection:
    def test_extracts_named_section(self) -> None:
        section = extract_changelog_section(_CHANGELOG, "1.4.0")
        assert section is not None
        assert "Feature A" in section
        assert "Feature B" in section
        assert "Old feature" not in section
        assert "Unreleased" not in section

    def test_accepts_v_prefix(self) -> None:
        section = extract_changelog_section(_CHANGELOG, "v1.4.0")
        assert section is not None
        assert "Feature A" in section

    def test_last_section_runs_to_end(self) -> None:
        section = extract_changelog_section(_CHANGELOG, "1.3.0")
        assert section is not None
        assert "Old feature" in section
        assert "Feature A" not in section

    def test_missing_version_returns_none(self) -> None:
        assert extract_changelog_section(_CHANGELOG, "9.9.9") is None

    def test_does_not_include_heading_line(self) -> None:
        section = extract_changelog_section(_CHANGELOG, "1.4.0")
        assert section is not None
        assert not section.startswith("## [")


class TestRuntimeVersion:
    """#722: the version the *running process* reports.

    ``resolve_version()`` reads the checkout on disk. The running process
    loaded its code at import time, so once the bot is up, disk and process
    can disagree (``git pull`` without a restart). Pinning at first call is
    therefore a correctness property, not an optimisation.
    """

    def test_resolved_once_per_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        def fake_resolve() -> str:
            calls["n"] += 1
            return f"v1.2.3-bdeadbee-2026090{calls['n']}"

        monkeypatch.setattr("c_lord.version.resolve_version", fake_resolve)
        runtime_version.cache_clear()
        try:
            first = runtime_version()
            second = runtime_version()
        finally:
            runtime_version.cache_clear()

        assert calls["n"] == 1, "version must be pinned at boot, not re-read per call"
        assert first == second == "v1.2.3-bdeadbee-20260901"

    def test_resolver_failure_degrades_to_unknown(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A banner must never be able to take the bot down."""

        def boom() -> str:
            raise RuntimeError("no git here")

        monkeypatch.setattr("c_lord.version.resolve_version", boom)
        runtime_version.cache_clear()
        try:
            assert runtime_version() == "unknown"
        finally:
            runtime_version.cache_clear()


class TestBuildAge:
    """#756: a build says how old it is once that age is worth saying.

    Judged from the local version string and today's date alone (AC3 — no
    network). The ``-YYYYMMDD`` tail is the commit date of the running build.
    """

    TODAY = date(2026, 9, 23)

    def test_reads_the_date_tail(self) -> None:
        assert build_date("v1.4.197-b3f06814-20260915") == date(2026, 9, 15)

    def test_date_without_commit(self) -> None:
        assert build_date("v1.4.197-20260915") == date(2026, 9, 15)

    @pytest.mark.parametrize(
        "version",
        ["unknown", "", "v1.4.197", "v1.4.197-b3f06814", "v1.4.197-b3f06814-20261399"],
    )
    def test_no_usable_date_is_none(self, version: str) -> None:
        assert build_date(version) is None

    def test_threshold_is_seven_days(self) -> None:
        assert STALE_BUILD_DAYS == 7

    def test_six_days_is_not_stale(self) -> None:
        assert stale_build_age("v1.4.197-b3f06814-20260917", today=self.TODAY) is None

    def test_seven_days_is_stale(self) -> None:
        assert stale_build_age("v1.4.197-b3f06814-20260916", today=self.TODAY) == 7

    def test_reports_the_real_age(self) -> None:
        assert stale_build_age("v1.4.183-bd80c47e-20260906", today=self.TODAY) == 17

    @pytest.mark.parametrize("version", ["unknown", "v1.4.197", "v1.4.197-b3f06814"])
    def test_undatable_build_says_nothing(self, version: str) -> None:
        """AC4: no date → no age, never ``None days``."""
        assert stale_build_age(version, today=self.TODAY) is None

    def test_future_date_is_not_stale(self) -> None:
        """A skewed clock must not turn into a negative or bogus age."""
        assert stale_build_age("v1.4.197-b3f06814-20261001", today=self.TODAY) is None

    def test_defaults_to_today(self) -> None:
        old = (date.today() - timedelta(days=30)).strftime("%Y%m%d")
        assert stale_build_age(f"v1.4.0-babcdef0-{old}") == 30


class TestLabelWithAge:
    """#756 AC2: the footer label gains ``(Nd)`` only once the build is stale."""

    TODAY = date(2026, 9, 23)

    def test_stale_build_gets_age(self) -> None:
        assert (
            label_with_age("v1.4.183-bd80c47e-20260913", today=self.TODAY)
            == "v1.4.183-bd80c47e-20260913 (10d)"
        )

    def test_fresh_build_is_unchanged(self) -> None:
        assert (
            label_with_age("v1.4.197-b3f06814-20260917", today=self.TODAY)
            == "v1.4.197-b3f06814-20260917"
        )

    def test_undatable_build_is_unchanged(self) -> None:
        assert label_with_age("v1.4.197", today=self.TODAY) == "v1.4.197"


class TestInstalledVersion:
    """#756: an installed wheel must carry its commit date too.

    hatch-vcs only puts a date in the local version of a *dirty* build, so a
    clean ``uv tool install git+…`` reported ``v1.4.197`` — no date, so the age
    check above could never fire for exactly the instances that fall behind.
    The build hook (``hatch_build.py``) bakes the date into ``_build_info``.
    """

    def test_tagged_wheel_gets_the_baked_date(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("c_lord.version._distribution_version", lambda: "1.4.197")
        monkeypatch.setattr("c_lord.version._baked_commit_date", lambda: "20260915")
        assert _installed_version() == "v1.4.197-20260915"

    def test_dev_wheel_keeps_its_commit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "c_lord.version._distribution_version", lambda: "1.4.198.dev1+g3f06814"
        )
        monkeypatch.setattr("c_lord.version._baked_commit_date", lambda: "20260915")
        assert _installed_version() == "v1.4.198-b3f06814-20260915"

    def test_date_in_the_local_version_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "c_lord.version._distribution_version", lambda: "1.4.198.dev1+g3f06814.d20260920"
        )
        monkeypatch.setattr("c_lord.version._baked_commit_date", lambda: "20260915")
        assert _installed_version() == "v1.4.198-b3f06814-20260920"

    def test_no_baked_date_leaves_the_version_undated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("c_lord.version._distribution_version", lambda: "1.4.197")
        monkeypatch.setattr("c_lord.version._baked_commit_date", lambda: None)
        assert _installed_version() == "v1.4.197"

    def test_not_installed_is_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("c_lord.version._distribution_version", lambda: None)
        assert _installed_version() is None

    def test_baked_date_is_validated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys
        import types

        mod = types.ModuleType("c_lord._build_info")
        mod.COMMIT_DATE = "not-a-date"  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "c_lord._build_info", mod)
        assert _baked_commit_date() is None
        mod.COMMIT_DATE = "20260915"  # type: ignore[attr-defined]
        assert _baked_commit_date() == "20260915"
