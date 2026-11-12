"""``DrainAware`` — how a cog says "wait for me before restarting" (``c_lord.protocols``).

The protocol is structural: ``AutoUpgradeCog`` finds the cogs to wait for with
``isinstance``, so a cog qualifies by having ``active_count`` and nothing else.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from c_lord.claude.config import ClaudeConfig
from c_lord.cogs.claude_chat import ClaudeChatCog
from c_lord.cogs.webhook_trigger import WebhookTriggerCog
from c_lord.protocols import DrainAware


class TestBuiltInCogs:
    """The two cogs that run Claude must both be waited for."""

    def test_the_chat_cog_is_drain_aware(self) -> None:
        cog = ClaudeChatCog(
            bot=MagicMock(),
            repo=MagicMock(),
            runner=MagicMock(spec=ClaudeConfig),
        )

        assert isinstance(cog, DrainAware)
        assert cog.active_count == 0

    def test_the_webhook_trigger_cog_is_drain_aware(self) -> None:
        cog = WebhookTriggerCog(
            bot=MagicMock(),
            runner=MagicMock(spec=ClaudeConfig),
            triggers={},
        )

        assert isinstance(cog, DrainAware)
        assert cog.active_count == 0


class TestStructuralMatching:
    def test_a_property_is_enough(self) -> None:
        """No inheritance needed: a third-party cog only has to expose the count."""

        class Queue:
            @property
            def active_count(self) -> int:
                return 3

        assert isinstance(Queue(), DrainAware)

    def test_a_plain_attribute_is_enough(self) -> None:
        class Counter:
            def __init__(self) -> None:
                self.active_count = 0

        assert isinstance(Counter(), DrainAware)

    def test_an_object_without_the_count_does_not_match(self) -> None:
        class Unrelated:
            active_sessions = 2

        assert not isinstance(Unrelated(), DrainAware)
        assert not isinstance(object(), DrainAware)
