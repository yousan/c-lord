"""c-lord の自動削除は Claude Code の会話ログ保持期間に合わせる — Issue #575。

**なぜ 90 日ではなく「Claude Code に合わせる」なのか。**

当初 #540 では「90日でフォルダを削除、会話履歴は残す」と決めた。実装しようとして
2つの事実に突き当たった。

1. Claude Code は **自前で会話ログを消している**。実測（本番ホスト 3,506 ファイル）
   では 21〜30日が 113 件あるのに **31日以上は 0 件** で、ちょうど 30 日で切れて
   いた。設定キーは ``cleanupPeriodDays``（Claude Code 2.1.245 のバイナリ内に実在。
   "Transcript retention cleanup" と明記）。
2. c-lord 自身も起動のたびに 30 日で ``sessions`` 行を消していた（#554）。行は
   スレッドと作業ディレクトリを結ぶ唯一の手掛かりなので、消えるとフォルダだけが
   取り残される。**118GB 溜まっていた直接の原因。**

結果、90日という数字は意味を持ち得なかった: 行は30日で消え、会話も30日で消える。
90日まで生き残る対象が存在しない。

**2026-08-27 の決定 (yousan)**: c-lord は ``cleanupPeriodDays`` を**変更しない**。
代わりに**自分の削除期間をそれに合わせ**、削除の通知で「この長さで消えます」と
案内する。Claude が会話を忘れた時点でフォルダも片付く、という一本の線になる。
"""

from __future__ import annotations

import json

from c_lord.retention import (
    RETENTION_FALLBACK_DAYS,
    claude_transcript_retention_days,
)


class TestReadsClaudeCodeSetting:
    def test_reads_user_settings(self, tmp_path, monkeypatch) -> None:
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"cleanupPeriodDays": 45}), encoding="utf-8"
        )
        monkeypatch.setenv("HOME", str(home))

        assert claude_transcript_retention_days() == 45

    def test_managed_settings_win_over_user(self, tmp_path, monkeypatch) -> None:
        """Managed settings are the top of Claude Code's precedence order — an
        organisation's retention policy must not be silently undercut."""
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"cleanupPeriodDays": 45}), encoding="utf-8"
        )
        managed = tmp_path / "managed" / "managed-settings.json"
        managed.parent.mkdir(parents=True)
        managed.write_text(json.dumps({"cleanupPeriodDays": 7}), encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))

        assert claude_transcript_retention_days(managed_path=str(managed)) == 7

    def test_absent_setting_falls_back_to_the_observed_default(
        self, tmp_path, monkeypatch
    ) -> None:
        """30 is Claude Code's own default, confirmed by measurement rather than
        by documentation (the settings page does not list the key)."""
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))

        assert claude_transcript_retention_days() == RETENTION_FALLBACK_DAYS == 30

    def test_missing_file_falls_back(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "nothing-here"))
        assert claude_transcript_retention_days() == RETENTION_FALLBACK_DAYS

    def test_malformed_json_falls_back_rather_than_raising(
        self, tmp_path, monkeypatch
    ) -> None:
        """A broken settings file must not stop the bot, and must not be read as
        "retention is zero" — that would delete everything."""
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))

        assert claude_transcript_retention_days() == RETENTION_FALLBACK_DAYS

    def test_a_nonsense_value_falls_back(self, tmp_path, monkeypatch) -> None:
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        (home / ".claude" / "settings.json").write_text(
            json.dumps({"cleanupPeriodDays": "forever"}), encoding="utf-8"
        )
        monkeypatch.setenv("HOME", str(home))

        assert claude_transcript_retention_days() == RETENTION_FALLBACK_DAYS

    def test_zero_or_negative_falls_back(self, tmp_path, monkeypatch) -> None:
        """Never let a bad value mean "delete immediately"."""
        home = tmp_path / "home"
        (home / ".claude").mkdir(parents=True)
        for bad in (0, -5):
            (home / ".claude" / "settings.json").write_text(
                json.dumps({"cleanupPeriodDays": bad}), encoding="utf-8"
            )
            assert claude_transcript_retention_days() == RETENTION_FALLBACK_DAYS


class TestClordNeverWritesTheSetting:
    def test_module_does_not_write_settings(self) -> None:
        """2026-08-27 決定: c-lord は cleanupPeriodDays を読むだけで、書き換えない。

        Claude Code 全体の挙動を変える設定なので、Discord フロントエンドが勝手に
        触ってよいものではない。
        """
        import inspect

        from c_lord import retention

        src = inspect.getsource(retention)
        for forbidden in ("open(", "write_text", "json.dump"):
            assert forbidden not in src or "read" in src.split(forbidden)[0][-80:], (
                f"retention.py must not write settings ({forbidden!r} found)"
            )
