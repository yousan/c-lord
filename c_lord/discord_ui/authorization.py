"""Who may drive c-lord — the one allowlist rule (#466, #713).

Talking to c-lord means running a shell on the host that runs it, so "who may
talk to it" is the security boundary of the whole project.  Every gate that
answers that question reads this module: ``on_message`` and slash commands via
:class:`Authorizer`, interactive buttons via :class:`AuthorizedViewMixin`, and
``SkillCommandCog`` / ``ChannelRepoCog`` — which used to carry their own copies
of the predicate — by holding an :class:`Authorizer` of their own.

The rule
--------

1. ``allowed_user_ids`` matches → allowed.
2. else if ``allowed_role_name`` is configured → allowed iff the member has
   that role (a plain :class:`discord.User`, e.g. a DM, has no roles and never
   matches).
3. else if ``allowed_user_ids`` is configured but did not match → denied.
4. else — **nothing configured at all** — allowed iff the user is the Discord
   application's own owner (or a member of its team).

Step 4 is #713.  It used to read "allowed iff no allowlist exists", i.e.
*everyone*, and was documented as the zero-config default.  Someone who follows
the README and starts the bot was therefore handing a shell on their host to
every member of the server, without being told.  Discord already knows who owns
the application, so the owner can be the default without anything being
configured — Zero-Config is kept, fail-open is not.

The owner is not known until the bot has logged in, so it is resolved once at
``on_ready`` (:func:`resolve_fallback_owner_ids`) and kept process-wide: one
process runs one bot (enforced by the #212 / #325 locks) and that bot has one
owner, so every :class:`Authorizer` in the process shares the answer — a cog
wired up by hand, by a consumer, still gets it.  Until it is resolved, nobody
passes: an unknown owner must not widen access.

Opening it back up to everyone stays possible with ``CLORD_ALLOW_ANYONE=1``,
and is announced with a startup warning — fail-open is a choice c-lord lets you
make, never one it lets you make by accident.

Usage (Views): list the mixin *before* ``discord.ui.View`` in the bases and
store the authorizer on ``self._authorizer`` in ``__init__``::

    class PlanApprovalView(AuthorizedViewMixin, ErrorReportingViewMixin, discord.ui.View):
        def __init__(self, runner, request_id, authorizer=None):
            super().__init__(...)
            self._authorizer = authorizer
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any

import discord

logger = logging.getLogger(__name__)

# Shown (ephemeral) to a user who clicks a button they are not allowed to use.
UNAUTHORIZED_MESSAGE = "⛔ あなたはこの bot を操作する権限がありません。"

# Opt back in to the pre-#713 "everyone may use it" default.
ALLOW_ANYONE_ENV = "CLORD_ALLOW_ANYONE"

_TRUE_VALUES = ("1", "true", "yes", "on")

# Resolved once at ``on_ready`` from the Discord application itself.  ``None``
# means "not resolved yet" — which denies, rather than allows, everyone.
_fallback_owner_ids: set[int] | None = None

# ``on_ready`` fires again on every reconnect; the startup statement below is
# about this process, so it is said once rather than on every network blip.
_announced = False

# The process's real :class:`Authorizer` — the one holding whatever allowlist
# was configured. A View that was constructed without one consults this rather
# than inventing a blank ``Authorizer()`` (#739: a blank one has no allowlist
# AND no resolved owner, so it rejected everybody — including the owner who was
# on the configured allowlist).
_default_authorizer: Authorizer | None = None


def set_default_authorizer(authorizer: Authorizer | None) -> None:
    """Publish the process's Authorizer for Views that were not handed one."""
    global _default_authorizer
    _default_authorizer = authorizer


def get_default_authorizer() -> Authorizer | None:
    """The process's Authorizer, or ``None`` before a cog has published one."""
    return _default_authorizer


def allow_anyone_enabled(explicit: bool | None = None) -> bool:
    """Whether the explicit fail-open switch is on (``CLORD_ALLOW_ANYONE``)."""
    if explicit is not None:
        return explicit
    return os.getenv(ALLOW_ANYONE_ENV, "").strip().lower() in _TRUE_VALUES


def set_fallback_owner_ids(ids: set[int] | None) -> None:
    """Publish the application owner(s) to every :class:`Authorizer`.

    ``None`` resets to "not resolved" (used by tests); an empty set means
    "resolution failed" and denies everyone, which is the safe direction.
    """
    global _fallback_owner_ids, _announced
    _fallback_owner_ids = set(ids) if ids is not None else None
    if ids is None:
        _announced = False  # reset (tests)


def get_fallback_owner_ids() -> set[int] | None:
    """The resolved application owner(s), or ``None`` if not resolved yet."""
    return _fallback_owner_ids


class Authorizer:
    """Decides whether a user may interact with the bot.

    One instance is built in ``setup_bridge`` and shared by every gate, so the
    owner resolved at startup reaches all of them.  See the module docstring
    for the rule.

    Args:
        allowed_user_ids: Discord user IDs allowed to drive the bot.
        allowed_role_name: Discord role whose members are allowed to.  OR
            logic with *allowed_user_ids*.
        allow_anyone: Explicit fail-open.  Defaults to the
            ``CLORD_ALLOW_ANYONE`` env var.
    """

    def __init__(
        self,
        allowed_user_ids: set[int] | None = None,
        allowed_role_name: str | None = None,
        *,
        allow_anyone: bool | None = None,
    ) -> None:
        self.allowed_user_ids = allowed_user_ids
        self.allowed_role_name = allowed_role_name
        self.allow_anyone = allow_anyone_enabled(allow_anyone)

    @property
    def has_explicit_allowlist(self) -> bool:
        """Whether the operator configured who may use the bot."""
        return self.allowed_user_ids is not None or self.allowed_role_name is not None

    def is_allowed(self, user: discord.Member | discord.User) -> bool:
        """Whether *user* may drive the bot."""
        member = user if isinstance(user, discord.Member) else None
        return self._decide(user.id, member)

    def is_allowed_user_id(self, user_id: int) -> bool:
        """:meth:`is_allowed` for a bare user ID (no role information).

        Same answer as for a DM: a role allowlist cannot be checked without a
        :class:`discord.Member`, so only an ID match — or the owner default —
        can pass.
        """
        return self._decide(user_id, None)

    def _decide(self, user_id: int, member: discord.Member | None) -> bool:
        if self.allowed_user_ids is not None and user_id in self.allowed_user_ids:
            return True
        if self.allowed_role_name is not None:
            if member is not None:
                return any(r.name == self.allowed_role_name for r in member.roles)
            return False  # DM / bare ID — no role info
        if self.allowed_user_ids is not None:
            return False  # configured, and this user is not on the list
        # Nothing configured (#713): the application's own owner, not everyone.
        if self.allow_anyone:
            return True
        if _fallback_owner_ids:
            return user_id in _fallback_owner_ids
        return False  # owner unknown — deny rather than widen


async def resolve_fallback_owner_ids(bot: Any, authorizer: Authorizer) -> None:
    """Resolve the default allowlist from the Discord application (#713).

    Called from ``on_ready``, when the bot is logged in and can ask Discord who
    owns it.  Does nothing when an allowlist is configured — an explicit
    allowlist is never quietly widened with the owner.  ``on_ready`` fires
    again on every reconnect, so the work and the log line happen once per
    process.

    Always says, in one startup log line, who ended up allowed and how to
    change it: narrowing access in silence is the failure shape c-lord refuses
    (#585), and so is widening it in silence.
    """
    global _announced
    if _announced:
        return
    _announced = True

    if authorizer.allow_anyone:
        logger.warning(
            "%s is set: EVERYONE in the server may drive this bot — and driving "
            "it runs shell commands on this host. Unset it (and optionally set "
            "DISCORD_OWNER_ID / CLORD_ALLOWED_ROLE) to restrict access.",
            ALLOW_ANYONE_ENV,
        )
        return
    if authorizer.has_explicit_allowlist:
        logger.info(
            "Authorization: using the configured allowlist (user_ids=%s role=%s)",
            sorted(authorizer.allowed_user_ids) if authorizer.allowed_user_ids else None,
            authorizer.allowed_role_name,
        )
        return
    try:
        app = getattr(bot, "application", None) or await bot.application_info()
        if getattr(app, "team", None) is not None:
            owner_ids = {m.id for m in app.team.members}
            owner_label = f"team {app.team.name}"
        else:
            owner_ids = {app.owner.id}
            owner_label = str(app.owner)
    except Exception:
        # Fail closed: an owner we could not read is not an owner we can trust.
        # But a network blip must not be a permanent lockout, so the once-only
        # flag is released and the next ``on_ready`` (reconnect) tries again.
        # The empty set denies everyone until one of those attempts succeeds.
        set_fallback_owner_ids(set())
        _announced = False
        logger.warning(
            "Authorization: could not ask Discord who owns this application, so "
            "nobody is allowed to drive the bot for now (retried on reconnect). "
            "If it keeps failing, set DISCORD_OWNER_ID to your Discord user ID "
            "(or %s=1 to allow everyone) and restart.",
            ALLOW_ANYONE_ENV,
            exc_info=True,
        )
        return

    set_fallback_owner_ids(owner_ids)
    logger.info(
        "Authorization: no allowlist configured, so only the app owner %s (%s) "
        "may use this bot. Set DISCORD_OWNER_ID / CLORD_ALLOWED_ROLE to allow "
        "others, or %s=1 to allow everyone in the server.",
        owner_label,
        ", ".join(str(i) for i in sorted(owner_ids)),
        ALLOW_ANYONE_ENV,
    )


def _denial_reason(authorizer: Authorizer, unwired: bool) -> str:
    """Why this click was refused — the two cases look identical otherwise (#739).

    A single "Rejected unauthorized button interaction" line cannot tell
    "this user is not on the configured allowlist" apart from "there is no
    usable allowlist, so nobody passes", and the second one is a c-lord bug
    rather than a user error. Naming which is which is what made #739 take
    longer to diagnose than it should have.
    """
    where = "view was not wired, used the process authorizer" if unwired else "view's authorizer"
    if authorizer.has_explicit_allowlist:
        ids = sorted(authorizer.allowed_user_ids) if authorizer.allowed_user_ids else None
        return (
            f"not on the configured allowlist "
            f"(user_ids={ids} role={authorizer.allowed_role_name}; {where})"
        )
    owners = get_fallback_owner_ids()
    if owners is None:
        return f"no allowlist configured and the app owner is not resolved yet ({where})"
    if not owners:
        return f"no allowlist configured and the app owner could not be read ({where})"
    return f"no allowlist configured, so only the app owner {sorted(owners)} may click ({where})"


class AuthorizedViewMixin:
    """Mixin adding allowlist enforcement to a ``discord.ui.View``.

    Reads ``self._authorizer`` (an :class:`Authorizer` or ``None``).  ``None``
    means the construction site has not been wired up; the check then falls back
    to the process's own authorizer (:func:`get_default_authorizer`) so the
    *configured* allowlist still decides.

    It used to build a blank ``Authorizer()`` there instead, which was wrong in
    the one case that mattered most (#739): a blank authorizer has no allowlist,
    so it took the "nothing configured" branch and consulted the app-owner
    fallback — which :func:`resolve_fallback_owner_ids` deliberately leaves
    unresolved whenever an allowlist *is* configured. The result was a View that
    rejected **everyone**, the owner on the allowlist included. Deny-by-default
    is right; deny-by-default computed from the wrong allowlist is not.

    A View should still be handed its authorizer explicitly — this fallback is
    the floor, not the design. ``tests/test_button_authorization.py`` holds
    every View to being wired.
    """

    # Class-level default so a View that forgets to set it still behaves
    # rather than raising AttributeError.
    _authorizer: Authorizer | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        wired = getattr(self, "_authorizer", None)
        authorizer = wired or get_default_authorizer()
        if authorizer is None:
            # No cog has published one: c-lord is not running this View, or it
            # was built before setup. Nothing can be checked against, so deny —
            # and say which of the two "denied" cases this is (#739 AC5).
            logger.warning(
                "Rejected button interaction from user %s on %s: no authorizer "
                "on the view and none published for this process — c-lord could "
                "not tell whether this user is allowed",
                getattr(interaction.user, "id", "?"),
                type(self).__name__,
            )
            with contextlib.suppress(discord.HTTPException):
                await interaction.response.send_message(UNAUTHORIZED_MESSAGE, ephemeral=True)
            return False

        if authorizer.is_allowed(interaction.user):
            return True
        logger.info(
            "Rejected unauthorized button interaction from user %s on %s: %s",
            getattr(interaction.user, "id", "?"),
            type(self).__name__,
            _denial_reason(authorizer, wired is None),
        )
        with contextlib.suppress(discord.HTTPException):
            await interaction.response.send_message(UNAUTHORIZED_MESSAGE, ephemeral=True)
        return False
