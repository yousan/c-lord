"""Notices for the workspace lifecycle — Issue #571, design agreed in #540.

**A notice is an inventory, not a label.** Every one of them says what stopped
*and what is still there*. The second half is the point: sleep and stop fire
automatically, so the reader did not ask for this and their real question is
"did I just lose something?". Answering it in the message itself is cheaper than
any amount of documentation, and it is the same place the reader is already
looking.

It also carries the word. Measured on the production tree, 「ワークスペース」
appeared **twice** in Japanese user-facing strings (both about attachment
storage, unrelated), while 「スレッド」 appeared 34 times and yousan's own
messages used スレッド 17× more often than ワークスペース. A word nobody has been
taught cannot be understood, whatever it is — so every title bridges from the
word people do know: 「このスレッドのワークスペース」.

Manual and automatic notices are built here by the same call. ``reason`` may
change **only the explanatory line** — the inventory is identical either way.
Two functions that produce "the same" message always drift eventually; #538 was
exactly that failure (the side that announced a behaviour and the side that
implemented it disagreed), so the structure prevents it rather than a review.
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING

import discord

from .discord_ui.embeds import COLOR_ERROR, COLOR_INFO

if TYPE_CHECKING:
    from .devenv import DevContainer

#: Amber — a stop is not an error, but it is not business as usual either.
_COLOR_STOP = 0xFEE75C
#: Grey — sleep is meant to be unremarkable.
_COLOR_SLEEP = 0x4E5058

#: Shown for the noun the reader already knows, so the new one can lean on it.
_SUBJECT = "このスレッドのワークスペース"

_KEPT = "✅ そのまま"


def _transcript_value() -> str:
    """The 会話履歴 row, including how long it actually lasts (#607).

    A bare 「そのまま」 is true the moment it is written — the workspace is
    stopped at day 7 and the transcript is still there — but it promises
    something about the future that c-lord does not control: Claude Code deletes
    transcripts itself after ``cleanupPeriodDays`` (30 by default, measured).
    Saying the period turns an over-promise into a fact the reader can act on,
    and points at the setting that changes it.
    """
    from .retention import claude_transcript_retention_days

    return f"✅ そのまま（Claude Code の保持期間 {claude_transcript_retention_days()} 日まで）"


class WorkspaceAction(Enum):
    """The three lifecycle operations. Each one contains the one above it."""

    SLEEP = "sleep"
    STOP = "stop"
    DELETE = "delete"


class DockerOutcome(Enum):
    """What the caller actually did to the dev environment.

    Deliberately not derived from the action. #571 wires this notice into
    ``/close-workspace`` before #574 teaches that command to stop containers, so
    an action-derived row would announce "停止（ポート解放）" while the containers
    kept running and kept their ports. A notice that lies is worse than none, and
    an enum the caller must pass cannot drift into prose.
    """

    NONE = "none"
    """No dev environment found."""

    LEFT_RUNNING = "left_running"
    """Containers were found and deliberately not touched."""

    STOPPED = "stopped"
    """Containers were stopped; their host ports are free again."""

    REMOVED = "removed"
    """Containers were removed entirely."""


class WorkspaceReason(Enum):
    """Who triggered it. Changes the wording, never the effect."""

    MANUAL = "manual"
    IDLE = "idle"

    #: #576: the resident-workspace cap evicted this one, longest-idle first.
    #: A separate reason because a capped eviction is **not** an idle timeout —
    #: it can take a workspace used ten minutes ago, and saying 「4時間 操作が
    #: 無かったため」 about that is simply false.
    CAP = "cap"

    #: #576: the emergency brake fired because the host was nearly out of
    #: memory. Neither "you were idle" nor "we hit the cap"; say what happened.
    PRESSURE = "pressure"


_TITLES = {
    WorkspaceAction.SLEEP: f"💤 {_SUBJECT}をスリープしました",
    WorkspaceAction.STOP: f"🛑 {_SUBJECT}を停止しました",
    WorkspaceAction.DELETE: f"🗑️ {_SUBJECT}を削除しました",
}

_COLORS = {
    WorkspaceAction.SLEEP: _COLOR_SLEEP,
    WorkspaceAction.STOP: _COLOR_STOP,
    WorkspaceAction.DELETE: COLOR_ERROR,
}

#: What to tell the reader they can do next, per action.
_NEXT_STEP = {
    WorkspaceAction.SLEEP: "このスレッドに投稿すれば、そのまま続きから再開します。",
    WorkspaceAction.STOP: (
        "▶️ 再開するには `/workspace-start`。このスレッドに投稿しても再開できます。"
    ),
    WorkspaceAction.DELETE: ("▶️ このスレッドに投稿すれば、リポジトリを取り直して再開します。"),
}

#: The explanatory line for a cap eviction. ``idle_label`` carries the limit
#: ("30本"), not a span of time.
_CAP_LINE = (
    "常駐ワークスペースが上限（{span}）に達したため、"
    "いちばん長く使われていないものからスリープしました。"
)

#: The explanatory line for the emergency brake.
_PRESSURE_LINE = (
    "ホストのメモリが逼迫したため、いちばん長く使われていないものからスリープしました。"
)

_IDLE_PREFIX = {
    WorkspaceAction.SLEEP: "操作が無かったためスリープしました。",
    WorkspaceAction.STOP: "操作が無かったため自動で停止しました。",
    WorkspaceAction.DELETE: (
        "操作が無かったため自動で削除しました。（未コミットの変更が無いことを確認済み）"
    ),
}


def _running_ports(containers: list[DevContainer]) -> list[int]:
    """Host ports still held. Only running containers hold one."""
    ports: set[int] = set()
    for c in containers:
        if c.running:
            ports.update(c.ports)
    return sorted(ports)


def _all_ports(containers: list[DevContainer]) -> list[int]:
    """Every host port these containers bind, running or not."""
    return sorted({p for c in containers for p in c.ports})


def _docker_value(outcome: DockerOutcome, containers: list[DevContainer]) -> str:
    if outcome is DockerOutcome.NONE or not containers:
        return "— （なし）"
    if outcome is DockerOutcome.LEFT_RUNNING:
        return "▶️ 動いたまま"
    ports = _all_ports(containers)
    freed = f"（:{' :'.join(str(p) for p in ports)} を解放）" if ports else ""
    if outcome is DockerOutcome.REMOVED:
        return f"🗑️ 削除{freed}"
    return f"⏹ 停止{freed}"


def _workdir_value(action: WorkspaceAction, freed_mb: int | None) -> str:
    if action is not WorkspaceAction.DELETE:
        return _KEPT
    return "🗑️ 削除" + (f"（{freed_mb} MB 解放）" if freed_mb else "")


def workspace_notice_embed(
    action: WorkspaceAction,
    *,
    reason: WorkspaceReason,
    idle_label: str | None = None,
    containers: list[DevContainer] | None = None,
    docker: DockerOutcome | None = None,
    freed_mb: int | None = None,
) -> discord.Embed:
    """Build the notice for *action*.

    ``idle_label`` is the human span that elapsed ("4時間" / "7日間" / "90日間")
    for :attr:`WorkspaceReason.IDLE`, and the configured limit ("30本") for
    :attr:`WorkspaceReason.CAP`. It is unread for the other reasons.

    ``containers`` is what :mod:`c_lord.devenv` discovered. An empty list is not
    the same as "docker is irrelevant" — it is reported as なし so the inventory
    is never silently short a row.
    """
    containers = containers or []
    if docker is None:
        # Default to what each action is *specified* to do, so callers that have
        # nothing surprising to report stay terse. Callers that deviate — like
        # /close-workspace before #574 — must say so explicitly.
        docker = (
            DockerOutcome.NONE
            if not containers
            else DockerOutcome.LEFT_RUNNING
            if action is WorkspaceAction.SLEEP
            else DockerOutcome.REMOVED
            if action is WorkspaceAction.DELETE
            else DockerOutcome.STOPPED
        )

    lines: list[str] = []
    if reason is WorkspaceReason.IDLE:
        span = idle_label or "しばらく"
        lines.append(f"💤 **{span} {_IDLE_PREFIX[action]}**")
    elif reason is WorkspaceReason.CAP:
        lines.append("💤 **" + _CAP_LINE.format(span=idle_label or "設定値") + "**")
    elif reason is WorkspaceReason.PRESSURE:
        lines.append(f"💤 **{_PRESSURE_LINE}**")
    lines.append(_NEXT_STEP[action])

    embed = discord.Embed(
        title=_TITLES[action],
        description="\n".join(lines),
        color=_COLORS[action],
    )

    # The inventory. Order is deliberate: what stopped first, what survived
    # after, so the reassuring half is what the eye lands on last.
    embed.add_field(name="Claude", value="⏹ 停止", inline=True)
    embed.add_field(name="開発環境 (docker)", value=_docker_value(docker, containers), inline=True)
    embed.add_field(name="作業フォルダ", value=_workdir_value(action, freed_mb), inline=True)
    embed.add_field(name="会話履歴", value=_transcript_value(), inline=True)
    embed.add_field(name="DBのデータ (volume)", value=_KEPT, inline=True)

    # Leaving docker up means the host ports stay taken. That is the one thing
    # the reader cannot work out for themselves, and the one thing that will
    # bite them (a port collision) next time they start an environment. Keyed on
    # the outcome, not the action: /close-workspace also leaves them running
    # until #574, and the warning matters just as much there.
    if docker is DockerOutcome.LEFT_RUNNING:
        ports = _running_ports(containers)
        if ports:
            detail = " / ".join(
                f"`{c.name}` :{p}" for c in containers if c.running for p in c.ports
            )
            embed.add_field(
                name="⚠️ 掴んだままのポート",
                value=detail or ", ".join(str(p) for p in ports),
                inline=False,
            )

    return embed


def workspace_notice_color(action: WorkspaceAction) -> int:
    """Colour used for *action* — exposed so callers can match surrounding UI."""
    return _COLORS.get(action, COLOR_INFO)
