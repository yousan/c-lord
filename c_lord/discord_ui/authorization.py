"""Button-level authorization for Discord UI Views (#466).

c-lord restricts who may *drive* a session — ``on_message`` and slash commands
go through ``ClaudeChatCog._is_allowed`` (``allowed_user_ids`` /
``allowed_role_name``).  Interactive buttons, however, run their callback for
anyone who can click them: ``discord.ui.View.interaction_check`` returns ``True``
by default.  Because c-lord threads are *public*, any server member who can see
the channel could Approve a plan, Allow a tool permission, or Stop a session —
bypassing the allowlist entirely.

This module supplies:

* :class:`Authorizer` — the allowlist predicate, extracted from
  ``ClaudeChatCog._is_allowed`` so the same rule applies to messages and
  buttons (DRY).
* :class:`AuthorizedViewMixin` — an ``interaction_check`` that consults the
  View's ``_authorizer``.  When no authorizer is set (zero-config / no
  allowlist) it allows everyone, preserving backward-compatible behavior.

Usage: list the mixin *before* ``discord.ui.View`` in the bases and store the
authorizer on ``self._authorizer`` in ``__init__``::

    class PlanApprovalView(AuthorizedViewMixin, ErrorReportingViewMixin, discord.ui.View):
        def __init__(self, runner, request_id, authorizer=None):
            super().__init__(...)
            self._authorizer = authorizer
"""

from __future__ import annotations

import contextlib
import logging

import discord

logger = logging.getLogger(__name__)

# Shown (ephemeral) to a user who clicks a button they are not allowed to use.
UNAUTHORIZED_MESSAGE = "⛔ あなたはこの bot を操作する権限がありません。"


class Authorizer:
    """Decides whether a user may interact with the bot.

    Mirrors ``ClaudeChatCog._is_allowed`` exactly so message-trigger gating and
    button gating share one rule:

    * ``allowed_user_ids`` matches → allowed.
    * else if ``allowed_role_name`` is configured → allowed iff the member has
      that role (a plain :class:`discord.User`, e.g. a DM, never matches).
    * else (no role configured) → allowed iff ``allowed_user_ids`` is ``None``
      (i.e. no allowlist at all means everyone is allowed — zero-config).
    """

    def __init__(
        self,
        allowed_user_ids: set[int] | None = None,
        allowed_role_name: str | None = None,
    ) -> None:
        self.allowed_user_ids = allowed_user_ids
        self.allowed_role_name = allowed_role_name

    def is_allowed(self, user: discord.Member | discord.User) -> bool:
        if self.allowed_user_ids is not None and user.id in self.allowed_user_ids:
            return True
        if self.allowed_role_name is not None:
            if isinstance(user, discord.Member):
                return any(r.name == self.allowed_role_name for r in user.roles)
            return False  # DM — no role info
        return self.allowed_user_ids is None


class AuthorizedViewMixin:
    """Mixin adding allowlist enforcement to a ``discord.ui.View``.

    Reads ``self._authorizer`` (an :class:`Authorizer` or ``None``).  When it is
    ``None`` every interaction is allowed — this keeps the zero-config default
    (no allowlist configured ⇒ everyone may click) and makes the mixin safe to
    add to a View whose construction site has not yet been wired.
    """

    # Class-level default so a View that forgets to set it still behaves
    # (allow-all) rather than raising AttributeError.
    _authorizer: Authorizer | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        authorizer = getattr(self, "_authorizer", None)
        if authorizer is None or authorizer.is_allowed(interaction.user):
            return True
        logger.info(
            "Rejected unauthorized button interaction from user %s on %s",
            getattr(interaction.user, "id", "?"),
            type(self).__name__,
        )
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.send_message(UNAUTHORIZED_MESSAGE, ephemeral=True)
        return False
