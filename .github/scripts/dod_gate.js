'use strict';

// DoD Gate のロジック本体（テスト可能にするため dod-gate.yml から切り出した純関数）。
// 層1: PR 本文の機械パースによる Definition of Done ゲート。構造的に検出可能な漏れだけを
// 決定論的に止める。真偽（本当に動作確認したか）は捕まえられない。詳細: yousan/c-lord#128
//
// evaluateDod({ body, labels }) -> { errors: string[] }  ... 純関数（I/O 無し）。
// CLI モード（直接実行）では GITHUB_EVENT_PATH の PR 本文/ラベルを読み、errors があれば exit 1。

const fs = require('fs');

function evaluateDod({ body = '', labels = [] } = {}) {
  const errors = [];
  const lower = (labels || []).map((l) => String(l).toLowerCase());
  // documentation / no-runtime-change ラベル付き PR は Rule 1（DoD 完了要件）を免除。
  // Rule 2（Closes 規律）は常に適用。
  const ruleOneExempt = lower.includes('documentation') || lower.includes('no-runtime-change');

  // 指定見出し (## <heading>) のセクション本文を返す。見つからなければ空文字。
  const section = (heading) => {
    const re = new RegExp(`##\\s*${heading}[\\s\\S]*?(?=\\n##\\s|$)`, 'i');
    const m = body.match(re);
    return m ? m[0] : '';
  };
  const countUnchecked = (text) => (text.match(/- \[ \]/g) || []).length;

  // 証跡シグナル (#391): markdown 画像 / <img> / Release アセット URL /
  // GitHub user-attachments URL / 画像拡張子つき URL。
  const EVIDENCE_RE =
    /!\[[^\]]*\]\([^)]+\)|<img\s|https?:\/\/\S+\/releases\/download\/\S+|https?:\/\/\S*user-attachments\/\S+|https?:\/\/\S+\.(?:png|jpe?g|gif|svg|webp)(?:[?#]\S*)?/i;

  const dodSection = section('Definition of Done checklist');
  const acSection = section('Acceptance Criteria');

  // テンプレ未使用（DoD セクションが無い）も落とす。
  if (!dodSection) {
    errors.push('PR本文に『Definition of Done checklist』セクションが無い。PRテンプレートを使うこと。');
  }

  // Rule 1: DoD チェックリストが全部チェック済みか（免除ラベルがあればスキップ）。
  if (!ruleOneExempt) {
    const n = countUnchecked(dodSection);
    if (n > 0) {
      errors.push(`DoDチェックリストに未チェック項目が ${n} 件。全部 [x] になるまでマージ不可。`);
    }
  }

  // Rule 2: Closes/Fixes/Resolves を使うなら AC は全達成（未チェック AC 禁止）。
  const usesClose = /\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#\d+/i.test(body);
  if (usesClose) {
    if (!acSection) {
      // fail-closed (#265): '## Acceptance Criteria' セクションが見つからない。見出しが
      // ### AC など h3 やリネームだと acSection='' で未チェック数 0 となり、AC 検証ゼロで
      // 素通りしていた。Closes を使うなら AC セクションの存在自体を必須にする。
      errors.push(
        '`Closes`/`Resolves #N` を使うなら `## Acceptance Criteria` セクション（`##` 見出し）が必須。' +
          '見出しが見つからない（`### AC` など h3 やリネームは不可）。' +
          'Issue の AC を貼って全項目チェックするか、`Refs #N` に変えること。'
      );
    } else {
      const acUnchecked = countUnchecked(acSection);
      if (acUnchecked > 0) {
        errors.push(
          `Closes/Fixes を使っているが Acceptance Criteria に未達(${acUnchecked}件)がある。` +
            '100%満たすか、満たさないなら "Refs #N" に変えて Issue を開いたままにすること。'
        );
      }
    }
  }

  // Rule 3 (#391): 挙動/UI を変える PR は証跡（画像 or Release アセット URL）が本文に必須。
  // 免除ラベル（documentation / no-runtime-change）があればスキップ。証跡を「言われる前に
  // 貼る」を機械的に強制する層。テキストログだけ（画像/URL 無し）は素通りさせない。
  if (!ruleOneExempt && !EVIDENCE_RE.test(body)) {
    errors.push(
      '証跡が見つからない。挙動/UI を変える PR は本文に証跡画像（`![...](URL)`）か ' +
        'Release アセット URL（`scripts/evidence_upload.py` で生成）を貼ること。' +
        '純リファクタ/CI/docs で証跡が不要なら `no-runtime-change` か `documentation` ラベルを付ける。'
    );
  }

  return { errors };
}

module.exports = { evaluateDod };

// --- CLI モード: GitHub Actions の pull_request イベントから body/labels を読む ---
if (require.main === module) {
  const eventPath = process.env.GITHUB_EVENT_PATH;
  let body = '';
  let labels = [];
  if (eventPath && fs.existsSync(eventPath)) {
    const event = JSON.parse(fs.readFileSync(eventPath, 'utf8'));
    const pr = event.pull_request || {};
    body = pr.body || '';
    labels = (pr.labels || []).map((l) => l.name);
  }
  const { errors } = evaluateDod({ body, labels });
  if (errors.length) {
    console.error('DoD Gate 失敗:\n- ' + errors.join('\n- '));
    process.exit(1);
  }
  console.log('DoD Gate 通過');
}
