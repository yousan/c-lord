"""Builders for the embeds c-lord posts — pure functions, no Discord calls."""

from __future__ import annotations

from dataclasses import dataclass

import discord

from ..claude.types import AskOption, UsageLimit

# Palette. One meaning per colour, so a glance at the stripe tells the kind.
COLOR_INFO = 0x5865F2  # neutral notice (Discord blurple)
COLOR_SUCCESS = 0x57F287  # it worked (green)
COLOR_ERROR = 0xED4245  # it failed (red)
COLOR_TOOL = 0xFEE75C  # yellow
COLOR_ASK = 0x3498DB  # Claude is asking something (blue)
COLOR_TODO = 0xE67E22  # orange

# Context usage, in percent, from which auto-compaction is considered imminent.
AUTOCOMPACT_THRESHOLD = 83.5

# Discord's ceilings on an embed's title and description.
_TITLE_LIMIT = 256
_DESCRIPTION_LIMIT = 4096
# How much of a session id the start embed shows.
_SESSION_ID_PREFIX = 8
# How much of a raw error text the generic error embed carries.
_ERROR_TEXT_LIMIT = 4000
# A stop is something the user asked for, so it is orange, not COLOR_ERROR.
_COLOR_STOPPED = 0xFFA500


def _notice(title: str, body: str, color: int) -> discord.Embed:
    """A title-and-text embed, both cut to what Discord accepts."""
    return discord.Embed(
        title=title[:_TITLE_LIMIT], description=body[:_DESCRIPTION_LIMIT], color=color
    )


def session_start_embed(
    session_id: str | None = None,
    *,
    window_name: str | None = None,
) -> discord.Embed:
    """The "session started" notice; the footer names the session when known."""
    embed = discord.Embed(title="🤖 Claude Code session started", color=COLOR_INFO)
    if session_id:
        if session_id.startswith("tmux-"):
            label = window_name or "tmux"
            embed.set_footer(text=f"Session: {label}")
        else:
            embed.set_footer(text=f"Session: {session_id[:_SESSION_ID_PREFIX]}...")
    return embed


def unknown_tui_prompt_embed(pane_snapshot: str) -> discord.Embed:
    """Create a warning embed for an unknown TUI interactive prompt.

    Posted when the TUI shows a selection menu that c-lord doesn't recognise,
    so the session would otherwise stall silently. Includes the raw pane
    snapshot to help identify the new prompt type for cataloging.
    """
    truncated = pane_snapshot[-800:] if len(pane_snapshot) > 800 else pane_snapshot
    return discord.Embed(
        title="⚠️ Unknown TUI prompt detected — session may be waiting for input",
        description=(
            "Claude Code is showing an interactive menu that c-lord doesn't handle yet.\n\n"
            "**What to do:**\n"
            "• Use `/attach` (or tmux) to respond manually\n"
            "• Consider filing a GitHub issue to add support for this prompt\n\n"
            f"**Pane snapshot:**\n```\n{truncated}\n```"
        ),
        color=0xF1C40F,  # Yellow-warning
    )


def error_embed(error: str) -> discord.Embed:
    """A generic failure: the error text itself, cut to what an embed can hold."""
    return _notice("❌ Error", error[:_ERROR_TEXT_LIMIT], COLOR_ERROR)


def api_error_embed(detail: str, advice: str) -> discord.Embed:
    """Embed for a turn Claude Code ended on an API error (#908).

    *detail* is the CLI's own first sentence (kept: it is what a reader would
    search for), *advice* the Japanese line saying what can be done from
    Discord. The CLI's keyboard steps ("Double press esc…") are never shown.
    """
    return discord.Embed(
        title="\u274c Claude Code が API エラーで止まりました",
        description=f"{advice}\n\n原文: {detail[:3800]}",
        color=COLOR_ERROR,
    )


def no_response_embed(detail: str) -> discord.Embed:
    """Embed for a turn that never produced anything (#562).

    Distinct from :func:`timeout_embed`: a timeout means Claude was working and
    stopped responding, while this means the turn never began. Saying the wrong
    one sends the reader looking for output that was never started.
    """
    return discord.Embed(
        title="\u26a0\ufe0f 応答がありませんでした",
        description=(
            f"{detail}\n\n"
            "**確認すること:**\n"
            "\u2022 同じ内容をもう一度送る\n"
            "\u2022 `/clord-attach` (または tmux) でペインを直接見る\n"
            "\u2022 ホストが混んでいると Claude の起動が遅れることがあります"
        ),
        color=COLOR_ERROR,
    )


def login_required_embed(detail: str) -> discord.Embed:
    """Embed for a turn Claude Code refused because it is not logged in (#812).

    Deliberately NOT :func:`no_response_embed`: that one says the turn never
    started and advises sending it again. Claude did answer — with "Please run
    /login" — and every resend gets the same answer until someone logs in on
    the host. Login is host-wide, so other threads are likely stopped too.
    """
    return discord.Embed(
        title="\U0001f511 Claude Code のログインが切れています",
        description=(
            f"{detail}\n\n"
            "Claude Code がログインを求めたため、このターンは実行されていません。\n"
            "**ログインするまでは、同じ内容を送り直しても同じ結果になります。**\n\n"
            "**できること:**\n"
            "\u2022 ホストで `claude` を開き `/login` する\n"
            "\u2022 ログインはホスト全体で共通です。他のスレッドも止まっている可能性があります"
        ),
        color=COLOR_ERROR,
    )


def model_unavailable_embed(detail: str) -> discord.Embed:
    """Embed for a turn Claude Code refused because it cannot use the model (#484).

    ``/model`` accepts any well-formed ID and leaves the verdict to the CLI
    (#478).  The CLI's own line says "Run /model", which inside the pane would
    only change that one session — the setting that keeps handing Claude the
    bad model is c-lord's, so this names ``/model``.
    """
    return discord.Embed(
        title="\U0001f9e9 設定されたモデルを Claude Code が使えません",
        description=(
            f"{detail}\n\n"
            "Claude Code がモデルを受け付けなかったため、このターンは実行されていません。\n"
            "**モデルを変えるまでは、同じ内容を送り直しても同じ結果になります。**\n\n"
            "**できること:**\n"
            "\u2022 `/model` で c-lord に設定されているモデルを確認する\n"
            "\u2022 `/model` で使えるモデル（`sonnet` / `opus` など）に戻す\n"
            "\u2022 モデル ID の綴りや、そのモデルがいま提供されているかを確認する"
        ),
        color=COLOR_ERROR,
    )


def trust_stuck_embed(detail: str) -> discord.Embed:
    """Embed for a turn blocked on a folder-trust dialog that would not close (#630).

    Before this existed the same situation was reported as a timeout or as "no
    response — send it again", both of which point the reader away from the one
    place that shows the problem: the pane, where the dialog is still sitting
    open. It is also the outcome of a bug that used to answer that dialog 178
    times in two minutes, so saying plainly that c-lord stopped trying matters.
    """
    return discord.Embed(
        title="\u26a0\ufe0f 信頼ダイアログが閉じられませんでした",
        description=(
            f"{detail}\n\n"
            "作業ディレクトリを信頼するか確認するダイアログ"
            "（`Yes, I trust this folder`）がペインに出たまま閉じません。"
            "同じ承認を送り続けないよう、c-lord はここで中断しました。\n\n"
            "**確認すること:**\n"
            "\u2022 `/clord-attach` (または tmux) でペインを開き、ダイアログを手で承認する\n"
            "\u2022 `claude` が落ちていないか（ペインがシェルに戻っていないか）を見る\n"
            "\u2022 復旧したらもう一度送る"
        ),
        color=COLOR_ERROR,
    )


def trust_start_failed_embed(detail: str) -> discord.Embed:
    """Embed for a trust dialog that was answered but left no ``claude`` (#684).

    Deliberately NOT :func:`trust_stuck_embed`: that one tells the reader a
    dialog is sitting open in the pane, and here it is not — it was answered and
    the process is gone. Sending someone to look for an open dialog that closed
    two minutes ago is the same class of misdirection #630 fixed. The generic
    "possible startup failure or crash" is no better: seven production threads
    ended on it and every reader went looking for a crash that never happened.
    """
    return discord.Embed(
        title="\u26a0\ufe0f 信頼ダイアログの応答に失敗しました",
        description=(
            f"{detail}\n\n"
            "作業ディレクトリを信頼するダイアログ（`Yes, I trust this folder`）に答えたあと、"
            "`claude` が起動していませんでした。起動し直しても同じだったので、"
            "このターンは走っていません。\n\n"
            "**確認すること:**\n"
            "\u2022 `/clord-attach` (または tmux) でペインを開き、ダイアログが残っていないか／"
            "シェルに戻っていないかを見る\n"
            "\u2022 Claude Code の版が上がってダイアログの形が変わっていないか"
            " (`claude --version`)\n"
            "\u2022 復旧したらもう一度送る"
        ),
        color=COLOR_ERROR,
    )


def claude_vanished_embed(detail: str) -> discord.Embed:
    """Embed for a claude that was alive at hand-off and gone by the first poll (#716).

    Sibling of :func:`trust_start_failed_embed`, and separate from it on purpose.
    There, c-lord pressed the Enter and can name what swallowed the turn. Here it
    only knows that the message was delivered into a running ``claude`` and that
    ``claude`` is no longer running — the trust dialog is the likeliest culprit
    (a message typed onto it selects "No, exit"), but saying so as fact would be
    the guess-as-diagnosis that ``docs/specs/trust-prompt.md`` rules out.

    What the reader needs either way is the same: the turn did not run, and
    re-sending it is the fix — which is exactly what nobody knew for 29 and 71
    minutes on the two production threads that died this way.
    """
    return discord.Embed(
        title="⚠️ Claude が起動直後に居なくなりました",
        description=(
            f"{detail}\n\n"
            "メッセージを渡した時点では `claude` が動いていましたが、"
            "その直後に居なくなっていました。起動し直しても同じだったので、"
            "**このターンは一度も走っていません**。\n\n"
            "スレッドを立てた直後（数秒以内）に2通目を送ったときは、"
            "作業ディレクトリの信頼ダイアログに本文が打ち込まれた可能性があります。\n\n"
            "**確認すること:**\n"
            "• `/clord-attach` (または tmux) でペインを開き、"
            "シェルに戻っていないか／ダイアログが残っていないかを見る\n"
            "• 復旧したらもう一度送る（同じ内容で構いません）"
        ),
        color=COLOR_ERROR,
    )


def fleet_tmux_restart_embed(detail: str) -> discord.Embed:
    """Embed for a turn cut off because the host's tmux server was replaced (#701).

    The only failure embed whose cause lies OUTSIDE the thread reading it. On
    2026-09-08 one work thread ran ``tmux -f /dev/null new-session …`` — which
    isolates nothing, ``-f`` only skips the config file — and the fleet's tmux
    server was replaced under three unrelated threads. All three showed "Claude
    exited without producing a response (possible startup failure or crash)",
    so each bystander was left looking for a crash in its own work; one had
    finished work (+372/-228, tests green) that nearly went unnoticed because
    nobody could tell why the thread had gone quiet.

    So this embed leads with "not your doing", says the work survives (the
    workspace is a git checkout on disk — only the pane died), and gives the one
    action that helps: send it again. It deliberately does NOT send the reader
    to the pane like the crash embeds do — the pane it would open belongs to a
    tmux server that no longer exists.
    """
    return discord.Embed(
        title="⚠️ フリートの tmux が落ちたため中断しました",
        description=(
            f"{detail}\n\n"
            "このスレッドの Claude が動いていた **tmux サーバが、ターンの途中で落ちました**"
            "（別のサーバに入れ替わった場合も含みます）。"
            "同じホストの全スレッドが1つの tmux サーバを共有しているため、"
            "**別の作業がそれを落とすと、動いていたスレッドは巻き添えで一斉に止まります**。\n\n"
            "**このスレッドの操作が原因ではありません。**\n\n"
            "**作業内容は残っています:** ワークスペースのファイル・ブランチ・コミットはディスク上に"
            "そのままです。失われたのは実行中だったペインだけです。\n\n"
            "**できること:**\n"
            "• もう一度送る（新しいペインで再開します）\n"
            "• 直前の作業が途中で切れていないか、`git status` / ブランチを確認する"
        ),
        color=COLOR_ERROR,
    )


def usage_limit_embed(limit: UsageLimit) -> discord.Embed:
    """Embed for a turn Claude refused because the account is rate limited (#631).

    Deliberately different from :func:`no_response_embed`: that one's advice is
    "send it again", which is exactly the advice that cannot work here. The
    reporter of #631 sent the same request three times on that advice. So this
    embed leads with the recovery time and offers only things that can actually
    change the outcome.

    ``resets_at`` is echoed verbatim as Claude Code printed it (already
    localised, e.g. "Aug 29, 4pm (Asia/Tokyo)") rather than re-derived — being
    confidently wrong about the one fact the reader came for is worse than
    quoting.
    """
    when = (
        f"**{limit.resets_at}** に回復します。"
        if limit.resets_at
        else "回復時刻は報告されませんでした。"
    )
    return discord.Embed(
        title="\u23f3 Claude の利用上限に達しました",
        description=(
            f"Claude が **{limit.scope}** に達したため、このターンは実行されていません。\n"
            f"{when}\n\n"
            "**回復するまでは、同じ内容を送り直しても同じ結果になります。**\n\n"
            "**できること:**\n"
            "\u2022 回復時刻まで待つ\n"
            "\u2022 `/model` で上限に余裕のあるモデルに切り替える"
            "（Opus/Sonnet は上限枠が別）\n"
            "\u2022 `/clord-attach` (または tmux) でペインの表示を直接確認する"
        ),
        color=COLOR_ERROR,
    )


_TIMEOUT_ADVICE = (
    "",
    "**What to do:**",
    "• Send a message to resume the session",
    "• Use `/clear` to start fresh",
)


def timeout_embed(seconds: int) -> discord.Embed:
    """Claude said nothing for *seconds* seconds; tell the user how to go on."""
    lines = (f"No response received for {seconds} seconds.", *_TIMEOUT_ADVICE)
    return _notice("⏱️ Session timed out", "\n".join(lines), COLOR_ERROR)


def ask_embed(
    question: str,
    header: str = "",
    options: list[AskOption] | None = None,
    multi_select: bool = False,
) -> discord.Embed:
    """Create an embed for an AskUserQuestion interactive prompt.

    When *options* render as buttons (≤4 options, single-select), a legend of
    ``**label** — description`` lines is appended to the body: Discord buttons
    cannot show option descriptions, so without this a viewer sees only opaque
    labels (#169).  The Select-menu case (≥5 options) shows descriptions in the
    dropdown itself, so no legend is added there.
    """
    title = f"❓ {header}" if header else "❓ Claude needs your input"
    body = question
    if options and not multi_select and len(options) <= 4:
        legend = "\n".join(f"**{o.label}** — {o.description}" for o in options if o.description)
        if legend:
            body = f"{question}\n\n{legend}"
    return _notice(title, body, COLOR_ASK)


def ask_answered_embed(
    question: str,
    header: str = "",
    selected: list[str] | None = None,
) -> discord.Embed:
    """Embed shown in place of a menu once the answer reached Claude (#536).

    Keeps the question: the old replacement was a single grey
    ``-# ✅ Selected: X`` line with the embed wiped, so scrolling back showed an
    answer with nothing to attach it to. Reading the thread later has to work.
    """
    answer = ", ".join(selected or []) or "（未選択）"
    title = f"✅ {header}" if header else "✅ 回答しました"
    return _notice(title, f"{question}\n\n**あなたの回答:** {answer}", COLOR_SUCCESS)


def ask_sending_embed(
    question: str,
    header: str = "",
    selected: list[str] | None = None,
) -> discord.Embed:
    """Interim state: the answer was taken, the outcome is not known yet (#651).

    The buttons are already gone at this point, so something has to stand in
    their place — and it must not be ✅. ✅ used to be printed the instant the
    click was accepted, which is one step too early: the keystrokes had not been
    sent, let alone accepted by the menu, and on 2026-09-01 a menu resolved with
    the answer thrown away under a ✅ (#650). This says what is actually true —
    "we sent it, we are checking" — for the second or so it takes to confirm.
    """
    answer = ", ".join(selected or []) or "（未選択）"
    title = f"⏳ {header}" if header else "⏳ 回答を送信中"
    body = f"{question}\n\n**送信中:** {answer}\n-# Claude に届いたか確認しています…"
    return discord.Embed(title=title[:256], description=body[:4096], color=COLOR_ASK)


def ask_confirming_embed(
    question: str,
    header: str = "",
    selected: list[str] | None = None,
) -> discord.Embed:
    """The keys went out; Claude's transcript has not recorded the result yet (#746).

    Not a failure and not a verdict. When one AskUserQuestion carries several
    questions the CLI writes its ``tool_result`` only once the LAST one is
    answered — production measured 198s and 53min — so an earlier answer cannot
    be confirmed until then. The menu is corrected to ✅ when the result lands.

    It must never suggest re-sending: a re-sent answer is an ordinary message,
    and an ordinary message interrupts the running turn (#631's shape).
    """
    answer = ", ".join(selected or []) or _NO_ANSWER
    title = f"⏳ {header}" if header else _CONFIRMING_TITLE
    body = (
        f"{question}{_CONFIRMING_ANSWER}{answer}\n"
        f"{_CONFIRMING_NOTE}確認でき次第、この表示が更新されます"
        "（まとめて聞かれた質問は、最後の質問に答えた時点で確認されます）。"
    )
    return discord.Embed(title=title[:256], description=body[:4096], color=COLOR_ASK)


_NO_ANSWER = "（未選択）"
_CONFIRMING_TITLE = "⏳ 回答の受け取りを確認中"
_CONFIRMING_ANSWER = "\n\n**送った答え:** "
_CONFIRMING_NOTE = "-# Claude の受け取りを確認中です。"


@dataclass(frozen=True)
class ConfirmingMenu:
    """What a ⏳ 確認中 menu says, read back off Discord (#786)."""

    question: str
    header: str
    selected: list[str]


def parse_confirming_embed(embed: discord.Embed) -> ConfirmingMenu | None:
    """Read :func:`ask_confirming_embed` back, or None for any other embed (#786).

    The menu message is the only record of a watch that a restart killed —
    nothing else is stored — so its wording is what the next process reads.
    A question long enough to be clipped at 4096 characters does not parse.
    """
    title = embed.title or ""
    body = embed.description or ""
    if not title.startswith("⏳ ") or _CONFIRMING_ANSWER not in body:
        return None
    question, _, rest = body.partition(_CONFIRMING_ANSWER)
    answer, sep, note = rest.partition("\n")
    if not sep or not note.startswith(_CONFIRMING_NOTE):
        return None
    header = "" if title == _CONFIRMING_TITLE else title[len("⏳ ") :]
    selected = [] if answer == _NO_ANSWER else answer.split(", ")
    return ConfirmingMenu(question=question, header=header, selected=selected)


def _asked_options(options: list[AskOption] | None) -> str:
    """The choices this menu offered, for an embed that replaces it (#804).

    A menu message is edited in place when it resolves, so whatever the
    replacement leaves out is gone from the thread. That was fine for ✅ (the
    answer is the point) and wrong for every failure: the reader's next job is
    to answer again, and 2026-09-24 left them a question with its four options
    deleted and no way to see what they had been.
    """
    labels = [o.label for o in (options or []) if o.label]
    if not labels:
        return ""
    return "**聞かれた選択肢:** " + " / ".join(labels) + "\n"


def ask_unconfirmed_embed(
    question: str,
    header: str = "",
    selected: list[str] | None = None,
    options: list[AskOption] | None = None,
) -> discord.Embed:
    """The outcome could not be confirmed either way, and c-lord stopped looking (#651).

    Deliberately neither ✅ nor "届きませんでした": silence is not evidence of
    success, and telling someone their answer was lost when it may well have
    landed is its own way of being wrong. Say what is known.

    #746: it used to add 「同じ内容をスレッドにもう一度送ってください」. The answer
    had usually landed — and a re-sent answer is an interrupt of the turn that is
    using it. Point at where the truth shows up instead.
    """
    answer = ", ".join(selected or []) or "（未選択）"
    title = f"❔ {header}" if header else "❔ 回答の結果を確認できませんでした"
    body = (
        f"{question}\n\n"
        f"**送った答え:** {answer}\n"
        f"{_asked_options(options)}"
        "\n"
        "回答は送りましたが、Claude が受け取ったかどうかは確認できませんでした。"
        "Claude の続きの返信に、この答えが反映されているかを見てください。"
    )
    return discord.Embed(title=title[:256], description=body[:4096], color=COLOR_TODO)


def ask_undelivered_embed(
    question: str,
    header: str = "",
    selected: list[str] | None = None,
    reason: str = "",
    options: list[AskOption] | None = None,
) -> discord.Embed:
    """Embed shown when a click could NOT be delivered to Claude (#536).

    Replaces an ephemeral notice that only the clicker saw, that vanished on
    refresh, and that blamed a bot restart no matter the real cause. *reason*
    is the cause in the user's words; the body also says what to do next.
    """
    answer = ", ".join(selected or []) or "（未選択）"
    title = f"⚠️ {header}" if header else "⚠️ 回答が届きませんでした"
    body = (
        f"{question}\n\n"
        f"**選ばれた答え:** {answer}\n"
        f"**届かなかった理由:** {reason}\n"
        f"{_asked_options(options)}"
        "\n"
        "この選択は Claude に伝わっていません。続けるにはスレッドに"
        "メッセージを送ってください。"
    )
    return discord.Embed(title=title[:256], description=body[:4096], color=COLOR_ERROR)


def stopped_embed(*, shells_stopped: int = 0) -> discord.Embed:
    """Create an embed for a manually stopped session.

    ``shells_stopped`` is how many Bash tool commands (foreground or background)
    the stop ended (#878).
    """
    background = (
        f"Also stopped {shells_stopped} running command(s), including background ones.\n\n"
        if shells_stopped
        else ""
    )
    paragraphs = (
        "The session was stopped.\n\n",
        background,
        "The session is preserved — send a message to resume, or use `/clear` to start fresh.",
    )
    return _notice("⏹️ Session stopped", "".join(paragraphs), _COLOR_STOPPED)
