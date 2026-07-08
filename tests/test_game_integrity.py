"""tests/test_game_integrity.py

ゲームの論理整合性に関するユニットテスト。

Group 1 (test_11–15): 占い結果の時系列整合性
Group 2 (test_21–30): 投票先とセリフの最終一致確認
Group 3 (test_31–36): 死人に口なし フィルター
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from engine import get_player_view
from scene_checks import _extract_vote_target, check_vote_consistency
import npc_agent


# ---------------------------------------------------------------------------
# 共通ヘルパー
# ---------------------------------------------------------------------------

def _p(name: str, role: str, alive: bool = True) -> dict:
    return {"name": name, "role": role, "alive": alive}


def _make_state(players: list, log: list | None = None, day: int = 3) -> dict:
    return {
        "day": day,
        "phase": "day_discussion",
        "players": players,
        "log": log or [],
    }


# ---------------------------------------------------------------------------
# Group 1: 占い結果の時系列整合性
# ---------------------------------------------------------------------------

PLAYERS_SEER = [
    _p("アリス",   "werewolf"),
    _p("ボブ",     "villager"),
    _p("カール",   "seer"),
    _p("ダナ",     "villager"),
    _p("エミリー", "villager"),
]

NOTES_SEER = {
    "public_co_claims": {
        "カール": {"role": "seer", "day": 1},
    },
    "public_medium_results": [],
}

# Day1夜: アリス → werewolf  /  Day2夜: ボブ → not_werewolf
LOG_SEER_CORRECT = [
    {"type": "seer", "day": 1, "actor": "カール", "target": "アリス", "result": "werewolf"},
    {"type": "seer", "day": 2, "actor": "カール", "target": "ボブ",   "result": "not_werewolf"},
]


def test_11_seer_result_day1_attributed_correctly():
    """Day1の占い結果が day=1 で返されること。"""
    view = get_player_view(_make_state(PLAYERS_SEER, LOG_SEER_CORRECT, day=3), "ダナ", NOTES_SEER)
    day1_results = [r for r in view["public_seer_results"] if r["target"] == "アリス"]
    assert len(day1_results) == 1, (
        f"アリスへの占い結果が1件でない: {view['public_seer_results']}"
    )
    assert day1_results[0]["day"] == 1, (
        f"Day1占い結果の day フィールドが 1 でない: {day1_results[0]}"
    )


def test_12_seer_result_day2_attributed_correctly():
    """Day2の占い結果が day=2 で返されること（Day1のAとDay2のBが混同されない）。"""
    view = get_player_view(_make_state(PLAYERS_SEER, LOG_SEER_CORRECT, day=3), "ダナ", NOTES_SEER)
    day2_results = [r for r in view["public_seer_results"] if r["target"] == "ボブ"]
    assert len(day2_results) == 1, (
        f"ボブへの占い結果が1件でない: {view['public_seer_results']}"
    )
    assert day2_results[0]["day"] == 2, (
        f"Day2占い結果の day フィールドが 2 でない: {day2_results[0]}"
    )


def test_13_duplicate_seer_target_both_entries_in_view():
    """同一ターゲットへの重複占いが log にある場合、両エントリが public_seer_results に出ること。

    view はログをそのまま反映する（フィルタではなくミラー）。
    重複の *検出* は autoplay 層の責務（test_14 で確認）。
    """
    log_with_dup = [
        {"type": "seer", "day": 1, "actor": "カール", "target": "アリス", "result": "werewolf"},
        {"type": "seer", "day": 2, "actor": "カール", "target": "アリス", "result": "werewolf"},
    ]
    view = get_player_view(_make_state(PLAYERS_SEER, log_with_dup, day=3), "ダナ", NOTES_SEER)
    alice_results = [r for r in view["public_seer_results"] if r["target"] == "アリス"]
    assert len(alice_results) == 2, (
        f"重複占いの両エントリが view に含まれるべき（2件期待）: {view['public_seer_results']}"
    )


def test_14_duplicate_seer_detection_logic():
    """autoplay.check_day_state 相当の重複占い検出ロジックが正しく動くこと。

    同一 target が log に2件あれば dupes に検出される。
    """
    log = [
        {"type": "seer", "day": 1, "actor": "カール", "target": "アリス", "result": "werewolf"},
        {"type": "seer", "day": 2, "actor": "カール", "target": "アリス", "result": "werewolf"},
    ]
    seer_targets = [e["target"] for e in log if e["type"] == "seer"]
    dupes = [t for t in set(seer_targets) if seer_targets.count(t) > 1]
    assert "アリス" in dupes, f"重複占い検出失敗: dupes={dupes}"


def test_15_no_cross_day_result_leakage():
    """異なる日の占い結果が互いの day フィールドに混入しないこと。

    「1日目にAを占ったのに、2日目のBの結果としてAが出る」論理のねじれがないことを確認。
    """
    view = get_player_view(_make_state(PLAYERS_SEER, LOG_SEER_CORRECT, day=3), "ダナ", NOTES_SEER)
    for r in view["public_seer_results"]:
        if r["target"] == "アリス":
            assert r["day"] == 1, (
                f"アリスの result.day が 1 でない（クロスデイ混入）: {r}"
            )
        elif r["target"] == "ボブ":
            assert r["day"] == 2, (
                f"ボブの result.day が 2 でない（クロスデイ混入）: {r}"
            )


# ---------------------------------------------------------------------------
# Group 2: 投票先とセリフの最終一致確認
# ---------------------------------------------------------------------------

ALIVE_NAMES = ["アリス", "ボブ", "カール", "ダナ", "エミリー"]


def test_21_extract_vote_target_ni_tohyo():
    """「に投票」フレーズから投票先を正しく抽出できること。"""
    result = _extract_vote_target("私はアリスに投票します", ALIVE_NAMES)
    assert result == "アリス", f"投票先の抽出失敗: {result!r}"


def test_22_extract_vote_target_wo_tsuri():
    """「を吊」フレーズから投票先を正しく抽出できること。"""
    result = _extract_vote_target("ボブを吊りましょう", ALIVE_NAMES)
    assert result == "ボブ", f"投票先の抽出失敗: {result!r}"


def test_23_extract_vote_target_ni_ippyo():
    """「に一票」フレーズから投票先を正しく抽出できること。"""
    result = _extract_vote_target("カールに一票入れます", ALIVE_NAMES)
    assert result == "カール", f"投票先の抽出失敗: {result!r}"


def test_24_extract_vote_target_no_phrase_returns_none():
    """投票フレーズが一切ない場合は None を返すこと。"""
    result = _extract_vote_target("まだ考え中です", ALIVE_NAMES)
    assert result is None, f"投票先なしのはずが {result!r} が抽出された"


def test_25_extract_vote_target_not_in_candidates():
    """候補者リストにない名前にはマッチしないこと。"""
    result = _extract_vote_target("ジョンに投票します", ALIVE_NAMES)
    assert result is None, f"候補外の名前にマッチしてしまった: {result!r}"


def test_26_check_vote_consistency_match(tmp_path):
    """セリフと npc_votes が一致する場合は issues が空であること。"""
    scene = tmp_path / "vote.txt"
    scene.write_text('アリス「ボブに投票します」\n', encoding="utf-8")
    npc_votes = {"アリス": {"target": "ボブ", "reason": "consensus", "role_hint": "villager"}}
    issues = check_vote_consistency(str(scene), npc_votes, "プレイヤー", ALIVE_NAMES)
    assert issues == [], f"一致しているのに issues が出た: {issues}"


def test_27_check_vote_consistency_mismatch_raises_error(tmp_path):
    """セリフと npc_votes が矛盾する場合は VOTE_CHECK_ERROR が返ること。"""
    scene = tmp_path / "vote.txt"
    scene.write_text('アリス「カールに投票します」\n', encoding="utf-8")
    # logic では ボブ に投票するはずが、セリフでは カール → 矛盾
    npc_votes = {"アリス": {"target": "ボブ", "reason": "consensus", "role_hint": "villager"}}
    issues = check_vote_consistency(str(scene), npc_votes, "プレイヤー", ALIVE_NAMES)
    errors = [i for i in issues if "ERROR" in i]
    assert errors, f"矛盾があるのに VOTE_CHECK_ERROR が出なかった: {issues}"
    # エラーメッセージに実際の名前が含まれること
    assert "カール" in errors[0] or "ボブ" in errors[0], (
        f"エラーメッセージに投票先名が含まれない: {errors[0]}"
    )


def test_28_check_vote_consistency_unreadable_raises_warn(tmp_path):
    """セリフから投票先を読み取れない場合は VOTE_CHECK_WARN が返ること。"""
    scene = tmp_path / "vote.txt"
    scene.write_text('アリス「うーん、難しいですね」\n', encoding="utf-8")
    npc_votes = {"アリス": {"target": "ボブ", "reason": "consensus", "role_hint": "villager"}}
    issues = check_vote_consistency(str(scene), npc_votes, "プレイヤー", ALIVE_NAMES)
    warns = [i for i in issues if "WARN" in i]
    assert warns, f"読み取れないのに VOTE_CHECK_WARN が出なかった: {issues}"


def test_29_check_vote_consistency_player_excluded(tmp_path):
    """プレイヤー自身は整合性チェックから除外されること。"""
    scene = tmp_path / "vote.txt"
    scene.write_text('アリス「ボブを処刑しましょう」\n', encoding="utf-8")
    npc_votes = {
        "アリス":     {"target": "ボブ",  "reason": "consensus",  "role_hint": "villager"},
        "プレイヤー": {"target": "ダナ",  "reason": "conviction", "role_hint": "villager"},
    }
    issues = check_vote_consistency(str(scene), npc_votes, "プレイヤー", ALIVE_NAMES)
    # プレイヤー自身に関するエラー・警告が出ないこと
    player_issues = [i for i in issues if "プレイヤー" in i]
    assert not player_issues, f"プレイヤーが除外されていない: {player_issues}"


def test_30_check_vote_consistency_multiple_npcs_partial(tmp_path):
    """複数 NPC のセリフを正しく区別し、一部一致・一部不一致を判定できること。"""
    scene = tmp_path / "vote.txt"
    scene.write_text(
        'アリス「ボブに投票します」\n'
        'カール「ダナに一票入れます」\n',
        encoding="utf-8",
    )
    npc_votes = {
        "アリス": {"target": "ボブ",    "reason": "consensus", "role_hint": "villager"},
        "カール": {"target": "エミリー", "reason": "pivot",     "role_hint": "villager"},
    }
    issues = check_vote_consistency(str(scene), npc_votes, "プレイヤー", ALIVE_NAMES)
    errors = [i for i in issues if "ERROR" in i]
    # アリスは一致、カールのみ矛盾（ダナ vs エミリー）
    assert len(errors) == 1, f"ERROR が1件でない: {errors}"
    assert "カール" in errors[0], f"カールのエラーでない: {errors[0]}"


# ---------------------------------------------------------------------------
# Group 3: 死人に口なし フィルター
# ---------------------------------------------------------------------------

PLAYERS_DEAD = [
    _p("アリス",   "werewolf"),
    _p("ボブ",     "villager"),
    _p("カール",   "seer",     alive=False),  # Day1 処刑済み
    _p("ダナ",     "villager", alive=False),  # Day1 夜 襲撃死
    _p("エミリー", "villager"),
]

LOG_DEAD = [
    {
        "type": "execute", "day": 1, "target": "カール",
        "alignment": "human", "tally": {"カール": 3}, "votes": {},
    },
    {"type": "attack", "day": 1, "target": "ダナ", "result": "killed"},
]

NOTES_DEAD: dict = {"public_co_claims": {}, "public_medium_results": []}


def test_31_dead_players_not_in_alive_list():
    """死亡者が alive_players に含まれないこと。"""
    view = get_player_view(_make_state(PLAYERS_DEAD, LOG_DEAD, day=2), "エミリー", NOTES_DEAD)
    for name in ("カール", "ダナ"):
        assert name not in view["alive_players"], (
            f"死亡者 {name} が alive_players に混入: {view['alive_players']}"
        )


def test_32_dead_players_present_in_dead_list():
    """死亡者が dead_players に正しく含まれること。"""
    view = get_player_view(_make_state(PLAYERS_DEAD, LOG_DEAD, day=2), "エミリー", NOTES_DEAD)
    dead_names = {d["name"] for d in view["dead_players"]}
    assert "カール" in dead_names, f"処刑者カールが dead_players にない: {dead_names}"
    assert "ダナ"   in dead_names, f"襲撃死者ダナが dead_players にない: {dead_names}"


def test_33_dead_cause_execution_label():
    """処刑者の cause が '処刑' を含む形式であること。"""
    view = get_player_view(_make_state(PLAYERS_DEAD, LOG_DEAD, day=2), "エミリー", NOTES_DEAD)
    karl = next((d for d in view["dead_players"] if d["name"] == "カール"), None)
    assert karl is not None, "カールが dead_players にない"
    assert "処刑" in karl["cause"], f"処刑の cause ラベルが不正: {karl['cause']!r}"


def test_34_dead_cause_attack_label():
    """襲撃死者の cause が '襲撃死' を含む形式であること。"""
    view = get_player_view(_make_state(PLAYERS_DEAD, LOG_DEAD, day=2), "エミリー", NOTES_DEAD)
    dana = next((d for d in view["dead_players"] if d["name"] == "ダナ"), None)
    assert dana is not None, "ダナが dead_players にない"
    assert "襲撃死" in dana["cause"], f"襲撃死の cause ラベルが不正: {dana['cause']!r}"


def test_35_npc_agent_uninit_returns_error_no_api_call():
    """バックエンドが未初期化の場合、generate_npc_message が API を呼ばずエラーを即返すこと。"""
    original = npc_agent._backend
    npc_agent._backend = None
    try:
        result = npc_agent.generate_npc_message(
            "カール",
            {
                "day": 2,
                "self": {"name": "カール", "role": "seer", "role_jp": "占い師"},
                "wolf_teammates": [],
                "alive_players": ["アリス", "カール", "エミリー"],
                "dead_players": [],
                "public_co_claims": {},
                "public_seer_results": [],
                "public_medium_results": [],
                "execution_history": [],
                "private": {},
            },
            {},
        )
        assert result["error"] is not None, (
            "error フィールドが None（未初期化エラーが報告されていない）"
        )
        assert result["message"] == "", (
            f"未初期化なのにメッセージが生成された: {result['message']!r}"
        )
    finally:
        npc_agent._backend = original


def test_36_npc_names_excludes_dead_players():
    """npc_names 構築ロジックが死亡者を除外すること。

    gemini_gm.py の [p["name"] for p in players if p["alive"] and p["name"] != player]
    パターンが正しく死亡者を除外することを単体で確認する。
    """
    player_name = "エミリー"
    npc_names = [
        p["name"] for p in PLAYERS_DEAD
        if p["alive"] and p["name"] != player_name
    ]
    assert "カール" not in npc_names, f"処刑済みカールが npc_names に含まれた: {npc_names}"
    assert "ダナ"   not in npc_names, f"襲撃死者ダナが npc_names に含まれた: {npc_names}"
    assert "アリス" in npc_names,     f"生存者アリスが npc_names にない: {npc_names}"
    assert "ボブ"   in npc_names,     f"生存者ボブが npc_names にない: {npc_names}"
