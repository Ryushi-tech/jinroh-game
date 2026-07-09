#!/usr/bin/env python3
"""発言プランナー: NPCの発言内容をエンジン側で決定する。

「LLMの関与を口調の翻訳に限定する」方針の中核モジュール。
誰を疑うか・その根拠・どの論点に触れるか・投票意思をすべて
台帳（public_co_claims / public_seer_claims / public_medium_results）と
log の公開事実から機械的に組み立てる。

- 各 act は text_jp（中立的な日本語の内容文）を持つ。
  レンダラー（npc_agent）はこれをキャラ口調の1〜3文に変換するだけで、
  新しい事実・名前・結果を追加してはならない。
- fallback_text は LLM が失敗したときにそのままシーンへ出せる定型文。
  NPC が沈黙せず、事実も壊れないことを構造的に保証する。
- 乱数はすべて決定的シード（day/disc/npc）。同一盤面なら同一プラン。

秘匿: プランは NPC 自身の役職知識（人狼の仲間、真占いの実結果、
狂人の推定狼）を使うが、text_jp には公開情報として語れる内容しか
書かない（「私は人狼だから」という根拠は生成しない）。
"""

from __future__ import annotations

import random

import engine

# 1ターンの最大 act 数（応答 act を除く）
_MAX_ACTS = 2


# ---------------------------------------------------------------------------
# 台帳ヘルパー
# ---------------------------------------------------------------------------

def _seer_claims_by_target(notes: dict) -> dict[str, list[dict]]:
    by_target: dict[str, list[dict]] = {}
    for c in notes.get("public_seer_claims", []):
        by_target.setdefault(c["target"], []).append(c)
    return by_target


def _undisputed_black_claims(notes: dict) -> list[dict]:
    """白宣言と係争していない黒宣言のリスト。"""
    out = []
    for target, cs in _seer_claims_by_target(notes).items():
        results = {c["result"] for c in cs}
        if results == {"人狼"}:
            out.extend(c for c in cs if c["result"] == "人狼")
    return out


def _conflicted_targets(notes: dict) -> dict[str, list[str]]:
    """黒白が食い違っている target → 宣言actor一覧。"""
    out: dict[str, list[str]] = {}
    for target, cs in _seer_claims_by_target(notes).items():
        if len({c["result"] for c in cs}) > 1:
            out[target] = sorted({c["actor"] for c in cs})
    return out


def _seer_co_names(notes: dict, alive: set[str]) -> list[str]:
    return [n for n, i in notes.get("public_co_claims", {}).items()
            if isinstance(i, dict) and i.get("role") == "seer" and n in alive]


def _last_execute(state: dict) -> dict | None:
    for e in reversed(state["log"]):
        if e["type"] == "execute":
            return e
    return None


def _previous_execute(state: dict, current_day: int | None = None) -> dict | None:
    """当日より前の処刑記録（投票宣言の「昨日」用）。"""
    day_limit = current_day if current_day is not None else state.get("day", 0)
    for e in reversed(state["log"]):
        if e["type"] == "execute" and e.get("day", 0) < day_limit:
            return e
    return None


def _believed_wolf(notes: dict, me: str, alive: set[str]) -> str | None:
    """狂人視点の推定狼 = 自分以外のactorによる最新の黒宣言先。"""
    for c in reversed(notes.get("public_seer_claims", [])):
        if (c["actor"] != me and c["result"] == "人狼"
                and c["target"] in alive and c["target"] != me):
            return c["target"]
    return None


def _player_asks_seer_target(msg: str) -> bool:
    """プレイヤーが占い師へ占い先を尋ねているか。"""
    if not msg:
        return False
    keys = ("誰を占", "占いたい", "占う相手", "占い先", "占うつもり", "占う予定")
    return any(k in msg for k in keys)


def _seer_target_reply(state: dict, npc_name: str, rng: random.Random) -> tuple[str, str]:
    """占い師CO者が占い先について答える内容（初日占いなしルール準拠）。"""
    alive = [p["name"] for p in engine.alive_players(state)
             if p["name"] != npc_name]
    if not alive:
        return "今夜占う相手はまだ決めていない", ""
    pick = rng.choice(alive)
    if state["day"] == 1:
        text = f"まだ占っていないが、今夜は{pick}を占う予定だ"
    else:
        text = f"今夜は{pick}を占うつもりだ"
    return text, pick


# ---------------------------------------------------------------------------
# 論点（topic）の収集: 「まだこのNPCが触れていない公開の出来事」
# ---------------------------------------------------------------------------

def _gather_topics(state: dict, notes: dict, npc_name: str,
                   alive: set[str]) -> list[dict]:
    """コメント候補の論点を作る。key は spoken_topics の重複防止に使う。"""
    topics: list[dict] = []

    # 占い結果の衝突（最重要の公開論点）
    for target, actors in _conflicted_targets(notes).items():
        if target not in alive:
            continue
        others = [a for a in actors if a != npc_name]
        if not others:
            continue
        topics.append({
            "key": f"conflict:{target}",
            "priority": 0,
            "act": {
                "type": "note_conflict", "target": target, "actors": actors,
                "text_jp": (
                    f"{'と'.join(actors)}の占い結果が{target}を巡って"
                    "食い違っている。少なくとも一方は騙りだ"
                ),
            },
            "must": [target],
        })

    # 係争のない黒宣言（自分の宣言は accuse 側で扱うため他者分のみ）
    for c in _undisputed_black_claims(notes):
        if c["actor"] == npc_name or c["target"] not in alive:
            continue
        topics.append({
            "key": f"black:{c['actor']}:{c['target']}",
            "priority": 1,
            "act": {
                "type": "note_black_claim",
                "target": c["target"], "actor": c["actor"],
                "text_jp": (
                    f"{c['actor']}が{c['target']}に黒を出している。"
                    "この宣言をどう扱うかが今日の軸だ"
                ),
            },
            "must": [c["target"]],
        })

    # 対抗占いCO（結果がまだ無くても真偽は論点になる）
    seer_cos = _seer_co_names(notes, alive)
    if len(seer_cos) >= 2:
        others = [n for n in seer_cos if n != npc_name]
        if others:
            topics.append({
                "key": "rival_seer_co",
                "priority": 2,
                "act": {
                    "type": "note_rival_co", "actors": seer_cos,
                    "text_jp": (
                        f"占い師COが{'と'.join(seer_cos)}で対抗している。"
                        "どちらかは騙りで、その周辺に人狼陣営がいる"
                    ),
                },
                "must": [],
            })

    # 昨日の投票の単独行動（個票から機械抽出）
    last_exec = _last_execute(state)
    if last_exec and last_exec.get("votes"):
        votes: dict[str, str] = last_exec["votes"]
        for voter, target in votes.items():
            if voter == npc_name or voter not in alive:
                continue
            same = [v for v, t in votes.items() if t == target]
            if len(same) == 1 and target != last_exec["target"]:
                topics.append({
                    "key": f"lone_vote:{last_exec['day']}:{voter}",
                    "priority": 3,
                    "act": {
                        "type": "note_lone_vote",
                        "target": voter, "voted": target,
                        "text_jp": (
                            f"昨日の投票で{voter}だけが{target}に入れていた。"
                            "多数派と違う動きの理由を聞きたい"
                        ),
                    },
                    "must": [voter],
                })

    return topics


# ---------------------------------------------------------------------------
# 疑い先とその根拠
# ---------------------------------------------------------------------------

def _grounded_reason(state: dict, notes: dict, npc_name: str,
                     target: str) -> str:
    """target を疑う根拠を公開事実から選ぶ（必ず何か返す）。"""
    for c in _undisputed_black_claims(notes):
        if c["target"] == target:
            if c["actor"] == npc_name:
                return f"私が出した黒の通り、{target}が人狼だと考えている"
            return f"{c['actor']}の黒宣言が出ている{target}を放置できない"
    if target in _conflicted_targets(notes):
        return f"{target}を巡る占い結果が割れている以上、真偽を投票で確かめるしかない"
    alive = {p["name"] for p in engine.alive_players(state)}
    if target in _seer_co_names(notes, alive):
        return f"対抗している{target}の占いCOは騙りだと見ている"
    prev_exec = _previous_execute(state, state.get("day", 0))
    if prev_exec and prev_exec.get("votes"):
        votes = prev_exec["votes"]
        if votes.get(target) and votes[target] != prev_exec["target"]:
            return f"昨日{target}が多数派と違う投票をしていたのが引っかかる"
        if votes.get(target) == npc_name:
            return f"昨日{target}が私に票を入れてきた、その理由が説明されていない"
    co_claims = notes.get("public_co_claims", {})
    if target not in co_claims:
        if state.get("day", 0) == 1:
            return f"{target}はまだあまり発言していないが、様子を見ている"
        return f"{target}はCOも情報も出していないグレーで、消去法で最も疑わしい"
    return f"{target}の発言は情報を出しているようで盤面を進めていない"


def _choose_accuse_target(state: dict, notes: dict, npc_name: str,
                          player: str, rng: random.Random) -> str | None:
    """役職知識と疑惑スコアから疑い先を選ぶ。"""
    me = engine.get_player(state, npc_name)
    alive = engine.alive_players(state)
    alive_names = {p["name"] for p in alive}
    others = [p["name"] for p in alive if p["name"] != npc_name]
    if not others:
        return None
    role = me["role"]
    day = state["day"]
    rivals = [n for n in _seer_co_names(notes, alive_names) if n != npc_name]

    if role == "seer":
        my_blacks = [e["target"] for e in state["log"]
                     if e["type"] == "seer" and e.get("actor") == npc_name
                     and e["result"] == "werewolf"
                     and e["target"] in alive_names]
        if my_blacks:
            return my_blacks[-1]
        if rivals:
            return rivals[0]
        if day == 1:
            return None  # 対抗未発生の初日はグレー吊りしない

    if role == "madman":
        if rivals:
            return rivals[0]
        bw = _believed_wolf(notes, npc_name, alive_names)
        if bw:
            # 推定狼に黒を出した相手（真占いの可能性大）を偽と決めつけて庇う
            accuser = next(
                (c["actor"] for c in reversed(notes.get("public_seer_claims", []))
                 if c["target"] == bw and c["result"] == "人狼"
                 and c["actor"] in alive_names and c["actor"] != npc_name),
                None)
            if accuser:
                return accuser

    susp = engine.compute_npc_suspicion(state, notes, player)
    own = dict(susp.get("by_rater", {}).get(npc_name, {}))
    own = {t: s for t, s in own.items() if t in others}

    if role == "werewolf":
        wolf_names = {p["name"] for p in state["players"]
                      if p["role"] == "werewolf"}
        own = {t: s for t, s in own.items() if t not in wolf_names}

    if day == 1 and len(_seer_co_names(notes, alive_names)) >= 2:
        seer_rivals = [n for n in _seer_co_names(notes, alive_names)
                       if n != npc_name and n in others]
        if seer_rivals and role != "werewolf":
            return rng.choice(seer_rivals)

    if own:
        top = max(own.values())
        cands = sorted(t for t, s in own.items() if s >= top - 0.01)
        return rng.choice(cands)
    return rng.choice(others)


# ---------------------------------------------------------------------------
# プラン構築（公開API）
# ---------------------------------------------------------------------------

def build_speech_plan(state: dict, notes: dict, npc_name: str, player: str,
                      *, respond_to_player: bool = False,
                      disc_index: int = 1,
                      co_inserted: str | None = None,
                      results_inserted: list[dict] | None = None,
                      player_message: str = "") -> dict:
    """このターンにNPCが言うべき内容を決定する。

    co_inserted / results_inserted: orchestrator がテンプレ挿入するCO・結果
    （挿入済みの前提でプランを組む。例: 初日CO直後は「結果はまだ無い」）。

    Returns: {
      "acts": [ {type, text_jp, ...} ],
      "must_mention": [レンダリング文に必ず含める名前],
      "topic_keys": [消費した論点キー（orchestrator が spoken_topics へ記録）],
      "fallback_text": str,  # LLM失敗時にそのまま使える定型文
    }
    """
    day = state["day"]
    alive_names = {p["name"] for p in engine.alive_players(state)}
    rng = random.Random(f"{day}:{disc_index}:{npc_name}")
    spoken = set(notes.get("spoken_topics", {}).get(npc_name, []))

    acts: list[dict] = []
    must: list[str] = []
    topic_keys: list[str] = []
    seer_cos = _seer_co_names(notes, alive_names)

    # 1) プレイヤー応答
    if respond_to_player and player_message:
        if _player_asks_seer_target(player_message):
            if npc_name in seer_cos:
                reply, pick = _seer_target_reply(state, npc_name, rng)
                acts.append({
                    "type": "answer_seer_target",
                    "text_jp": reply,
                })
                if pick:
                    must.append(pick)
                respond_to_player = False
            else:
                acts.append({
                    "type": "deflect_seer_question",
                    "text_jp": (
                        "占い先の質問は占い師COした"
                        f"{'と'.join(seer_cos)}に聞くべきだと伝える"
                    ),
                })
                respond_to_player = False
        if respond_to_player:
            acts.append({
                "type": "respond_player",
                "text_jp": (
                    f"プレイヤー（{player}）の「{player_message[:40]}」に"
                    "直接答える"
                ),
            })

    # 2) CO・結果テンプレ挿入直後のフォロー
    if co_inserted == "seer" and day == 1:
        acts.append({
            "type": "after_co_day1",
            "text_jp": "初日はまだ占っていないため結果は無い、と正直に述べる",
        })
    for r in (results_inserted or []):
        if r.get("kind") == "seer" and r.get("result_jp") == "人狼":
            acts.append({
                "type": "push_own_black", "target": r["target"],
                "text_jp": f"発表した通り{r['target']}が人狼。今日の吊り先は{r['target']}しかない",
            })
            must.append(r["target"])

    # 3) 未消費の論点に1つ反応する
    remaining = _MAX_ACTS - max(0, len(acts) - (1 if respond_to_player else 0))
    if remaining > 0:
        topics = [t for t in _gather_topics(state, notes, npc_name, alive_names)
                  if t["key"] not in spoken]
        topics.sort(key=lambda t: (t["priority"], t["key"]))
        if topics:
            top_p = topics[0]["priority"]
            pick = rng.choice([t for t in topics if t["priority"] == top_p])
            acts.append(pick["act"])
            must.extend(pick["must"])
            topic_keys.append(pick["key"])
            remaining -= 1

    # 4) 疑い先の表明（同一日・同一対象の繰り返しは省略）
    if remaining > 0 and not any(a["type"] == "push_own_black" for a in acts):
        target = _choose_accuse_target(state, notes, npc_name, player, rng)
        prev = notes.get("last_accuse", {}).get(npc_name, {})
        duplicate = (target and prev.get("day") == day
                     and prev.get("target") == target)
        if target and not duplicate:
            reason = _grounded_reason(state, notes, npc_name, target)
            acts.append({
                "type": "accuse", "target": target, "reason_jp": reason,
                "text_jp": f"{reason}。今は{target}を最も疑っている",
            })
            must.append(target)

    # 5) 投票意思（各NPC・各日1回まで）
    stated_today = notes.get("vote_intent_stated", {}).get(str(day), [])
    if disc_index >= 2 and npc_name not in stated_today:
        accuse = next((a for a in acts if a["type"] in ("accuse", "push_own_black")), None)
        if accuse:
            acts.append({
                "type": "vote_intent", "target": accuse["target"],
                "text_jp": f"このままなら投票は{accuse['target']}に入れるつもりだ",
            })

    if not acts:
        acts.append({
            "type": "observe",
            "text_jp": "新しい論点は無いが、様子を見ている",
        })

    fallback = "。".join(a["text_jp"] for a in acts
                          if a["type"] != "respond_player")
    if fallback:
        fallback += "。"

    return {
        "acts": acts,
        "must_mention": sorted(set(must)),
        "topic_keys": topic_keys,
        "fallback_text": fallback or "……様子を見ている。",
    }


def mark_topics_spoken(notes: dict, npc_name: str, topic_keys: list[str]) -> None:
    """消費した論点を notes に記録する（同じ論点の繰り返し防止）。

    呼び出し側が engine.save_notes(notes) を行うこと。
    """
    if not topic_keys:
        return
    spoken = notes.setdefault("spoken_topics", {})
    lst = spoken.setdefault(npc_name, [])
    for k in topic_keys:
        if k not in lst:
            lst.append(k)


def record_speech_memory(notes: dict, npc_name: str, plan: dict, day: int) -> None:
    """疑い先・投票意思の重複防止用メモを記録する。"""
    for act in plan.get("acts", []):
        if act["type"] in ("accuse", "push_own_black"):
            notes.setdefault("last_accuse", {})[npc_name] = {
                "day": day, "target": act["target"],
            }
        if act["type"] == "vote_intent":
            notes.setdefault("vote_intent_stated", {}).setdefault(
                str(day), []).append(npc_name)
