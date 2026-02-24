#!/usr/bin/env python3
"""マルチエージェント NPC 実行エンジン（最終安定版）"""

from __future__ import annotations
import json, re, sys, threading, datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from logic_engine import get_player_view

MAX_CONTEXT_LINES = 5
_DEBUG_LOG_FILE = "debug_view.log"
_debug_log_lock = threading.Lock()

def _write_debug_log(npc_name: str, prompt: str, thought: str, raw: str = "") -> None:
    with _debug_log_lock:
        with open(_DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n[{npc_name}]  {datetime.datetime.now()}\n")
            f.write(f"{'─'*40}\nRAW RESPONSE:\n{raw}\n")
            f.write(f"{'─'*40}\nTHOUGHT:\n{thought or '(空)'}\n")

def _parse_json_bulletproof(raw: str, npc_name: str) -> dict:
    cleaned = raw.strip()
    json_match = re.search(r'(\{.*\})', cleaned, re.DOTALL)
    if json_match:
        try: return json.loads(json_match.group(1))
        except: pass
    thought_m = re.search(r'"thought"\s*:\s*"((?:[^"\\]|\\.)*)"', cleaned, re.DOTALL)
    message_m = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', cleaned, re.DOTALL)
    if message_m:
        return {"thought": thought_m.group(1) if thought_m else "", "message": message_m.group(1).replace("\\n", "\n")}
    raise ValueError(f"JSON extraction failed for {npc_name}")

def _dialogue_lines_to_json(text: str) -> str:
    entries = []
    for line in text.split("\n"):
        m = re.match(r'^(\S+)「(.+)」\s*$', line.strip())
        if m: entries.append({"speaker": m.group(1), "speech": m.group(2)})
    return json.dumps(entries, ensure_ascii=False) if entries else "[]"

def init(call_fn, call_fn_json=None, npc_model_name=""):
    global _call_fn, _call_fn_json, _npc_model_name
    _call_fn, _call_fn_json, _npc_model_name = call_fn, call_fn_json, npc_model_name

def write_debug_header(label: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _debug_log_lock:
        with open(_DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n{'#'*60}\n# {label}  [{ts}]\n{'#'*60}\n")

def _build_npc_prompt(npc_name, player_view, char_data, context_discs) -> str:
    role_jp = player_view["self"]["role_jp"]
    json_context = _dialogue_lines_to_json(context_discs)
    
    # 精密な性格矯正
    persona = {
        "ディータ": "紳士的かつ知的。論理で説得せよ。",
        "トーマス": "寡黙だが、必ず1名を具体的に疑え。",
        "パメラ": "『ねぇねぇ』禁止。親しみやすいが冷徹な計算に基づけ。",
        "リーザ": "怯えつつも、矛盾を必死に探せ。"
    }.get(npc_name, "自身のキャラを全うせよ。")

    return f"""あなたは{npc_name}({role_jp})。JSONエンジンとして動作せよ。
## 状況
- 生存者: {', '.join(player_view['alive_players'])}
- 議論文脈(JSON): {json_context}
### END OF CONTEXT ###
特別指令: {persona} 冒頭に必ず他者への言及（アンカー）を入れよ。
{{ "thought": "分析", "message": "セリフ" }}
"""

def generate_npc_message(npc_name, player_view, char_data, co_hint="", context_discs="") -> dict:
    prompt = _build_npc_prompt(npc_name, player_view, char_data, context_discs)
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

def _collect_one_suspicion(name, state, notes, char_map, context_discs) -> dict[str, int]:
    try: player_view = get_player_view(state, name, notes)
    except: return {}
    role = player_view["self"]["role"]
    wolf_teammates = player_view.get("wolf_teammates", [])
    alive_others = [n for n in player_view["alive_players"] if n != name] # 自爆防止

    prompt = f"あなたは{name}({role})。他者への疑惑度(1-10)をJSONで返せ。対象: {', '.join(alive_others)}"
    _fn = _call_fn_json if _call_fn_json else _call_fn
    try:
        raw = _fn(prompt).strip()
        scores = _parse_json_bulletproof(raw, name)
        if role in ("werewolf", "madman"):
            for teammate in wolf_teammates:
                if teammate in scores: scores[teammate] = max(1, int(scores[teammate] * 0.2)) # 身内バイアス
        if name in scores: del scores[name]
        return {k: max(1, min(10, int(v))) for k, v in scores.items() if isinstance(v, (int, float))}
    except: return {}

def generate_all_npc_messages(npc_names, state, notes, chars, co_hints, context_discs="", player_context="", on_progress=None, waves=None):
    char_map = {c["name"]: c for c in chars}
    all_res, completed = [], [player_context] if player_context else []
    for wave in (waves or [npc_names]):
        if not wave: continue
        json_recent = _dialogue_lines_to_json("\n".join(completed[-MAX_CONTEXT_LINES:]))
        running_context = f"{context_discs}\n\n## 今までの発言\n{json_recent}"
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(generate_npc_message, n, get_player_view(state, n, notes), char_map.get(n, {}), "", running_context): n for n in wave}
            for f in as_completed(futs):
                r = f.result()
                all_res.append(r)
                if r["message"]: completed.append(r["message"])
                if on_progress: on_progress(r["name"], r["message"])
    return all_res

def collect_all_suspicion_scores(npc_names, state, notes, chars, context_discs=""):
    all_s = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = [ex.submit(_collect_one_suspicion, n, state, notes, {}, context_discs) for n in npc_names]
        for f in as_completed(futs):
            res = f.result()
            if res: all_s.append(res)
    totals = {}
    for s in all_s:
        for p, v in s.items(): totals.setdefault(p, []).append(v)
    return {p: sum(vs)/len(vs) for p, vs in totals.items()}

def save_thoughts(day, disc): pass