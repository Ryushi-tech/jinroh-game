"""tests/test_view_filter.py

get_player_view() の視点フィルターが正しく機能しているかを検証するユニットテスト。

各テストは「知ってはいけない情報が1文字でも混入しないこと」を確認する。
対象: logic_engine.get_player_view()
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from logic_engine import get_player_view

# ---------------------------------------------------------------------------
# 共通テストデータ
# ---------------------------------------------------------------------------

def _p(name: str, role: str, alive: bool = True) -> dict:
    return {"name": name, "role": role, "alive": alive}


def _make_state(players: list, log: list | None = None, day: int = 2) -> dict:
    return {
        "day": day,
        "phase": "day_discussion",
        "players": players,
        "log": log or [],
    }


# 8人ゲーム構成
# アリス・ボブ: 人狼ペア
# カール: 占い師 (公開CO済み)
# ダナ: 占い師 (未CO ─ 対抗CO未実施)
# エミリー: 村人
# フランク: 狩人
# グレイス: 狂人
# ヘンリー: 霊媒師 (Day1 処刑済み)

PLAYERS = [
    _p("アリス",   "werewolf"),
    _p("ボブ",     "werewolf"),
    _p("カール",   "seer"),
    _p("ダナ",     "seer"),
    _p("エミリー", "villager"),
    _p("フランク", "bodyguard"),
    _p("グレイス", "madman"),
    _p("ヘンリー", "medium", alive=False),  # Day1 処刑済み
]

LOG = [
    # Day1 処刑: ヘンリー（alignment付き ─ フィルター対象）
    {
        "type": "execute", "day": 1, "target": "ヘンリー",
        "alignment": "villager",
        "tally": {"ヘンリー": 5, "アリス": 2},
        "votes": {"エミリー": "ヘンリー", "フランク": "ヘンリー"},
    },
    # Day1 夜: カール(公開CO済み)がアリスを占って人狼を検出
    {
        "type": "seer", "day": 1, "actor": "カール",
        "target": "アリス", "result": "werewolf",
    },
    # Day1 夜: ダナ(未CO)がボブを占って人狼を検出 ─ 非公開
    {
        "type": "seer", "day": 1, "actor": "ダナ",
        "target": "ボブ", "result": "werewolf",
    },
    # Day1 夜: フランク(狩人)がカールを護衛 ─ 非公開
    {
        "type": "guard", "day": 1, "actor": "フランク",
        "target": "カール",
    },
    # Day1 夜: 狼がエミリーを襲撃
    {
        "type": "attack", "day": 1, "target": "エミリー",
        "result": "killed",
    },
]

# notes: カールのみ公開CO済み。ダナは未CO。
# 内部情報（wolf_accusations, npc_suspicion_avg 等）も含む。
NOTES = {
    "public_co_claims": {
        "カール": {"role": "seer", "day": 1},
    },
    "public_medium_results": [],
    # 以下は内部情報 ─ view に漏洩してはならない
    "wolf_accusations": {"アリス": ["カール"]},
    "npc_suspicion_avg": {"アリス": 8.5, "ボブ": 3.0},
}

ALL_ROLE_KEYWORDS = [
    "werewolf", "seer", "villager", "medium", "bodyguard", "madman",
    "人狼", "占い師", "村人", "霊媒師", "狩人", "狂人",
]


# ---------------------------------------------------------------------------
# テスト 1: 村人は wolf_teammates が空
# ---------------------------------------------------------------------------

def test_01_villager_wolf_teammates_empty():
    """村人視点: wolf_teammates が空リストであること。"""
    view = get_player_view(_make_state(PLAYERS, LOG), "エミリー", NOTES)
    assert view["wolf_teammates"] == [], (
        f"村人の wolf_teammates が空でない: {view['wolf_teammates']}"
    )


# ---------------------------------------------------------------------------
# テスト 2: 狼は仲間の名前だけを知っている
# ---------------------------------------------------------------------------

def test_02_wolf_knows_own_teammate_only():
    """狼アリス視点: wolf_teammates にボブの名前のみ含まれること。"""
    view = get_player_view(_make_state(PLAYERS, LOG), "アリス", NOTES)
    assert view["wolf_teammates"] == ["ボブ"], (
        f"狼の wolf_teammates が期待値と異なる: {view['wolf_teammates']}"
    )


# ---------------------------------------------------------------------------
# テスト 3: 狂人は狼の仲間を知らない
# ---------------------------------------------------------------------------

def test_03_madman_wolf_teammates_empty():
    """狂人グレイス視点: wolf_teammates が空リストであること。"""
    view = get_player_view(_make_state(PLAYERS, LOG), "グレイス", NOTES)
    assert view["wolf_teammates"] == [], (
        f"狂人の wolf_teammates が空でない: {view['wolf_teammates']}"
    )


# ---------------------------------------------------------------------------
# テスト 4: 占い師は狼の仲間を知らない
# ---------------------------------------------------------------------------

def test_04_seer_wolf_teammates_empty():
    """占い師カール視点: wolf_teammates が空リストであること。"""
    view = get_player_view(_make_state(PLAYERS, LOG), "カール", NOTES)
    assert view["wolf_teammates"] == [], (
        f"占い師の wolf_teammates が空でない: {view['wolf_teammates']}"
    )


# ---------------------------------------------------------------------------
# テスト 5: 処刑履歴に alignment フィールドが含まれない
# ---------------------------------------------------------------------------

def test_05_execution_history_no_alignment():
    """execution_history の各エントリに alignment フィールドがないこと。"""
    view = get_player_view(_make_state(PLAYERS, LOG), "エミリー", NOTES)
    for entry in view["execution_history"]:
        assert "alignment" not in entry, (
            f"execution_history に alignment が漏洩: {entry}"
        )


# ---------------------------------------------------------------------------
# テスト 6: 死亡者の cause に役職名が含まれない
# ---------------------------------------------------------------------------

def test_06_dead_players_cause_no_role_name():
    """dead_players の cause フィールドに役職名が含まれないこと。"""
    view = get_player_view(_make_state(PLAYERS, LOG), "エミリー", NOTES)
    for dead in view["dead_players"]:
        cause = dead["cause"]
        for kw in ALL_ROLE_KEYWORDS:
            assert kw not in cause, (
                f"dead_players[{dead['name']}].cause に役職キーワード漏洩: {kw!r} in {cause!r}"
            )


# ---------------------------------------------------------------------------
# テスト 7: 未CO占い師ダナの占い結果は public_seer_results に出ない
# ---------------------------------------------------------------------------

def test_07_unco_seer_result_excluded():
    """COしていない占い師ダナの結果が public_seer_results に含まれないこと。"""
    view = get_player_view(_make_state(PLAYERS, LOG), "エミリー", NOTES)
    actors = [r["actor"] for r in view["public_seer_results"]]
    assert "ダナ" not in actors, (
        f"未CO占い師ダナの結果が漏洩: {view['public_seer_results']}"
    )


# ---------------------------------------------------------------------------
# テスト 8: 死亡したCO済み占い師の結果は public_seer_results に出ない
# ---------------------------------------------------------------------------

def test_08_dead_co_seer_result_excluded():
    """CO済みでも死亡した占い師の結果が public_seer_results に含まれないこと。"""
    players_dead_seer = [
        _p("アリス",   "werewolf"),
        _p("ボブ",     "werewolf"),
        _p("カール",   "seer", alive=False),   # CO済みだが死亡
        _p("エミリー", "villager"),
        _p("グレイス", "madman"),
    ]
    # notes の public_co_claims にはカールの CO 記録が残ったまま
    view = get_player_view(_make_state(players_dead_seer, LOG), "エミリー", NOTES)
    actors = [r["actor"] for r in view["public_seer_results"]]
    assert "カール" not in actors, (
        f"死亡したCO済み占い師カールの結果が漏洩: {view['public_seer_results']}"
    )


# ---------------------------------------------------------------------------
# テスト 9: guard / attack ログが view の構造フィールドに混入しない
# ---------------------------------------------------------------------------

def test_09_guard_and_attack_log_not_exposed():
    """護衛ログ・襲撃ログが execution_history / public_seer_results に混入しないこと。"""
    view = get_player_view(_make_state(PLAYERS, LOG), "エミリー", NOTES)

    for entry in view.get("execution_history", []):
        assert entry.get("type") not in ("guard", "attack"), (
            f"execution_history に guard/attack ログが混入: {entry}"
        )

    for entry in view.get("public_seer_results", []):
        # public_seer_results のエントリは actor/target/result/day のみ
        assert "type" not in entry or entry["type"] == "seer", (
            f"public_seer_results に不正なエントリが混入: {entry}"
        )

    # 護衛先（カール）がどのフィールドでも「護衛」として登場しないこと
    for dead in view.get("dead_players", []):
        assert "護衛" not in dead.get("cause", ""), (
            f"dead_players.cause に護衛情報が混入: {dead}"
        )


# ---------------------------------------------------------------------------
# テスト 10: alive_players / dead_players に role フィールドが混入しない
# ---------------------------------------------------------------------------

def test_10_player_lists_contain_no_role_field():
    """alive_players は文字列リスト、dead_players の各エントリには role フィールドがないこと。"""
    view = get_player_view(_make_state(PLAYERS, LOG), "エミリー", NOTES)

    # alive_players: 名前の文字列リストのみ
    for entry in view["alive_players"]:
        assert isinstance(entry, str), (
            f"alive_players の要素が文字列でない: {entry!r}"
        )

    # dead_players: 各エントリは name と cause のみ（role フィールドなし）
    for entry in view["dead_players"]:
        assert "role" not in entry, (
            f"dead_players に role フィールドが漏洩: {entry}"
        )
        allowed_keys = {"name", "cause"}
        extra_keys = set(entry.keys()) - allowed_keys
        assert not extra_keys, (
            f"dead_players に予期しないキーが混入: {extra_keys}"
        )
