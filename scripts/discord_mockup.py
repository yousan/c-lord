#!/usr/bin/env python3
"""Dev tool (#316): render a hand-authored Discord conversation spec → PNG mockup.

Use this at Issue/spec time to show "**this is the goal**" as a picture — a
design comp / wireframe of how a feature should look in Discord — and attach the
PNG to the Issue. Because it draws from data you author by hand, it can mock up
states that don't exist yet (a real screenshot can't).

This is a **c-lord-internal dev tool**, not a shipped consumer feature.

Usage:
    uv run python scripts/discord_mockup.py scripts/examples/mockup_spec.json -o mockup.png
    uv run python scripts/discord_mockup.py spec.json -o out.png --title "#feature-name"

Spec format: a JSON list of message dicts (or ``{"messages": [...]}``) — see
``c_lord.discord_ui.conversation_renderer.conversation_from_spec`` and
``docs/design-comp-mockup.md``. Requires the optional ``[table]`` extra:
``uv sync --extra table``.
"""

from __future__ import annotations

import argparse
import sys

from c_lord.discord_ui.conversation_renderer import load_spec_file, render_conversation_png


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a Discord design-comp mockup PNG from a hand-authored spec (#316)."
    )
    parser.add_argument("spec", help="Path to a JSON conversation spec")
    parser.add_argument(
        "-o", "--out", default="mockup.png", help="Output PNG path (default: mockup.png)"
    )
    parser.add_argument("--title", default=None, help="Optional header title, e.g. '#feature-name'")
    args = parser.parse_args(argv)

    messages = load_spec_file(args.spec)
    png = render_conversation_png(messages, title=args.title)
    if png is None:
        print(
            "error: rendering unavailable — install the optional extra: "
            "uv sync --extra table  (Pillow + a usable font)",
            file=sys.stderr,
        )
        return 1

    with open(args.out, "wb") as fh:
        fh.write(png)
    print(f"wrote {args.out} ({len(png)} bytes, {len(messages)} message(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
