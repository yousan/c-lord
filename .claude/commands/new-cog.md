c-lord に新しい Cog を足してください。名前: $ARGUMENTS

進め方は `add-cog` スキル（`.claude/skills/add-cog/SKILL.md`）にあります。先に読んでから始めること。

1. 失敗するテストを `tests/test_{name}.py` に書く（TDD — `tdd` スキル）
2. Cog を `c_lord/cogs/{name}.py` に作る
3. `c_lord/setup.py` の `setup_bridge()` に登録する（利用側が何も書かずに使えるようにする）
4. `c_lord/cogs/__init__.py` と `c_lord/__init__.py` から export する
5. `/verify` を流して全部通す
