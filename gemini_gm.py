#!/usr/bin/env python3
"""GM ナラティブ生成エンジン（Gemini 2.5 Pro 重厚描写版）"""

from __future__ import annotations
import argparse, json, os, subprocess, sys, re
from pathlib import Path
import npc_agent as _npc_agent

# ---------------------------------------------------------------------------
# 設定・プロンプト定義
# ---------------------------------------------------------------------------
STATE_FILE, CHAR_FILE, PLAYER_FILE, NOTES_FILE = "game_state.json", "characters.json", ".player_name", ".gm_notes.json"
GM_MODEL_NAME, NPC_MODEL_NAME = "gemini-2.5-pro", "gemini-2.5-flash"

CHARACTER_GUIDELINES = """
- ディータ: 知的で紳士的、論理の鋭さで圧する。
- トーマス: 寡黙。必ず1名を具体的に名指しで疑え。
- パメラ: 「ねぇねぇ」禁止。冷徹な計算に基づく。
"""

SYSTEM_INSTRUCTION = f"あなたは人狼ゲームの熟練GM。{CHARACTER_GUIDELINES}\n発言は『名前「セリフ」』形式、ナレーションは地の文で描写せよ。"
NPC_AGENT_SYSTEM = '{"thought": "戦略的思考", "message": "発言内容"}' # スキーマヒント

# ---------------------------------------------------------------------------
# API呼び出し
# ---------------------------------------------------------------------------
def init_gemini():
    from google import genai
    return genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

def call_gemini(client, prompt: str, is_npc: bool = False) -> str:
    from google.genai import types
    model = NPC_MODEL_NAME if is_npc else GM_MODEL_NAME
    sys_inst = NPC_AGENT_SYSTEM if is_npc else SYSTEM_INSTRUCTION
    response = client.models.generate_content(
        model=model, contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=sys_inst,
            response_mime_type="application/json" if is_npc else "text/plain",
            temperature=0.7
        )
    )
    return response.text

# ---------------------------------------------------------------------------
# シーン生成コマンド（Pro の知性を活用）
# ---------------------------------------------------------------------------
def cmd_morning(client, state, chars, player):
    day = state["day"]
    victim = next((e["target"] for e in reversed(state["log"]) if e["type"] == "attack" and e["day"] == day-1 and e.get("result") == "killed"), "なし")
    prompt = f"Day{day}の朝が来ました。昨夜の犠牲者は【{victim}】です。死体発見のシーンと村人の動揺を重厚に描写してください。"
    text = call_gemini(client, prompt)
    Path(f"scene_day{day}_morning.txt").write_text(text, encoding="utf-8")
    print(f"scene_day{day}_morning.txt")

def cmd_discussion(client, state, chars, player, context=""):
    day = state["day"]
    filepath = f"scene_day{day}_disc1.txt"
    npc_names = [p["name"] for p in state["players"] if p["alive"] and p["name"] != player]
    
    # NPC発言を生成（ここが議論の核）
    results = _npc_agent.generate_all_npc_messages(npc_names, state, {}, chars, {}, "", f'{player}「{context}」')
    
    lines = [f'{player}「{context}」\n']
    for r in results: lines.append(f"{r['message']}\n")
    Path(filepath).write_text("\n".join(lines), encoding="utf-8")
    print(filepath)

def cmd_vote(client, state, chars, player, npc_votes=None):
    day = state["day"]
    prompt = f"各人の投票先はこれです: {json.dumps(npc_votes, ensure_ascii=False)}\n各キャラクターが自分の言葉で誰に投票するか宣言するシーンを作れ。"
    text = call_gemini(client, prompt)
    Path(f"scene_day{day}_vote.txt").write_text(text, encoding="utf-8")
    print(f"scene_day{day}_vote.txt")

def cmd_execution(client, state, chars, player):
    day = state["day"]
    executed = next((e["target"] for e in reversed(state["log"]) if e["type"] == "execute" and e["day"] == day), "不明")
    prompt = f"{executed}が処刑されることになりました。処刑の儀式と、最後に残した言葉、村の静寂を重厚に描写せよ。"
    text = call_gemini(client, prompt)
    Path(f"scene_day{day}_execution.txt").write_text(text, encoding="utf-8")
    print(f"scene_day{day}_execution.txt")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("scene", choices=["morning", "discussion", "suspicion-json", "vote", "execution"])
    parser.add_argument("--context", default=""), parser.add_argument("--votes", default="")
    args = parser.parse_args()
    
    # APIキー読み込み（簡易版）
    if not os.environ.get("GEMINI_API_KEY"):
        for line in open(".env"):
            if "=" in line: k,v = line.split("="); os.environ[k.strip()] = v.strip().strip('"')

    client = init_gemini()
    _npc_agent.init(lambda p: call_gemini(client, p, False), lambda p: call_gemini(client, p, True), NPC_MODEL_NAME)
    _npc_agent.write_debug_header(f"ACTION: {args.scene}")

    state = json.loads(Path(STATE_FILE).read_text(encoding="utf-8"))
    chars = json.loads(Path(CHAR_FILE).read_text(encoding="utf-8"))
    player = Path(PLAYER_FILE).read_text(encoding="utf-8").strip()

    if args.scene == "morning": cmd_morning(client, state, chars, player)
    elif args.scene == "discussion": cmd_discussion(client, state, chars, player, args.context)
    elif args.scene == "vote": cmd_vote(client, state, chars, player, json.loads(args.votes) if args.votes else None)
    elif args.scene == "execution": cmd_execution(client, state, chars, player)
    elif args.scene == "suspicion-json":
        # 疑惑スコア収集
        npc_names = [p["name"] for p in state["players"] if p["alive"] and p["name"] != player]
        avg = _npc_agent.collect_all_suspicion_scores(npc_names, state, {}, chars, "")
        notes = json.loads(Path(NOTES_FILE).read_text(encoding="utf-8")) if Path(NOTES_FILE).exists() else {}
        notes["npc_suspicion_avg"] = avg
        Path(NOTES_FILE).write_text(json.dumps(notes, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()