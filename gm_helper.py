#!/usr/bin/env python3
"""GMヘルパー: NPC の意思決定を行い、安全な出力のみを返す。

すべてのロール情報は内部で処理され、画面に表示されるのは
プレイヤーが知ってよい情報のみ。

Usage:
    python3 gm_helper.py setup              -- 新規ゲーム作成
    python3 gm_helper.py night_actions      -- 夜のNPC行動を決定・実行
    python3 gm_helper.py npc_reactions <msg> -- 議論中のNPC反応を .gm_scene.txt に出力
    python3 gm_helper.py vote_decide        -- NPC投票先を決定し実行
"""

import json
import random
import subprocess
import sys

STATE_FILE = "game_state.json"
SCENE_FILE = ".gm_scene.txt"
CHAR_FILE = "characters.json"
PLAYER_FILE = ".player_name"


def load_state():
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_characters():
    with open(CHAR_FILE, encoding="utf-8") as f:
        return {c["name"]: c for c in json.load(f)}


def player_name():
    with open(PLAYER_FILE, encoding="utf-8") as f:
        return f.read().strip()


def get_player(state, name):
    for p in state["players"]:
        if p["name"] == name:
            return p
    return None


def alive_players(state):
    return [p for p in state["players"] if p["alive"]]


def alive_names(state):
    return [p["name"] for p in alive_players(state)]


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


# -------------------------------------------------------------------
# setup: 新規ゲーム
# -------------------------------------------------------------------
def cmd_setup():
    all_names = [
        "カタリナ", "パメラ", "ヨアヒム", "ヤコブ", "シモン",
        "フリーデル", "オットー", "リーザ", "ニコラス", "ディータ",
        "モーリッツ", "レジーナ", "ヴァルター", "ジムゾン", "トーマス",
        "アルビン",
    ]
    selected = random.sample(all_names, 9)
    roles = [
        "werewolf", "werewolf", "madman", "seer",
        "medium", "bodyguard", "villager", "villager", "villager",
    ]
    random.shuffle(roles)
    players = [{"name": n, "role": r, "alive": True}
               for n, r in zip(selected, roles)]
    player_char = random.choice(selected)

    state = {"day": 0, "phase": "night", "players": players, "log": []}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    with open(PLAYER_FILE, "w", encoding="utf-8") as f:
        f.write(player_char)

    # Night 0: 何も起きない → Day 1 へ
    state["day"] = 1
    state["phase"] = "day_discussion"
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")

    # プレイヤー情報のみ出力
    p = get_player(state, player_char)
    role_jp = {
        "villager": "村人", "werewolf": "人狼", "seer": "占い師",
        "medium": "霊媒師", "bodyguard": "狩人", "madman": "狂人",
    }
    print(f"PLAYER_NAME={player_char}")
    print(f"PLAYER_ROLE={role_jp.get(p['role'], p['role'])}")
    names = [pl["name"] for pl in players]
    print(f"ALL_PLAYERS={','.join(names)}")
    # 人狼なら仲間情報
    if p["role"] == "werewolf":
        allies = [x["name"] for x in players
                  if x["role"] == "werewolf" and x["name"] != player_char]
        print(f"WOLF_ALLY={','.join(allies)}")


# -------------------------------------------------------------------
# night_actions: 夜のNPC行動を決定・実行
# -------------------------------------------------------------------
def cmd_night_actions():
    state = load_state()
    pname = player_name()
    player = get_player(state, pname)
    alive = alive_players(state)

    seer = find_role(state, "seer")
    guard = find_role(state, "bodyguard")
    wolf_list = wolves(state)

    # --- 占い師の行動 ---
    seer_target = None
    if seer and seer["alive"]:
        if seer["name"] == pname:
            pass  # プレイヤーが占い師 → 外部で入力を受ける
        else:
            # NPC占い師: まだ占ってない生存者からランダム（自分と確定白を除く）
            already_divined = {e["target"] for e in state["log"]
                               if e["type"] == "seer"}
            candidates = [p["name"] for p in alive
                          if p["name"] != seer["name"]
                          and p["name"] not in already_divined]
            if candidates:
                # 優先: まだ占ってない非狼（わからないから実質ランダム）
                seer_target = random.choice(candidates)

    # --- 狩人の行動 ---
    guard_target = None
    if guard and guard["alive"]:
        if guard["name"] == pname:
            pass  # プレイヤーが狩人 → 外部で入力を受ける
        else:
            # NPC狩人: 占い師COがいればその人を護衛、なければランダム
            prev = last_guard_target(state)
            candidates = [p["name"] for p in alive
                          if p["name"] != guard["name"]
                          and p["name"] != prev]
            # 占い師COを探す（ログから推定）
            seer_co = None
            for e in state["log"]:
                if e["type"] == "seer":
                    seer_co = e["actor"]
            if seer_co and seer_co in [p["name"] for p in alive] \
                    and seer_co != prev:
                guard_target = seer_co
            elif candidates:
                guard_target = random.choice(candidates)

    # --- 人狼の襲撃 ---
    attack_target = None
    wolf_is_player = any(w["name"] == pname for w in wolf_list)
    if not wolf_is_player:
        # NPC人狼が襲撃先を決定
        non_wolves = [p["name"] for p in alive if p["role"] != "werewolf"]
        if non_wolves:
            # 優先的に占い師COしている人を狙う
            seer_co = None
            for e in state["log"]:
                if e["type"] == "seer":
                    seer_co = e["actor"]
            if seer_co and seer_co in non_wolves:
                attack_target = seer_co
            else:
                attack_target = random.choice(non_wolves)

    # --- logic_engine.py を呼ぶ ---
    cmd = ["python3", "logic_engine.py", "night"]
    if attack_target:
        cmd += ["--attack", attack_target]
    if seer_target:
        cmd += ["--seer", seer_target]
    if guard_target:
        cmd += ["--guard", guard_target]

    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # リロード
    state = load_state()

    # プレイヤー向け出力: 襲撃結果のみ
    victim = None
    for e in reversed(state["log"]):
        if e["type"] == "attack" and e["day"] == state["day"] - 1:
            if e.get("result") == "killed":
                victim = e["target"]
            elif e.get("result") == "guarded":
                victim = None  # 護衛成功
            break

    if victim:
        print(f"VICTIM={victim}")
    else:
        print("VICTIM=none")

    # プレイヤーが占い師なら、結果待ちフラグ
    if seer and seer["name"] == pname and seer["alive"]:
        print("NEED_SEER_INPUT=true")
    else:
        print("NEED_SEER_INPUT=false")

    # プレイヤーが狩人なら、護衛先待ちフラグ
    if guard and guard["name"] == pname and guard["alive"]:
        print("NEED_GUARD_INPUT=true")
    else:
        print("NEED_GUARD_INPUT=false")

    # プレイヤーが人狼なら、襲撃先待ちフラグ
    if wolf_is_player:
        print("NEED_ATTACK_INPUT=true")
    else:
        print("NEED_ATTACK_INPUT=false")


# -------------------------------------------------------------------
# vote_decide: NPC投票先決定
# -------------------------------------------------------------------
def cmd_vote_decide():
    state = load_state()
    pname = player_name()
    alive = alive_players(state)
    alive_n = [p["name"] for p in alive]

    # 簡易AI: 各NPCが投票先を決める
    votes = {}
    for p in alive:
        if p["name"] == pname:
            continue  # プレイヤーは外部入力

        # 人狼: 村人に投票（仲間を避ける）
        if p["role"] == "werewolf":
            candidates = [x["name"] for x in alive
                          if x["role"] != "werewolf" and x["name"] != p["name"]]
            # 占い師COしてる人を優先
            seer_co = None
            for e in state["log"]:
                if e["type"] == "seer":
                    seer_co = e["actor"]
            if seer_co and seer_co in candidates:
                votes[p["name"]] = seer_co
            elif candidates:
                votes[p["name"]] = random.choice(candidates)

        # 狂人: ランダム（自分以外）、占い師COを避ける傾向
        elif p["role"] == "madman":
            candidates = [x["name"] for x in alive if x["name"] != p["name"]]
            votes[p["name"]] = random.choice(candidates)

        # 村側: 怪しい人に投票
        else:
            candidates = [x["name"] for x in alive if x["name"] != p["name"]]
            # シンプルヒューリスティック: 投票パターンや発言から
            votes[p["name"]] = random.choice(candidates)

    # NPC投票先のみ出力（ロール非公開）
    print("NPC_VOTES_START")
    for voter, target in votes.items():
        print(f"{voter}={target}")
    print("NPC_VOTES_END")
    print(f"NEED_PLAYER_VOTE=true")


# -------------------------------------------------------------------
# main
# -------------------------------------------------------------------
def main():
    if len(sys.argv) < 2:
        print("Usage: python3 gm_helper.py <command>", file=sys.stderr)
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "setup":
        cmd_setup()
    elif cmd == "night_actions":
        cmd_night_actions()
    elif cmd == "vote_decide":
        cmd_vote_decide()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
