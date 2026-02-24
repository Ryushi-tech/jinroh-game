#!/usr/bin/env python3
"""人狼ゲーム ロジックエンジン

game_state.json を唯一の真実源として、ゲームの状態遷移を管理する。
手動でのJSON編集は禁止。すべての状態変更はこのスクリプトを経由すること。

Usage:
    python3 logic_engine.py night [--attack TARGET] [--seer TARGET] [--guard TARGET]
    python3 logic_engine.py vote --votes 'JSON'
    python3 logic_engine.py check_win
    python3 logic_engine.py advance_phase
"""

import argparse
import json
import random
import sys
from collections import Counter

STATE_FILE = "game_state.json"

# ---------------------------------------------------------------------------
# State I/O
# ---------------------------------------------------------------------------

def load_state():
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"[engine] {STATE_FILE} を更新しました")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_player(state, name):
    for p in state["players"]:
        if p["name"] == name:
            return p
    return None


def alive_players(state):
    return [p for p in state["players"] if p["alive"]]


def find_role(state, role, alive_only=True):
    """指定役職のプレイヤーを返す。"""
    for p in state["players"]:
        if p["role"] == role and (not alive_only or p["alive"]):
            return p
    return None


def last_guard_target(state):
    """直前の夜の護衛対象を返す（連続護衛禁止チェック用）。"""
    for entry in reversed(state["log"]):
        if entry["type"] == "guard":
            return entry["target"]
    return None


# ---------------------------------------------------------------------------
# Win condition
# ---------------------------------------------------------------------------

def check_win(state):
    """勝利判定。人狼全滅なら village_win、人狼 >= 非人狼 なら werewolf_win。"""
    alive = alive_players(state)
    wolves = [p for p in alive if p["role"] == "werewolf"]
    others = [p for p in alive if p["role"] != "werewolf"]

    if len(wolves) == 0:
        return "village_win"
    if len(wolves) >= len(others):
        return "werewolf_win"
    return None


def print_win_status(state):
    alive = alive_players(state)
    wolves = [p for p in alive if p["role"] == "werewolf"]
    others = [p for p in alive if p["role"] != "werewolf"]

    print(f"[生存状況] 人狼: {len(wolves)}名 / その他: {len(others)}名 / 計: {len(alive)}名")

    result = check_win(state)
    if result == "village_win":
        print("[終了] 村人陣営の勝利！ すべての人狼が排除されました。")
    elif result == "werewolf_win":
        print("[終了] 人狼陣営の勝利！ 人狼が村を支配しました。")
    else:
        print("[継続] ゲームは続行中")
    return result


# ---------------------------------------------------------------------------
# Subcommand: night
# ---------------------------------------------------------------------------

def cmd_night(args):
    state = load_state()
    day = state["day"]

    if state["phase"] != "night":
        print(f"[error] 現在のフェーズは {state['phase']} です。night ではありません。",
              file=sys.stderr)
        sys.exit(1)

    # --- Seer divination ---
    if args.seer:
        seer = find_role(state, "seer")
        if not seer:
            print("[error] 占い師が生存していません", file=sys.stderr)
            sys.exit(1)
        target = get_player(state, args.seer)
        if not target:
            print(f"[error] 占い対象 {args.seer} が見つかりません", file=sys.stderr)
            sys.exit(1)
        # 狂人は「人狼ではない」と出る
        result = "werewolf" if target["role"] == "werewolf" else "not_werewolf"
        state["log"].append({
            "day": day, "phase": "night", "type": "seer",
            "actor": seer["name"], "target": args.seer, "result": result
        })
        print(f"[占い結果] {seer['name']} → {args.seer}: {result}")

    # --- Bodyguard protection ---
    guarded = None
    if args.guard:
        guard = find_role(state, "bodyguard")
        if not guard:
            print("[error] 狩人が生存していません", file=sys.stderr)
            sys.exit(1)
        # 連続護衛禁止チェック
        prev = last_guard_target(state)
        if prev == args.guard:
            print(f"[error] {args.guard} は前夜も護衛対象です。連続護衛は禁止。",
                  file=sys.stderr)
            sys.exit(1)
        guarded = args.guard
        state["log"].append({
            "day": day, "phase": "night", "type": "guard",
            "actor": guard["name"], "target": guarded
        })
        print(f"[護衛] {guard['name']} → {guarded}")

    # --- Werewolf attack ---
    if args.attack:
        target = get_player(state, args.attack)
        if not target:
            print(f"[error] 襲撃対象 {args.attack} が見つかりません", file=sys.stderr)
            sys.exit(1)
        if target["role"] == "werewolf":
            print(f"[error] 人狼を襲撃対象にはできません", file=sys.stderr)
            sys.exit(1)

        if guarded == args.attack:
            state["log"].append({
                "day": day, "phase": "night", "type": "attack",
                "target": args.attack, "result": "guarded"
            })
            print(f"[襲撃] {args.attack} → 護衛成功！ 犠牲者なし")
        else:
            target["alive"] = False
            state["log"].append({
                "day": day, "phase": "night", "type": "attack",
                "target": args.attack, "result": "killed"
            })
            print(f"[襲撃] {args.attack} が犠牲になりました")
    else:
        print("[襲撃] なし（初夜 or 指定なし）")

    # --- Advance to next day ---
    state["day"] += 1
    state["phase"] = "day_discussion"
    save_state(state)

    print(f"[フェーズ遷移] Day {state['day']} / {state['phase']}")
    print_win_status(state)


# ---------------------------------------------------------------------------
# Subcommand: vote
# ---------------------------------------------------------------------------

def cmd_vote(args):
    state = load_state()

    if state["phase"] != "day_vote":
        print(f"[error] 現在のフェーズは {state['phase']} です。day_vote ではありません。",
              file=sys.stderr)
        sys.exit(1)

    votes = json.loads(args.votes)

    # バリデーション: 投票者・対象が全員生存プレイヤーか
    alive_names = {p["name"] for p in alive_players(state)}
    for voter, target in votes.items():
        if voter not in alive_names:
            print(f"[error] 投票者 {voter} は生存していません", file=sys.stderr)
            sys.exit(1)
        if target not in alive_names:
            print(f"[error] 投票対象 {target} は生存していません", file=sys.stderr)
            sys.exit(1)

    # 集計
    tally = Counter(votes.values())
    print("[投票結果]")
    for name, count in tally.most_common():
        print(f"  {name}: {count}票")

    # 最多得票者の決定（同票はランダム）
    max_count = tally.most_common(1)[0][1]
    top = [name for name, count in tally.items() if count == max_count]

    if len(top) > 1:
        executed = random.choice(top)
        print(f"[決選] {', '.join(top)} が同票（{max_count}票）→ 決選の結果 {executed} を処刑")
    else:
        executed = top[0]
        print(f"[処刑] {executed}（{max_count}票）")

    # 処刑実行
    target = get_player(state, executed)
    target["alive"] = False

    # 霊媒結果: 内部ログに記録（公開は霊媒師のCOによる。自動表示しない）
    alignment = "werewolf" if target["role"] == "werewolf" else "human"

    state["log"].append({
        "day": state["day"], "phase": "day_vote", "type": "execute",
        "target": executed,
        "alignment": alignment,
        "tally": dict(tally),
        "votes": votes
    })

    # フェーズ遷移 → 夜
    state["phase"] = "night"
    save_state(state)

    print(f"[フェーズ遷移] Day {state['day']} / {state['phase']}")
    print_win_status(state)


# ---------------------------------------------------------------------------
# Subcommand: check_win
# ---------------------------------------------------------------------------

def cmd_check_win(args):
    state = load_state()
    print_win_status(state)


# ---------------------------------------------------------------------------
# Subcommand: advance_phase
# ---------------------------------------------------------------------------

def cmd_advance_phase(args):
    state = load_state()
    current = state["phase"]

    transitions = {
        "night":          "day_discussion",
        "day_discussion": "day_vote",
        "day_vote":       "night",
    }

    new_phase = transitions[current]

    # night → day_discussion の場合は day もインクリメント
    if current == "night":
        state["day"] += 1

    state["phase"] = new_phase
    save_state(state)

    print(f"[フェーズ遷移] {current} → {new_phase} (Day {state['day']})")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="人狼ゲーム ロジックエンジン")
    sub = parser.add_subparsers(dest="command", required=True)

    # night
    p_night = sub.add_parser("night", help="夜フェーズの処理")
    p_night.add_argument("--attack", help="人狼の襲撃対象")
    p_night.add_argument("--seer",   help="占い師の占い対象")
    p_night.add_argument("--guard",  help="狩人の護衛対象")

    # vote
    p_vote = sub.add_parser("vote", help="投票集計と処刑")
    p_vote.add_argument("--votes", required=True,
                        help='投票JSON（例: \'{"太郎":"花子","花子":"太郎"}\'）')

    # check_win
    sub.add_parser("check_win", help="勝利判定")

    # advance_phase
    sub.add_parser("advance_phase", help="フェーズを次に進める")

    args = parser.parse_args()

    if args.command == "night":
        cmd_night(args)
    elif args.command == "vote":
        cmd_vote(args)
    elif args.command == "check_win":
        cmd_check_win(args)
    elif args.command == "advance_phase":
        cmd_advance_phase(args)


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# Player-view filter (for multi-agent NPC system)
# ---------------------------------------------------------------------------

_ROLE_JP = {
    "villager": "村人", "werewolf": "人狼", "seer": "占い師",
    "medium": "霊媒師", "bodyguard": "狩人", "madman": "狂人",
}


def get_player_view(state: dict, target_player: str, notes: dict) -> dict:
    """target_player の視点でフィルター済みのゲーム情報を返す。

    戻り値は Gemini NPC エージェントにそのまま渡せる clean dict。
    役職情報は本人分のみ。wolf_teammates は狼の場合のみ仲間名リスト。
    """
    p = next((q for q in state["players"] if q["name"] == target_player), None)
    if p is None:
        raise ValueError(f"プレイヤー {target_player} が見つかりません")

    role    = p["role"]
    role_jp = _ROLE_JP.get(role, role)

    # 仲間の狼（狼陣営のみ知っている情報）
    wolf_teammates: list[str] = []
    if role == "werewolf":
        wolf_teammates = [
            q["name"] for q in state["players"]
            if q["role"] == "werewolf" and q["name"] != target_player
        ]

    # 生存者・死亡者
    alive_players_list = [q["name"] for q in state["players"] if q["alive"]]

    dead_players: list[dict] = []
    for q in state["players"]:
        if not q["alive"]:
            cause = "死亡"
            for e in reversed(state["log"]):
                if e["type"] == "execute" and e["target"] == q["name"]:
                    cause = f"Day{e['day']} 処刑"
                    break
                if (e["type"] == "attack" and e["target"] == q["name"]
                        and e.get("result") == "killed"):
                    cause = f"Day{e['day']} 夜・襲撃死"
                    break
            dead_players.append({"name": q["name"], "cause": cause})

    # 公開CO一覧（notes["public_co_claims"] をそのまま渡す）
    public_co_claims: dict = notes.get("public_co_claims", {})

    # 公開占い結果: 生存かつ公開CO済みの占い師のlogエントリのみ
    alive_set = set(alive_players_list)
    co_seer_names = {
        name for name, info in public_co_claims.items()
        if isinstance(info, dict) and info.get("role") == "seer"
        and name in alive_set
    }
    public_seer_results: list[dict] = []
    for e in state["log"]:
        if e["type"] == "seer" and e.get("actor", "") in co_seer_names:
            result_jp = "人狼" if e["result"] == "werewolf" else "白（人間）"
            public_seer_results.append({
                "actor": e["actor"],
                "target": e["target"],
                "result": result_jp,
                "day": e["day"],
            })

    # 公開霊媒結果（notes["public_medium_results"] をそのまま渡す）
    public_medium_results: list = notes.get("public_medium_results", [])

    # 処刑履歴（alignment フィールドは除外して渡す）
    execution_history: list[dict] = []
    for e in state["log"]:
        if e["type"] == "execute":
            execution_history.append({
                "day":    e["day"],
                "target": e["target"],
                "tally":  e.get("tally", {}),
                "votes":  e.get("votes", {}),
            })

    return {
        "day": state["day"],
        "self": {
            "name":    target_player,
            "role":    role,
            "role_jp": role_jp,
        },
        "wolf_teammates":       wolf_teammates,
        "alive_players":        alive_players_list,
        "dead_players":         dead_players,
        "public_co_claims":     public_co_claims,
        "public_seer_results":  public_seer_results,
        "public_medium_results": public_medium_results,
        "execution_history":    execution_history,
    }
