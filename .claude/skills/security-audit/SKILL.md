---
name: security-audit
description: Security checklist specific to c-lord — subprocess injection, env leaks, input validation
---

# security-audit — c-lord で守るものと、その確かめ方

c-lord に話しかけられる人は、bot を動かしているホストで Claude Code を走らせられる。つまり
**「誰が話しかけられるか」と「Discord から来た文字列がどこへ流れるか」がそのまま安全性**になる。
背景と受け入れているリスクの全体は `docs/SECURITY.md`。このスキルは、変更をコミットする前に
差分をそこに照らすための手順。

## いつ通すか

- `c_lord/tmux.py`、`c_lord/claude/tmux_runner.py`、`c_lord/cogs/_run_helper.py`、`c_lord/cogs/` の
  Cog を変えたとき（CLAUDE.md の Security 節で、コミット前の実施が必須）
- スラッシュコマンド・テキストコマンド・ボタンを足したとき
- 外部コマンド（`tmux`、`git`、`claude`、`docker` など）の呼び方や引数を変えたとき
- 環境変数・設定項目・REST API のエンドポイントを足したとき

## 何がどこで守られているか

| 脅威 | 入口 | いまの守り |
|------|------|-----------|
| 許可していない人が Claude を動かす | メッセージ、コマンド、ボタン | 全ゲートが 1 つの `Authorizer`（`c_lord/discord_ui/authorization.py`）を共有。未設定なら Discord アプリの所有者だけ（#713） |
| プロンプトがシェルや CLI のフラグとして解釈される | 利用者のメッセージ → `claude` の起動行 | プロンプトはファイル経由で変数に入れ `-- "$CLORD_PROMPT"` として渡す（`TmuxSessionManager.start_claude`）。起動後の入力は `send-keys -l … --` でそのまま打つ |
| 細工した session id がパスやフラグになる | DB や `.clord-session` から読んだ id → `--resume` | `transcript/claim.py` の `is_session_id()`（UUID の形だけ通す）。記録する時点で弾く |
| スキル名にコマンドを混ぜる | `/skill name:` | `cogs/skill_command.py` が `^[\w-]+$` で検証し、読み込み済みスキルと照合 |
| リポジトリ URL で `git` にコマンドを実行させる | `/clord-init`、`/clord repo:` | `database/channel_repo.py` の `validate_repo_url()`（`ext::`・先頭 `-`・改行を拒否）＋ `git clone … -- <url>` |
| 秘密がペインの環境に継承される | bot の環境 → tmux サーバ → 全ウィンドウ | `SENSITIVE_ENV_KEYS`（`c_lord/tmux.py`）を 3 層で外す（#353、下記） |
| Claude が「入れ子」と判断して起動しない | `CLAUDECODE` の継承 | 起動行で `env -u CLAUDECODE` |
| 別の Unix ユーザーが REST API を叩く | loopback の TCP ポート | 相手の UID を照合、または `Authorization: Bearer <CLORD_API_SECRET>`（#457、`ext/peer_uid.py`） |

**守っていないもの**も知っておく: 同じ Unix ユーザーで動く Claude は bot の `.env` を読める
（`discord-read` スキルが実際にそこから token を読む — #259 で受け入れ済み）。環境変数からの除去は
**事故による継承**（#322 の「第 2 の本番 bot」）を防ぐもので、秘匿ではない。

## チェックリスト

### 外部コマンド

- [ ] `shell=True` / `create_subprocess_shell` / `os.system` を足していない。呼び出しは
      `asyncio.create_subprocess_exec(*argv)` か、引数リストを渡す `subprocess.run([...])`
      （ブロッキングなものは `asyncio.to_thread` 経由）
- [ ] 利用者由来の値を位置引数で渡す箇所には、その前に `--` がある（`claude`、`git`、`tmux send-keys`、`du`）
- [ ] **ペインに打ち込むコマンド行**（`start_claude` が組み立てる文字列）は、ペインのシェルが解釈する。
      そこへ値を足すなら `shlex.quote()` を通すか、プロンプトと同じくファイル＋変数で渡す。
      f-string で素の値を埋めない
- [ ] `--resume` / `--session-id` に渡す id は `is_session_id()` を通ったものだけ
- [ ] スキル名は `^[\w-]+$`、モデル名は `model_scopes.is_valid_model_id()`、リポジトリ URL は
      `validate_repo_url()` — 既にある検証を迂回する新しい入口を作っていない

### 秘密と環境

- [ ] 新しい秘密の環境変数は `SENSITIVE_ENV_KEYS` に足した（足すと 3 層すべてに効く:
      `_tmux_client_env()` が tmux クライアントの環境から外す / `_strip_sensitive_env()` が
      セッションに `set-environment -r` を付ける / `start_claude()` が `env -u` する）
- [ ] c-lord が自分で起動する子プロセス（`probe_context_window` のような `claude -p`）にも、
      `SENSITIVE_ENV_KEYS` と `CLAUDECODE` を除いた環境を渡している
- [ ] token・secret・サインイン用のコードをログや Discord の投稿に出していない
      （ログイン中継は長さだけを記録する — `login_relay.py`）
- [ ] `.env` は `.gitignore` に入ったまま。ワークスペースに c-lord が書くファイル
      （`discord-read` の `SKILL.md`、スレッド履歴）は `.git/info/exclude` に載る（`git_exclude.py`）。
      token そのものをファイルに焼き込まない — 渡すのは `.env` のパスだけ

### 認可

- [ ] 新しいコマンドは共有の `Authorizer` を通る（`self._is_allowed` / `self._authorize` /
      `command_gate.is_message_authorized`）。規則の写しを自前で持たない。
      `tests/test_command_authorization_coverage.py` が全コマンドを AST で検査していて、通さないコマンドは
      CI で落ちる。誰でも使えてよい読み取り専用コマンドは、そのテストの `PUBLIC_COMMANDS` に理由つきで足す
- [ ] 新しいボタン / View は `AuthorizedViewMixin` を混ぜている（スレッドは公開なので、
      見える人なら誰でも押せてしまう — #466）
- [ ] webhook 経由の入力を扱うなら、c-lord 自身がサブエージェントの声として使う webhook を
      命令として受け取らない（`voice_webhooks.is_voice_message` — #971）
- [ ] テキストコマンドは「どのインスタンスが担当か」も `command_gate` に任せる（#596。
      1 つのギルドに c-lord が複数いると、全員に同じコマンドが届く）

### データとエラー

- [ ] SQL の**値**はすべて `?` プレースホルダ。f-string で組み立ててよいのは、コード内で固定した
      カラム名や条件句だけ
- [ ] REST API に足したエンドポイントは、既存の UID / Bearer のゲートの内側にある。ファイルパスを
      受け取るなら、セッションディレクトリの外を開かない（`/api/reply` の `progress_file` と同じ扱い）
- [ ] 例外の詳細（スタックトレース、内部パス）は Discord に流さずログへ。ログは `log_ctx()` で
      スレッドを引けるようにする
- [ ] 失敗したときに tmux ウィンドウや子プロセスを残さない

### 依存

- [ ] 依存を足したなら `uv.lock` も更新した。ほとんどの利用者に要らないものは
      `[project.optional-dependencies]` に置く
- [ ] import するだけで秘密を要求するコードを足していない

## 手早い走査

```bash
# シェルを通す呼び出し。docstring の説明文以外に出てはいけない
grep -rn "shell=True\|create_subprocess_shell\|os\.system(" c_lord/

# 外部コマンドの呼び出し箇所。自分の差分にあるものは、引数リストか・`--` があるかを目で見る
grep -rn "create_subprocess_exec\|subprocess\.run(" c_lord/

# 秘密の一覧と、それを外している場所
grep -n "SENSITIVE_ENV_KEYS" -r c_lord/

# 認可を通らないコマンドが無いこと / 秘密が環境に残らないこと
uv run pytest tests/test_command_authorization_coverage.py tests/test_tmux_secret_env.py -q

# .env が追跡されていないこと
git check-ignore -v .env
```

## 見つけたときの扱い

| 重さ | 例 | 対応 |
|------|----|------|
| 致命的 | 利用者の文字列がコマンドとして実行される、認可を通らない入口、秘密の漏出 | ほかの作業を止めて先に直す。コミットしない |
| 高 | 検証の抜け、`--` の抜け | コミット前に直す |
| 中 | ログに余計な情報が出る | 同じ PR で直す |
| 低 | 追加の堅牢化 | Issue にして追う（黙って落とさない） |

`docs/SECURITY.md` に書いてある守りを変えた・足したときは、同じ PR でその文書も直す。
