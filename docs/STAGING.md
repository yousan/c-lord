# STAGING.md — staging 運用の単一ソース (#327)

**staging の起動・停止・検証・原状復帰の手順はこのファイルと `scripts/staging.sh` が唯一の正**。
CLAUDE.md・メモリ・他ドキュメントに別レシピが書いてあったら、それは drift — このファイルを正として直すこと。
背景(なぜこの形になったか)は Issue #322(調査まとめ)と `docs/DESIGN_DECISIONS.md` §13 を参照。

## 環境レイアウト

| | 本番 (prod) | staging |
|---|---|---|
| clone | `/home/yousan/c-lord` | `/home/yousan/c-lord-staging-1` |
| bot | `C-lord#8255` (`1475105094071750818`) | `C-lord-3#1206` (`1503195981142032405`) |
| channel | `.env` の `DISCORD_CHANNEL_ID` | `#c-lord-3` (`1503196656265597082`) |
| API port | 8087 | 8089 |
| tmux session | `c-lord` | `c-lord-staging-1` |
| session dir | `c-lord-sessions/` | `c-lord-sessions-staging/` |
| ログ (最新) | `/tmp/clord-bot-c-lord.log`* | `/tmp/clord-bot-c-lord-staging-1.log`* |
| ライフサイクル | **systemd `c-lord.service` だけ**(kill+自前起動禁止 — #195 / ops#2) | `scripts/staging.sh` で手動管理 |
| idle ブランチ | `main` | **`main`** |

session dir は `.env` の `SESSION_DIR_BASE` で決めている(上表はいまの実値。#837 以前に立てたので `~` 直下にある)。
**新しく立てるインスタンスは `SESSION_DIR_BASE` を書かなければ `~/.c-lord/<instance>/sessions/`** に作られる
(`<instance>` は `CLORD_INSTANCE`、未設定なら clone のディレクトリ名)。
稼働中の session dir は移さないこと — transcript が作業ディレクトリのパスに紐付くので、移すと既存スレッドの `--resume` が切れる。

\* staging は per-run ログ `/tmp/clord-bot-<name>-<timestamp>.log` + 最新への symlink。本番のログは `journalctl --user -u c-lord.service`(本番の `/tmp/clord-bot-c-lord.log` は、以前 staging.sh が本番を自前起動していた頃の名残り)。
旧固定パス (`/tmp/clord-bot.log` / `/tmp/clord-bot-staging.log`) は旧手順の名残り。

> **idle ブランチについて**: かつて CLAUDE.md は `fix/wire-max-concurrent-sessions` を idle ブランチとしていたが、
> このブランチは staging DB のスキーマ前進 (`mirror_replied_uuid`, #232/#242) と不整合で**起動クラッシュする**。
> idle = `main` が現在の正(2026-06-10 改訂)。

## staging フリート(並行検証用に増設, 2026-06-11)

単一 staging では複数セッションが lease を取り合う(2026-05-29 実害)ため、staging bot を **4台**に
増設した。各台は独立した clone / bot identity / channel / E2E スレッドを持ち、**別ブランチを同時に検証**
できる。1 つ借りられていても他の空き番号を borrow すればよい。

| # | clone | bot (user id) | channel (id) | E2E スレッド id | port |
|---|---|---|---|---|---|
| 1 | `/home/yousan/c-lord-staging-1`† | `C-lord-staging-1` (`1503195981142032405`) | `#c-lord-staging-1` (`1503196656265597082`) | `1514085380666691664` | 8089 |
| 2 | `/home/yousan/c-lord-staging-2` | `C-lord-staging-2` (`1514518564403413014`) | `#c-lord-staging-2` (`1514535894575743056`) | `1514545583459926117` | 8091 |
| 3 | `/home/yousan/c-lord-staging-3` | `C-lord-staging-3` (`1503234123932635206`) | `#c-lord-staging-3` (`1503245597841559623`) | `1514546023282769920` | 8093 |
| 4 | `/home/yousan/c-lord-staging-4` | `C-lord-staging-4` (`1514523658780016771`) | `#c-lord-staging-4` (`1514535896328700015`) | `1514546025631580260` | 8095 |

† #1 は既存 staging。2026-06-11 に bot/channel/ディレクトリを全て `staging-1` 系へ改称完了(旧名 `c-lord-parallel-3` / `C-lord-3` / `#c-lord-3`)。統合ロール名のみ `C-lord-3` のまま残る(managed ロールは API 改名不可。Portal の Application 名変更で揃う。機能には無影響)。

- **port = 8087 + 2×N**(prod=N0=8087)。#712 以降は **全台が実際にこのポートを bind する**(REST API は常時起動)。
  重複すると WARNING が出て API 無しで動く(bot は落ちない)ので、増設時は必ず空きポートを振る。
- channel アクセスは共有ロール **`c-lord-staging`**(`1514537446132682853`)一本で制御(staging bot 全台に付与)。
  bot を増やしたらこのロールを付けるだけ(個別の permission overwrite は不要)。
- 各 clone の `.env` に `DISCORD_CHANNEL_ID` / `EXPECTED_BOT_USER_ID` / `E2E_TEST_THREAD_ID` 設定済み。
  各 staging channel は自分の clone に `channel_repo_bindings` で bind 済み(`/clord-init` 相当)。

### 他エージェントからのトリガー(信頼bot方式, webhook 不要)

新 staging (#2–#4) は `.env` に `CLORD_TRUSTED_BOT_IDS=1475105094071750818`(prod bot) を設定済み。
prod の bot token で各台の **E2E スレッド**に投稿すれば、その staging bot が信頼 bot として受理し
(`claude_chat._is_message_authorized`)、Claude を起動して応答をスレッドにミラーする(jsonl の
`TranscriptMirrorCog` 経由)。Discord webhook も Manage Webhooks も要らない。

```bash
PTOK=$(grep '^DISCORD_BOT_TOKEN=' /home/yousan/c-lord/.env | cut -d= -f2-)
THREAD=1514545583459926117   # 上表の E2E スレッド id (例: staging-2)
curl -s -X POST -H "Authorization: Bot $PTOK" -H "User-Agent: DiscordBot/1.0" \
  -H "Content-Type: application/json" \
  -d '{"content":"<検証入力>"}' \
  "https://discord.com/api/v10/channels/$THREAD/messages"
# → 応答は同じ E2E スレッドに返る(別 bot 投稿なので staging bot だけが反応)
```

borrow → ブランチ切替(`staging.sh restart <branch>`) → トリガー → 原状復帰 → release の流れは
下の「占有プロトコル」「検証レシピ」と同じ(対象 clone を上表で読み替えるだけ)。既存 #1 は従来どおり
webhook (`E2E_TEST_WEBHOOK_URL`) でもトリガーできる。

## 安全原理(コードで強制されているもの)

1. **directory == identity** (#324): `.env` に書かれたキーは継承 env に常に勝つ。正しいディレクトリで起動すれば正しい bot になる。
2. **identity fail-fast** (#323): `.env` の `EXPECTED_BOT_USER_ID` と実ログイン identity が違えば bot は即 exit(1)。**新しい環境の .env には必ず設定すること。**
3. **センチネル .env** (#326): `c-lord-parallel` / `-2` / `c-lord-issue63` の `.env` は無効値。そこから bot は起動できない(本番 token の読み取りは `/home/yousan/c-lord/.env` を絶対パスで)。
4. **単一インスタンス flock**: 同一 data dir の二重起動 (#212) に加え、**同一トークンの二重起動**もホスト全域ロックで拒否される (#325, `~/.cache/c-lord/locks/token-<hash>.lock`)。別ディレクトリからでも同じ bot は2つ立てられない。緊急回避は `CLORD_ALLOW_MULTI_INSTANCE=1`(両ロックを無効化 — 理解した上でのみ)。

## 操作 — `scripts/staging.sh`(clone のルートで実行)

```bash
cd /home/yousan/c-lord-staging-1

bash scripts/staging.sh status             # identity / branch / pid / instances / log
bash scripts/staging.sh stop               # この clone の bot を安全停止
bash scripts/staging.sh restart            # 現在のブランチで安全再起動
bash scripts/staging.sh restart <branch>   # branch を origin の最新に同期して再起動 (PR 検証用)
```

`restart` は: /proc/cwd で自分の bot だけを同定 → PID 直 kill → setsid + venv python で起動 →
per-run ログ → `Logged in as` を待って identity を検証(mismatch なら非0で失敗)→ 単一インスタンス確認、まで自動で行う。
`OK` を出したら**呼び出し元へ必ず return し、自分のプロセスを残さない**(#401)。`$(bash scripts/staging.sh restart)` や
`… restart 2>&1 | tail` のように出力を読み切る呼び出し方でもそこで止まらない。bot の親は `systemd --user` になり、
`pgrep -af '^bash scripts/staging.sh restart'` に restart の残骸は出ない。

**止まりきらない bot は SIGKILL で落とす**(#699)。`stop`(と `restart` の停止段)は SIGTERM のあと
`CLORD_STOP_GRACE_SECONDS`(既定 15)秒待ち、まだ生きていれば **`/proc/<pid>/cwd` を照合し直したうえで
PID 直指定の SIGKILL** に進む。その事実は標準エラーと **bot の per-run ログ**の両方に
`staging.sh: pid <pid> が SIGTERM から 15 秒で終了しないため SIGKILL します (#699)` と残る。以前はここで
`ERROR: プロセスが 15 秒で終了しない` と中断し、`restart` が起動まで到達せず**本番が止まったまま**になっていた。
bot 側にも shutdown watchdog があり(既定 10 秒、下記トラブルシュート)、通常はそちらが先に終わらせる。

`restart <branch>` は起動前に **`git fetch origin <branch>` → checkout → `git merge --ff-only origin/<branch>`** まで行い、
ローカルブランチを **origin の最新に確実に同期**する(#436)。単なる `checkout` は fetch 済みでも
ローカルブランチを古い HEAD のまま切り替えるだけなので、これが無いと「最新の fix を回したつもりで
**古いコードを検証**」して偽の RED/GREEN を得る(#399 検証中に実害)。ローカルが origin と分岐していて
ff できない場合は、**黙って古いコードを起動せず明示エラーで停止**する(`git reset --hard origin/<branch>` で
破棄するか push してから再実行)。起動ログには回しているコミットが `checked out <branch> @ <short-sha>` と出る。

**禁止事項(全て実害のあった事故パターン — #322 根因D)**:
- `pgrep -f "c_lord.main" | xargs kill` 系の**パターン kill**(本番・自分のシェルに当たる/相対パス起動を取り逃す)
- kill を**並列ツールバッチに入れる**(キャンセルしても発射済みの kill は戻らない)
- `nohup uv run ...` での起動(Bash ツール teardown で exit 144 死する)
- 本番 (`/home/yousan/c-lord`) への手動 kill+nohup(#195 の二重 bot 事故になる)。本番は下の「[本番の再起動](#本番の再起動--systemd-だけ-ops2)」の手順だけ

## 本番の再起動 — systemd だけ (ops#2)

**本番を起動する入口は systemd の unit `c-lord.service` 1 つだけ。**

```bash
cd /home/yousan/c-lord && git pull
systemctl --user restart c-lord.service      # これだけ。staging.sh restart でも同じことが起きる(下記)
bash scripts/staging.sh status               # 監視下に戻ったかを確かめる
```

**`staging.sh` は本番を見分けて kill しない**: user unit の `WorkingDirectory=` が実行した clone と一致すれば
(= 本番)、`restart` / `stop` は bot を kill せず `systemctl --user restart|stop <unit>` に委ねる。リースも不要。
`restart` は journal の `Logged in as` と identity を確かめ、unit が `active`・起動中に落ちていない
(`NRestarts` が増えていない)・監視外の bot がいない、まで見てから `OK` を出す。

- **systemctl が使えない**(`Failed to connect to bus` 等)ときは**エラーで止まり、bot を kill しない**。
  bus の直し方は下の[禁止事項 2・3](#systemd-操作で-tmux--bot-を巻き添えにする事故-504)
- **監視外の bot**(unit の cgroup の外で動いている本番 bot — 過去の事故の残骸)が居れば、PID 直指定で
  止めてから systemd に渡す。放っておくと systemd の起動が単一インスタンスロック(#212/#325)に
  弾かれて `failed` に戻るため
- 判定は unit ファイルを読むだけなので、bus に繋がらないときでも「本番だ」と分かる(そこで kill に落ちない)

**なぜこうしたか**: 以前は CLAUDE.md の本番再起動手順が `staging.sh restart` で、その中身は
kill → `setsid` 自前起動だった。systemd は「落ちた」と判断して 5 秒おきに立て直しを試み、
単一インスタンスロックに弾かれて 6 回失敗 → `Start request repeated too quickly` で `failed` になって諦める。
以後、本番は**監視外**で動き続け、落ちても誰も立て直さない(2026-08-31・09-28 に 2 回。08-31〜09-25 の
約 25 日間は監視外だった)。手順書どおりに動いた担当が事故を起こしていたので、スクリプトの側で塞いだ。

**「監視されているか」は unit の状態と cgroup で見る**:

```bash
bash scripts/staging.sh status
# supervisor: systemd c-lord.service — active (running) MainPID=12345 NRestarts=0
#   pid 12345: 監視下 (c-lord.service)
#   pid 12346: 監視下 (c-lord.service)
```

`監視外` の pid や `active` 以外の状態があれば終了コード 2 と WARNING が出る(`staging.sh restart` で戻す)。
手で見るなら `systemctl --user is-active c-lord` と `cat /proc/<pid>/cgroup`(`…/c-lord.service` の中か)。
**「bot の親が `systemd --user` だから監視下」は根拠にならない** — `setsid` で立った孤児も親は
systemd --user に付け替わるので、監視外でも親だけ見ると監視下に見える(過去の確認はこれで誤った)。

**unit は repo の `deploy/c-lord.service` そのもの**。`bash scripts/install-systemd.sh` が
`~/.config/systemd/user/c-lord.service` に**そのまま**置き、ホスト固有の値(clone の場所・uv・PATH)は
drop-in `c-lord.service.d/10-install.conf` に書く。unit を手で書き換えない(`status` が
`deploy/c-lord.service と違う` と警告する)。unit を変えたら `install-systemd.sh` を打ち直し、
`systemctl --user restart c-lord.service` で反映する(`enable --now` は動いている unit を再起動しない)。
以前の `start-clord.sh`(repo 外の起動スクリプト)と `--guard` モードは廃止した。

### systemd 操作で tmux / bot を巻き添えにする事故 (#504)

上の 4 件が「c-lord を直接殺してしまう」系なのに対し、以下は **c-lord を狙っていないのに
c-lord と tmux が死ぬ**系。2026-08-07 に 3 件とも実際に踏んだ。コマンド単体は正しく見えるので、
知らないと必ず踏む。

**1. `systemctl --user restart|stop c-lord.service` は tmux サーバを道連れにする**(#503 で解消済み — 記録として残す)

> **現在は起きない**: #503 以降、c-lord は tmux サーバを `systemd-run --user` で**別の unit**(`tmux-spawn-*.scope`)に
> 起こすので、`c-lord.service` の cgroup に tmux は入らない。`systemctl --user restart c-lord.service` は tmux を道連れにしない
> (`systemd-run` が使えない環境だけ旧来の `tmux new-session` に落ちる — `docs/SECURITY.md`)。以下は当時の記録。

当時の c-lord は Discord メッセージ受信時に `tmux new-session` でサーバを起こしていたため、
**tmux サーバが `c-lord.service` の cgroup に入る**(→ [supervision モデル](#supervision-モデル--誰が-respawn-するか-437))。
systemd はユニット停止時に cgroup 内の全プロセスを kill するので、bot を再起動しただけで
**c-lord と無関係な作業セッションまで全滅**する。

```
Aug 07 14:33:19 c_lord.tmux: Created global tmux session: claude_base  ← ここで tmux が c-lord の cgroup に入る
Aug 07 14:37:16 systemd[379]: Stopping c-lord.service...               ← restart しただけ
→ tmux ls: no server running(16 セッション消滅)
```

- 事前確認: `systemd-cgls --user-unit c-lord.service` に `tmux: server` が居たらアウト(#503 以降は居ない)
- 根本対処: #503(c-lord 側が cgroup 外でサーバを起こす)
- 復旧: tmux-continuum の `@continuum-restore on` が次のサーバ起動時に自動復元する。
  ただしスナップショットは 15 分間隔なので直近の作業は失われ、**復元 window と c-lord が作る window が
  二重になって #501 の発生条件を再生成する**

**2. `systemctl kill` は `--kill-whom` を省略すると user slice 全体に飛ぶ**

`systemd --user` のバスを直すため re-exec シグナルを送る場面があるが、**既定は `--kill-whom=all`**。

```bash
# ❌ user slice の全プロセスに飛ぶ(tmux・全ペインの zsh・実行中の claude・c-lord bot が即死)
sudo systemctl kill -s SIGRTMIN+25 user@1000.service

# ✅ systemd 本体だけに届く
sudo systemctl kill -s SIGRTMIN+25 --kill-whom=main user@1000.service
systemctl --user daemon-reexec        # バスが生きているならこちら
```

SIGRTMIN+25 を「再実行せよ」と解釈するのは systemd だけ。**他のプロセスにとっては未知の
リアルタイムシグナルで、既定動作は即時終了**。だから全部死ぬ。実際の journal(2026-08-07 14:26):

```
sudo: yousan : COMMAND=/usr/bin/systemctl kill -s SIGRTMIN+25 user@1000.service
systemd[1]: user@1000.service: Sent signal SIGRTMIN+25 to main process 379 (systemd)
systemd[1]: user@1000.service: Sending signal SIGRTMIN+25 to process 2039564 (tmux: server)
systemd[1]: user@1000.service: Sending signal SIGRTMIN+25 to process 1058 (python3)   ← prod bot
(以下、全ペインの zsh と claude に続く)
```

同日に `--kill-whom=main` 付きで実行したところ、tmux 16 セッションと bot は**無傷のまま**
systemd だけが再実行された。フラグ 1 つの差であることが対照実験として確認できている。

**3. `systemd-analyze --user verify` は稼働中のユーザーマネージャを壊しうる**

ユニットファイルの構文チェックのつもりで叩くと、**一時的な systemd テストインスタンスが起動**し、
稼働中のユーザーマネージャの制御ソケット `$XDG_RUNTIME_DIR/systemd/private` を張り替える。
以後 `systemctl --user` が `Failed to connect to bus: No such file or directory` になる。

inode を見ると起動時からのものと別物になっている(実測):

```
/run/user/1006/systemd/private   inode=2796       ← 起動時から(別ユーザー、正常)
/run/user/1000/bus               inode=4066       ← 起動時から
/run/user/1000/systemd/private   inode=30768266   ← verify が張り替えた
```

- サービス自体は動き続ける(マネージャは生きている)。壊れるのは **`systemctl --user` の操作系だけ**
- 復旧: 上の 2 の**正しい形**(`--kill-whom=main` 付き)を実行するか、再ログイン / 再起動
- 代替: 本番の user manager に対して `systemd-analyze verify` を使わない
  (構文チェックだけなら別ユーザーやコンテナで行う)

### 実機 tmux を触る検証は必ず `-L` で分離する (#701)

**このホストの全スレッドは 1 つの tmux サーバを共有している。** だから作業スレッド1本の
tmux 操作が、無関係な全スレッドの Claude を同時に殺せる。2026-09-08 に実際に起きた:
担当は「隔離した tmux サーバで実機検証します。生きているフリートに危険が及ばないように」と
**正しい意図を宣言した上で**、隔離になっていないコマンドを打ち、動いていた3本
(#677 / #678 / W13) が一斉に死んだ。**知識が1つ足りなかっただけ**で全滅する。

**規約**:

- **実機 tmux の検証は `tmux -L <ソケット名>` (または `-S <パス>`) で分離する。以降のコマンド全部に付ける**
- **既定ソケット (`/tmp/tmux-<uid>/default`) に対して `new-session` / `kill-server` / `kill-window` を打たない。**
  読み取り (`list-sessions` / `display-message` / `capture-pane`) は可
- 後始末も分離したソケットの中で完結させる (`tmux -L <名> kill-server`)

```bash
tmux -L i701rig new-session -d -s rig -c /tmp/i701-rig   # 別ソケット = 完全に別サーバ
tmux -L i701rig list-windows -a                           # 以降すべてに -L
tmux -L i701rig kill-server                               # 後始末もこれで完結する
```

**隔離に見えて隔離になっていないもの**(いずれも実測):

| 打ったもの | 何をするか | 分離されるか |
|---|---|---|
| `tmux -f /dev/null …` | 設定ファイルを読まないだけ | ❌ **既定ソケットのまま**。#701 の事故そのもの |
| `TMUX_TMPDIR=<dir> tmux …` | ソケット**ディレクトリ**を変える | ⚠️ **ペインの中では効かない** — `$TMUX` が勝ち、そのペインのサーバに繋がる |
| `tmux -L <名> …` / `tmux -S <パス> …` | ソケットそのものを変える | ✅ 別サーバ |

**なぜ既定ソケットへの `new-session` が危ないか**（2026-09-08 に `-L` 分離した実機リグで実測）:

- 相手サーバが**生きていれば** `new-session` は**そこに合流するだけ**（`-f /dev/null` を付けても同じ）。単体では壊さない
- 相手サーバが**応答不能なら**（ソケットが消えている/繋がらない）、tmux は**そのソケットを張り替えて新サーバを立てる**。
  このとき旧サーバはプロセスとしては生きたまま**到達不能な孤児**になり、c-lord の `has-session` は失敗して
  `w1` から作り直す — 「新規作成したのに古い窓も実在する」という一見矛盾した状態はこれ
- `kill-server` / `kill-window` は言うまでもなく直接破壊する


ペイン内から実行するときは `-L` が唯一確実。`env -u TMUX -u TMUX_PANE` を併せると
`$TMUX` 経由の取り違えも塞げる(`tests/conftest.py::_isolated_tmux_socket` が
pytest 全体に対してこれをやっている — テストは `-L` を忘れてもフリートに届かない)。

**万一やってしまったら**: 走っていたスレッドには
「⚠️ フリートの tmux が落ちたため中断しました」が出る (#701 で実装)。作業内容
(ブランチ・コミット・ワークスペース) はディスクに残っているので、各スレッドに
もう一度投げれば再開する。黙って消えたように見えても**成果は残っている**。

## supervision モデル — 誰が respawn するか (#437)

「prod に c_lord.main が **2 プロセス**見えるが二重起動か?」「`staging.sh stop` したのに復活するか?」を
切り分けるための、起動・監視の実態:

| | prod (`/home/yousan/c-lord`) | staging (`/home/yousan/c-lord-staging-N`) |
|---|---|---|
| 起動者 | **`systemd --user c-lord.service`**(`Restart=always`, `RestartSec=5`) — **これ以外から起動しない** | **`scripts/staging.sh`**(手動) |
| 起動コマンド | `uv run python -m c_lord.main`(unit の `ExecStart`) | `setsid <venv>/bin/python -m c_lord.main`(uv ラッパ無し) |
| プロセス数 | **2 が正常**: `uv` ラッパ(親) + `python` 実体(子) | **1**: `python` のみ |
| 死んだら | systemd が 5 秒後に respawn | **respawn しない**(stop したら止まったまま) |

- **prod の「2 プロセス」は二重起動ではない**。`uv run python -m c_lord.main` は uv ラッパ(親)を
  立て、その子として実体の `python` を exec する。`pgrep -f c_lord.main` は cmdline に `c_lord.main` を
  含む**両方**に当たるので 2 に見えるだけ。親子は `ps -o ppid=` で確認できる(子の ppid = 親の pid)。
  `staging.sh status` / `restart` は**この parent+child を 1 インスタンスと数える**(`instance_leaders`:
  親が同一 clone の matched pid である pid = 子 を代表から除く)。本物の二重起動(独立した 2 起動)は
  ちゃんと `instances: 2` + 終了コード 2 で検出する。
- **staging には systemd ユニットが無い**。`staging.sh` は `setsid` で venv python を直起動する
  だけなので、`stop` 後に勝手に復活する経路は無い。staging で churn(pid が数秒おきに変わる)を見たら、
  それは respawn ではなく**手動 `restart` の競合**を疑う。
- **tmux サーバは c-lord の cgroup に入らない (#504 → #503)**。c-lord は `systemd-run --user` で
  tmux サーバを別の unit に起こすので、bot(本番・staging とも)を再起動・kill しても tmux は落ちない。
  確認は `systemd-cgls --user-unit c-lord.service` に `tmux: server` が居ないこと。

## 占有(借用)プロトコル (#328)

staging は**共有リソース**。複数セッションが同時に使うと kill / checkout の踏み合いになる(2026-05-29 実害)。
占有はリースファイル(clone 直下の `.staging-lease`、環境ごとに1枚・中央台帳なし)で機械的に管理する:

```bash
cd /home/yousan/c-lord-staging-1
export CLORD_LEASE_OWNER="<自分のセッション識別子>"   # 例: claude-session-<thread_id>

bash scripts/staging.sh borrow --purpose "PR #NNN 検証" [--ttl-hours 2]
#   → 他セッションの有効リース中なら拒否され、誰が・何のために・いつまでが表示される
#   → 失効リースは奪取できる(旧リース内容がログに残る)

bash scripts/staging.sh restart <branch>   # ← 有効な自リースが無いと拒否される
bash scripts/staging.sh stop               # ← 他人の有効リース中は拒否される

bash scripts/staging.sh release            # 検証後の原状復帰とセットで必ず実行
```

**ルール**: `restart` は常に自リース必須(borrow → restart → … → restart main → release)。
`stop` はリースが無ければ可(掃除目的)、他人の有効リース中は不可(検証中の bot を殺さない)。
リースの確認だけなら `status`(lease 行に owner / purpose / TTL が出る)。

> 旧ドキュメントの「Lounge API (`/api/lounge`) で占有を宣言」は**使わない**: 占有は
> `scripts/staging.sh borrow/release` のリースが唯一の正(API は #712 以降起動しているが、
> リースの所在を2箇所に分けない)。

## 検証レシピ(RED→GREEN on staging)

**前提**(これを満たさないと curl が静かに no-op して偽 GREEN になる — #322 根因C):
- staging の `.env` に `E2E_TEST_WEBHOOK_URL` と **`E2E_TEST_THREAD_ID`** が設定されていること
- `E2E_TEST_THREAD_ID` のスレッドが staging の `sessions.db` に**セッションレコードを持つ**こと
  (持たないスレッドへの投稿は `on_message` が無視する。チャンネル直投稿も Claude を起動しない)
- 確認/再導出: `python3 -c "import sqlite3; print(sqlite3.connect('data/sessions.db').execute('select thread_id from sessions order by last_used_at desc limit 3').fetchall())"`

```bash
cd /home/yousan/c-lord-staging-1
set -a; . ./.env; set +a   # E2E_* を読み込む

# 1. RED — 修正前のコード (通常 main) で問題を再現
bash scripts/staging.sh restart main
curl -X POST -H "Content-Type: application/json" \
  -d '{"content":"<bug を再現する入力>"}' \
  "$E2E_TEST_WEBHOOK_URL?wait=true&thread_id=$E2E_TEST_THREAD_ID"
# → ログ (staging.sh status が示す per-run ログ) と Discord で症状を確認

# 2. GREEN — 修正ブランチで再現しないことを確認
bash scripts/staging.sh restart <fix-branch>
curl -X POST ...(同じ入力)

# 3. 原状復帰(必須)
bash scripts/staging.sh restart main && rm -f .staging-lease
```

証跡の規約(スクショ主・ログ従)は CLAUDE.md の DoD を参照。

## staging を増設するとき(チェックリスト)

環境が増えても `staging.sh` はそのまま使える(全値をディレクトリから導出)。増設手順:

1. Discord Developer Portal で新 bot application を作成 → token 取得(1 token = 1 接続なので既存と共用不可)
2. サーバに専用チャンネルを作成し、bot を招待(送信・スレッド権限)。チャンネルに Webhook を作成
3. `git clone` で新ディレクトリ(例 `/home/yousan/c-lord-parallel-4`)を作成、`uv sync --dev`
4. `.env` を**実ファイル**で作成(symlink 禁止 — #326)。必須: `DISCORD_BOT_TOKEN` / `DISCORD_CHANNEL_ID` /
   **`EXPECTED_BOT_USER_ID`(新 bot の user id)** / `CLORD_API_PORT`(未使用ポート、#258 で自動化予定) /
   **`CLORD_INSTANCE`(例 `staging-5`)** / `E2E_TEST_WEBHOOK_URL`
   - session dir は `SESSION_DIR_BASE` を書かなければ `~/.c-lord/<CLORD_INSTANCE>/sessions/` に作られる(#837)。
     `CLORD_INSTANCE` を書いておけば、あとで clone を改名しても置き場が変わらない(改名で `--resume` が切れない)
   - `E2E_TEST_WEBHOOK_URL` は任意。webhook を作らない場合は次の信頼bot方式で代替できる。
   - 信頼bot方式を使うなら `CLORD_TRUSTED_BOT_IDS=<prod bot user id>` も入れる(prod token 投稿でトリガー可能になる)。
5. **channel アクセス**: 新 bot に共有ロール **`c-lord-staging`** を付与(`PUT /guilds/{g}/members/{bot}/roles/{role}`)。
   非公開カテゴリでもこのロール 1 つで閲覧可になる(個別 overwrite は不要。「staging フリート」節参照)。
6. `bash scripts/staging.sh restart` → `status` で identity を確認
7. repo を bind: `/clord-init`(slash)、または `channel_repo_bindings(channel_id, source_repo=clone)` を直接 INSERT
8. E2E スレッドを 1 つ作って `sessions` 行を seed → その id を `.env` の `E2E_TEST_THREAD_ID` に追記
9. **「staging フリート」節の表に行を追加**(port = 8087 + 2×N)
10. 信頼bot方式なら prod token で E2E スレッドに投稿して RED→GREEN を確認(「他エージェントからのトリガー」節)

## トラブルシュート

| 症状 | 見る場所 | 典型原因 |
|---|---|---|
| `IDENTITY MISMATCH` で起動失敗 | per-run ログ | 意図と違う token(.env の token と EXPECTED_BOT_USER_ID の組を確認)。**ガードが正しく働いている** |
| webhook を投げても無反応 | `E2E_TEST_THREAD_ID` の sessions レコード有無 | スレッドにセッションが無い / thread_id 空(前提節を参照) |
| `instances: 2+` | `staging.sh status` | 二重起動 — `stop` → `restart`。手動 kill 禁止事項を守ったか確認 |
| 起動直後に死ぬ | per-run ログ末尾 | LoginFailure(token 不正)/ DB スキーマ不整合(古いブランチ — idle は main) |
| `API server not listening` | per-run ログの `REST API could not bind` | ポート衝突。その clone の `CLORD_API_PORT` を空き番号に(#712 以降 API は常時起動する) |
| per-run ログ末尾に `shutdown did not finish within 10s of the stop signal` と `Thread 0x…` のスタックが並ぶ | そのスタック(`Current thread` 以外で `join` / `select` / `subprocess` に居るスレッド) | 停止中に何かが返ってこなかった。bot の shutdown watchdog(#699)が全スレッドのスタックを書いて exit 70 で終わらせた — **本番は止まったままにならない**。スタックが原因の一次資料なので Issue に貼る。期限は `CLORD_SHUTDOWN_TIMEOUT_SECONDS`(既定 10、`0` で無効) |
| `restart` が `OK` まで出したのに返らない / `pgrep -af 'staging.sh restart'` に残る | その clone の `scripts/staging.sh` に `exec setsid` があるか | #401 修正前のスクリプト。旧版は bot を起動したサブシェルが bot の親として居座り、呼び出し元の stdout を握り続けた。**1 回 `restart` すると新しいスクリプトに入れ替わる**(その 1 回は旧スクリプトで走るので、出力はファイルへ向けておく)。残骸は旧 bot が止まると一緒に消える |
