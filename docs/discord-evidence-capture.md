# Discord 実画面キャプチャ (dev/保守ツール, #243)

PR/Issue の証跡として **Discord クライアントの実際の見た目**のスクリーンショットを
AI 自身が取得するための内製ツール。テキストログでは表出しない問題
(レイアウト・ボタンの出/不出・リアクションランプ・添付の見え方) を
スクショとして残すことが目的 ([#287](https://github.com/yousan/c-lord/issues/287) P4)。

**これは c-lord 自身の開発・保守のためのツールであり、利用者に配る機能ではない。**
bot のランタイム (Cog / イベント / コマンド) には一切組み込まれていない。

## 仕組み — 役割分担

```
┌─ bot アプリ (c-lord / staging bot) ──────────────┐
│ REST API で読み取り (公認の自動化面)              │
│ メッセージ・スレッド・チャンネルの ID を特定      │
│ → https://discord.com/channels/<g>/<c>/<m> を構成 │
└──────────────┬───────────────────────────────────┘
               │ URL を渡すだけ
┌─ テストアカウント (使い捨て・通常アカウント) ────┐
│ 何もしない。ただログイン済みプロファイルが存在    │
│ Chrome が URL を開いて描画する (入力注入ゼロ)     │
└──────────────┬───────────────────────────────────┘
               │ OS レベルで撮影 (Discord から不可視)
┌─ Xvfb + ffmpeg x11grab ──────────────────────────┐
│ 仮想ディスプレイのフレームバッファ → PNG          │
└──────────────────────────────────────────────────┘
```

- **読み取り・特定 = bot** (bot アカウントの自動化は Discord が公認する面)
- **描画 = テストアカウントの素の公式クライアント** (アカウントは何の操作もしない)
- **撮影 = OS レベル** (クライアント改造なし・Discord から見えない)

## 規約面の整理 (#243 で確認済み)

Discord が禁じるのは (1) **ユーザーアカウントに自動でアクションさせること**
(self-bot) と (2) **クライアントの改造**。本ツールはどちらにも該当しない:

- アカウントは送信・リアクション・ナビゲート等のアクションを一切行わない。
  ナビゲーションは「ブラウザ起動時に URL を渡す」= 人がリンクを開くのと同じ
  クライアント操作で、ロード後の入力注入はしない
- 素の公式 Discord web をそのまま使う (改造なし)
- スクリーンキャプチャは OS の操作であり、アカウントのアクションではない

詳細な出典・判断材料は
[#243 の 2026-06-10 コメント](https://github.com/yousan/c-lord/issues/243)を参照。

## セットアップ (人間が一度だけ)

1. **使い捨てのテスト専用アカウント**を Discord で新規作成する
   (捨てアドで可。本人の常用アカウントは使わない)
2. 撮影対象のサーバ (staging サーバ等) にそのアカウントを招待し、
   **閲覧 + メッセージ履歴**の読み取り権限だけ与える (送信権限も不要)
3. bot ホスト上で専用プロファイルにログインする (WSLg 環境なら
   ウィンドウが Windows デスクトップに出る):

   ```bash
   mkdir -p ~/.clord && chmod 700 ~/.clord
   google-chrome --user-data-dir=$HOME/.clord/discord-evidence-profile \
     "https://discord.com/login"
   ```

   テストアカウントでログインし、サーバが見えたらウィンドウを閉じる。
   セッショントークンがプロファイルに保存され、以降の撮影はすべて無人で動く。

## 使い方

```bash
# チャンネルの現在の画面
scripts/discord_evidence_shot.sh \
  "https://discord.com/channels/<guild_id>/<channel_id>" -o evidence.png

# 特定メッセージ周辺 (メッセージリンクを渡すとクライアントがそこへジャンプする)
scripts/discord_evidence_shot.sh \
  "https://discord.com/channels/<guild_id>/<channel_id>/<message_id>" -o evidence.png
```

URL は Discord 上でチャンネル/メッセージを右クリック → 「リンクをコピー」、
または bot の REST API で ID を特定して組み立てる。

## 証跡の置き場の規約 (#390)

**原則: 証跡 PNG は git ツリーに commit しない。GitHub Release アセットに
アップロードして URL を参照する。** バイナリを source repo に貯めると履歴が
不可逆に膨らむため。業界一般でも「使い捨てスクショは off-repo に置き PR には
リンクを載せる」が定石 (visual regression の Percy / Chromatic / Argos も
画像は自前ストレージ + PR にリンク)。GitHub にアップロードされた画像は
期限切れしない。

- **アップロード**: `scripts/evidence_upload.py` を使う。

  ```bash
  # 撮る → 上げる
  scripts/discord_evidence_shot.sh "<thread URL>" -o red.png
  scripts/discord_evidence_shot.sh "<thread URL>" -o green.png
  scripts/evidence_upload.py red.png green.png --issue 390
  # → 各 browser_download_url と貼り付け用 markdown が stdout に出るので
  #    その URL / ![...](url) を PR・Issue 本文に貼る
  ```

  画像は専用の **prerelease タグ `evidence`** にアセットとして載る
  (`https://github.com/<owner>/<repo>/releases/download/evidence/<name>`)。
  prerelease なので `releases/latest` には出ず、auto-upgrade を妨げない。

- **証跡は催促される前に貼る (必須)**。「言われたら付ける」ではなく、挙動/UI を
  変える PR・Issue には最初から RED / GREEN (前/後) の両方を貼る。
  **`dod-gate` CI は、免除ラベル (`no-runtime-change` / `documentation`) の無い PR の
  本文に証跡画像 (`![...](URL)`) も Release アセット URL も無ければ落とす (#391)。**
- **やってはいけない**:
  - PNG を `docs/evidence/<issue番号>/` 等に commit する (旧規約。既存の
    commit 済み PNG は過去 PR のリンク保全のため残すが、新規は作らない)。
  - **Discord CDN の添付 URL (`cdn.discordapp.com/attachments/...`) を
    本文に直貼りする** — 2024 年以降は期限付き署名 URL で数日で 404 になる。
    Discord 由来の画像は一度ローカルに落として上の script で上げ直すこと。
- **bot から user-attachments CDN は使えない**: ドラッグ&ドロップで付く
  `github.com/user-attachments/...` への自動アップロードは公式 API が無く
  (Web UI はブラウザ Cookie 必須)、headless な bot からは扱えない。だから
  Release アセットを使う。
- **yousan 提供のスクショ**は Issue にそのまま添付で良い (GitHub に上がった
  画像は期限なし)。

## 制約

- 撮れるのは**テストアカウントが参加しているサーバ/チャンネルだけ**。
  新しいサーバを撮影対象にするには人間が一度招待する
- 撮れるのは「リンク位置周辺のビューポート 1 画面」。長い流れは
  メッセージリンクを変えて複数枚撮る (スクロール注入はしない)
- セッションはいつか失効する。**キャプチャにログイン画面が写っていたら失効**
  → 人間がセットアップ手順 3 を再実行する (数ヶ月に一度程度の想定)
- 同一プロファイルで Chrome は 1 つしか起動できない。スクリプトが
  使用中チェックで直列化する
- Discord の販促モーダル(「ショップ新着」等)がテストアカウントに出ることがある。
  スクリプトは入力注入をしない設計なので**閉じるボタンを自動では押せない**。
  モーダルは常に**画面中央に固定サイズ**で出るので、`--size` を縦に大きく
  伸ばす(例: `--size 900x2600`)と、モーダルの上下にはみ出た部分に地の
  メッセージが(暗くはなるが)読める程度に写ることがある — 撮りたいメッセージが
  たまたまその範囲に来るよう調整すれば、モーダルを閉じずに撮れる場合がある。
  それでも証跡の可読性が足りなければ、自動化で粘らず人間に一度モーダルを
  閉じてもらう(#492 PR #494 で発見)。

## セキュリティ

- プロファイル (`~/.clord/discord-evidence-profile`) には**テストアカウントの
  セッショントークン**が入っている。漏れたらそのアカウントは乗っ取られ得る。
  だからこそ**使い捨て・最小権限・staging のみ参加**にして、漏洩時の損失を
  ゼロに設計する。**本人の常用アカウントでのログインは絶対にしない**
- スクリプトは URL を `discord.com/channels/` に制限している。
  ログイン済みブラウザを任意サイトに向ける汎用ツールとして使えないようにするため
- パスワードはどこにも保存しない (ログイン時に人間が手入力するだけ)
