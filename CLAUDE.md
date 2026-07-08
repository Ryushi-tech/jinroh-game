# CLAUDE.md — 人狼ゲーム プロジェクトガイド

このリポジトリは **スクリプトGM方式** の1人用人狼ゲームである。
ゲーム進行はすべて Python スクリプトが行い、プレイヤーはブラウザで遊ぶ。
**チャットのAI（あなた）はゲーム進行に関与しない。役割は開発のみ。**

---

## 1. アーキテクチャ

```
ブラウザUI (viewer/index.html + app.js)
    │  GET  /api/state /api/scenes /api/typing /api/hash
    │  POST /api/new_game /api/say /api/continue /api/vote /api/night_action
    ▼
viewer/server.py（ゲームサーバー・視点フィルタ）
    ▼
orchestrator.py（進行ループ・シーン生成・公開イベント記録）
    ├── engine.py（ルール・状態遷移・視点フィルタ get_player_view）
    ├── npc_agent.py（NPC発言生成: 逐次ターン制 + 再生成リトライ）
    │       └── llm_backend.py（cursor-agent / Gemini / Fake アダプタ）
    ├── validator.py（シーン検査: 死人発言・役職漏洩・フォーマット）
    └── scene_checks.py（投票整合・死者呼びかけ検出）
```

| ファイル | 役割 |
|---|---|
| `game_state.json` | ゲーム状態（単一の真実源。手動編集禁止） |
| `.gm_notes.json` | 公開CO・霊媒発表・疑惑スコア等の進行メモ |
| `characters.json` | キャラクター設定マスタ |
| `config.json` | LLMバックエンド・モデル設定 |
| `scene_*.txt` | 生成済みシーン（ビューアが表示） |
| `logs/` | debug_view.log・NPC思考ログ・autoplayレポート |

---

## 2. 起動・操作

```bash
# ゲームサーバー起動（ブラウザで http://localhost:8080）
python3 viewer/server.py

# 自動テストプレイ（LLMなし・高速）
python3 autoplay.py --runs 5

# 実LLMでの通しテスト
python3 autoplay.py --runs 1 --backend cursor

# ユニットテスト
python3 -m pytest tests/
```

LLMバックエンドは `config.json` の `backend` キーで切替（`cursor` / `gemini` / `fake`）。

- `cursor`: cursor-agent CLI が必要（`curl https://cursor.com/install -fsS | bash` → `cursor-agent login`）
- `gemini`: `.env` に `GEMINI_API_KEY` が必要

---

## 3. ゲーム仕様

役職構成・勝利条件・進行ルールは `docs/game_rules.md` を参照。
実装上の要点:

- 9人村: 村人3・占い師1・霊媒師1・狩人1 / 人狼2・狂人1
- 初日占いなし / 連続護衛禁止 / 同票はランダム決選
- 霊媒結果は自動公開されない（霊媒師COによる発表のみ）
- 襲撃死 = 人間確定。具体的役職は死亡時に公開されない
- 全役職の公開はエピローグ（勝敗確定後）のみ

---

## 4. 秘匿設計（変更時に壊さないこと）

役職情報の漏洩は「プロンプトで禁止する」のではなく「構造で遮断する」。

1. **視点フィルタ**: NPC・UIに渡る情報は必ず `engine.get_player_view()` /
   `viewer/server.py build_filtered_state()` を通す。`state["players"][*]["role"]`
   を直接外へ出すコードを書かない。
2. **公開占い結果は「宣言」ベース**: `notes["public_seer_claims"]` に記録された
   発表のみが公開情報。log の真の結果を直接ビューへ出すと偽COが構造的に
   見分けられてしまう。
3. **LLM隔離**: CursorBackend は空ディレクトリを `--workspace` に指定して呼ぶ。
   エージェントにリポジトリ（`game_state.json`）を読ませない。
4. **discussion_brief は内部専用**: `engine.discussion_brief()` の出力
   （counter_co 等）はNPC戦略指示への変換のみに使い、APIレスポンス・
   シーンテキスト・print に出さない。

---

## 5. 生成の防御設計

LLM出力の崩壊・逸脱は「必ず起きる」前提で多層防御する:

```
LLM出力 → [JSON抽出3層] → [内容検証] → [再生成リトライ(上限3)] → [validator] → シーン確定
```

- JSON抽出: `npc_agent.parse_json_bulletproof`（フェンス除去→全体→部分→regex救出）
- 内容検証: 空メッセージ・死者名混入・かぎ括弧混入は**置換せず再生成**
- リトライ超過はそのNPCのターンをスキップし、`error` として観測可能にする
- シーンは `validator.validate_file` を通過したものだけが確定する

**観測可能性**: LLMの全プロンプト・生レスポンスは `logs/debug_view.log` に、
NPC思考は `logs/npc_thoughts_day{N}_disc{M}.json` に記録される。
エラーの握りつぶし（`except: pass` / stderr破棄）は禁止。

---

## 6. 開発時の規範

- **状態変更は engine.py の関数経由**。`game_state.json` の手動編集禁止
- **テストを回してから完了とする**: `pytest tests/` と `autoplay.py --runs 2`（fake）が
  グリーンであること
- バグ調査はまず `logs/debug_view.log` と実データを確認してから仮説を立てる
- 「稀なケース」と結論する前に全ログを確認し、頻度を数値で示す
- 修正の効果はログ・テストで確認してから「修正済み」と報告する
