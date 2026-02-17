#!/usr/bin/env python3
"""プレイヤー用ステータス表示。

Usage: python3 player_status.py <プレイヤー名>

game_state.json から、指定プレイヤーに見せてよい情報のみを表示する。
他者の役職や非公開ログは一切表示しない。
"""

import json
import sys

STATE_FILE = "game_state.json"

ROLE_JP = {
    "villager":  "村人",
    "werewolf":  "人狼",
    "seer":      "占い師",
    "bodyguard": "狩人",
    "madman":    "狂人",
    "medium":    "霊媒師",
    "mason":     "共有者",
}

PHASE_JP = {
    "night":          "夜",
    "day_discussion":  "昼・議論",
    "day_vote":        "昼・投票",
}


def load_state():
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def get_player(state, name):
    for p in state["players"]:
        if p["name"] == name:
            return p
    return None


def public_death_info(state):
    """公開情報: 処刑・襲撃による死亡者リスト。
    処刑者: 霊媒結果は霊媒師のCOによってのみ公開されるため、陣営は非表示。
    襲撃死者: 人狼に喰われた＝人狼ではないことだけ確定。
    """
    deaths = []
    for entry in state["log"]:
        if entry["type"] == "execute":
            deaths.append({
                "name": entry["target"],
                "day": entry["day"],
                "cause": "処刑",
            })
        elif entry["type"] == "attack" and entry.get("result") == "killed":
            deaths.append({
                "name": entry["target"],
                "day": entry["day"] + 1,  # 翌朝に判明
                "cause": "襲撃",
                "role": "人間",
            })
    return deaths


def private_info(state, player):
    """役職固有の秘密情報。"""
    role = player["role"]
    info = []

    if role == "seer":
        for entry in state["log"]:
            if entry["type"] == "seer" and entry["actor"] == player["name"]:
                result_jp = "人狼" if entry["result"] == "werewolf" else "人狼ではない"
                info.append(f"  Night {entry['day']}: {entry['target']} → {result_jp}")

    elif role == "bodyguard":
        for entry in state["log"]:
            if entry["type"] == "guard" and entry["actor"] == player["name"]:
                # 護衛成功したかは、同夜のattackログから判定
                success = any(
                    e["type"] == "attack"
                    and e["day"] == entry["day"]
                    and e["target"] == entry["target"]
                    and e.get("result") == "guarded"
                    for e in state["log"]
                )
                mark = " ★護衛成功" if success else ""
                info.append(f"  Night {entry['day']}: {entry['target']} を護衛{mark}")

    elif role == "werewolf":
        # 仲間の人狼を表示
        allies = [p["name"] for p in state["players"]
                  if p["role"] == "werewolf" and p["name"] != player["name"]]
        if allies:
            info.append(f"  仲間の人狼: {', '.join(allies)}")
        for entry in state["log"]:
            if entry["type"] == "attack":
                result_jp = "護衛された" if entry.get("result") == "guarded" else "成功"
                info.append(f"  Night {entry['day']}: {entry['target']} を襲撃 → {result_jp}")

    elif role == "medium":
        for entry in state["log"]:
            if entry["type"] == "execute":
                alignment = entry.get("alignment")
                if alignment:
                    result_jp = "人狼" if alignment == "werewolf" else "人間"
                else:
                    target = get_player(state, entry["target"])
                    result_jp = "人狼" if target["role"] == "werewolf" else "人間"
                info.append(f"  Day {entry['day']} 処刑: {entry['target']} → {result_jp}")

    elif role == "madman":
        info.append("  ※ 人狼が誰かは分かりません。勘と推理で人狼陣営を勝利に導いてください。")

    return info


def display(player_name):
    state = load_state()
    player = get_player(state, player_name)

    if not player:
        print(f"[error] {player_name} はこのゲームに参加していません。")
        sys.exit(1)

    day = state["day"]
    phase_jp = PHASE_JP.get(state["phase"], state["phase"])
    role_jp = ROLE_JP.get(player["role"], player["role"])
    alive = [p for p in state["players"] if p["alive"]]
    dead = [p for p in state["players"] if not p["alive"]]

    W = 44
    print("=" * W)
    print(f"  Day {day} / {phase_jp}")
    print("=" * W)
    print(f"  あなた: {player['name']}")
    print(f"  役職:   {role_jp}")
    status = "生存" if player["alive"] else "★ 死亡"
    print(f"  状態:   {status}")
    print("-" * W)

    # 生存者一覧
    print(f"  【生存者】{len(alive)}名")
    names = [p["name"] for p in alive]
    # 3名ずつ改行
    for i in range(0, len(names), 4):
        chunk = ", ".join(names[i:i+4])
        print(f"    {chunk}")

    # 死亡者（公開情報）
    deaths = public_death_info(state)
    if deaths:
        print(f"  【死亡者】{len(deaths)}名")
        for d in deaths:
            print(f"    {d['name']} ({d['role']}) - Day {d['day']} {d['cause']}")
    else:
        print("  【死亡者】なし")

    print("-" * W)

    # 秘密情報
    info = private_info(state, player)
    if info:
        print("  【あなただけの情報】")
        for line in info:
            print(line)
    else:
        print("  【あなただけの情報】なし")

    print("=" * W)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 player_status.py <プレイヤー名>", file=sys.stderr)
        sys.exit(1)
    display(sys.argv[1])


if __name__ == "__main__":
    main()
