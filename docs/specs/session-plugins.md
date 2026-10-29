# セッションの claude に付けるプラグイン (#965)

## あるべき動き（利用者から見て）

- C-lord が立てるセッションの `claude` には、**そのセッションで出番のないプラグインを付けない**。Discord への返事やセッションの動きは何も変わらず、ホストのメモリだけが減る。
- 利用者が手元の Claude Code で使っている設定（`~/.claude/settings.json`）は**そのまま**。リポジトリの `.claude/settings.json` も書き換えない。C-lord は起動するその1本の `claude` にだけ、公式の `--settings '{"enabledPlugins":{…}}'` を渡す。

## 何を外すか

ユーザー設定で有効（`true`）になっているプラグインのうち:

| プラグイン | セッションでは | 理由 |
|---|---|---|
| `discord@…` | **無効** | Discord との行き来は C-lord 本体（transcript ミラー・質問カード）がやる。プラグインはセッション1本ごとに `bun server.ts`（約 50〜80MB）を立てるうえ、セッションから呼ぶと `Missing Access` で失敗する（2026-10 時点で 8 回中 8 回） |
| 言語 LSP（`pyright-lsp` など） | そのディレクトリに**その言語のファイルが無ければ無効** | 出番のない言語サーバを抱えない。どの拡張子の言語かは、プラグイン自身が marketplace に書いている `lspServers.*.extensionToLanguage` で決める |
| それ以外 | 触らない | ユーザー設定のまま |

- 「その言語のファイルがあるか」は、セッションのディレクトリを見る（`.` で始まるディレクトリ・`node_modules`・`vendor`・`venv`・`dist`・`build`・`target` などは見ない）。**5 万ファイル見ても決まらなければ「ある」とみなす**（間違えるなら付ける側に倒す）。
- 拡張子を宣言していない LSP プラグインは判定できないので**触らない**。
- すでに起動している `claude` には効かない。次に起動するとき（新しいスレッド・スリープからの復帰など）から効く。

## ディレクトリごとに決める

公式の仕組みで決める。セッションのディレクトリの `.claude/settings.json` か `.claude/settings.local.json` の `enabledPlugins` にそのプラグインが書いてあれば、**C-lord はそのキーを上書きしない**（`true` でも `false` でも）。

```json
// <session_dir>/.claude/settings.local.json — このセッションでは Discord プラグインを使う
{ "enabledPlugins": { "discord@claude-plugins-official": true } }
```

## 止める

`.env` に `CLORD_SESSION_PLUGINS=0` を書くと、この機能ごと止まる（`--settings` を付けない）。

## 確かめ方

bot ログに `session_plugins: disabling … (#965)` が出る。セッションの `claude` の配下に `plugins/cache/claude-plugins-official/discord/…` の `bun` が居ないこと:

```bash
pstree -a -p <claude の pid> | grep discord/   # 何も出なければ OK
```
