# Staging Evidence — #88 (PR #303)

staging: `c-lord-parallel-3` / channel `1503196656265597082` / 2026-06-11 / branch `fix/88-url-normalize`

`!clord-init https://github.com/yousan/c-lord/pull/2` を webhook 経由で投げ、bot の Bound 返信を比較。

- **RED** (`main`): `red-bound-raw-pr-url.png` — `Bound … → https://github.com/yousan/c-lord/pull/2`（PR URL がそのまま bind される）
- **GREEN** (`fix/88-url-normalize`): `green-normalized.png` — `Bound … → https://github.com/yousan/c-lord.git`（owner/repo.git に正規化）
