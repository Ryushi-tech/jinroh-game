#!/usr/bin/env python3
"""GMヘルパー: NPC の意思決定を行い、安全な出力のみを返す。

Usage:
    python3 gm_helper.py setup [--player NAME]
        新規ゲーム。旧シーンファイルを削除し、役職をランダム割り当て。

    python3 gm_helper.py discussion_brief
        議論フェーズ冒頭に呼ぶ。確定白黒・怪しさスコア・対抗CO判断・
        投票先予測・縄計算を出力する。LLMはこれに従って描写を生成する。

    python3 gm_helper.py night_actions [--seer TARGET] [--guard TARGET] [--attack TARGET]
        夜フェーズを処理。NPC行動を自動決定し logic_engine.py を呼ぶ。

    python3 gm_helper.py vote_decide --player-vote TARGET
        投票・処刑を一括処理。
"""

import argparse
import glob
import json
import os
import random
import subprocess
import sys

STATE_FILE  = "game_state.json"
CHAR_FILE   = "characters.json"
PLAYER_FILE = ".player_name"
NOTES_FILE  = ".gm_notes.json"

ROLE_JP = {
    "villager": "村人", "werewolf": "人狼", "seer": "占い師",
    "medium": "霊媒師", "bodyguard": "狩人", "madman": "狂人",
}

ALL_NAMES = [
    "カタリナ", "パメラ", "ヨアヒム", "ヤコブ", "シモン",
    "フリーデル", "オットー", "リーザ", "ニコラス", "ディータ",
    "モーリッツ", "レジーナ", "ヴァルター", "ジムゾン", "トーマス",
    "アルビン",
]

# 対抗CO確率パラメータ
_NOBODY_PROB    = 0.01   # 全員出ない確率（外側ゲート）
_MADMAN_PROB    = 0.54   # 狂人が出る確率
_WOLF_WITH_PROB = 0.15   # 狂人が出た場合に狼も出る確率
# 狂人が出なかった場合: 狼は必ず1人出る（100%）


# ---------------------------------------------------------------------------
# 共通ヘルパー
# ---------------------------------------------------------------------------

def load_state():
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)

def load_notes():
    try:
        with open(NOTES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_notes(notes):
    with open(NOTES_FILE, "w", encoding="utf-8") as f:
        json.dump(notes, f, ensure_ascii=False, indent=2)
        f.write("\n")

def get_player(state, name):
    for p in state["players"]:
        if p["name"] == name:
            return p
    return None

def alive_players(state):
    return [p for p in state["players"] if p["alive"]]

def find_role(state, role, alive_only=True):
    for p in state["players"]:
        if p["role"] == role and (not alive_only or p["alive"]):
            return p
    return None

def wolves(state, alive_only=True):
    return [p for p in state["players"]
            if p["role"] == "werewolf" and (not alive_only or p["alive"])]

def last_guard_target(state):
    for entry in reversed(state["log"]):
        if entry["type"] == "guard":
            return entry["target"]
    return None

def win_status(state):
    alive = alive_players(state)
    wc = sum(1 for p in alive if p["role"] == "werewolf")
    oc = len(alive) - wc
    if wc == 0:     return "village"
    if wc >= oc:    return "werewolf"
    return "none"

def run_silent(*args):
    subprocess.run(
        ["python3", "logic_engine.py"] + list(args),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

def pname():
    return open(PLAYER_FILE, encoding="utf-8").read().strip()


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------

def cmd_setup(args):
    player_choice = args.player

    if player_choice and player_choice not in ALL_NAMES:
        print(f"ERROR: {player_choice} は登録されていません", file=sys.stderr)
        sys.exit(1)

    # 旧シーンファイル・ノートファイルを削除
    for f in glob.glob("scene_*.txt"):
        os.remove(f)
    if os.path.exists(NOTES_FILE):
        os.remove(NOTES_FILE)

    if player_choice:
        others = [n for n in ALL_NAMES if n != player_choice]
        selected = [player_choice] + random.sample(others, 8)
    else:
        selected = random.sample(ALL_NAMES, 9)
        player_choice = selected[0]

    roles = [
        "werewolf", "werewolf", "madman", "seer",
        "medium", "bodyguard", "villager", "villager", "villager",
    ]
    random.shuffle(roles)
    players = [{"name": n, "role": r, "alive": True}
               for n, r in zip(selected, roles)]

    state = {"day": 1, "phase": "day_discussion", "players": players, "log": []}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(PLAYER_FILE, "w", encoding="utf-8") as f:
        f.write(player_choice)

    p = get_player(state, player_choice)
    print(f"PLAYER_NAME={player_choice}")
    print(f"PLAYER_ROLE={ROLE_JP.get(p['role'], p['role'])}")
    print(f"ALL_PLAYERS={','.join(pl['name'] for pl in players)}")
    if p["role"] == "werewolf":
        allies = [x["name"] for x in players
                  if x["role"] == "werewolf" and x["name"] != player_choice]
        print(f"WOLF_ALLY={','.join(allies)}")


# ---------------------------------------------------------------------------
# discussion_brief
# ---------------------------------------------------------------------------

def _confirmed_info(state):
    """村視点の確定白・確定黒をログから計算する。"""
    alive_names = {p["name"] for p in state["players"] if p["alive"]}
    black, white = [], []
    for e in state["log"]:
        if e["type"] == "seer":
            # 占い師が死亡していれば結果を発表できないため除外
            actor = e.get("actor", "")
            if actor not in alive_names:
                continue
            if e["result"] == "werewolf":
                black.append(e["target"])
            else:
                white.append(e["target"])
        elif e["type"] == "attack" and e.get("result") == "killed":
            white.append(e["target"])   # 襲撃死 = 人間確定
        elif e["type"] == "execute" and e.get("alignment") == "werewolf":
            black.append(e["target"])
        elif e["type"] == "execute" and e.get("alignment") == "human":
            white.append(e["target"])
    # 重複除去（順序維持）
    seen = set()
    black = [x for x in black if not (x in seen or seen.add(x))]
    seen = set()
    white = [x for x in white if not (x in seen or seen.add(x))]
    return black, white


def _suspicion_scores(state, alive, confirmed_white, confirmed_black, player):
    """生存者ごとの怪しさスコアを計算する。"""
    scores = {p["name"]: 0 for p in alive if p["name"] != player}

    # 過去の得票数 × 2
    for e in state["log"]:
        if e["type"] == "execute" and "tally" in e:
            for name, count in e["tally"].items():
                if name in scores:
                    scores[name] += count * 2

    # 確定白 → 大幅減点
    for name in confirmed_white:
        if name in scores:
            scores[name] -= 10

    # 役職CO済み（占い師・霊媒師・狩人）→ 減点
    co_actors = {e["actor"] for e in state["log"] if e["type"] == "seer"}
    for name in co_actors:
        if name in scores:
            scores[name] -= 3

    # 確定黒はスコアリング対象外（吊り一択）
    for name in confirmed_black:
        scores.pop(name, None)

    return scores


def _decide_attack_target(state, alive, seer_target, notes):
    """
    人狼の襲撃先決定ロジック（優先順位順）:
    1. 今夜 seer_target が人狼 → 占い師を狙う（護衛ギャンブル）
    2. 直前の処刑が人狼      → 霊媒師を狙う（護衛ギャンブル）
    3. それ以外              → wolf_accusations の疑惑者を優先、なければランダム
    """
    wolf_names = {p["name"] for p in state["players"] if p["role"] == "werewolf"}
    non_wolves = [p["name"] for p in alive if p["role"] != "werewolf"]
    if not non_wolves:
        return None

    # Case 1: 今夜の占い先が人狼 → 占い師を狙う
    if seer_target and seer_target in wolf_names:
        seer = next((p["name"] for p in alive if p["role"] == "seer"), None)
        if seer and seer in non_wolves:
            return seer

    # Case 2: 直前の処刑が人狼 → 霊媒師を狙う
    last_exec = next((e for e in reversed(state["log"]) if e["type"] == "execute"), None)
    if last_exec and last_exec.get("alignment") == "werewolf":
        medium = next((p["name"] for p in alive if p["role"] == "medium"), None)
        if medium and medium in non_wolves:
            return medium

    # Case 3: wolf_accusations から生存狼への疑惑者を集め、最多の人を狙う
    accusations = notes.get("wolf_accusations", {})
    alive_set = {p["name"] for p in alive}
    suspectors: dict[str, int] = {}
    for wolf_name, accusers in accusations.items():
        if wolf_name in wolf_names:  # 生存中の狼への疑惑のみカウント
            for accuser in accusers:
                if accuser in alive_set and accuser not in wolf_names:
                    suspectors[accuser] = suspectors.get(accuser, 0) + 1
    if suspectors:
        top_count = max(suspectors.values())
        top = [name for name, cnt in suspectors.items() if cnt == top_count]
        return random.choice(top)

    # フォールバック: ランダム
    return random.choice(non_wolves)


def _decide_counter_co(state, notes, player):
    """
    対抗CO判断。
    - 狂人: 54% で出る
    - 狂人が出なかった場合: 狼は必ず1人出る
    - 狂人が出た場合: 各狼が独立に15%で出る
    - 外側ゲート: 1% で全員出ない
    """
    # 3日目以降は手遅れ
    if state["day"] > 2:
        return []

    already_co = set(notes.get("counter_co_actors", []))

    madman = find_role(state, "madman")
    madman_eligible = (
        madman and madman["alive"]
        and madman["name"] != player
        and madman["name"] not in already_co
    )
    wolf_eligible = [
        w for w in wolves(state)
        if w["name"] != player and w["name"] not in already_co
    ]

    if not madman_eligible and not wolf_eligible:
        return []

    # 外側ゲート: 1% で全員出ない
    if random.random() < _NOBODY_PROB:
        return []

    cos = []

    if madman_eligible and random.random() < _MADMAN_PROB:
        # 狂人が出る → 各狼が独立に15%で追随
        cos.append(madman["name"])
        for wolf in wolf_eligible:
            if random.random() < _WOLF_WITH_PROB:
                cos.append(wolf["name"])
    else:
        # 狂人が出ない → 狼から1人必ず出る
        if wolf_eligible:
            cos.append(random.choice(wolf_eligible)["name"])

    return cos


def cmd_discussion_brief(_args):
    state  = load_state()
    player = pname()
    alive  = alive_players(state)
    alive_set = {p["name"] for p in alive}
    notes  = load_notes()

    # 1. 確定白・黒（生存者に絞る）
    all_black, all_white = _confirmed_info(state)
    confirmed_black = [n for n in all_black if n in alive_set]
    confirmed_white = [n for n in all_white if n in alive_set]

    # 2. 怪しさスコア
    scores = _suspicion_scores(state, alive, confirmed_white, confirmed_black, player)

    # 3. 対抗CO判断
    counter_co = _decide_counter_co(state, notes, player)
    if counter_co:
        existing = set(notes.get("counter_co_actors", []))
        notes["counter_co_actors"] = list(existing | set(counter_co))
        save_notes(notes)

    # 4. 投票先予測（確定黒優先 → スコア上位）
    if confirmed_black:
        vote_plan = confirmed_black[0]
    elif scores:
        vote_plan = max(scores, key=scores.get)
    else:
        vote_plan = "none"

    # 5. 縄計算
    wolf_alive    = sum(1 for p in alive if p["role"] == "werewolf")
    village_alive = len(alive) - wolf_alive
    rope_margin   = max(0, village_alive - wolf_alive - 1)

    # 出力
    print(f"CONFIRMED_WHITE={','.join(confirmed_white) or 'none'}")
    print(f"CONFIRMED_BLACK={','.join(confirmed_black) or 'none'}")
    print(f"COUNTER_CO={','.join(counter_co) or 'none'}")
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    print(f"SUSPICION={','.join(f'{n}:{s}' for n, s in ranked) or 'none'}")
    print(f"VOTE_PLAN={vote_plan}")
    print(f"WOLF_ALIVE={wolf_alive}")
    print(f"VILLAGE_ALIVE={village_alive}")
    print(f"ROPE_MARGIN={rope_margin}")


# ---------------------------------------------------------------------------
# night_actions
# ---------------------------------------------------------------------------

def cmd_night_actions(args):
    state  = load_state()
    notes  = load_notes()
    player = pname()
    alive  = alive_players(state)

    seer_p    = find_role(state, "seer")
    guard_p   = find_role(state, "bodyguard")
    wolf_list = wolves(state)

    # --- 占い師 ---
    seer_target = None
    need_seer   = False
    if seer_p and seer_p["alive"]:
        if seer_p["name"] == player:
            if args.seer:   seer_target = args.seer
            else:           need_seer = True
        else:
            already = {e["target"] for e in state["log"] if e["type"] == "seer"}
            cands = [p["name"] for p in alive
                     if p["name"] != seer_p["name"] and p["name"] not in already]
            if cands:
                npc_seer_target = notes.get("npc_seer_target")
                if npc_seer_target and npc_seer_target in cands:
                    seer_target = npc_seer_target
                else:
                    seer_target = random.choice(cands)

    # --- 狩人 ---
    guard_target = None
    need_guard   = False
    if guard_p and guard_p["alive"]:
        if guard_p["name"] == player:
            if args.guard:  guard_target = args.guard
            else:           need_guard = True
        else:
            prev  = last_guard_target(state)
            cands = [p["name"] for p in alive
                     if p["name"] != guard_p["name"] and p["name"] != prev]
            npc_guard_target = notes.get("npc_guard_target")
            if npc_guard_target and npc_guard_target in cands:
                guard_target = npc_guard_target
            else:
                seer_co = next(
                    (e["actor"] for e in state["log"] if e["type"] == "seer"), None
                )
                alive_names = [p["name"] for p in alive]
                if seer_co and seer_co in alive_names and seer_co != prev:
                    guard_target = seer_co
                elif cands:
                    guard_target = random.choice(cands)

    # --- 人狼 ---
    attack_target = None
    need_attack   = False
    wolf_is_player = any(w["name"] == player for w in wolf_list)
    if wolf_is_player:
        if args.attack: attack_target = args.attack
        else:           need_attack = True
    else:
        attack_target = _decide_attack_target(state, alive, seer_target, notes)

    if need_seer or need_guard or need_attack:
        if need_seer:   print("NEED_SEER_INPUT=true")
        if need_guard:  print("NEED_GUARD_INPUT=true")
        if need_attack: print("NEED_ATTACK_INPUT=true")
        return

    cmd = ["python3", "logic_engine.py", "night"]
    if attack_target: cmd += ["--attack", attack_target]
    if seer_target:   cmd += ["--seer",   seer_target]
    if guard_target:  cmd += ["--guard",  guard_target]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    state = load_state()
    processed_day = state["day"] - 1
    victim, guarded = None, False
    for e in reversed(state["log"]):
        if e["type"] == "attack" and e["day"] == processed_day:
            if e.get("result") == "killed":     victim  = e["target"]
            elif e.get("result") == "guarded":  guarded = True
            break

    print(f"VICTIM={victim or 'none'}")
    print(f"GUARDED={'true' if guarded else 'false'}")
    print(f"WIN={win_status(state)}")


# ---------------------------------------------------------------------------
# vote_decide
# ---------------------------------------------------------------------------

def _village_vote_candidate(state, alive, me, village_vote_target=None):
    cands = [p["name"] for p in alive if p["name"] != me]
    if not cands:
        return None
    # 議論の着地点が記録されていればそれに従う
    if village_vote_target and village_vote_target in cands:
        return village_vote_target
    # フォールバック: 前回タリーのトップ
    last_tally = None
    for e in reversed(state["log"]):
        if e["type"] == "execute" and "tally" in e:
            last_tally = e["tally"]
            break
    if last_tally:
        weighted = [(n, c) for n, c in last_tally.items() if n in cands]
        if weighted:
            return max(weighted, key=lambda x: x[1])[0]
    return random.choice(cands)


def cmd_vote_decide(args):
    state  = load_state()
    player = pname()
    alive  = alive_players(state)
    alive_set = {p["name"] for p in alive}

    if args.player_vote not in alive_set:
        print(f"ERROR: {args.player_vote} は生存者にいません", file=sys.stderr)
        sys.exit(1)

    if state["phase"] == "day_discussion":
        run_silent("advance_phase")
        state = load_state()

    notes = load_notes()
    village_vote_target = notes.get("village_vote_target")

    # 死亡プレイヤーは投票できない
    player_alive = any(p["name"] == player and p["alive"] for p in state["players"])
    votes = {player: args.player_vote} if player_alive else {}
    seer_co = next((e["actor"] for e in state["log"] if e["type"] == "seer"), None)

    for p in alive:
        if p["name"] == player:
            continue
        if p["role"] == "werewolf":
            cands = [x["name"] for x in alive
                     if x["role"] != "werewolf" and x["name"] != p["name"]]
            votes[p["name"]] = (
                seer_co if seer_co and seer_co in cands
                else random.choice(cands) if cands else None
            )
        elif p["role"] == "madman":
            cands = [x["name"] for x in alive
                     if x["name"] != p["name"] and x["name"] != seer_co]
            if not cands:
                cands = [x["name"] for x in alive if x["name"] != p["name"]]
            votes[p["name"]] = random.choice(cands) if cands else None
        else:
            votes[p["name"]] = _village_vote_candidate(
                state, alive, p["name"], village_vote_target
            )

    votes = {k: v for k, v in votes.items() if v}

    result = subprocess.run(
        ["python3", "logic_engine.py", "vote", "--votes",
         json.dumps(votes, ensure_ascii=False)],
        capture_output=True, text=True,
    )

    executed = None
    for line in result.stdout.splitlines():
        if line.startswith("[処刑]"):
            executed = line.split("]", 1)[1].strip().split("（")[0].strip()
        elif "決選の結果" in line:
            executed = line.split("決選の結果")[1].strip().split("を処刑")[0].strip()

    state = load_state()
    print(f"EXECUTED={executed or 'unknown'}")
    print("NPC_VOTES_START")
    for voter, target in votes.items():
        if voter != player:
            print(f"{voter}={target}")
    print("NPC_VOTES_END")
    print(f"WIN={win_status(state)}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="GMヘルパー")
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup")
    p_setup.add_argument("--player", help="プレイヤーキャラクター名")

    sub.add_parser("discussion_brief")

    p_night = sub.add_parser("night_actions")
    p_night.add_argument("--seer")
    p_night.add_argument("--guard")
    p_night.add_argument("--attack")

    p_vote = sub.add_parser("vote_decide")
    p_vote.add_argument("--player-vote", required=True)

    args = parser.parse_args()

    if args.command == "setup":             cmd_setup(args)
    elif args.command == "discussion_brief": cmd_discussion_brief(args)
    elif args.command == "night_actions":   cmd_night_actions(args)
    elif args.command == "vote_decide":     cmd_vote_decide(args)


if __name__ == "__main__":
    main()
