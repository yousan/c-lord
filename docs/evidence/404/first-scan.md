# #404 monitor 初回スキャン — 実ログから本物の不具合を検出（方向Aの検証）

ファザー(#377)は11回走って0件。一方この monitor は**実 staging+prod ログを1回スキャンしただけ**で
以下を検出。狙いどおり「実トラフィックには実バグが出ている」ことを実証。

```
実行: python -m scripts.monitor --dry-run --logs '/tmp/clord-bot-c-lord-staging-{1..4}.log,/tmp/clord-bot-c-lord.log'

total: 4 / by kind: {'LOG_ERROR': 3, 'LOG_TRACEBACK': 1}

- LOG_ERROR | clord-bot-c-lord-staging-4-20260612-085705.log | thread= 1514546025631580260
   evidence: discord.ext.commands.bot: Ignoring exception in command None
- LOG_ERROR | clord-bot-c-lord-staging-4-20260612-085705.log | thread= 1514546025631580260
   evidence: discord.ext.commands.bot: Ignoring exception in command None
- LOG_ERROR | clord-bot-c-lord-20260612-103154.log | thread= 1514462096601907331
   evidence: c_lord.cogs._run_helper: [ ] Error running Claude CLI
- LOG_TRACEBACK | clord-bot-c-lord-20260612-103154.log | thread= 1514462096601907331
   evidence: KeyError: <id>
```

## 最重要: prod bot の未処理 traceback（KeyError, tmux capture 経路）

これは診断の核心を裏付ける — 実バグは text→reply ではなく **tmux イベント経路**に出る:

```
Traceback (most recent call last):
  File "/home/yousan/c-lord/c_lord/cogs/_run_helper.py", line 346, in run_claude_with_config
    async for event in runner.run(config.prompt, session_id=config.session_id):
  File "/home/yousan/c-lord/c_lord/claude/tmux_runner.py", line 836, in run
    raw_current = await asyncio.to_thread(self._tmux.capture_pane, self._thread_id)
                  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/lib/python3.12/asyncio/threads.py", line 25, in to_thread
    return await loop.run_in_executor(None, func_call)
           ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/usr/lib/python3.12/concurrent/futures/thread.py", line 58, in run
    result = self.fn(*self.args, **self.kwargs)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/yousan/c-lord/c_lord/tmux.py", line 1074, in capture_pane
    window = self._find_window_for_thread(thread_id)
             ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "/home/yousan/c-lord/c_lord/tmux.py", line 222, in _find_window_for_thread
    del self._thread_to_window[thread_id]
        ~~~~~~~~~~~~~~~~~~~~~~^^^^^^^^^^^
KeyError: 1514462096601907331
```
