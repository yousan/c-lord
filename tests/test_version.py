"""Tests for c_lord.version — pure version helpers + release logic.

These are pure-logic functions (90%+ coverage target per CLAUDE.md).
The article format being implemented is ``v1.4.0-b599631-20251203`` —
semver tag + short commit + commit date (https://qiita.com/yousan/items/cffa19f67f225097127d).
"""

from __future__ import annotations

import pytest

from c_lord.version import (
    bump_version,
    detect_bump_level,
    extract_changelog_section,
    format_version_string,
    parse_local_version,
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
