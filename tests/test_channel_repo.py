"""Tests for ChannelRepository and ChannelRepoCog."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from c_lord.database.channel_repo import ChannelRepository
from c_lord.cogs.channel_repo import ChannelRepoCog


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def repo(tmp_path) -> ChannelRepository:
    r = ChannelRepository(str(tmp_path / "channel.db"))
    await r.init_db()
    return r


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.loop = MagicMock()
    bot.get_cog = MagicMock(return_value=None)
    return bot


@pytest.fixture
def cog(repo: ChannelRepository, tmp_path) -> ChannelRepoCog:
    return ChannelRepoCog(
        _make_bot(),
        repo=repo,
        allowed_user_ids=None,
        session_dir_base=str(tmp_path / "sessions"),
    )


# ===========================================================================
# ChannelRepository tests
# ===========================================================================


class TestChannelRepoSave:
    async def test_save_and_get(self, repo: ChannelRepository) -> None:
        await repo.save(channel_id=100, source_repo="https://github.com/org/repo.git")
        binding = await repo.get(100)
        assert binding is not None
        assert binding["channel_id"] == 100
        assert binding["source_repo"] == "https://github.com/org/repo.git"
        assert binding["clone_branch"] is None

    async def test_save_with_branch(self, repo: ChannelRepository) -> None:
        await repo.save(
            channel_id=200,
            source_repo="https://github.com/org/repo.git",
            clone_branch="develop",
        )
        binding = await repo.get(200)
        assert binding is not None
        assert binding["clone_branch"] == "develop"

    async def test_save_upsert(self, repo: ChannelRepository) -> None:
        await repo.save(channel_id=300, source_repo="https://github.com/org/old.git")
        await repo.save(channel_id=300, source_repo="https://github.com/org/new.git")
        binding = await repo.get(300)
        assert binding is not None
        assert binding["source_repo"] == "https://github.com/org/new.git"


class TestChannelRepoGet:
    async def test_get_nonexistent(self, repo: ChannelRepository) -> None:
        binding = await repo.get(999)
        assert binding is None


class TestChannelRepoDelete:
    async def test_delete_existing(self, repo: ChannelRepository) -> None:
        await repo.save(channel_id=400, source_repo="https://github.com/org/repo.git")
        deleted = await repo.delete(400)
        assert deleted is True
        assert await repo.get(400) is None

    async def test_delete_nonexistent(self, repo: ChannelRepository) -> None:
        deleted = await repo.delete(999)
        assert deleted is False


class TestChannelRepoListAll:
    async def test_list_empty(self, repo: ChannelRepository) -> None:
        bindings = await repo.list_all()
        assert bindings == []

    async def test_list_all(self, repo: ChannelRepository) -> None:
        await repo.save(channel_id=500, source_repo="https://github.com/org/a.git")
        await repo.save(channel_id=600, source_repo="https://github.com/org/b.git")
        bindings = await repo.list_all()
        assert len(bindings) == 2
        ids = {b["channel_id"] for b in bindings}
        assert ids == {500, 600}


# ===========================================================================
# ChannelRepoCog tests
# ===========================================================================


class TestChannelRepoCogResolveManager:
    async def test_resolve_returns_none_without_binding(
        self, cog: ChannelRepoCog
    ) -> None:
        manager = await cog.resolve_manager(999)
        assert manager is None

    async def test_resolve_creates_manager_from_binding(
        self, cog: ChannelRepoCog, repo: ChannelRepository
    ) -> None:
        await repo.save(channel_id=100, source_repo="/tmp/fake-repo.git")
        manager = await cog.resolve_manager(100)
        assert manager is not None
        assert manager.source_repo == "/tmp/fake-repo.git"

    async def test_resolve_caches_manager(
        self, cog: ChannelRepoCog, repo: ChannelRepository
    ) -> None:
        await repo.save(channel_id=100, source_repo="/tmp/fake-repo.git")
        m1 = await cog.resolve_manager(100)
        m2 = await cog.resolve_manager(100)
        assert m1 is m2  # same object from cache

    async def test_resolve_with_branch(
        self, cog: ChannelRepoCog, repo: ChannelRepository
    ) -> None:
        await repo.save(
            channel_id=200,
            source_repo="/tmp/fake-repo.git",
            clone_branch="develop",
        )
        manager = await cog.resolve_manager(200)
        assert manager is not None
        assert manager._clone_branch == "develop"


class TestChannelRepoCogEvictCache:
    async def test_evict_removes_from_cache(
        self, cog: ChannelRepoCog, repo: ChannelRepository
    ) -> None:
        await repo.save(channel_id=100, source_repo="/tmp/fake-repo.git")
        await cog.resolve_manager(100)
        assert 100 in cog._manager_cache
        cog.evict_cache(100)
        assert 100 not in cog._manager_cache
