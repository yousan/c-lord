# <<REDACTED 24 lines>>
from ..command_gate import NOT_AUTHORIZED
from ..discord_ui.authorization import AuthorizedViewMixin, Authorizer, get_default_authorizer
# <<REDACTED 1 lines>>
from ..thread_settings import resolve_auto_archive_duration
from ..voice_webhooks import is_voice_message
# <<REDACTED 7 lines>>
class UpgradeApprovalView(AuthorizedViewMixin, discord.ui.View):
    # <<REDACTED 24 lines>>
        authorizer: Authorizer | None = None,
    # <<REDACTED 4 lines>>
        self._authorizer = authorizer
        # <<REDACTED 67 lines>>
            package_name="c-lord",
            # <<REDACTED 31 lines>>
    def claims_message(self, message: discord.Message) -> bool:
        """Whether *message* is this cog's upgrade trigger — #862.
# <<REDACTED 1 lines>>
        ``ClaudeChatCog`` asks before turning a webhook message in a thread with
        no session into a new conversation, so the trigger never also reaches
        Claude as a request.
        """
        if not message.webhook_id or is_voice_message(message):
            return False  # #971: c-lord speaking for a sub-agent triggers nothing
        # <<REDACTED 4 lines>>
            return False
        # <<REDACTED 4 lines>>
            return False
        return message.content.strip() == self.config.trigger_prefix
# <<REDACTED 4 lines>>
        if not self.claims_message(message):
            # <<REDACTED 11 lines>>
    def _is_allowed(self, user: discord.Member | discord.User) -> bool:
        """Whether *user* is on the process's allowlist (#781).

        The cog holds no :class:`Authorizer` of its own — it is the one
        ``ClaudeChatCog`` published on the bot, as for the approval buttons
        below. With none published there is nobody to ask, so the answer is no.
        """
        authorizer = getattr(self.bot, "authorizer", None) or get_default_authorizer()
        return authorizer is not None and authorizer.is_allowed(user)

    # <<REDACTED 7 lines>>

        "Authorised" is the process's allowlist (#781) — it used to be anyone
        who could see the command. Checked before anything else, including
        whether the command is enabled at all.
        # <<REDACTED 1 lines>>
        if not self._is_allowed(interaction.user):
            logger.info(
                "/upgrade rejected: user %s is not authorized (#781)",
                getattr(interaction.user, "id", "?"),
            )
            await interaction.response.send_message(NOT_AUTHORIZED, ephemeral=True)
            return

        # <<REDACTED 32 lines>>
        archive_minutes = await resolve_auto_archive_duration(
            getattr(self.bot, "settings_repo", None)
        )
        # <<REDACTED 3 lines>>
                auto_archive_duration=archive_minutes,
            # <<REDACTED 5 lines>>
        archive_minutes = await resolve_auto_archive_duration(
            getattr(self.bot, "settings_repo", None)
        )
        thread = await trigger_message.create_thread(
            name=self.config.trigger_prefix[:100], auto_archive_duration=archive_minutes
        )
        # <<REDACTED 92 lines>>
        """Record *thread_ids* in the pending-resumes table for a restart notice.
# <<REDACTED 2 lines>>
        were active at upgrade time get a quiet restart notice on the next bot
        # <<REDACTED 2 lines>>
        #406: we do **not** re-prompt Claude on restart — the ``claude`` process
        survives the upgrade restart and the observers self-restore, so
        ``on_ready`` only posts a quiet notice (it never re-runs Claude).

        ``session_id`` is auto-resolved from ``bot.session_repo`` when available
        (kept for diagnostics / future use).
        # <<REDACTED 19 lines>>
                # resume_prompt is intentionally None (#406): on_ready posts a
                # quiet restart notice, it never re-runs Claude with a prompt.
                # <<REDACTED 4 lines>>
                    resume_prompt=None,
                # <<REDACTED 3 lines>>
                logger.warning("Failed to record thread %d for restart notice", tid, exc_info=True)
# <<REDACTED 3 lines>>
                f"📌 {marked} active session(s) noted — "
                "a quiet restart notice will be posted (no auto-resume)."
            # <<REDACTED 72 lines>>
            view = UpgradeApprovalView(
                approved_event=approved,
                bot_id=bot_id,
                authorizer=getattr(self.bot, "authorizer", None),
            )
            # <<REDACTED 115 lines>>
