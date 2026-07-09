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

# 5人村: 人狼1・狂人1 / 占い師1・村人2
# （9人村から縮小。霊媒師・狩人は不在で、関連ロジックは find_role が
#   None を返すことで自然にスキップされる）
ROLE_COMPOSITION = [
    "werewolf", "madman", "seer", "villager", "villager",
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


def _atomic_write_json(path: Path, data: dict) -> None:
    """一時ファイルに書いてから os.replace で置換するアトミック書き込み。

    truncate→write だと HTTP サーバーの GET スレッドが同時に読んだとき
    壊れた JSON を読む可能性があるため。
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def save_state(state: dict) -> None:
    _atomic_write_json(STATE_FILE, state)


def load_notes() -> dict:
    try:
        with open(NOTES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_notes(notes: dict) -> None:
    _atomic_write_json(NOTES_FILE, notes)


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


def format_vote_breakdown(votes: dict[str, str]) -> str:
    """投票者→投票先の一覧（各人1票）。シーン・盤面・UIの共通フォーマット。"""
    if not votes:
        return ""
    return "、".join(f"{voter}→{target}"
                       for voter, target in sorted(votes.items()))


def format_execution_votes(votes: dict[str, str], executed: str, *,
                           runoff: bool = False) -> str:
    """投票シーン冒頭用の固定フォーマット。得票数ではなく個票を列挙する。"""
    lines = ["―― 投票結果 ――"]
    for voter in sorted(votes.keys()):
        lines.append(f"{voter} → {votes[voter]}")
    suffix = "（同票決選）" if runoff else ""
    lines.append(f"処刑: {executed}{suffix}")
    return "\n".join(lines)


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


def finalize_game(winner: str) -> None:
    """勝敗確定後に phase を凍結する。orchestrator.epilogue から呼ぶ。"""
    state = load_state()
    state["phase"] = "epilogue"
    save_state(state)


def format_epilogue_scene(winner: str, state: dict) -> str:
    """幕引きシーン本文（テンプレのみ。キャラ発言・LLM不使用）。"""
    winner_jp = "村人陣営" if winner == "village" else "人狼陣営"
    lines = [
        f"長い争いに終止符が打たれた。勝者は【{winner_jp}】。",
        "",
        "── 全役職公開 ──",
    ]
    for p in state["players"]:
        status = "生存" if p["alive"] else "死亡"
        lines.append(f"{p['name']}: {ROLE_JP[p['role']]}（{status}）")
    lines.extend(["", "こうして、村に静けさが戻った。"])
    return "\n".join(lines)


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
    # NPC思考ログは day/disc 名でしか区別されず、前ゲームの内容に
    # マージ追記されてしまうため、ゲーム開始時に消す
    # （debug_view.log は時系列追記なので残す）
    for f in glob.glob(str(BASE_DIR / "logs" / "npc_thoughts_*.json")):
        os.remove(f)
    if NOTES_FILE.exists():
        NOTES_FILE.unlink()

    n_players = len(ROLE_COMPOSITION)
    if player_choice:
        others = [n for n in ALL_NAMES if n != player_choice]
        selected = [player_choice] + random.sample(others, n_players - 1)
    else:
        selected = random.sample(ALL_NAMES, n_players)
        player_choice = selected[0]

    roles = ROLE_COMPOSITION[:]
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

def _confirmed_info(state: dict, notes: dict) -> tuple[list[str], list[str]]:
    """公開情報（宣言台帳＋襲撃死）のみから確定黒・確定白を計算する。

    log の真の占い結果・未発表の霊媒判定（execute の alignment）は使わない。
    未発表情報で村NPCの投票が動くと、発表されていない真結果が
    行動から逆算できてしまう神視点リークになるため。
    材料は public_seer_claims / public_medium_results の宣言と、
    襲撃死（=人間確定という公開ルール）のみ。
    発表者（actor）が死亡していても宣言は有効なまま数える。
    同一 target に黒と白の両方の宣言がある場合（対抗COの食い違い等）は
    係争中とみなし、どちらからも除外する。
    """
    black, white = [], []
    for c in notes.get("public_seer_claims", []):
        (black if c.get("result") == "人狼" else white).append(c["target"])
    for r in notes.get("public_medium_results", []):
        if r.get("result") == "werewolf":
            black.append(r["target"])
        elif r.get("result") == "human":
            white.append(r["target"])
    for e in state["log"]:
        if e["type"] == "attack" and e.get("result") == "killed":
            white.append(e["target"])

    disputed = set(black) & set(white)
    black = [x for x in black if x not in disputed]
    white = [x for x in white if x not in disputed]

    def _dedup(xs):
        seen = set()
        return [x for x in xs if not (x in seen or seen.add(x))]

    return _dedup(black), _dedup(white)


def _suspicion_scores(state, alive, confirmed_white, confirmed_black,
                      notes) -> dict[str, int]:
    """生存者全員の疑惑スコアを集計する。

    プレイヤーも村の疑惑・合意先の対象に含める。
    除外するとプレイヤーが構造的に吊られなくなる。
    """
    scores = {p["name"]: 0 for p in alive}

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


def compute_npc_suspicion(state: dict, notes: dict, player: str) -> dict:
    """NPC各自の視点による疑惑スコアを機械計算する（LLM不使用）。

    旧実装は LLM に1体ずつ「怪しい順スコアを出せ」と問い合わせていたが、
    LLMの関与を口調の翻訳に限定する方針に伴い、公開情報の台帳
    （public_seer_claims / public_co_claims / execute の votes）と
    rater 自身の役職知識のみから決定的に計算する。

    Returns: {"avg": {target: float}, "by_rater": {rater: {target: float}}}
    rater = 生存NPC全員（プレイヤー除く）。
    target = rater 以外の生存者全員（プレイヤー含む）。
    """
    alive = alive_players(state)
    alive_names = {p["name"] for p in alive}
    raters = [p for p in alive if p["name"] != player]

    seer_claims = notes.get("public_seer_claims", [])
    black_declared = {c["target"] for c in seer_claims
                      if c.get("result") == "人狼"}
    white_declared = {c["target"] for c in seer_claims
                      if c.get("result") != "人狼"}
    disputed = black_declared & white_declared

    seer_co_names = {
        n for n, info in notes.get("public_co_claims", {}).items()
        if isinstance(info, dict) and info.get("role") == "seer"
    }

    _, all_white = _confirmed_info(state, notes)
    confirmed_white = {n for n in all_white if n in alive_names}

    # 直近の処刑の個票（単独投票・遺恨の判定に使う）
    last_votes: dict[str, str] = {}
    for e in reversed(state["log"]):
        if e["type"] == "execute" and e.get("votes"):
            last_votes = e["votes"]
            break
    vote_counts = Counter(last_votes.values())

    by_rater: dict[str, dict[str, float]] = {}
    for rp in raters:
        r = rp["name"]
        r_role = rp["role"]

        teammates = {p["name"] for p in state["players"]
                     if p["role"] == "werewolf" and p["name"] != r}
        own_black: set[str] = set()
        own_white: set[str] = set()
        if r_role == "seer":
            for e in state["log"]:
                if e["type"] == "seer" and e.get("actor") == r:
                    dest = own_black if e["result"] == "werewolf" else own_white
                    dest.add(e["target"])
        believed_wolf = None
        if r_role == "madman":
            # 推定狼 = 自分以外の actor（真占いの可能性が高い）による最新の黒宣言先
            believed_wolf = next(
                (c["target"] for c in reversed(seer_claims)
                 if c.get("actor") != r and c.get("result") == "人狼"),
                None)

        rated: dict[str, float] = {}
        for tp in alive:
            t = tp["name"]
            if t == r:
                continue
            score = 3.0
            fixed = False  # 固定値はノイズを加えない（決定的な確信を表す）

            # --- 公開情報ベース（全rater共通） ---
            if t in black_declared:
                score += 3 if t in disputed else 5
            if len(seer_co_names) >= 2 and t in seer_co_names:
                score += 2
            if last_votes.get(t) and vote_counts[last_votes[t]] == 1:
                score += 1  # 単独投票
            if last_votes.get(t) == r:
                score += 2  # 遺恨
            if t in confirmed_white:
                score, fixed = 1.0, True

            # --- rater の役職知識（公開情報より優先） ---
            if r_role == "werewolf" and t in teammates:
                score, fixed = 1.0, True
            elif r_role == "seer":
                if t in own_black:
                    score, fixed = 10.0, True
                elif t in own_white:
                    score, fixed = 1.0, True
                elif t in seer_co_names and not fixed:
                    score += 4  # 自分以外の占い師COは偽
            elif r_role == "madman" and t == believed_wolf:
                score, fixed = 1.0, True

            if not fixed:
                score += random.Random(f"{state['day']}:{r}:{t}").uniform(-0.5, 0.5)
            rated[t] = round(min(10.0, max(1.0, score)), 2)
        by_rater[r] = rated

    totals: dict[str, list[float]] = {}
    for rated in by_rater.values():
        for t, v in rated.items():
            totals.setdefault(t, []).append(v)
    avg = {t: round(sum(vs) / len(vs), 2) for t, vs in totals.items()}
    return {"avg": avg, "by_rater": by_rater}


def _decide_counter_co(state: dict, notes: dict, player: str) -> list[str]:
    """対抗CO判断（gm_helper.py から移植。確率パラメータ同一）。

    偽占い師COの割り当ては初日1回のみ。2日目以降に新規割り当てすると
    既存の偽CO者と真占い師COが矛盾して破綻する。
    """
    if state["day"] > 1:
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

    all_black, all_white = _confirmed_info(state, notes)
    confirmed_black = [n for n in all_black if n in alive_set]
    confirmed_white = [n for n in all_white if n in alive_set]

    scores = _suspicion_scores(state, alive, confirmed_white, confirmed_black,
                               notes)

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
    alive = alive_players(state)
    alive_set = {p["name"] for p in alive}
    notes = load_notes()

    all_black, all_white = _confirmed_info(state, notes)
    confirmed_black = [n for n in all_black if n in alive_set]
    confirmed_white = [n for n in all_white if n in alive_set]

    if confirmed_black:
        return confirmed_black[0]
    scores = _suspicion_scores(state, alive, confirmed_white, confirmed_black,
                               notes)
    if state["day"] == 1:
        seer_cos = [
            n for n, info in notes.get("public_co_claims", {}).items()
            if isinstance(info, dict) and info.get("role") == "seer"
            and n in alive_set
        ]
        if len(seer_cos) >= 2:
            pool = {n: scores[n] for n in seer_cos if n in scores}
            if pool:
                return max(pool, key=pool.get)
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

    同一 (actor, target, day) は再記録しない（シーン全文の再スキャンに対して冪等）。
    """
    notes = load_notes()
    claims = notes.setdefault("public_seer_claims", [])
    if any(c["actor"] == actor and c["target"] == target and c["day"] == day
           for c in claims):
        return
    claims.append({"actor": actor, "target": target, "result": result_jp, "day": day})
    save_notes(notes)


def record_public_medium_result(actor: str, target: str, result: str, day: int) -> None:
    """result: 'werewolf' | 'human'（同一 actor/target/day は再記録しない）"""
    notes = load_notes()
    results = notes.setdefault("public_medium_results", [])
    if any(r["actor"] == actor and r["target"] == target and r["day"] == day
           for r in results):
        return
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

    win = win_status(state)
    if win == "none":
        state["day"] += 1
        state["phase"] = "day_discussion"
    save_state(state)

    return {
        "victim": victim,
        "guarded": guarded,
        "win": win,
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
    wolf_names = {p["name"] for p in state["players"] if p["role"] == "werewolf"}
    village_vote_target = notes.get("village_vote_target")

    votes: dict[str, str] = {}
    for p in alive:
        if p["name"] == player:
            continue
        if p["role"] == "werewolf":
            # 潜伏最優先: 村の合意先に乗る。単独で占いCOへ入れる黒票は
            # 実戦では即バレの負け筋なのでやらない。
            cands = [x["name"] for x in alive
                     if x["name"] not in wolf_names and x["name"] != p["name"]]
            if not cands:
                continue
            if village_vote_target in cands:
                votes[p["name"]] = village_vote_target
            else:
                # 合意先が仲間狼（または未定）: 疑惑の高い村側へ票を逸らす
                susp = notes.get("npc_suspicion_avg", {})
                scored = [n for n in cands if n in susp]
                votes[p["name"]] = (
                    max(scored, key=susp.get) if scored else random.choice(cands)
                )
        elif p["role"] == "madman":
            me = p["name"]
            cands = [x["name"] for x in alive if x["name"] != me]
            if not cands:
                continue
            alive_set = {x["name"] for x in alive}
            seer_claims = notes.get("public_seer_claims", [])
            # 推定狼 = 他者（真占いの可能性が高い）による最新の黒宣言先。
            # 自分の宣言は騙りと知っているので除外。
            believed_wolf = next(
                (c["target"] for c in reversed(seer_claims)
                 if c["actor"] != me and c["result"] == "人狼"
                 and c["target"] in alive_set and c["target"] != me),
                None)
            # 自分が白を出した相手には入れない（騙りの自己整合）
            own_whites = {c["target"] for c in seer_claims
                          if c["actor"] == me and c["result"] != "人狼"}
            susp = notes.get("npc_suspicion_avg", {})
            if believed_wolf:
                others = [n for n in cands if n != believed_wolf]
                if not others:
                    votes[me] = believed_wolf  # 推定狼しか残っていない
                elif len(alive) <= 3:
                    # PP圏: 狼+狂で多数を握れる。推定狼を守り第三者へ票を合わせる
                    votes[me] = max(others, key=lambda n: susp.get(n, 0))
                else:
                    # 通常時も推定狼への投票は避け、村に紛れる
                    safe = [n for n in others if n not in own_whites] or others
                    votes[me] = (village_vote_target
                                 if village_vote_target in safe
                                 else random.choice(safe))
            else:
                safe = [n for n in cands if n not in own_whites] or cands
                votes[me] = (village_vote_target
                             if village_vote_target in safe
                             else random.choice(safe))
        elif p["role"] == "seer":
            # 真占い師は自分の黒結果（生存中）を最優先で吊りに行く
            my_blacks = [e["target"] for e in state["log"]
                         if e["type"] == "seer" and e.get("actor") == p["name"]
                         and e["result"] == "werewolf"
                         and any(x["name"] == e["target"] for x in alive)]
            if my_blacks:
                votes[p["name"]] = my_blacks[-1]
            else:
                target = _village_vote_candidate(state, alive, p["name"], village_vote_target)
                if target:
                    votes[p["name"]] = target
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

    npc_vote_details = {
        voter: {"target": t, "reason": _reason(t)}
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

    # 公開占い結果 = 宣言台帳（public_seer_claims）のみ。
    # 発表は orchestrator がテンプレ挿入と同時に必ず記録するため、
    # log の真結果をビューへ直接出す必要はない（出すと未発表の結果が
    # 全員に漏れ、真偽COの区別も構造的に漏れる）。
    # 発表済みの宣言は発表者の死後も公開情報として残す
    # （噛まれた占い師の遺した結果で村が推理するのは人狼の根幹）。
    public_seer_results: list[dict] = [
        {"actor": c["actor"], "target": c["target"],
         "result": c["result"], "day": c["day"]}
        for c in notes.get("public_seer_claims", [])
    ]

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
        # success = 同じ日・同じ対象への襲撃が guarded で終わったこと
        private["guard_history"] = [
            {
                "day": e["day"], "target": e["target"],
                "success": any(
                    a["type"] == "attack" and a["day"] == e["day"]
                    and a["target"] == e["target"]
                    and a.get("result") == "guarded"
                    for a in state["log"]
                ),
            }
            for e in state["log"]
            if e["type"] == "guard" and e.get("actor") == target_player
        ]
    elif role == "werewolf":
        # 人狼は自陣営の襲撃結果（成功/護衛された）を知っている
        private["attack_history"] = [
            {"day": e["day"], "target": e["target"], "result": e.get("result")}
            for e in state["log"] if e["type"] == "attack"
        ]

    # 役職構成は全員に公開のセットアップ情報（誰がどれかは含まない）
    composition: dict[str, int] = {}
    for q in state["players"]:
        composition[q["role"]] = composition.get(q["role"], 0) + 1

    return {
        "day": state["day"],
        "self": {
            "name": target_player,
            "role": role,
            "role_jp": role_jp,
        },
        "role_composition": composition,
        "wolf_teammates": wolf_teammates,
        "alive_players": alive_players_list,
        "dead_players": dead_players,
        "public_co_claims": public_co_claims,
        "public_seer_results": public_seer_results,
        "public_medium_results": public_medium_results,
        "execution_history": execution_history,
        "private": private,
    }
