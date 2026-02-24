#!/usr/bin/env python3
"""自動テストプレイ: 誤検知を排除し、厳密な生存者名簿に基づいて判定を行う最終責任版"""

from __future__ import annotations
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

MAX_DAYS    = 9
REPORT_FILE = "autoplay_report.json"
STATE_FILE  = "game_state.json"

ROLE_MAP = {
    "villager": "村人", "werewolf": "人狼", "seer": "占い師",
    "medium": "霊媒師", "bodyguard": "狩人", "madman": "狂人"
}

def run(cmd: list[str]) -> tuple[str, str]:
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout.strip(), r.stderr.strip()

def parse_kv(text: str) -> dict[str, str]:
    res = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            res[k.strip()] = v.strip()
    return res

def check_logic_errors(state_at_start: dict, disc_text: str, vote_data: dict) -> list[str]:
    """
    誤検知を排除した精密バリデーター
    1. DEAD_TALK: 議論開始時に「死んでいた」はずの特定の名前(9人中)に、敬称を付けて呼びかけたらアウト。
    2. GHOST_TALK: 9人の名簿にない名前(マリア等)に、敬称を付けて呼びかけたらアウト。
    """
    errors = []
    # 議論開始時の生死（このリストにない名前や、この時点で False の人をチェック）
    players = state_at_start["players"]
    all_known_names = [p["name"] for p in players]
    alive_at_start  = [p["name"] for p in players if p["alive"]]
    dead_at_start   = [p["name"] for p in players if not p["alive"]]

    # --- 1. 死者・幽霊への「呼びかけ」チェック ---
    # 名前 + 敬称(さん/君/様) で呼ばれている固有名詞を抽出
    calls = re.findall(r'([ア-ンー]{2,})(?:さん|君|様)', disc_text)
    
    for called_name in calls:
        # 名簿に存在するか？
        if called_name in all_known_names:
            # 存在する場合、その時死んでいたか？
            if called_name in dead_at_start:
                errors.append(f"DEAD_TALK: 既に死んでいる {called_name} に話しかけています")
        else:
            # 名簿にない名前（マリア等）
            errors.append(f"GHOST_TALK: 存在しない {called_name} (マリア現象) に話しかけています")

    # --- 2. 存在しない人物への「投票宣言」チェック ---
    # 「XXに投票」「XXにします」のXXが名簿にない場合
    vote_matches = re.findall(r'([ア-ンー]{2,})(?:に投票|にします|に一票)', disc_text)
    for target_name in vote_matches:
        if target_name not in all_known_names:
            errors.append(f"GHOST_TALK: 存在しない {target_name} への投票を宣言しています")
        elif target_name in dead_at_start:
            errors.append(f"DEAD_TALK: 死者 {target_name} への投票を宣言しています")

    return list(set(errors))

def run_game(game_num: int, total: int) -> dict:
    print(f"\n[ゲーム {game_num}/{total}]")
    run(["python3.11", "gm_helper.py", "setup", "--player", "オットー"])
    
    state = json.loads(Path(STATE_FILE).read_text(encoding="utf-8"))
    role_jp = ROLE_MAP.get(state['players'][0]['role'], "不明")
    print(f"  setup: オットー / {role_jp}")
    
    issues = []
    winner = None
    
    for day in range(1, MAX_DAYS + 1):
        print(f"  --- Day {day} ---")
        # 議論開始時の「死んでいないはずのリスト」を正確に取得
        state_at_start = json.loads(Path(STATE_FILE).read_text(encoding="utf-8"))
        
        run(["python3.11", "gemini_gm.py", "morning"])
        brief_out, _ = run(["python3.11", "gm_helper.py", "discussion_brief"])
        brief = parse_kv(brief_out)
        
        # 議論
        run(["python3.11", "gemini_gm.py", "discussion", "--context", "議論しましょう"])
        disc_path = Path(f"scene_day{day}_disc1.txt")
        disc_text = disc_path.read_text(encoding="utf-8") if disc_path.exists() else ""
        
        # 投票
        run(["python3.11", "gemini_gm.py", "suspicion-json"])
        vote_out, _ = run(["python3.11", "gm_helper.py", "vote_decide", "--player-vote", brief.get("VOTE_PLAN", "none")])
        vkv = parse_kv(vote_out)
        
        # 精密な整合性チェック
        day_errors = check_logic_errors(state_at_start, disc_text, vkv)
        issues.extend(day_errors)
        
        executed = vkv.get("EXECUTED", "なし")
        print(f"  処刑: {executed}  WIN={vkv.get('WIN', 'none')}")
        
        if not day_errors:
            print(f"    ✓ Day{day} 整合性 OK")
        else:
            for e in day_errors: print(f"    ⚠ {e}")

        if vkv.get("WIN") in ("village", "werewolf"):
            winner = vkv["WIN"]
            break

        # 夜
        night_out, _ = run(["python3.11", "gm_helper.py", "night_actions"])
        nkv = parse_kv(night_out)
        victim = nkv.get("KILLED", "none")
        print(f"  夜: 犠牲={victim}  WIN={nkv.get('WIN', 'none')}")

        if nkv.get("WIN") in ("village", "werewolf"):
            winner = nkv["WIN"]
            break
            
    print(f"  epilogue 完了  勝者={winner}")
    print(f"\n  ── 論理破綻チェック ({'問題なし' if not issues else '警告あり'}) ──")
    print(f"    GHOST_TALK     : {str(issues).count('GHOST_TALK')}件")
    print(f"    DEAD_TALK      : {str(issues).count('DEAD_TALK')}件")
    return {"game": game_num, "winner": winner, "days": day, "issues": issues}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()

    all_results = []
    for i in range(1, args.runs + 1):
        all_results.append(run_game(i, args.runs))

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()