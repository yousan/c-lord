# Staging Evidence — #75 (PR #302)

staging: `c-lord-staging-1` / channel `1503196656265597082` / 2026-06-12 / 完全自走で実機検証。

## セットアップ（自走）
staging bot は自身の権限 overwrite を変更できない（403）が、**prod bot `C-lord`（`MANAGE_CHANNELS`/`MANAGE_ROLES` 保持）のトークン**で staging-1 bot (`1503195981142032405`) の **「スレッドでのメッセージ送信」(`SEND_MESSAGES_IN_THREADS` = 1<<38)** を channel に deny（`PUT …/permissions/<bot>` → 204）。検証後 `DELETE` で復元。

webhook 経由で `!clord <prompt>` を投げ、`spawn_session` の seed `thread.send` が `Forbidden` になる状況を作る。

## RED — `main`
```
Traceback (most recent call last):
  File ".../c_lord/cogs/claude_chat.py", line 811, in spawn_session
    raise Forbidden(response, data)
discord.errors.Forbidden: 403 Forbidden (error code: 50001): Missing Access
discord.ext.commands.errors.CommandInvokeError: Command raised an exception: Forbidden …
```
→ サーバーログに traceback が漏れるだけで、**Discord 上のユーザーには何も返らない**（C-lord-staging-1 は無言）。

## GREEN — `fix/75-no-send-perm`
bot ログ:
```
[WARNING] c_lord.cogs.claude_chat: [thread=…] spawn seed send forbidden (missing send permission)
```
Discord（親チャンネル）に C-lord-staging-1 が投稿（`green-guidance-message.png`）:
```
❌ このチャンネル/スレッドに書き込み権限がありません。
Bot に「メッセージの送信」「公開スレッドでのメッセージ送信」「プライベートスレッドでのメッセージ送信」を付与してください。
```
→ 握り潰さず `logger.warning(exc_info=True)` でログを残しつつ、**権限名入りのユーザー向け案内**に変換。
