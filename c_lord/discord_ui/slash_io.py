"""Reply plumbing for slash commands — ``ephemeral=True`` must mean it (#748).

A slash command that may take longer than Discord's 3-second window
acknowledges first (``ack()`` → ``interaction.response.defer()``), which shows a
"thinking…" placeholder, and answers later through followups.  Discord has a
rule about that placeholder that the cogs did not account for:

    The first followup sent while the deferred placeholder is still pending
    does **not** create a message.  It *edits the placeholder*, and the
    placeholder keeps the visibility the defer gave it — the followup's own
    ``ephemeral`` flag is ignored (Discord API docs, "Create Followup Message").

So after a public ``ack()``, every ``respond(..., ephemeral=True)`` that came
first was posted to the whole thread.  That is how an operator-facing
``pip install c-lord[table]`` hint ended up as the last message of an unrelated
user's work thread (#748).

The fix lives here, once, instead of in each cog's copy of the plumbing (there
were three): when a reply must be private and the pending placeholder is
public, the placeholder is deleted first, so the reply goes out as its own
message with the visibility it asked for.  Everyone else briefly sees
"thinking…" and then nothing — which is what "only for you" means.

Only that direction is handled.  A *private* placeholder followed by a reply
without a flag stays private, as before: ``/tmux-list`` acks ephemerally and
relies on exactly that, and "went private by accident" is the safe way to be
wrong.

The pending-placeholder state is kept on ``interaction.extras`` so that
:func:`followup_ephemeral` gives the same answer outside :func:`slash_io` —
``on_app_command_error`` answers a command that raised after ``ack()``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

import discord

logger = logging.getLogger(__name__)

Responder = Callable[..., Awaitable[None]]
Acknowledger = Callable[..., Awaitable[None]]

# ``interaction.extras`` key: True while a *public* "thinking…" placeholder is
# waiting to be replaced by the first followup.
_PUBLIC_PLACEHOLDER = "clord_public_placeholder_pending"


def _public_placeholder_pending(interaction: discord.Interaction) -> bool:
    # ``is True`` rather than truthiness: a mocked Interaction answers every
    # lookup with a truthy mock, and must read as "nothing pending".
    return interaction.extras.get(_PUBLIC_PLACEHOLDER) is True


async def followup(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    ephemeral: bool = False,
    **kwargs: Any,
) -> None:
    """Send a followup that keeps the visibility it asks for (see module doc)."""
    if ephemeral and _public_placeholder_pending(interaction):
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            # Nothing better is left: the reply below lands in the public
            # placeholder. Say so, since that is the leak this module exists
            # to prevent.
            logger.warning(
                "slash reply: could not delete the public 'thinking…' placeholder, "
                "so this ephemeral reply will be visible to everyone",
                exc_info=True,
            )
    interaction.extras[_PUBLIC_PLACEHOLDER] = False
    await interaction.followup.send(content or "", ephemeral=ephemeral, **kwargs)


async def followup_ephemeral(interaction: discord.Interaction, content: str) -> None:
    """:func:`followup` for a reply that is only for the invoker."""
    await followup(interaction, content, ephemeral=True)


def slash_io(interaction: discord.Interaction) -> tuple[Responder, Acknowledger]:
    """``(respond, ack)`` for a slash command's shared ``_*_impl`` core (#209).

    ``ack(ephemeral=False)`` defers; ``respond(content, *, embed, file,
    ephemeral, silent)`` answers — on the initial response before ``ack()``,
    through :func:`followup` after it.
    """
    state = {"acked": False}

    async def ack(*, ephemeral: bool = False) -> None:
        state["acked"] = True
        interaction.extras[_PUBLIC_PLACEHOLDER] = not ephemeral
        await interaction.response.defer(ephemeral=ephemeral)

    async def respond(
        content: str | None = None,
        *,
        embed: discord.Embed | None = None,
        file: discord.File | None = None,
        ephemeral: bool = False,
        silent: bool = False,
    ) -> None:
        extra: dict[str, Any] = {}
        if embed is not None:
            extra["embed"] = embed
        if file is not None:
            extra["file"] = file
        if silent:
            extra["silent"] = True
        if state["acked"]:
            await followup(interaction, content, ephemeral=ephemeral, **extra)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral, **extra)

    return respond, ack
