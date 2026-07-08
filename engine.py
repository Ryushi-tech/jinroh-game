#!/usr/bin/env python3
"""人狼ゲーム エンジン（ライブラリ）

logic_engine.py + gm_helper.py を統合したもの。
- game_state.json を唯一の真実源とする
- すべての関数は dict を返し、印字しない（subprocess・stdoutパース全廃）
- 失敗は GameError で報告する（握りつぶさない）

秘匿方針:
- 役職・夜行動などの秘密は state / notes の中にのみ存在する
- 外部（UI・NPCプロンプト）へ出してよい情報は get_player_view() を通す
"""

from __future__ import annotations

import glob
import json
import os
import random
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

STATE_FILE = BASE_DIR / "game_state.json"
NOTES_FILE = BASE_DIR / ".gm_notes.json"
PLAYER_FILE = BASE_DIR / ".player_name"
CHAR_FILE = BASE_DIR / "characters.json"

ROLE_JP = {
    "villager": "村人", "werewolf": "人狼", "seer": "占い師",
    "medium": "霊媒師", "bodyguard": "狩人", "madman": "狂人",
}

ROLES_9 = [
    "werewolf", "werewolf", "madman", "seer",
    "medium", "bodyguard", "villager", "villager", "villager",
]

ALL_NAMES = [
    "カタリナ", "パメラ", "ヨアヒム", "ヤコブ", "シモン",
    "フリーデル", "オットー", "リーザ", "ニコラス", "ディータ",
    "モーリッツ", "レジーナ", "ヴァルター", "ジムゾン", "トーマス",
    "アルビン",
]

# 対抗CO確率パラメータ（gm_helper.py から移植）
_NOBODY_PROB = 0.01     # 全員出ない確率（外側ゲート）
_MADMAN_PROB = 0.54     # 狂人が出る確率
_WOLF_WITH_PROB = 0.15  # 狂人が出た場合に狼も出る確率


class GameError(Exception):
    """ルール違反・不正入力・不正フェーズなどのゲームエラー。"""


# ---------------------------------------------------------------------------
# State / Notes I/O
# ---------------------------------------------------------------------------

def load_state() -> dict:
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_notes() -> dict:
    try:
        with open(NOTES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_notes(notes: dict) -> None:
    with open(NOTES_FILE, "w", encoding="utf-8") as f:
        json.dump(notes, f, ensure_ascii=False, indent=2)
        f.write("\n")


def load_characters() -> list:
    with open(CHAR_FILE, encoding="utf-8") as f:
        return json.load(f)


def player_name() -> str:
    return PLAYER_FILE.read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# 共通ヘルパー
# ---------------------------------------------------------------------------

def get_player(state: dict, name: str) -> dict | None:
    for p in state["players"]:
        if p["name"] == name:
            return p
    return None


def alive_players(state: dict) -> list[dict]:
    return [p for p in state["players"] if p["alive"]]


def find_role(state: dict, role: str, alive_only: bool = True) -> dict | None:
    for p in state["players"]:
        if p["role"] == role and (not alive_only or p["alive"]):
            return p
    return None


def wolves(state: dict, alive_only: bool = True) -> list[dict]:
    return [p for p in state["players"]
            if p["role"] == "werewolf" and (not alive_only or p["alive"])]


def last_guard_target(state: dict) -> str | None:
    for entry in reversed(state["log"]):
        if entry["type"] == "guard":
            return entry["target"]
    return None


def win_status(state: dict) -> str:
    """'village' / 'werewolf' / 'none'"""
    alive = alive_players(state)
    wc = sum(1 for p in alive if p["role"] == "werewolf")
    oc = len(alive) - wc
    if wc == 0:
        return "village"
    if wc >= oc:
        return "werewolf"
    return "none"


# ---------------------------------------------------------------------------
# セットアップ
# ---------------------------------------------------------------------------

def setup_game(player_choice: str | None = None) -> dict:
    """新規ゲーム。旧シーン・ノートを削除し、役職をランダム割り当てる。

    Returns: {player, role, role_jp, all_players, wolf_allies}
    """
    if player_choice and player_choice not in ALL_NAMES:
        raise GameError(f"{player_choice} は登録されていません")

    for f in glob.glob(str(BASE_DIR / "scene_*.txt")):
        os.remove(f)
    if NOTES_FILE.exists():
        NOTES_FILE.unlink()

    if player_choice:
        others = [n for n in ALL_NAMES if n != player_choice]
        selected = [player_choice] + random.sample(others, 8)
    else:
        selected = random.sample(ALL_NAMES, 9)
        player_choice = selected[0]

    roles = ROLES_9[:]
    random.shuffle(roles)
    players = [{"name": n, "role": r, "alive": True}
               for n, r in zip(selected, roles)]

    state = {"day": 1, "phase": "day_discussion", "players": players, "log": []}
    save_state(state)
    PLAYER_FILE.write_text(player_choice, encoding="utf-8")

    p = get_player(state, player_choice)
    wolf_allies = []
    if p["role"] == "werewolf":
        wolf_allies = [x["name"] for x in players
                       if x["role"] == "werewolf" and x["name"] != player_choice]
    return {
        "player": player_choice,
        "role": p["role"],
        "role_jp": ROLE_JP[p["role"]],
        "all_players": [pl["name"] for pl in players],
        "wolf_allies": wolf_allies,
    }


# ---------------------------------------------------------------------------
# 内部確定情報・疑惑スコア（GM専用。UIに直接出さないこと）
# ---------------------------------------------------------------------------

def _confirmed_info(state: dict) -> tuple[list[str], list[str]]:
    """内部視点の確定黒・確定白（生存占い師の結果 / 襲撃死 / 処刑霊媒）。"""
    alive_names = {p["name"] for p in state["players"] if p["alive"]}
    black, white = [], []
    for e in state["log"]:
        if e["type"] == "seer":
            if e.get("actor", "") not in alive_names:
                continue
            (black if e["result"] == "werewolf" else white).append(e["target"])
        elif e["type"] == "attack" and e.get("result") == "killed":
            white.append(e["target"])
        elif e["type"] == "execute" and e.get("alignment") == "werewolf":
            black.append(e["target"])
        elif e["type"] == "execute" and e.get("alignment") == "human":
            white.append(e["target"])

    def _dedup(xs):
        seen = set()
        return [x for x in xs if not (x in seen or seen.add(x))]

    return _dedup(black), _dedup(white)


def _suspicion_scores(state, alive, confirmed_white, confirmed_black,
                      player, notes) -> dict[str, int]:
    scores = {p["name"]: 0 for p in alive if p["name"] != player}

    for e in state["log"]:
        if e["type"] == "execute" and "tally" in e:
            for name, count in e["tally"].items():
                if name in scores:
                    scores[name] += count * 2

    for name in confirmed_white:
        if name in scores:
            scores[name] -= 10

    for name in notes.get("public_co_claims", {}):
        if name in scores:
            scores[name] -= 3

    for name, llm_score in notes.get("npc_suspicion_avg", {}).items():
        if name in scores:
            scores[name] += (llm_score - 5)

    for name in confirmed_black:
        scores.pop(name, None)

    return scores


def _decide_counter_co(state: dict, notes: dict, player: str) -> list[str]:
    """対抗CO判断（gm_helper.py から移植。確率パラメータ同一）。"""
    if state["day"] > 2:
        return []
    if notes.get("counter_co_decided_day") == state["day"]:
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
    if random.random() < _NOBODY_PROB:
        return []

    cos = []
    if madman_eligible and random.random() < _MADMAN_PROB:
        cos.append(madman["name"])
        for wolf in wolf_eligible:
            if random.random() < _WOLF_WITH_PROB:
                cos.append(wolf["name"])
    else:
        if wolf_eligible:
            cos.append(random.choice(wolf_eligible)["name"])
    return cos


def discussion_brief() -> dict:
    """議論フェーズ用の内部ブリーフ。

    ※ 出力に役職情報（counter_co の正体等）を含むため、
      この dict を UI・プレイヤー向けテキストにそのまま出してはならない。
      呼び出し側（orchestrator）がNPC戦略指示への変換のみに使う。
    """
    state = load_state()
    player = player_name()
    alive = alive_players(state)
    alive_set = {p["name"] for p in alive}
    notes = load_notes()

    all_black, all_white = _confirmed_info(state)
    confirmed_black = [n for n in all_black if n in alive_set]
    confirmed_white = [n for n in all_white if n in alive_set]

    scores = _suspicion_scores(state, alive, confirmed_white, confirmed_black,
                               player, notes)

    counter_co = _decide_counter_co(state, notes, player)
    if counter_co:
        existing = set(notes.get("counter_co_actors", []))
        notes["counter_co_actors"] = sorted(existing | set(counter_co))
    if notes.get("counter_co_decided_day") != state["day"]:
        notes["counter_co_decided_day"] = state["day"]
    save_notes(notes)

    if confirmed_black:
        vote_plan = confirmed_black[0]
    elif scores:
        vote_plan = max(scores, key=scores.get)
    else:
        vote_plan = None

    wolf_alive = sum(1 for p in alive if p["role"] == "werewolf")
    village_alive = len(alive) - wolf_alive

    return {
        "confirmed_white": confirmed_white,
        "confirmed_black": confirmed_black,
        "counter_co": counter_co,
        "co_order": random.choice(["counter_first", "real_first"]) if counter_co else None,
        "suspicion": dict(sorted(scores.items(), key=lambda x: -x[1])),
        "vote_plan": vote_plan,
        "wolf_alive": wolf_alive,
        "village_alive": village_alive,
        "rope_margin": max(0, village_alive - wolf_alive - 1),
    }


def compute_vote_plan() -> str | None:
    """村NPC多数派の投票先を計算する（確定黒優先 → 疑惑スコア最大）。

    discussion_brief と違い対抗CO判断などの副作用を持たない。
    投票直前に village_vote_target を決めるために使う。
    """
    state = load_state()
    player = player_name()
    alive = alive_players(state)
    alive_set = {p["name"] for p in alive}
    notes = load_notes()

    all_black, all_white = _confirmed_info(state)
    confirmed_black = [n for n in all_black if n in alive_set]
    confirmed_white = [n for n in all_white if n in alive_set]

    if confirmed_black:
        return confirmed_black[0]
    scores = _suspicion_scores(state, alive, confirmed_white, confirmed_black,
                               player, notes)
    if scores:
        return max(scores, key=scores.get)
    return None


# ---------------------------------------------------------------------------
# 公開CO・霊媒結果の記録（orchestrator が使用）
# ---------------------------------------------------------------------------

def record_public_co(name: str, role: str, day: int) -> None:
    notes = load_notes()
    claims = notes.setdefault("public_co_claims", {})
    claims[name] = {"role": role, "day": day}
    save_notes(notes)


def record_public_seer_claim(actor: str, target: str, result_jp: str, day: int) -> None:
    """占い師CO者（真偽問わず）が発表した占い結果を公開情報として記録する。

    真の結果は log にあるが、それを直接ビューに出すと
    「どのCO者の結果が本物か」が構造的に漏れるため、
    公開情報は必ずこの宣言記録を経由する。
    result_jp: '人狼' | '白（人間）'
    """
    notes = load_notes()
    claims = notes.setdefault("public_seer_claims", [])
    claims.append({"actor": actor, "target": target, "result": result_jp, "day": day})
    save_notes(notes)


def record_public_medium_result(actor: str, target: str, result: str, day: int) -> None:
    """result: 'werewolf' | 'human'"""
    notes = load_notes()
    results = notes.setdefault("public_medium_results", [])
    results.append({"actor": actor, "target": target, "result": result, "day": day})
    save_notes(notes)


# ---------------------------------------------------------------------------
# 夜フェーズ
# ---------------------------------------------------------------------------

def _decide_attack_target(state: dict, alive: list[dict], notes: dict) -> str | None:
    """人狼の襲撃先決定（公開情報のみ使用。gm_helper.py から移植）。"""
    wolf_names = {p["name"] for p in state["players"] if p["role"] == "werewolf"}
    non_wolves = [p["name"] for p in alive if p["role"] != "werewolf"]
    alive_set = {p["name"] for p in alive}
    if not non_wolves:
        return None

    co_claims = notes.get("public_co_claims", {})

    seer_cos = [
        name for name, info in co_claims.items()
        if isinstance(info, dict) and info.get("role") == "seer"
        and name in alive_set and name not in wolf_names
    ]
    if seer_cos:
        return random.choice(seer_cos)

    last_exec = next((e for e in reversed(state["log"]) if e["type"] == "execute"), None)
    if last_exec and last_exec.get("alignment") == "werewolf":
        medium_cos = [
            name for name, info in co_claims.items()
            if isinstance(info, dict) and info.get("role") == "medium"
            and name in alive_set and name not in wolf_names
        ]
        if medium_cos:
            return random.choice(medium_cos)

    accusations = notes.get("wolf_accusations", {})
    suspectors: dict[str, int] = {}
    for wolf_name, accusers in accusations.items():
        if wolf_name in wolf_names:
            for accuser in accusers:
                if accuser in alive_set and accuser not in wolf_names:
                    suspectors[accuser] = suspectors.get(accuser, 0) + 1
    if suspectors:
        top_count = max(suspectors.values())
        top = [name for name, cnt in suspectors.items() if cnt == top_count]
        return random.choice(top)

    return random.choice(non_wolves)


def night_requirements() -> dict:
    """今夜プレイヤーに必要な入力を返す。{'seer': bool, 'guard': bool, 'attack': bool}"""
    state = load_state()
    player = player_name()
    seer_p = find_role(state, "seer")
    guard_p = find_role(state, "bodyguard")
    return {
        "seer": bool(seer_p and seer_p["alive"] and seer_p["name"] == player),
        "guard": bool(guard_p and guard_p["alive"] and guard_p["name"] == player),
        "attack": any(w["name"] == player for w in wolves(state)),
    }


def resolve_night(seer: str | None = None, guard: str | None = None,
                  attack: str | None = None) -> dict:
    """夜フェーズを一括処理する。

    プレイヤーが夜役職の場合は対応する引数が必須（欠けると GameError）。
    NPC の行動は notes（npc_seer_target / npc_guard_target / wolf_accusations）
    とフォールバックで自動決定する。

    Returns:
        {victim, guarded, win, seer_result, day, phase}
        seer_result はプレイヤーが占い師のときのみ {'target', 'result'}。
    """
    state = load_state()
    if state["phase"] != "night":
        raise GameError(f"現在のフェーズは {state['phase']} です。night ではありません")

    notes = load_notes()
    player = player_name()
    alive = alive_players(state)
    alive_names = {p["name"] for p in alive}
    day = state["day"]

    seer_p = find_role(state, "seer")
    guard_p = find_role(state, "bodyguard")
    wolf_list = wolves(state)

    # --- 占い先の決定 ---
    seer_target = None
    if seer_p and seer_p["alive"]:
        if seer_p["name"] == player:
            if not seer:
                raise GameError("占い先の指定が必要です")
            if seer not in alive_names or seer == player:
                raise GameError(f"{seer} は占い対象にできません")
            seer_target = seer
        else:
            already = {e["target"] for e in state["log"] if e["type"] == "seer"}
            cands = [p["name"] for p in alive
                     if p["name"] != seer_p["name"] and p["name"] not in already]
            if cands:
                npc_pref = notes.get("npc_seer_target")
                seer_target = npc_pref if npc_pref in cands else random.choice(cands)

    # --- 護衛先の決定 ---
    guard_target = None
    prev_guard = last_guard_target(state)
    if guard_p and guard_p["alive"]:
        if guard_p["name"] == player:
            if not guard:
                raise GameError("護衛先の指定が必要です")
            if guard not in alive_names or guard == player:
                raise GameError(f"{guard} は護衛対象にできません")
            if guard == prev_guard:
                raise GameError(f"{guard} は前夜も護衛しています（連続護衛禁止）")
            guard_target = guard
        else:
            cands = [p["name"] for p in alive
                     if p["name"] != guard_p["name"] and p["name"] != prev_guard]
            npc_pref = notes.get("npc_guard_target")
            if npc_pref in cands:
                guard_target = npc_pref
            else:
                seer_co_public = next(
                    (n for n, info in notes.get("public_co_claims", {}).items()
                     if isinstance(info, dict) and info.get("role") == "seer"
                     and n in alive_names),
                    None,
                )
                if seer_co_public and seer_co_public in cands:
                    guard_target = seer_co_public
                elif cands:
                    guard_target = random.choice(cands)

    # --- 襲撃先の決定 ---
    attack_target = None
    wolf_is_player = any(w["name"] == player for w in wolf_list)
    if wolf_list:
        if wolf_is_player:
            if not attack:
                raise GameError("襲撃先の指定が必要です")
            tp = get_player(state, attack)
            if tp is None or not tp["alive"]:
                raise GameError(f"{attack} は襲撃対象にできません")
            if tp["role"] == "werewolf":
                raise GameError("人狼を襲撃対象にはできません")
            attack_target = attack
        else:
            attack_target = _decide_attack_target(state, alive, notes)

    # --- 適用 ---
    seer_result = None
    if seer_target:
        tp = get_player(state, seer_target)
        result = "werewolf" if tp["role"] == "werewolf" else "not_werewolf"
        state["log"].append({
            "day": day, "phase": "night", "type": "seer",
            "actor": seer_p["name"], "target": seer_target, "result": result,
        })
        if seer_p["name"] == player:
            seer_result = {"target": seer_target, "result": result}

    if guard_target:
        state["log"].append({
            "day": day, "phase": "night", "type": "guard",
            "actor": guard_p["name"], "target": guard_target,
        })

    victim, guarded = None, False
    if attack_target:
        if guard_target == attack_target:
            guarded = True
            state["log"].append({
                "day": day, "phase": "night", "type": "attack",
                "target": attack_target, "result": "guarded",
            })
        else:
            get_player(state, attack_target)["alive"] = False
            victim = attack_target
            state["log"].append({
                "day": day, "phase": "night", "type": "attack",
                "target": attack_target, "result": "killed",
            })

    state["day"] += 1
    state["phase"] = "day_discussion"
    save_state(state)

    return {
        "victim": victim,
        "guarded": guarded,
        "win": win_status(state),
        "seer_result": seer_result,
        "day": state["day"],
        "phase": state["phase"],
    }


# ---------------------------------------------------------------------------
# 投票・処刑
# ---------------------------------------------------------------------------

def _village_vote_candidate(state, alive, me, village_vote_target=None) -> str | None:
    cands = [p["name"] for p in alive if p["name"] != me]
    if not cands:
        return None
    if village_vote_target and village_vote_target in cands:
        return village_vote_target
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


def decide_npc_votes(state: dict, notes: dict, player: str) -> dict[str, str]:
    """NPC全員の投票先を決定する（プレイヤー分は含まない）。"""
    alive = alive_players(state)
    alive_names = {p["name"] for p in alive}
    wolf_names = {p["name"] for p in state["players"] if p["role"] == "werewolf"}
    village_vote_target = notes.get("village_vote_target")

    co_claims = notes.get("public_co_claims", {})
    seer_cos_public = [
        n for n, info in co_claims.items()
        if isinstance(info, dict) and info.get("role") == "seer"
        and n in alive_names
    ]
    public_seer_co = seer_cos_public[0] if seer_cos_public else None

    votes: dict[str, str] = {}
    for p in alive:
        if p["name"] == player:
            continue
        if p["role"] == "werewolf":
            cands = [x["name"] for x in alive
                     if x["name"] not in wolf_names and x["name"] != p["name"]]
            if not cands:
                continue
            votes[p["name"]] = (
                public_seer_co if public_seer_co and public_seer_co in cands
                else random.choice(cands)
            )
        elif p["role"] == "madman":
            cands = [x["name"] for x in alive
                     if x["name"] != p["name"] and x["name"] != public_seer_co]
            if not cands:
                cands = [x["name"] for x in alive if x["name"] != p["name"]]
            if cands:
                votes[p["name"]] = random.choice(cands)
        else:
            target = _village_vote_candidate(state, alive, p["name"], village_vote_target)
            if target:
                votes[p["name"]] = target
    return votes


def resolve_vote(player_vote: str | None) -> dict:
    """投票・処刑を一括処理する。

    player_vote: プレイヤーの投票先。プレイヤー死亡時は None 可。

    Returns:
        {executed, alignment_secret, tally, votes, npc_votes, win, runoff}
        alignment_secret は霊媒用内部情報。UIへ直接出さないこと。
    """
    state = load_state()
    player = player_name()
    alive = alive_players(state)
    alive_names = {p["name"] for p in alive}

    player_alive = any(p["name"] == player and p["alive"] for p in state["players"])
    if player_alive:
        if not player_vote:
            raise GameError("プレイヤーの投票先が必要です")
        if player_vote not in alive_names:
            raise GameError(f"{player_vote} は生存者にいません")
        if player_vote == player:
            raise GameError("自分には投票できません")

    if state["phase"] == "day_discussion":
        state["phase"] = "day_vote"
    if state["phase"] != "day_vote":
        raise GameError(f"現在のフェーズは {state['phase']} です。投票できません")

    notes = load_notes()
    votes = decide_npc_votes(state, notes, player)
    npc_votes = dict(votes)
    if player_alive:
        votes[player] = player_vote

    tally = Counter(votes.values())
    max_count = tally.most_common(1)[0][1]
    top = [name for name, count in tally.items() if count == max_count]
    runoff = len(top) > 1
    executed = random.choice(top) if runoff else top[0]

    target = get_player(state, executed)
    target["alive"] = False
    alignment = "werewolf" if target["role"] == "werewolf" else "human"

    state["log"].append({
        "day": state["day"], "phase": "day_vote", "type": "execute",
        "target": executed,
        "alignment": alignment,
        "tally": dict(tally),
        "votes": votes,
    })
    state["phase"] = "night"
    save_state(state)

    # 投票理由カテゴリ（LLM描写用ヒント。役職は出さない）
    village_vote_target = notes.get("village_vote_target")

    def _reason(t: str) -> str:
        if not village_vote_target:
            return "conviction"
        return "consensus" if t == village_vote_target else "pivot"

    role_hint_map = {"werewolf": "wolf_strategic", "madman": "madman_disrupt"}
    alive_role = {p["name"]: p["role"] for p in alive}
    npc_vote_details = {
        voter: {
            "target": t,
            "reason": _reason(t),
            "role_hint": role_hint_map.get(alive_role.get(voter, ""), "villager"),
        }
        for voter, t in npc_votes.items()
    }

    return {
        "executed": executed,
        "alignment_secret": alignment,
        "tally": dict(tally),
        "votes": votes,
        "npc_votes": npc_vote_details,
        "runoff": runoff,
        "win": win_status(state),
    }


# ---------------------------------------------------------------------------
# 視点フィルター（logic_engine.py から移植）
# ---------------------------------------------------------------------------

def get_player_view(state: dict, target_player: str, notes: dict) -> dict:
    """target_player の視点でフィルター済みのゲーム情報を返す。

    役職情報は本人分のみ。wolf_teammates は狼の場合のみ仲間名リスト。
    """
    p = next((q for q in state["players"] if q["name"] == target_player), None)
    if p is None:
        raise GameError(f"プレイヤー {target_player} が見つかりません")

    role = p["role"]
    role_jp = ROLE_JP.get(role, role)

    wolf_teammates: list[str] = []
    if role == "werewolf":
        wolf_teammates = [
            q["name"] for q in state["players"]
            if q["role"] == "werewolf" and q["name"] != target_player
        ]

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

    public_co_claims: dict = notes.get("public_co_claims", {})

    # 公開占い結果 = 真占い師（CO済み・生存）の log 由来結果
    #             + 宣言記録（偽CO者の捏造結果を含む。orchestrator が記録）
    # 偽CO者の結果を含めないと「結果を持たないCO者=偽」と構造的に漏れるため。
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
    seen = {(r["actor"], r["target"], r["day"]) for r in public_seer_results}
    for c in notes.get("public_seer_claims", []):
        if c.get("actor") not in alive_set:
            continue
        key = (c["actor"], c["target"], c["day"])
        if key in seen:
            continue
        seen.add(key)
        public_seer_results.append({
            "actor": c["actor"],
            "target": c["target"],
            "result": c["result"],
            "day": c["day"],
        })

    public_medium_results: list = notes.get("public_medium_results", [])

    execution_history: list[dict] = []
    for e in state["log"]:
        if e["type"] == "execute":
            execution_history.append({
                "day": e["day"],
                "target": e["target"],
                "tally": e.get("tally", {}),
                "votes": e.get("votes", {}),
            })

    # 自分の役職固有の秘密情報（NPC自身のプロンプト用）
    private: dict = {}
    if role == "seer":
        private["seer_results"] = [
            {"day": e["day"], "target": e["target"],
             "result": "人狼" if e["result"] == "werewolf" else "白（人間）"}
            for e in state["log"]
            if e["type"] == "seer" and e.get("actor") == target_player
        ]
    elif role == "medium":
        private["medium_results"] = [
            {"day": e["day"], "target": e["target"],
             "result": "人狼" if e.get("alignment") == "werewolf" else "人間"}
            for e in state["log"] if e["type"] == "execute"
        ]
    elif role == "bodyguard":
        private["guard_history"] = [
            {"day": e["day"], "target": e["target"]}
            for e in state["log"]
            if e["type"] == "guard" and e.get("actor") == target_player
        ]

    return {
        "day": state["day"],
        "self": {
            "name": target_player,
            "role": role,
            "role_jp": role_jp,
        },
        "wolf_teammates": wolf_teammates,
        "alive_players": alive_players_list,
        "dead_players": dead_players,
        "public_co_claims": public_co_claims,
        "public_seer_results": public_seer_results,
        "public_medium_results": public_medium_results,
        "execution_history": execution_history,
        "private": private,
    }
