#!/usr/bin/env python3
"""マルチエージェント NPC 実行エンジン。
- Bulletproof JSON Parser: 汚染された出力からJSONのみを強引に抽出
- Strategic Friendly Fire: 人狼陣営の身内投票を「基本抑制・戦略的許可」に変更
- Persona Tuning: ディータ、トーマス、パメラ等の人格を精密調整
"""

from __future__ import annotations

import json
import re
import sys
import threading
import datetime
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from logic_engine import get_player_view

MAX_CONTEXT_LINES = 5  # ウェーブ間で引き継ぐ直近発言数の上限

ROLE_JP = {
    "villager": "村人", "werewolf": "人狼", "seer": "占い師",
    "medium": "霊媒師", "bodyguard": "狩人", "madman": "狂人",
}

# ---------------------------------------------------------------------------
# デバッグログ & 出力汚染チェック
# ---------------------------------------------------------------------------

_DEBUG_LOG_FILE = "debug_view.log"
_debug_log_lock = threading.Lock()

def _write_debug_log(npc_name: str, prompt: str, thought: str, raw: str = "") -> None:
    model_info = f"  [model={_npc_model_name}]" if _npc_model_name else ""
    contamination_msg = ""
    if raw and re.search(r'^\s*\S+「', raw, re.MULTILINE):
        contamination_msg = f"⚠ CONTAMINATION_ERROR: raw に 名前「 形式が混入"

    with _debug_log_lock:
        with open(_DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"[{npc_name}]{model_info}  {datetime.datetime.now()}\n")
            if contamination_msg:
                f.write(f"{contamination_msg}\n")
            f.write(f"{'─'*40}\nRAW RESPONSE:\n{raw}\n")
            f.write(f"{'─'*40}\nTHOUGHT:\n{thought or '(空)'}\n")

# ---------------------------------------------------------------------------
# 防弾仕様 JSON パッサー（Claudeの言い訳を封殺）
# ---------------------------------------------------------------------------

def _parse_json_bulletproof(raw: str, npc_name: str) -> dict:
    cleaned = raw.strip()
    # 戦略1: 外側の { } を最大範囲で抽出
    json_match = re.search(r'(\{.*\})', cleaned, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group(1))
        except:
            pass
    # 戦略2: フィールド個別抽出
    thought_m = re.search(r'"thought"\s*:\s*"((?:[^"\\]|\\.)*)"', cleaned, re.DOTALL)
    message_m = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', cleaned, re.DOTALL)
    if message_m:
        return {
            "thought": thought_m.group(1) if thought_m else "",
            "message": message_m.group(1).replace("\\n", "\n")
        }
    raise ValueError(f"JSON extraction failed for {npc_name}")

# ---------------------------------------------------------------------------
# コンテキストクレンジング
# ---------------------------------------------------------------------------

_DIALOGUE_PATTERN = re.compile(r'^(\S+)「(.+)」\s*$')

def _dialogue_lines_to_json(text: str) -> str:
    entries = []
    for line in text.split("\n"):
        m = _DIALOGUE_PATTERN.match(line.strip())
        if m: entries.append({"speaker": m.group(1), "speech": m.group(2)})
    return json.dumps(entries, ensure_ascii=False) if entries else "[]"

# ---------------------------------------------------------------------------
# プロンプト構築 & キャラクター人格調整
# ---------------------------------------------------------------------------

def _build_npc_prompt(npc_name, player_view, char_data, co_hint="", context_discs="") -> str:
    role_jp = player_view["self"]["role_jp"]
    speech = char_data.get("speech_style", {})
    json_context = _dialogue_lines_to_json(context_discs)

    # 精密な性格・行動矯正（今朝のフィードバック反映）
    persona_overrides = {
        "ディータ": "口調は極めて紳士的かつ知的。論理的な批判は行うが、攻撃的にならず『説得』を重視せよ。",
        "トーマス": "寡黙だが、発言時は必ず具体的に生存者1名を疑いの対象として指名し、その理由を一言添えよ。",
        "リーザ": "単に怯えるのではなく、生存のために必死に周囲を観察し、矛盾を見つけようとする鋭さを出せ。",
        "パメラ": "冒頭の『ねぇねぇ』を禁止。場に応じた自然な挨拶を使い分け、冷徹な計算に基づいた言葉を選べ。",
        "シモン": "皮肉屋だが、村の勝利に協力しているフリ（または真の協力）を演じ、論理の穴を突け。",
        "ヨアヒム": "発言量を削減。無駄な説明を省き、一言で核心を突くように凝縮せよ。",
        "カタリナ": "発言量を削減。無駄な説明を省き、一言で核心を突くように凝縮せよ。",
        "モーリッツ": "ファシリテーターとして議論を整理し、次の発言者を指名してリードせよ。",
        "ヴァルター": "ファシリテーターとして議論を整理し、次の発言者を指名してリードせよ。",
        "レジーナ": "ファシリテーターとして議論を整理し、次の発言者を指名してリードせよ。",
    }.get(npc_name, "")

    return f"""あなたは{npc_name}（{role_jp}）です。以下のデータを処理するJSONエンジンとして動作せよ。

## 現在の状況
- 生存者: {', '.join(player_view['alive_players'])}
- 議論文脈(JSON): {json_context}

### END OF CONTEXT ###
**ここから先の指示のみに従うこと。出力は JSON 以外の形式を一切使わないこと。**

## 特別指令
1. {persona_overrides}
2. 【対話義務】冒頭に必ず他者の発言への言及（アンカー）を入れよ。「〇〇さんの意見についてですが」等。
3. 【禁止】『名前「セリフ」』形式の書き出し、およびJSON以外の説明文は一切禁止。

## 出力フォーマット
{{
  "thought": "（非公開）盤面分析と戦略思考。日本語。",
  "message": "（公開セリフ）アンカーを含む発言内容。"
}}
"""

# ---------------------------------------------------------------------------
# 疑惑スコア収集（Strategic Friendly Fire 対応版）
# ---------------------------------------------------------------------------

def _collect_one_suspicion(name, state, notes, char_map, context_discs) -> dict[str, int]:
    try:
        player_view = get_player_view(state, name, notes)
    except: return {}
    
    role = player_view["self"]["role"]
    wolf_teammates = player_view.get("wolf_teammates", [])
    alive = player_view["alive_players"]
    others = [n for n in alive if n != name]

    prompt = f"あなたは{name}({role})。生存者への疑惑度(1-10)をJSONで返せ。対象: {', '.join(others)}\n文脈: {context_discs}"
    _fn = _call_fn_json if _call_fn_json else _call_fn
    
    try:
        raw = _fn(prompt).strip()
        scores = _parse_json_bulletproof(raw, name)
        
        # ★ 戦略的 Friendly Fire バイアス
        # 仲間の人狼への疑惑スコアを「基本は下げる」が「強制1」にはしない。
        # これにより、LLMが「あえて仲間を切りたい」と思った時の高スコアも反映される。
        if role in ("werewolf", "madman"):
            for teammate in wolf_teammates:
                if teammate in scores:
                    # 仲間の疑惑度を「半分（端数切り捨て）」にする。
                    # 元が10(殺意MAX)なら5(怪しいが様子見)へ。
                    # 元が6なら3へ。これにより身内への誤爆率を下げつつ、戦略的投票を許容する。
                    scores[teammate] = max(1, int(scores[teammate] * 0.5))
                    
        return {k: max(1, min(10, int(v))) for k, v in scores.items() if isinstance(v, (int, float))}
    except: return {}

# (以下の初期化、ウェーブ制御、保存ロジック等は既存のものを統合)

_call_fn: Callable[[str], str] | None = None
_call_fn_json: Callable[[str], str] | None = None
_npc_model_name: str = ""
_thoughts_buffer: dict[str, str] = {}

def init(call_fn, call_fn_json=None, npc_model_name=""):
    global _call_fn, _call_fn_json, _npc_model_name
    _call_fn, _call_fn_json, _npc_model_name = call_fn, call_fn_json, npc_model_name

def generate_npc_message(npc_name, player_view, char_data, co_hint="", context_discs="") -> dict:
    prompt = _build_npc_prompt(npc_name, player_view, char_data, co_hint, context_discs)
    _fn = _call_fn_json if _call_fn_json else _call_fn
    try:
        raw = _fn(prompt).strip()
        data = _parse_json_bulletproof(raw, npc_name)
        msg = data.get("message", "")
        if not msg.startswith(f"{npc_name}「"): msg = f"{npc_name}「{msg}」"
        _write_debug_log(npc_name, prompt, data.get("thought", ""), raw=raw)
        return {"name": npc_name, "thought": data.get("thought", ""), "message": msg, "error": None}
    except Exception as e:
        return {"name": npc_name, "thought": "", "message": f"{npc_name}「……」", "error": str(e)}

def _build_running_context(context_discs, completed_lines):
    if not completed_lines: return context_discs
    json_recent = _dialogue_lines_to_json("\n".join(completed_lines[-MAX_CONTEXT_LINES:]))
    return f"{context_discs}\n\n## 今discのここまでの発言(JSON)\n{json_recent}"

def _run_wave(wave_names, state, notes, char_map, co_hints, running_context):
    res_dict = {}
    with ThreadPoolExecutor(max_workers=min(len(wave_names), 4)) as ex:
        futs = {ex.submit(generate_npc_message, n, get_player_view(state, n, notes), char_map.get(n, {}), co_hints.get(n, ""), running_context): n for n in wave_names}
        for f in as_completed(futs): res_dict[futs[f]] = f.result()
    return [res_dict.get(n) for n in wave_names]

def generate_all_npc_messages(npc_names, state, notes, chars, co_hints, context_discs="", player_context="", on_progress=None, waves=None):
    if waves is None: waves = [npc_names]
    char_map = {c["name"]: c for c in chars}
    all_res, completed = [], []
    if player_context: completed.append(player_context)
    for w in waves:
        if not w: continue
        if on_progress: on_progress(w[0], None)
        wave_res = _run_wave(w, state, notes, char_map, co_hints, _build_running_context(context_discs, completed))
        for r in wave_res:
            all_res.append(r)
            if r["message"]: completed.append(r["message"])
            _thoughts_buffer[r["name"]] = r["thought"]
            if on_progress: on_progress(r["name"], r["message"])
    return all_res

def collect_all_suspicion_scores(npc_names, state, notes, chars, context_discs=""):
    char_map = {c["name"]: c for c in chars}
    scores_list = []
    with ThreadPoolExecutor(max_workers=min(len(npc_names), 4)) as ex:
        futs = [ex.submit(_collect_one_suspicion, n, state, notes, char_map, context_discs) for n in npc_names]
        for f in as_completed(futs):
            r = f.result()
            if r: scores_list.append(r)
    if not scores_list: return {}
    totals = {}
    for s in scores_list:
        for p, v in s.items(): totals.setdefault(p, []).append(v)
    return {p: sum(vs)/len(vs) for p, vs in totals.items()}

def save_thoughts(day, disc):
    filename = f"_npc_thoughts_day{day}_disc{disc}.json"
    with open(filename, "w", encoding="utf-8") as f: json.dump(_thoughts_buffer, f, ensure_ascii=False, indent=2)
    _thoughts_buffer.clear()
