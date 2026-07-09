# 変更記録: LLM関与削減・5人村・プレイテスト修正（2026-07-09）

**基準コミット**: `fa60930` — スクリプトGM v2 全面改装  
**本記録の状態**: 開発凍結時点（Fable API 復旧待ち）  
**テスト**: `pytest tests/` 80件パス / `autoplay.py --runs 2`（FakeBackend）グリーン

---

## 1. 背景と経緯

### 1.1 きっかけ

v2 改装（`fa60930`）後、実 LLM（Anthropic Sonnet）でのプレイテストと辛口レビューを実施。
以下の問題が構造的に露呈した。

| カテゴリ | 症状 |
|----------|------|
| 秘匿 | 未発表占い結果が `_confirmed_info` 経由で神視点リーク |
| 投票 | 得票数表示で「誰が誰に入れたか」が分からず誤認 |
| NPC戦略 | 狂人が推定狼に投票、真占いが自黒を無視、プレイヤーが疑惑対象外 |
| LLM依存 | 発言内容・疑惑・投票理由を LLM に任せ、逸脱は事後検証で却下 |
| UX | プレイヤー発言のたびに新議論ラウンドが自動開始し同内容を反復 |
| 初日投票 | CO していないグレー村人が「消去法」で全員票（4票）される |
| エンド | 勝利確定後も `phase` が `night` のまま / エピローグで役職自白 |

### 1.2 方針転換: 「LLMの関与を口調の翻訳に限定」

```
旧: 盤面+会話 → LLMが内容から考える → 事後検証で却下
新: speech_planner が発言計画（疑い先・根拠・論点）を機械決定
    → LLMは口調への翻訳のみ（render_speech）
    → 失敗時は plan["fallback_text"] をそのまま使用
```

この方針に沿い、疑惑スコア・発言プランナー・レンダラー・orchestrator 統合を段階的に実装した。

### 1.3 凍結理由

プレイテストで「初日グレー全員票」「議論ループ」等の設計バグは修正したが、
全体として人間が納得できる議論・投票の質には至らず、Fable API 復旧後に再開する判断。
**本ブランチ以降の作業は意図的にここで止めている。**

---

## 2. アーキテクチャ変更（概要）

```
orchestrator.py
    ├── engine.py          … ルール・疑惑スコア・投票・エピローグ凍結
    ├── speech_planner.py  … 【新規】発言内容の機械決定
    ├── npc_agent.py       … render_speech（口調変換）+ 旧 generate_npc_message
    └── llm_backend.py     … Anthropic バックエンド追加
```

### 2.1 新規: `speech_planner.py`

公開 API:

- `build_speech_plan(...)` — act 列（`text_jp`）・`must_mention`・`fallback_text`・`topic_keys`
- `mark_topics_spoken(...)` — 論点の重複防止（`notes["spoken_topics"]`）
- `record_speech_memory(...)` — 疑い先・投票意思の日次メモ

内部ロジック:

- 論点: 占い衝突 / 黒宣言 / 対抗占いCO / 単独投票 等を `_gather_topics` で収集
- 疑い先: `_choose_accuse_target`（役職知識 + `compute_npc_suspicion`）
- 根拠: `_grounded_reason`（公開台帳のみ）
- プレイヤー質問: 占い先を聞かれたら `_player_asks_seer_target` → `answer_seer_target`

### 2.2 `npc_agent.py`

- **`render_speech`**: プランの `text_jp` をキャラ口調に変換。盤面全文は渡さない
- **`generate_npc_message`**: 後方互換用に残存（通常 UI 進行では未使用）
- 検証強化: `check_co_misattribution`, `check_day1_seer_result_demand`, `dead_as_alive_check`
- **`run_discussion_round`**: 削除（`npc_speak_one` 経路に統一）

### 2.3 `orchestrator.py`

- `npc_speak_one`: `build_speech_plan` → `render_speech` → 失敗時 `fallback_text`
- `_vote_declaration`: 理由は `_grounded_reason`、LLM は口調のみ
- `player_say`: NPC 待ちが空でも **新 disc を作らず** 現シーンへ追記
- `MAX_DISC_ROUNDS_PER_DAY = 3`: 1日の議論ラウンド上限
- `epilogue`: 幕引きは `format_epilogue_scene`（テンプレ固定）、感想戦のみ LLM

### 2.4 `engine.py`（主要追加・変更）

| 項目 | 内容 |
|------|------|
| `ROLE_COMPOSITION` | 9人村 → **5人村**（村人2・占い1 / 狼1・狂人1） |
| `compute_npc_suspicion` | LLM 不使用の決定的疑惑スコア（rater 別 + avg） |
| `_confirmed_info` | 公開台帳ベースに修正（未発表 log 結果を出さない） |
| `decide_npc_votes` | 狂人 PP・真占い自黒優先・狼の潜伏投票 |
| `compute_vote_plan` | 初日・占い師 CO 対抗時は **グレーより CO 者** を村合意先に |
| `finalize_game` / `format_epilogue_scene` | 勝利後 `phase=epilogue`、幕引きテンプレ |
| `resolve_night` | 決着時は日付を進めない |
| 投票表示 | 個票（`voter → target`）形式 |

### 2.5 `llm_backend.py`

- **`AnthropicBackend`** 追加（`config.json` の `backend: anthropic` が既定）
- プロンプトキャッシュ対応（`cache` / `cache_ttl`）
- `FakeBackend` は autoplay・単体テスト用

### 2.6 `viewer/`

- CO / 結果発表プルダウン（テンプレ挿入 + 台帳直接記録）
- NPC 逐次発言（`/api/npc_speak` mode=one|all）
- 議論シーンのポーリング再取得
- `can_start_new_disc` / `max_disc_rounds` UI ヒント
- 投票履歴を個票表示

### 2.7 `validator.py` / `scene_checks.py`

- エピローグ幕引きにキャラ発言（`名前「」`）が混入したらエラー
- 投票宣言と実投票の整合チェック強化

---

## 3. プレイテストで発見・修正した具体例

### 3.1 初日グレー全員票（リーザ 4票）

**原因**: `speech_planner` が「CO も情報も出していないグレー」をデフォルト最疑わしいと判定。
占い師 CO 対抗が始まっているのに、両占い師がグレー村人を疑い、
`compute_vote_plan` も疑惑スコア最大のグレーを選んでいた。

**修正**:
- 初日・占い師 CO が2人以上いる場合、村合意先を CO 者に限定
- 真占い・狂人・村人の `_choose_accuse_target` も初日は CO 対抗を優先
- 初日の `_grounded_reason` でグレー消去法の文言を弱める

### 3.2 議論ループ（disc5, disc6…）

**原因**: `player_say` が NPC 待ち空のたびに `force_new=True` で新 disc を作成。
全 NPC が毎ラウンド同じ `accuse` + `vote_intent` を生成。

**修正**:
- 待ち空時は現シーンへ追記のみ
- 1日3ラウンド上限
- `last_accuse` / `vote_intent_stated` / `spoken_topics` で重複抑制
- 占い先質問への構造化応答

### 3.3 勝利後 phase が night のまま

**修正**: `engine.finalize_game()` で `phase = epilogue`

### 3.4 エピローグ役職漏洩

**修正**: `scene_epilogue.txt` を LLM なしテンプレに。感想戦のみ LLM（`validate_as=epilogue_thread`）

### 3.5 投票宣言「昨日〜」が初日に出る

**修正**: `_previous_execute` で当日より前の処刑のみ参照

---

## 4. テスト

`tests/test_game_integrity.py` を中心に **60件 → 80件** に拡張。

主な追加グループ:

- 公開占い台帳・視点フィルタ整合
- CO/結果テンプレ・台帳記録
- 狂人 PP・真占い投票・プレイヤー疑惑対象
- `compute_npc_suspicion` 決定性
- `speech_planner` + `render_speech` + フォールバック
- 初日投票計画・議論ラウンド制限・エピローグ phase

---

## 5. 変更ファイル一覧（fa60930 からの diff）

| ファイル | 概要 |
|----------|------|
| `speech_planner.py` | **新規** — 発言プランナー |
| `engine.py` | 5人村・疑惑スコア・投票・エピローグ・秘匿修正 |
| `orchestrator.py` | プランナー統合・議論制限・CO/結果テンプレ |
| `npc_agent.py` | render_speech・検証強化 |
| `llm_backend.py` | Anthropic バックエンド |
| `viewer/server.py`, `app.js`, `index.html` | 対話 UI 拡張 |
| `validator.py`, `scene_checks.py` | 検査強化 |
| `tests/test_game_integrity.py` | 大幅追加 |
| `tests/test_view_filter.py` | 5人村対応 |
| `config.json` | anthropic 既定 |
| `requirements.txt` | anthropic SDK |
| `docs/game_rules.md` | 5人村ルール |
| `CLAUDE.md` | アーキテクチャ・防御設計更新 |
| `autoplay.py` | 軽微 |

**統計**: 16ファイル変更 + 1新規、+3392 / -524 行（`git diff fa60930 --stat`）

---

## 6. 未完了・凍結時点の既知課題

再開時に優先検討:

1. **議論の人間らしさ** — 機械プラン + 口調変換だけでは単調。Fable 等での品質再評価が必要
2. **`generate_npc_message` 経路** — コード上は残存するが通常未使用。削除か完全統一
3. **狂人・狼の中長期戦略** — PP タイミング・偽 CO の結果捏造の自然さ
4. **プレイヤーへの説明** — 議論上限・投票タイミングの UI ガイド
5. **実 LLM コスト** — Anthropic キャッシュ効果の実測（`logs/debug_view.log`）

---

## 7. 再開時のクイックスタート

```bash
python3 -m pytest tests/          # 80件
python3 autoplay.py --runs 3      # FakeBackend 通し
python3 viewer/server.py          # http://localhost:8080
```

LLM バックエンド: `config.json` の `backend`（`anthropic` / `cursor` / `gemini` / `fake`）

---

*記録作成: 2026-07-09（開発凍結コミット用）*
