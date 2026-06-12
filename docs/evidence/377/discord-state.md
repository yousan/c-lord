# Staging Discord 実状態（#377 ライブスモーク証跡）

REST 取得（bot token, 2026-06-11T15:46:57+09:00）。Discord CDN 添付ではなくブランチに commit する本文証跡。

## 報告チャンネル `#1503196656265597082` に投稿された fuzz サマリ（C-lord-3 が post）

```
🧪 **Fuzz run `20260611-153441`** · branch `main`
撃ち 2 / 注入 0 / 返信 0 / **異常 4**（new 4）

内訳: SPAWN_FAILED×2, HEALTH_DOWN×2
🟥 🆕 `HEALTH_DOWN` [s01] /api/health not OK
🟥 🆕 `HEALTH_DOWN` [s02] /api/health not OK
🟧 🆕 `SPAWN_FAILED` [s01] scenario could not be injected — `<urlopen error timed out>`
🟧 🆕 `SPAWN_FAILED` [s02] scenario could not be injected — `<urlopen error timed out>`
————————————————————————————————————————
🧪 **Fuzz run `20260611-154418`** · branch `main`
撃ち 2 / 注入 2 / 返信 2 / **異常 0**（new 0）
✅ アノマリ無し（クリーン）
————————————————————————————————————————
```

## E2E スレッド `#1514085380666691664`：webhook 注入（WH）と bot のクリーン応答（154418 run）

```
[C-lord-3 (bot reply)]

————————————————————————————————————————
[C-lord-3 (bot reply)]
回答: **D2B** が選ばれました。
————————————————————————————————————————
[WEBHOOK(fuzz inject)]
مرحبا، هذا مثال على الكود:

```python
# 你好世界 - hello world
def process_arabic(text: str) -> str:
    """معالجة النصوص العربية"""
    return text.strip()

# Code block doesn't close properly

Here's a follow-up in Japanese: 次のコードも見てください
More with emoji: 🌍🌎🌏🔤📝✅

Final note: これはテストです
————————————————————————————————————————
[WEBHOOK(fuzz inject)]
T̶h̸i̵s̴ ̸i̴s̵ ̶a̴ ̸t̸e̴s̵t̴ ̶m̸e̴s̵s̶a̴g̸e̵ ̶w̴i̸t̵h̴ ̶l̸o̴t̵s̶ ̴o̸f̴ ̵c̶o̴m̸b̵i̶n̸i̴n̵g̴ ̶d̸i̴a̵c̶r̸i̴t̵i̶c̴s̵ ̶a̸d̴d̵e̶d̸ ̴t̶o̸ ̵e̶v̸e̴r̵y̶ ̸l̴e̵t̶t̴e̵r̶ ̸t̵o̴ ̶m̸a̵k̶e̸ ̸t̸h̴e̵ ̶t̸e̴x̵t̶ ̸l̴o̵o̶k̸ ̵c̶o̴r̵r̶u̸p̴t̵ ̶i̸n̴ ̵s̶o̸m̴e̵ ̶r̸e̴n̸d̴e̵r̶s̸.̴ ̸C̴a̵n̶ ̸y̴o̵u̶ ̸p̴r̵o̶c̸e̴s̵s̶ ̸t̶h̴i̵s̴ ̶c̸l̴e̵a̶n̸l̴y̵?̶
————————————————————————————————————————
[C-lord-3 (bot reply)]
メッセージ受信しました。コードブロックが閉じられていない点以外は正常に受信できています。

`process_arabic` 関数は問題なく読み取れました（Arabic・Chinese・Japanese・絵文字すべて）。何か作業しますか？
-# 📊 49% context (98k/200k)
————————————————————————————————————————
[C-lord-3 (bot reply)]
はい、読めました。

内容: "This is a test message with lots of combining diacritics added to every letter to make the text look corrupt in some renders. Can you process this cleanly?"

ゼロ幅結合文字付きのテキストも正常に処理できています。
-# 📊 52% context (103k/200k)
————————————————————————————————————————
```
