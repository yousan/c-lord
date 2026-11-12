# <<REDACTED 12 lines>>
COGS_DIR = Path(__file__).parent.parent / "c_lord" / "cogs"
# <<REDACTED 64 lines>>


class TestSingleDeliveryPath:
    """Issue #712: the JSONL transcript mirror is the ONLY delivery path.

    The skill-push bridge (#53) was removed, not merely defaulted off, because a
    legacy opt-in kept the "Claude forgot to post and the turn vanished" failure
    mode (#491) one env line away — and its gate also kept the REST API control
    plane from starting in the default configuration (#543).

    So no module may branch on the removed switches again. ``legacy_env.py`` is
    the single exemption: warning about a var requires naming it.
    """

    _FORBIDDEN = ("CLORD_BRIDGE_MODE", "USE_SKILL_REPLY", "skills_enabled", "bridge_mode_jsonl")
    _EXEMPT = {"legacy_env.py"}

    def test_no_bridge_mode_branching_in_package(self) -> None:
        pkg_dir = Path(__file__).parent.parent / "c_lord"
        violations = []
        for py_file in sorted(pkg_dir.rglob("*.py")):
            if py_file.name in self._EXEMPT:
                continue
            for line_no, line in enumerate(py_file.read_text().splitlines(), start=1):
                for token in self._FORBIDDEN:
                    if token in line:
                        rel = py_file.relative_to(pkg_dir.parent)
                        violations.append(f"  {rel}:{line_no}: {line.strip()}")

        if violations:
            pytest.fail(
                "Removed delivery-path switches are referenced again (#712).\n"
                "jsonl is the only bridge; do not reintroduce a mode gate:\n"
                + "\n".join(violations)
            )
