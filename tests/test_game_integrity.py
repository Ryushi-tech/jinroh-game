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

import engine
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

# 公開占い結果は「宣言台帳」（public_seer_claims）のみをミラーする。
# log の真結果は view に直接出さない（未発表の結果が漏れるため）。
NOTES_SEER = {
    "public_co_claims": {
        "カール": {"role": "seer", "day": 1},
    },
    "public_seer_claims": [
        {"actor": "カール", "target": "アリス", "result": "人狼", "day": 1},
        {"actor": "カール", "target": "ボブ", "result": "白（人間）", "day": 2},
    ],
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


def test_13_view_mirrors_claims_not_log():
    """公開占い結果は宣言台帳のみをミラーし、log の未発表結果は出ないこと。"""
    log_unannounced = [
        {"type": "seer", "day": 1, "actor": "カール", "target": "アリス", "result": "werewolf"},
        {"type": "seer", "day": 2, "actor": "カール", "target": "エミリー", "result": "werewolf"},
    ]
    notes = {
        "public_co_claims": {"カール": {"role": "seer", "day": 1}},
        "public_seer_claims": [
            {"actor": "カール", "target": "アリス", "result": "人狼", "day": 1},
        ],
        "public_medium_results": [],
    }
    view = get_player_view(_make_state(PLAYERS_SEER, log_unannounced, day=3), "ダナ", notes)
    targets = [r["target"] for r in view["public_seer_results"]]
    assert targets == ["アリス"], (
        f"未発表のエミリー結果が view に漏れている: {view['public_seer_results']}"
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
    npc_votes = {"アリス": {"target": "ボブ", "reason": "consensus"}}
    issues = check_vote_consistency(str(scene), npc_votes, "プレイヤー", ALIVE_NAMES)
    assert issues == [], f"一致しているのに issues が出た: {issues}"


def test_27_check_vote_consistency_mismatch_raises_error(tmp_path):
    """セリフと npc_votes が矛盾する場合は VOTE_CHECK_ERROR が返ること。"""
    scene = tmp_path / "vote.txt"
    scene.write_text('アリス「カールに投票します」\n', encoding="utf-8")
    # logic では ボブ に投票するはずが、セリフでは カール → 矛盾
    npc_votes = {"アリス": {"target": "ボブ", "reason": "consensus"}}
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
    npc_votes = {"アリス": {"target": "ボブ", "reason": "consensus"}}
    issues = check_vote_consistency(str(scene), npc_votes, "プレイヤー", ALIVE_NAMES)
    warns = [i for i in issues if "WARN" in i]
    assert warns, f"読み取れないのに VOTE_CHECK_WARN が出なかった: {issues}"


def test_29_check_vote_consistency_player_excluded(tmp_path):
    """プレイヤー自身は整合性チェックから除外されること。"""
    scene = tmp_path / "vote.txt"
    scene.write_text('アリス「ボブを処刑しましょう」\n', encoding="utf-8")
    npc_votes = {
        "アリス":     {"target": "ボブ",  "reason": "consensus"},
        "プレイヤー": {"target": "ダナ",  "reason": "conviction"},
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
        "アリス": {"target": "ボブ",    "reason": "consensus"},
        "カール": {"target": "エミリー", "reason": "pivot"},
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


def test_37_detect_role_co_seer_claims():
    from orchestrator import detect_role_co

    assert detect_role_co("私、占い師です。初日のため結果はまだありません。", "seer")
    assert detect_role_co("ほっほ、わしが占い師じゃよ——初日ゆえ結果はまだ無い。", "seer")
    assert not detect_role_co("占い師が二人出た以上、どちらかは嘘だと見ていい。", "seer")
    assert not detect_role_co("ジムゾン、占い師以外でいちばん黒い方。", "seer")
    # 誤検出しやすい「私、占い師の〜」形（COではない言及）
    assert not detect_role_co("私、占い師のCOを待ちたいと思います。", "seer")
    assert not detect_role_co("私、占い師の言葉を信じます。", "seer")
    assert not detect_role_co("わたし、霊媒師の結果が気になります。", "medium")


def test_38_format_vote_tally():
    from orchestrator import format_vote_tally

    votes = {"モーリッツ": "カタリナ", "カタリナ": "モーリッツ", "アリス": "モーリッツ"}
    text = format_vote_tally(votes, "モーリッツ")
    assert "―― 投票結果 ――" in text
    assert "モーリッツ → カタリナ" in text
    assert "アリス → モーリッツ" in text
    assert "処刑: モーリッツ" in text


def test_39_player_spoke_last_and_address():
    from npc_agent import player_spoke_last, last_player_speech, _addresses_player_message

    convo = "オットー「疑ってる」\n\nヤコブ「アルビンで確定か？」"
    assert player_spoke_last(convo, "ヤコブ")
    assert last_player_speech(convo, "ヤコブ") == "アルビンで確定か？"
    assert _addresses_player_message("ヤコブ、対抗COがなければアルビン確定でいい", "アルビンで確定か？", "ヤコブ")
    assert not _addresses_player_message("パメラの票意向を聞こう", "アルビンで確定か？", "ヤコブ")


def test_40_record_public_seer_claim_dedup(tmp_path, monkeypatch):
    """同一 (actor, target, day) の占い/霊媒結果が重複記録されないこと。"""
    import engine as eng
    notes_file = tmp_path / ".gm_notes.json"
    monkeypatch.setattr(eng, "NOTES_FILE", notes_file)

    eng.record_public_seer_claim("アリス", "ボブ", "白（人間）", 2)
    eng.record_public_seer_claim("アリス", "ボブ", "白（人間）", 2)
    eng.record_public_seer_claim("アリス", "カール", "人狼", 2)
    claims = eng.load_notes()["public_seer_claims"]
    assert len(claims) == 2

    eng.record_public_medium_result("ダナ", "ボブ", "human", 2)
    eng.record_public_medium_result("ダナ", "ボブ", "human", 2)
    results = eng.load_notes()["public_medium_results"]
    assert len(results) == 1


def test_41_co_misattribution_check():
    """CO誤帰属の機械検証（実プレイで起きたHaikuの誤読がテストケース）。"""
    from npc_agent import check_co_misattribution

    def make_view(co_claims):
        return {
            "alive_players": ["ヤコブ", "ディータ", "シモン", "レジーナ"],
            "dead_players": [{"name": "パメラ", "cause": "襲撃"}],
            "public_co_claims": co_claims,
        }

    no_co = make_view({})
    dieter_seer = make_view({"ディータ": {"role": "seer", "day": 1}})

    # 実プレイの誤読: COしていないヤコブへの「占い師CO」帰属 → 却下
    bad = "ヤコブの占い師CO自体は悪くない判断だ。"
    assert check_co_misattribution(bad, "シモン", no_co) is not None
    assert "ヤコブ" in check_co_misattribution(bad, "シモン", no_co)

    # 完了形の誤帰属 → 却下
    assert check_co_misattribution(
        "レジーナが占い師COした以上、対抗を待つべきだ。", "シモン", dieter_seer,
    ) is not None

    # 役職の取り違え（ディータは占い師COなのに霊媒師扱い）→ 却下
    assert check_co_misattribution(
        "ディータは霊媒師COだったな。", "シモン", dieter_seer,
    ) is not None

    # 騙り指摘はCOの存在が前提 → COしていない相手には却下
    assert check_co_misattribution(
        "ヤコブは占い師を騙っているのでは。", "シモン", no_co,
    ) is not None

    # --- 以下は正当な発言（誤爆させない） ---
    # 仮定「〜なら」（実プレイのオットーの発言）
    assert check_co_misattribution(
        "ただヤコブが占い師なら初日COは村にとっちゃ悪くない判断だろうよ。",
        "オットー", no_co,
    ) is None
    # COの要求・待望・助言
    assert check_co_misattribution(
        "占い師の即座のCOを求めるお考えですね。", "ジムゾン", no_co) is None
    assert check_co_misattribution(
        "レジーナのCOを待ちたい。", "シモン", no_co) is None
    assert check_co_misattribution(
        "ヤコブが占い師をCOした方がいいと思う。", "シモン", no_co) is None
    assert check_co_misattribution(
        "レジーナは狩人をCOしてほしい。", "シモン", no_co) is None
    # 台帳に存在するCOへの言及
    assert check_co_misattribution(
        "ディータの占い師COは初日として珍しい。", "ヨアヒム", dieter_seer,
    ) is None
    # 自分自身のCO（この発言自体で成立する）
    assert check_co_misattribution(
        "言っておくが、シモンが占い師だ。つまり俺のことだ。", "シモン", no_co,
    ) is None


def test_43_player_speech_distance_and_speaker_weights():
    """距離減衰するプレイヤー応答確率と、言及ブースト付き話者選出。"""
    from npc_agent import player_speech_distance, last_speech_line
    from orchestrator import respond_probability, speaker_weights, _MENTION_BOOST

    convo = (
        "ヤコブ「占い師はCOしてくれ」\n\n"
        "リーザ「ヤコブさん、急ぎすぎじゃない？」\n\n"
        "シモン「リーザの言う通りだ、まず様子を見よう」"
    )
    # ヤコブの発言から2発言経過
    assert player_speech_distance(convo, "ヤコブ") == 2
    assert player_speech_distance(convo, "シモン") == 0
    assert player_speech_distance(convo, "不在者") is None
    assert player_speech_distance("", "ヤコブ") is None

    # 距離が離れるほど応答確率が下がる（未発言は0）
    probs = [respond_probability(d) for d in (0, 1, 2, 3, 10)]
    assert probs == sorted(probs, reverse=True)
    assert respond_probability(0) < 1.0       # 直後でも100%にはしない
    assert respond_probability(10) > 0.0      # 遠くても完全には消えない
    assert respond_probability(None) == 0.0

    # 直前の発言で言及されたNPCがブーストされる（が確定ではない）
    queue = ["リーザ", "カタリナ", "フリーデル"]
    w = speaker_weights(queue, last_speech_line(convo))
    assert w[0] == _MENTION_BOOST      # シモンの発言中に「リーザ」
    assert w[1] == 1.0 and w[2] == 1.0
    assert w[0] < sum(w)               # 選出確率は100%未満

    # 会話が空なら全員均等
    assert speaker_weights(queue, None) == [1.0, 1.0, 1.0]


def test_44_generate_message_respond_flag():
    """respond_to_player フラグでプロンプトの応答指示が切り替わること。"""
    from npc_agent import build_npc_prompt
    from llm_backend import join_prompt

    view = {
        "day": 1,
        "self": {"name": "リーザ", "role": "villager", "role_jp": "村人"},
        "alive_players": ["リーザ", "ヤコブ"],
        "dead_players": [],
        "private": {},
    }
    convo = "ヤコブ「占い師はCOしてくれ」\n\nリーザ「様子を見ようよ」"

    # プレイヤーの発言が直前でなくても True なら応答指示が入る
    p = join_prompt(build_npc_prompt(
        "リーザ", view, {}, "", convo, "ヤコブ", respond_to_player=True))
    assert "プレイヤー（人間）の発言に応答せよ" in p

    # False なら直前がプレイヤーでも応答指示は入らない
    convo2 = "リーザ「様子を見ようよ」\n\nヤコブ「占い師はCOしてくれ」"
    p2 = join_prompt(build_npc_prompt(
        "リーザ", view, {}, "", convo2, "ヤコブ", respond_to_player=False))
    assert "応答せよ" not in p2

    # None（従来動作）: 直前がプレイヤーなら入る
    p3 = join_prompt(build_npc_prompt(
        "リーザ", view, {}, "", convo2, "ヤコブ", respond_to_player=None))
    assert "プレイヤー（人間）の発言に応答せよ" in p3


def test_42_setup_game_5_players(tmp_path, monkeypatch):
    """5人村構成（人狼1・狂人1・占い師1・村人2）でセットアップされること。"""
    import engine as eng
    monkeypatch.setattr(eng, "BASE_DIR", tmp_path)
    monkeypatch.setattr(eng, "STATE_FILE", tmp_path / "game_state.json")
    monkeypatch.setattr(eng, "NOTES_FILE", tmp_path / ".gm_notes.json")
    monkeypatch.setattr(eng, "PLAYER_FILE", tmp_path / ".player_name")

    info = eng.setup_game()
    state = eng.load_state()
    assert len(state["players"]) == 5
    roles = sorted(p["role"] for p in state["players"])
    assert roles == ["madman", "seer", "villager", "villager", "werewolf"]
    assert info["player"] in [p["name"] for p in state["players"]]

    # 視点ビューに役職構成（公開情報）が入ること
    view = eng.get_player_view(state, state["players"][0]["name"], {})
    assert view["role_composition"] == {
        "werewolf": 1, "madman": 1, "seer": 1, "villager": 2,
    }


def test_45_detect_role_co_casual_endings():
    """実ゲームで取りこぼした口語CO（語尾よ・助詞も）を検出できること。"""
    from orchestrator import detect_role_co

    # 実際に取りこぼした2件
    assert detect_role_co("私が占い師よ！バンバン狼を占ってやるわ！", "seer")
    assert detect_role_co("んー、実はおらも占い師だべ。", "seer")
    # 類似の口語形
    assert detect_role_co("僕が占い師なの。信じてほしい", "seer")
    assert detect_role_co("わしも霊媒師じゃ", "medium")
    # 誤検出しないこと
    assert not detect_role_co("私は占い師よりも霊媒師を重視したい", "seer")
    assert not detect_role_co("占い師が二人もいるのはおかしい", "seer")
    assert not detect_role_co("私は占い師を信じるわ", "seer")


def test_46_game_timeline():
    """state['log'] から事実の時系列テキストが作られること。"""
    from orchestrator import game_timeline

    state = {"log": [
        {"day": 1, "phase": "day_vote", "type": "execute", "target": "アルビン",
         "alignment": "werewolf", "tally": {"アルビン": 3, "ヤコブ": 1}},
        {"day": 1, "phase": "night", "type": "seer",
         "actor": "ヤコブ", "target": "ニコラス", "result": "not_werewolf"},
        {"day": 1, "phase": "night", "type": "attack",
         "target": "フリーデル", "result": "killed"},
    ]}
    text = game_timeline(state)
    assert "1日目の昼: 投票" in text and "アルビンを処刑" in text
    assert "ヤコブがニコラスを占い、結果は【人間】" in text
    assert "襲撃でフリーデルが死亡" in text

    assert game_timeline({"log": []}) == "- 記録された出来事なし"


def test_48_co_directives_and_player_co_template(tmp_path, monkeypatch):
    """CO のテンプレ化: NPCのCO指示計算と、プレイヤーのプルダウンCOが
    テンプレ文生成＋台帳直接記録になること（正規表現検出に依存しない）。"""
    import engine as eng
    import orchestrator as orch_mod
    from llm_backend import FakeBackend

    monkeypatch.setattr(eng, "BASE_DIR", tmp_path)
    monkeypatch.setattr(eng, "STATE_FILE", tmp_path / "game_state.json")
    monkeypatch.setattr(eng, "NOTES_FILE", tmp_path / ".gm_notes.json")
    monkeypatch.setattr(eng, "PLAYER_FILE", tmp_path / ".player_name")
    monkeypatch.setattr(orch_mod, "BASE_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "TYPING_FILE", tmp_path / ".typing.json")

    eng.setup_game()
    state = eng.load_state()
    state["phase"] = "day_discussion"
    eng.save_state(state)
    player = eng.player_name()

    o = orch_mod.Orchestrator(backend=FakeBackend(), config={"backend": "fake"})

    # NPCのCO指示: 未COの真占い師（NPCの場合）が対象になる
    seer = eng.find_role(state, "seer")
    directives = o._co_directives(state, {}, player)
    if seer["name"] != player:
        assert directives.get(seer["name"]) == "seer"
    else:
        assert seer["name"] not in directives

    # プレイヤーのプルダウンCO: テンプレ生成 + 台帳直接記録
    res = o.player_say("結果はまだ無いわ", co_role="seer")
    scene_text = (tmp_path / res["scene"]).read_text(encoding="utf-8")
    assert f"{player}「COします、私は占い師です。結果はまだ無いわ」" in scene_text
    claims = eng.load_notes()["public_co_claims"]
    assert claims[player]["role"] == "seer"

    # 無効な役職は拒否
    import pytest
    with pytest.raises(orch_mod.OrchestratorError):
        o.player_say("", co_role="werewolf")


def test_49_result_directives():
    """結果発表の構造化: 真占い師=実結果、偽CO=定石の白捏造をエンジンが決めること。"""
    import orchestrator as orch_mod
    from llm_backend import FakeBackend

    o = orch_mod.Orchestrator(backend=FakeBackend(), config={"backend": "fake"})

    def make_state(day):
        return {"day": day, "phase": "day_discussion", "players": [
            {"name": "A", "role": "werewolf", "alive": True},
            {"name": "B", "role": "madman", "alive": True},
            {"name": "C", "role": "seer", "alive": True},
            {"name": "D", "role": "villager", "alive": True},
            {"name": "E", "role": "villager", "alive": True},
        ], "log": []}

    # 真占い師: log の未発表結果がそのまま発表指示になる
    state = make_state(2)
    state["log"].append({"day": 1, "phase": "night", "type": "seer",
                         "actor": "C", "target": "A", "result": "werewolf"})
    notes = {"public_co_claims": {"C": {"role": "seer", "day": 1}}}
    d = o._result_directives(state, notes, "E")
    assert d["C"] == [{"kind": "seer", "target": "A", "result_jp": "人狼"}]

    # 発表済みなら重複指示しない
    notes["public_seer_claims"] = [
        {"actor": "C", "target": "A", "result": "人狼", "day": 2}]
    assert "C" not in o._result_directives(state, notes, "E")

    # 未COでも assume_co があればCO当日に結果発表が同時に出る
    d2 = o._result_directives(state, {}, "E", assume_co={"C": "seer"})
    assert "C" in d2

    # 偽占い師CO（狂人B）: 2日目に白を1件捏造。自分以外が対象
    notes_fake = {"public_co_claims": {"B": {"role": "seer", "day": 1}},
                  "counter_co_actors": ["B"]}
    items = o._result_directives(make_state(2), notes_fake, "E").get("B", [])
    assert len(items) == 1
    assert items[0]["result_jp"] == "白（人間）"
    assert items[0]["target"] != "B"

    # 初日は結果を捏造しない（初日占いなしルール）
    assert "B" not in o._result_directives(make_state(1), notes_fake, "E")


def test_50_player_announce_result(tmp_path, monkeypatch):
    """プレイヤーの結果発表: CO済みが前提、テンプレ生成＋台帳直接記録。"""
    import pytest
    import engine as eng
    import orchestrator as orch_mod
    from llm_backend import FakeBackend

    monkeypatch.setattr(eng, "BASE_DIR", tmp_path)
    monkeypatch.setattr(eng, "STATE_FILE", tmp_path / "game_state.json")
    monkeypatch.setattr(eng, "NOTES_FILE", tmp_path / ".gm_notes.json")
    monkeypatch.setattr(eng, "PLAYER_FILE", tmp_path / ".player_name")
    monkeypatch.setattr(orch_mod, "BASE_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "TYPING_FILE", tmp_path / ".typing.json")

    eng.setup_game()
    state = eng.load_state()
    state["phase"] = "day_discussion"
    eng.save_state(state)
    player = eng.player_name()
    other = next(p["name"] for p in state["players"] if p["name"] != player)

    o = orch_mod.Orchestrator(backend=FakeBackend(), config={"backend": "fake"})

    # CO していないのに結果発表はできない
    with pytest.raises(orch_mod.OrchestratorError):
        o.player_say("", result_target=other, result_black=True)

    # 占い師CO → 黒出し（騙りでも同じ経路）
    o.player_say("", co_role="seer")
    res = o.player_say("吊るしかないわ", result_target=other, result_black=True)
    scene_text = (tmp_path / res["scene"]).read_text(encoding="utf-8")
    assert (f"{player}「占い結果を発表します。{other}は【人狼】でした。"
            "吊るしかないわ」") in scene_text
    claims = eng.load_notes()["public_seer_claims"]
    assert {"actor": player, "target": other,
            "result": "人狼", "day": 1} in claims

    # 対象が不正なら拒否
    with pytest.raises(orch_mod.OrchestratorError):
        o.player_say("", result_target="存在しない人", result_black=False)


def test_47_wolf_vote_blends_with_consensus():
    """狼・狂人は占いCOへ突っ張らず村の合意先へ投票する（潜伏）こと。"""
    import engine as eng

    def make_state():
        return {"players": [
            {"name": "A", "role": "werewolf", "alive": True},
            {"name": "B", "role": "madman", "alive": True},
            {"name": "C", "role": "seer", "alive": True},
            {"name": "D", "role": "villager", "alive": True},
            {"name": "E", "role": "villager", "alive": True},
        ], "log": []}

    # 占いCO者(C)がいても、村の合意先(D)があれば狼・狂人ともDへ乗る
    notes = {
        "village_vote_target": "D",
        "public_co_claims": {"C": {"role": "seer", "day": 1}},
    }
    for _ in range(10):
        votes = eng.decide_npc_votes(make_state(), notes, "E")
        assert votes["A"] == "D", "狼は占いCOではなく合意先へ投票すべき"
        assert votes["B"] == "D", "狂人も合意先へ投票すべき"

    # 合意先が狼自身の場合: 疑惑スコア最大の村側へ逸らす（自分・仲間は不可）
    notes = {
        "village_vote_target": "A",
        "npc_suspicion_avg": {"A": 8.0, "C": 3.0, "D": 6.5, "E": 2.0},
    }
    votes = eng.decide_npc_votes(make_state(), notes, "E")
    assert votes["A"] == "D", "合意先が自分なら疑惑最大の村側へ逸らすべき"


# ---------------------------------------------------------------------------
# Group 4: 確定情報は公開台帳ベース（未発表の真結果で村が動かないこと）
# ---------------------------------------------------------------------------

PLAYERS_VOTE = [
    _p("アリス",   "werewolf"),
    _p("ボブ",     "madman"),
    _p("カール",   "seer"),
    _p("ダナ",     "villager"),
    _p("エミリー", "villager"),
]


def _setup_engine_files(tmp_path, monkeypatch, state, notes, player):
    import engine as eng
    monkeypatch.setattr(eng, "STATE_FILE", tmp_path / "game_state.json")
    monkeypatch.setattr(eng, "NOTES_FILE", tmp_path / ".gm_notes.json")
    monkeypatch.setattr(eng, "PLAYER_FILE", tmp_path / ".player_name")
    eng.save_state(state)
    eng.save_notes(notes)
    (tmp_path / ".player_name").write_text(player, encoding="utf-8")


def test_51_unannounced_seer_black_does_not_drive_vote(tmp_path, monkeypatch):
    """真占い師が log 上で黒を引いても、public_seer_claims に未記録（未発表）なら
    compute_vote_plan がその黒に収束しないこと（神視点リーク防止）。"""
    import engine as eng
    log = [
        {"type": "seer", "day": 1, "phase": "night",
         "actor": "カール", "target": "アリス", "result": "werewolf"},
    ]
    notes = {
        "public_co_claims": {},
        "public_seer_claims": [],
        "npc_suspicion_avg": {"ダナ": 9.0, "アリス": 3.0},
    }
    _setup_engine_files(tmp_path, monkeypatch,
                        _make_state(PLAYERS_VOTE, log, day=2), notes, "エミリー")
    plan = eng.compute_vote_plan()
    assert plan != "アリス", (
        f"未発表の真占い黒（アリス）に投票計画が収束した（神視点リーク）: {plan}"
    )
    assert plan == "ダナ", f"疑惑スコア最大のダナが選ばれていない: {plan}"


def test_52_announced_black_claim_drives_vote(tmp_path, monkeypatch):
    """public_seer_claims に黒宣言（偽COの捏造黒でも可）があれば
    compute_vote_plan がそれを返すこと。"""
    import engine as eng
    notes = {
        "public_co_claims": {"ボブ": {"role": "seer", "day": 1}},
        # 狂人ボブによる捏造黒。真偽を問わず公開宣言が投票を動かす
        "public_seer_claims": [
            {"actor": "ボブ", "target": "ダナ", "result": "人狼", "day": 2},
        ],
    }
    _setup_engine_files(tmp_path, monkeypatch,
                        _make_state(PLAYERS_VOTE, [], day=2), notes, "エミリー")
    assert eng.compute_vote_plan() == "ダナ", "公開黒宣言のダナへ収束すべき"


def test_53_disputed_target_excluded_from_confirmed():
    """同一 target に黒と白の宣言が両方ある場合（対抗COの食い違い等）、
    confirmed_black にも confirmed_white にも入らないこと。"""
    import engine as eng
    state = _make_state(PLAYERS_VOTE, [], day=2)
    notes = {
        "public_seer_claims": [
            {"actor": "カール", "target": "ダナ", "result": "人狼", "day": 2},
            {"actor": "ボブ",   "target": "ダナ", "result": "白（人間）", "day": 2},
        ],
        "public_medium_results": [],
    }
    black, white = eng._confirmed_info(state, notes)
    assert "ダナ" not in black, f"係争中のダナが確定黒に入った: {black}"
    assert "ダナ" not in white, f"係争中のダナが確定白に入った: {white}"


def test_54_dead_actor_seer_claim_stays_in_view():
    """死亡した actor の public_seer_claims が get_player_view の
    public_seer_results に残ること（噛まれた占い師の遺した結果は公開情報）。"""
    players = [
        _p("アリス",   "werewolf"),
        _p("ボブ",     "villager"),
        _p("カール",   "seer", alive=False),  # 発表後に襲撃死
        _p("ダナ",     "villager"),
        _p("エミリー", "villager"),
    ]
    log = [
        {"type": "attack", "day": 2, "target": "カール", "result": "killed"},
    ]
    notes = {
        "public_co_claims": {"カール": {"role": "seer", "day": 1}},
        "public_seer_claims": [
            {"actor": "カール", "target": "アリス", "result": "人狼", "day": 2},
        ],
        "public_medium_results": [],
    }
    view = get_player_view(_make_state(players, log, day=3), "ダナ", notes)
    assert {"actor": "カール", "target": "アリス",
            "result": "人狼", "day": 2} in view["public_seer_results"], (
        f"死亡した占い師カールの発表済み結果が view から消えた: "
        f"{view['public_seer_results']}"
    )


# ---------------------------------------------------------------------------
# Group 5: regex 検出のプレイヤー限定 / 視点フィルタの private 拡張
# ---------------------------------------------------------------------------

def _make_orchestrator():
    import orchestrator as orch_mod
    from llm_backend import FakeBackend
    return orch_mod.Orchestrator(backend=FakeBackend(), config={"backend": "fake"})


def test_55_record_public_events_ignores_npc_speculation(tmp_path, monkeypatch):
    """CO済み占い師NPCの考察「Xは人狼だと思う」が結果発表として
    public_seer_claims に誤記録されないこと（NPC発表はテンプレ直接記録のみ）。"""
    import engine as eng
    state = _make_state(PLAYERS_VOTE, [], day=2)
    notes = {
        "public_co_claims": {"カール": {"role": "seer", "day": 1}},
        "public_seer_claims": [],
    }
    _setup_engine_files(tmp_path, monkeypatch, state, notes, "エミリー")
    o = _make_orchestrator()
    o._record_public_events('カール「ダナは人狼だと思う」\n', state, notes)
    claims = eng.load_notes().get("public_seer_claims", [])
    assert claims == [], (
        f"NPC行の考察が占い結果として誤記録された: {claims}"
    )


def test_56_record_public_events_player_line_still_detected(tmp_path, monkeypatch):
    """プレイヤーの自由文発表は従来どおり regex フォールバックで記録されること。"""
    import engine as eng
    state = _make_state(PLAYERS_VOTE, [], day=2)
    notes = {
        "public_co_claims": {"エミリー": {"role": "seer", "day": 1}},
        "public_seer_claims": [],
    }
    _setup_engine_files(tmp_path, monkeypatch, state, notes, "エミリー")
    o = _make_orchestrator()
    o._record_public_events('エミリー「ダナは人狼だと思う」\n', state, notes)
    claims = eng.load_notes().get("public_seer_claims", [])
    assert {"actor": "エミリー", "target": "ダナ",
            "result": "人狼", "day": 2} in claims, (
        f"プレイヤー行の自由文発表が記録されていない: {claims}"
    )


PLAYERS_GUARD = [
    _p("アリス",   "werewolf"),
    _p("ボブ",     "bodyguard"),
    _p("カール",   "seer"),
    _p("ダナ",     "villager"),
    _p("エミリー", "villager"),
]

LOG_GUARD = [
    {"type": "guard",  "day": 1, "actor": "ボブ", "target": "カール"},
    {"type": "attack", "day": 1, "target": "カール", "result": "guarded"},
    {"type": "guard",  "day": 2, "actor": "ボブ", "target": "ダナ"},
    {"type": "attack", "day": 2, "target": "エミリー", "result": "killed"},
]


def test_57_bodyguard_guard_history_success_flag():
    """guard_history の各エントリに success（同日・同対象の guarded 有無）が入ること。"""
    view = get_player_view(_make_state(PLAYERS_GUARD, LOG_GUARD, day=3), "ボブ", {})
    gh = view["private"]["guard_history"]
    assert gh == [
        {"day": 1, "target": "カール", "success": True},
        {"day": 2, "target": "ダナ",   "success": False},
    ], f"guard_history の success が不正: {gh}"


def test_58_werewolf_private_attack_history():
    """人狼の private に襲撃履歴（結果込み）が入ること。"""
    view = get_player_view(_make_state(PLAYERS_GUARD, LOG_GUARD, day=3), "アリス", {})
    ah = view["private"]["attack_history"]
    assert ah == [
        {"day": 1, "target": "カール",   "result": "guarded"},
        {"day": 2, "target": "エミリー", "result": "killed"},
    ], f"attack_history が不正: {ah}"


# ---------------------------------------------------------------------------
# Group: 死者の「生存者扱い」検出（言及は許容する）
# ---------------------------------------------------------------------------

def test_59_dead_as_alive_check():
    """死者を生存中の相手として扱う発言はリジェクト、言及のみは通過。"""
    from npc_agent import dead_as_alive_check

    dead = ["ジムゾン"]

    # 生存者扱い → リジェクト（理由に対象名を含む）
    for bad in [
        "ジムゾン、どう思う？",
        "ジムゾンさんに投票します",
        "ジムゾンが一番怪しい",
        "ジムゾンさんはどう思いますか",
        "ジムゾン君を吊るべきだ",
    ]:
        reason = dead_as_alive_check(bad, dead)
        assert reason is not None, f"リジェクトされるべき: {bad!r}"
        assert "ジムゾン" in reason

    # 死者が残した情報への言及 → 通過
    for ok in [
        "ジムゾンの出した黒は本物だと思う",
        "昨夜ジムゾンが噛まれたのは占い師だったからだ",
        "ジムゾンさんの遺した結果を整理しよう",
        "ジムゾンは生前ヤコブを疑っていた",
        "初日はジムゾンが怪しかったが、今は違う",
    ]:
        assert dead_as_alive_check(ok, dead) is None, f"通過すべき: {ok!r}"

    # _validate_message 経由でも同じ判定になる
    assert npc_agent._validate_message("ジムゾン、答えてくれ", dead) is not None
    assert npc_agent._validate_message("ジムゾンの黒判定を信じる", dead) is None


def test_60_check_discussion_text_dead_talk_vocative_only():
    """DEAD_TALK は呼びかけ限定。敬称付き言及では発火しない。GHOST_TALK は言及でも検出。"""
    from scene_checks import check_discussion_text

    state = _make_state([
        _p("ジムゾン", "seer", alive=False),
        _p("ヤコブ",   "villager"),
        _p("パメラ",   "werewolf"),
    ])

    # 死者への呼びかけ → DEAD_TALK
    errs = check_discussion_text(state, "ジムゾンさん、どう思いますか")
    assert any("DEAD_TALK" in e and "ジムゾン" in e for e in errs), errs

    # 死者の遺した情報への敬称付き言及 → 検出なし
    assert check_discussion_text(state, "ジムゾンさんが遺した結果を信じよう") == []

    # 存在しない名前の敬称付き言及 → 従来どおり GHOST_TALK
    errs = check_discussion_text(state, "マリアさんの意見も聞いてみたい")
    assert any("GHOST_TALK" in e and "マリア" in e for e in errs), errs

    # 死者への投票宣言 → 現状維持で DEAD_TALK
    errs = check_discussion_text(state, "私はジムゾンに投票します")
    assert any("DEAD_TALK" in e and "投票" in e for e in errs), errs


def test_62_madman_pp_protects_believed_wolf():
    """狂人PP: 残り3人（狂人・狼・第三者）で公開黒宣言から狼を推定し、
    推定狼ではなく第三者へ票を合わせること（狼+狂で多数を握る）。"""
    import engine as eng

    state = _make_state([
        _p("アリス",   "werewolf"),
        _p("ボブ",     "madman"),
        _p("カール",   "seer",     alive=False),
        _p("ダナ",     "villager"),
        _p("エミリー", "villager", alive=False),
    ], day=3)
    notes = {
        # 第三者ダナ（真占いの可能性が高い）がアリスに黒宣言
        "public_seer_claims": [
            {"actor": "ダナ", "target": "アリス", "result": "人狼", "day": 2},
        ],
    }
    for _ in range(10):
        votes = eng.decide_npc_votes(state, notes, "エミリー")
        assert votes["ボブ"] == "ダナ", (
            f"PP圏の狂人は推定狼アリスを守り第三者ダナへ投票すべき: {votes}"
        )


def test_63_madman_never_votes_own_white():
    """狂人の自己整合: 自分が白宣言した相手が村の合意先でも、
    そこには投票しないこと（生存4人以上の通常時）。"""
    import engine as eng

    state = _make_state([
        _p("アリス",   "werewolf"),
        _p("ボブ",     "madman"),
        _p("カール",   "seer"),
        _p("ダナ",     "villager"),
        _p("エミリー", "villager"),
    ], day=2)
    notes = {
        "village_vote_target": "ダナ",
        "public_seer_claims": [
            {"actor": "ボブ", "target": "ダナ", "result": "白（人間）", "day": 2},
        ],
    }
    for _ in range(20):
        votes = eng.decide_npc_votes(state, notes, "エミリー")
        assert votes["ボブ"] != "ダナ", (
            f"狂人が自分の白宣言先に投票した（騙りの自己矛盾）: {votes}"
        )


def test_64_seer_npc_votes_own_black_over_consensus():
    """真占い師NPCは自分の黒結果（生存中）を村の合意先より優先して吊りに行くこと。"""
    import engine as eng

    log = [
        {"type": "seer", "day": 1, "phase": "night",
         "actor": "カール", "target": "アリス", "result": "werewolf"},
    ]
    state = _make_state([
        _p("アリス",   "werewolf"),
        _p("ボブ",     "madman"),
        _p("カール",   "seer"),
        _p("ダナ",     "villager"),
        _p("エミリー", "villager"),
    ], log, day=2)
    notes = {"village_vote_target": "ダナ"}
    votes = eng.decide_npc_votes(state, notes, "エミリー")
    assert votes["カール"] == "アリス", (
        f"真占い師が自分の黒アリスでなく合意先へ投票した: {votes}"
    )


def test_65_compute_vote_plan_can_target_player(tmp_path, monkeypatch):
    """プレイヤーも疑惑スコアの集計対象。疑惑最大なら compute_vote_plan が
    プレイヤー名を返すこと（除外するとプレイヤーが構造的に吊られない）。"""
    import engine as eng

    notes = {
        "public_seer_claims": [],
        "npc_suspicion_avg": {"エミリー": 9.0, "ダナ": 3.0},
    }
    _setup_engine_files(tmp_path, monkeypatch,
                        _make_state(PLAYERS_VOTE, [], day=2), notes, "エミリー")
    plan = eng.compute_vote_plan()
    assert plan == "エミリー", (
        f"疑惑最大のプレイヤーが合意先にならない: {plan}"
    )


def test_61_addresses_player_message_requires_content_words():
    """機能語（です・ます等）の偶然一致では応答扱いにならないこと。"""
    from npc_agent import _addresses_player_message

    player_line = "私は占い師です。ヤコブが怪しいと思う"

    # 機能語のみの一致 → False
    assert not _addresses_player_message("そうですね、しますね", player_line, "ディータ")

    # 内容語（カタカナ・漢字を含む3文字以上）の一致 → True
    assert _addresses_player_message(
        "ヤコブが怪しいという意見には賛成だ", player_line, "ディータ")

    # プレイヤー名が含まれる → True
    assert _addresses_player_message("ディータはどう考える？", player_line, "ディータ")


def test_62_vote_breakdown_in_view_and_npc_board():
    """execution_history に個票が入り、NPC盤面が A→B 形式で表示されること。"""
    from npc_agent import _format_view

    state = {
        "day": 2, "phase": "day_discussion",
        "players": [
            {"name": "トーマス", "role": "villager", "alive": True},
            {"name": "ヴァルター", "role": "villager", "alive": False},
            {"name": "リーザ", "role": "seer", "alive": False},
            {"name": "ヤコブ", "role": "madman", "alive": True},
            {"name": "レジーナ", "role": "werewolf", "alive": True},
        ],
        "log": [{
            "day": 1, "type": "execute", "target": "ヴァルター",
            "votes": {
                "トーマス": "ヴァルター", "リーザ": "ヴァルター",
                "ヤコブ": "ヴァルター", "レジーナ": "ヴァルター",
                "ヴァルター": "トーマス",
            },
            "tally": {"ヴァルター": 4, "トーマス": 1},
        }],
    }
    notes: dict = {}
    view = engine.get_player_view(state, "ヤコブ", notes)
    assert view["execution_history"][0]["votes"]["トーマス"] == "ヴァルター"
    board = _format_view(view)
    assert "トーマス→ヴァルター" in board
    assert "ヴァルター→トーマス" in board
    assert "4票" not in board
    assert engine.format_vote_breakdown(view["execution_history"][0]["votes"]) == (
        "トーマス→ヴァルター、ヤコブ→ヴァルター、リーザ→ヴァルター、"
        "レジーナ→ヴァルター、ヴァルター→トーマス"
    )


# ---------------------------------------------------------------------------
# Group: 疑惑スコアの機械計算（compute_npc_suspicion。LLM不使用）
# ---------------------------------------------------------------------------

PLAYERS_SUSPICION = [
    _p("アリス",   "werewolf"),
    _p("ボブ",     "madman"),
    _p("カール",   "seer"),
    _p("ダナ",     "villager"),
    _p("エミリー", "villager"),
]


def test_66_black_claim_raises_suspicion_avg():
    """公開黒宣言（係争なし）のある target の avg が、宣言のない
    同条件 target（プレイヤー）より高いこと。"""
    state = _make_state(PLAYERS_SUSPICION, [], day=2)
    notes = {
        "public_co_claims": {"カール": {"role": "seer", "day": 1}},
        "public_seer_claims": [
            {"actor": "カール", "target": "ダナ", "result": "人狼", "day": 2},
        ],
    }
    result = engine.compute_npc_suspicion(state, notes, "エミリー")
    assert result["avg"]["ダナ"] > result["avg"]["エミリー"], (
        f"黒宣言のあるダナが宣言なしのエミリーより低い: {result['avg']}"
    )


def test_67_true_seer_rater_uses_own_results():
    """真占い師 rater は自分の log 上の実結果で黒に 10.0、白に 1.0 を付けること。"""
    log = [
        {"type": "seer", "day": 1, "phase": "night",
         "actor": "カール", "target": "アリス", "result": "werewolf"},
        {"type": "seer", "day": 2, "phase": "night",
         "actor": "カール", "target": "ダナ", "result": "not_werewolf"},
    ]
    state = _make_state(PLAYERS_SUSPICION, log, day=3)
    result = engine.compute_npc_suspicion(state, {}, "エミリー")
    rated = result["by_rater"]["カール"]
    assert rated["アリス"] == 10.0, f"真占い師の黒が 10.0 でない: {rated}"
    assert rated["ダナ"] == 1.0, f"真占い師の白が 1.0 でない: {rated}"


def test_68_werewolf_rater_shields_teammate():
    """人狼 rater は仲間狼に 1.0 を付けること。"""
    players = [
        _p("アリス",   "werewolf"),
        _p("ボブ",     "werewolf"),
        _p("カール",   "seer"),
        _p("ダナ",     "villager"),
        _p("エミリー", "villager"),
    ]
    state = _make_state(players, [], day=2)
    result = engine.compute_npc_suspicion(state, {}, "エミリー")
    assert result["by_rater"]["アリス"]["ボブ"] == 1.0, (
        f"狼アリスが仲間ボブに 1.0 を付けていない: {result['by_rater']['アリス']}"
    )
    assert result["by_rater"]["ボブ"]["アリス"] == 1.0, (
        f"狼ボブが仲間アリスに 1.0 を付けていない: {result['by_rater']['ボブ']}"
    )


def test_69_madman_rater_shields_believed_wolf():
    """狂人 rater は他者による黒宣言先（推定狼）に 1.0 を付けること。"""
    state = _make_state(PLAYERS_SUSPICION, [], day=2)
    notes = {
        "public_co_claims": {"カール": {"role": "seer", "day": 1}},
        "public_seer_claims": [
            {"actor": "カール", "target": "アリス", "result": "人狼", "day": 2},
        ],
    }
    result = engine.compute_npc_suspicion(state, notes, "エミリー")
    assert result["by_rater"]["ボブ"]["アリス"] == 1.0, (
        f"狂人ボブが推定狼アリスに 1.0 を付けていない: {result['by_rater']['ボブ']}"
    )


def test_70_compute_npc_suspicion_deterministic():
    """同一入力で2回呼んで結果が完全一致すること（決定的ノイズ）。"""
    log = [
        {"type": "execute", "day": 1, "phase": "day_vote", "target": "ダナ",
         "alignment": "human", "tally": {"ダナ": 3, "カール": 1},
         "votes": {"アリス": "ダナ", "ボブ": "ダナ", "エミリー": "ダナ",
                   "ダナ": "カール"}},
    ]
    players = [
        _p("アリス",   "werewolf"),
        _p("ボブ",     "madman"),
        _p("カール",   "seer"),
        _p("ダナ",     "villager", alive=False),
        _p("エミリー", "villager"),
    ]
    state = _make_state(players, log, day=2)
    notes = {
        "public_co_claims": {"カール": {"role": "seer", "day": 1}},
        "public_seer_claims": [
            {"actor": "カール", "target": "アリス", "result": "人狼", "day": 2},
        ],
    }
    r1 = engine.compute_npc_suspicion(state, notes, "エミリー")
    r2 = engine.compute_npc_suspicion(state, notes, "エミリー")
    assert r1 == r2, f"同一入力で結果が変わった:\n{r1}\n{r2}"


def test_63_day1_no_seer_result_demand():
    """初日に占い師へ結果を求める発言は機械検証で却下されること。"""
    from npc_agent import check_day1_seer_result_demand

    view = {"day": 1, "public_co_claims": {"アルビン": {"role": "seer", "day": 1}}}
    assert check_day1_seer_result_demand(
        "まずは占い結果を聞くのが筋だろう", view) is not None
    assert check_day1_seer_result_demand(
        "誰を占ったか教えてくれ", view) is not None
    # 結果なしの説明・ルール言及は通す
    assert check_day1_seer_result_demand(
        "初日は結果がまだ無いのは当たり前だ", view) is None
    assert check_day1_seer_result_demand(
        "占い結果なんてねぇよ", view) is None
    # 2日目は検査しない
    assert check_day1_seer_result_demand(
        "占い結果を聞かせて", {"day": 2}) is None


# ---------------------------------------------------------------------------
# Group 8: speech_planner + render_speech（LLM関与削減）
# ---------------------------------------------------------------------------

def test_71_build_speech_plan_deterministic():
    """同一入力で build_speech_plan が決定的であること。"""
    import speech_planner

    players = [
        _p("アリス",   "werewolf"),
        _p("ボブ",     "madman"),
        _p("カール",   "seer"),
        _p("ダナ",     "villager"),
        _p("エミリー", "villager"),
    ]
    state = _make_state(players, [], day=2)
    notes = {
        "public_co_claims": {"カール": {"role": "seer", "day": 1}},
        "public_seer_claims": [
            {"actor": "カール", "target": "アリス", "result": "人狼", "day": 2},
        ],
    }
    p1 = speech_planner.build_speech_plan(
        state, notes, "エミリー", "ダナ", disc_index=2)
    p2 = speech_planner.build_speech_plan(
        state, notes, "エミリー", "ダナ", disc_index=2)
    assert p1 == p2
    assert p1["acts"]
    assert "アリス" in p1["must_mention"]
    assert p1["fallback_text"]


def test_72_mark_topics_spoken_prevents_repeat():
    """mark_topics_spoken で消費した論点が次ターンに出ないこと。"""
    import speech_planner

    players = [
        _p("アリス",   "werewolf"),
        _p("ボブ",     "madman"),
        _p("カール",   "seer"),
        _p("ダナ",     "villager"),
        _p("エミリー", "villager"),
    ]
    state = _make_state(players, [], day=2)
    notes = {
        "public_co_claims": {"カール": {"role": "seer", "day": 1}},
        "public_seer_claims": [
            {"actor": "カール", "target": "アリス", "result": "人狼", "day": 2},
        ],
        "spoken_topics": {},
    }
    # エミリー（村人）が黒宣言論点を拾う
    p1 = speech_planner.build_speech_plan(
        state, notes, "エミリー", "ダナ", disc_index=1)
    assert p1["topic_keys"], f"論点が選ばれなかった: {p1}"
    speech_planner.mark_topics_spoken(notes, "エミリー", p1["topic_keys"])
    p2 = speech_planner.build_speech_plan(
        state, notes, "エミリー", "ダナ", disc_index=2)
    assert p1["topic_keys"][0] not in p2["topic_keys"]


def test_73_render_speech_success():
    """render_speech が FakeBackend で正常にセリフを返すこと。"""
    from llm_backend import FakeBackend

    backend = FakeBackend(responder=lambda *a, **k: (
        '{"message": "アリスが怪しいと思うわ"}'
    ))
    npc_agent.init(backend, "fake")
    plan = {
        "acts": [{"type": "accuse", "text_jp": "アリスを疑っている"}],
        "must_mention": ["アリス"],
        "topic_keys": [],
        "fallback_text": "アリスを疑っている。",
    }
    view = get_player_view(_make_state([_p("ボブ", "villager")], [], day=2),
                           "ボブ", {})
    res = npc_agent.render_speech(
        "ボブ", view, {"speech_style": {"tone": "穏やか"}}, plan)
    assert res["message"] == "ボブ「アリスが怪しいと思うわ」"
    assert res["error"] is None


def test_74_render_speech_fails_without_must_mention():
    """must_mention 欠落時は render_speech が空メッセージを返すこと。"""
    from llm_backend import FakeBackend

    backend = FakeBackend(responder=lambda *a, **k: (
        '{"message": "様子を見ましょう"}'
    ))
    npc_agent.init(backend, "fake")
    plan = {
        "acts": [{"type": "accuse", "text_jp": "アリスを疑っている"}],
        "must_mention": ["アリス"],
        "topic_keys": [],
        "fallback_text": "アリスを疑っている。",
    }
    view = get_player_view(_make_state([_p("ボブ", "villager")], [], day=2),
                           "ボブ", {})
    res = npc_agent.render_speech(
        "ボブ", view, {"speech_style": {}}, plan)
    assert res["message"] == ""
    assert res["error"]


def test_75_npc_speak_one_uses_fallback(tmp_path, monkeypatch):
    """npc_speak_one: render 失敗時に plan.fallback_text がシーンへ出ること。"""
    import engine as eng
    import orchestrator as orch_mod
    from llm_backend import FakeBackend

    monkeypatch.setattr(eng, "BASE_DIR", tmp_path)
    monkeypatch.setattr(eng, "STATE_FILE", tmp_path / "game_state.json")
    monkeypatch.setattr(eng, "NOTES_FILE", tmp_path / ".gm_notes.json")
    monkeypatch.setattr(eng, "PLAYER_FILE", tmp_path / ".player_name")
    monkeypatch.setattr(orch_mod, "BASE_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "TYPING_FILE", tmp_path / ".typing.json")

    eng.setup_game()
    state = eng.load_state()
    state["phase"] = "day_discussion"
    eng.save_state(state)
    notes = eng.load_notes()
    notes["discussion_queue"] = [
        p["name"] for p in state["players"] if p["name"] != eng.player_name()
    ]
    notes["active_disc_scene"] = "scene_day1_disc1.txt"
    notes["discussion_day"] = 1
    eng.save_notes(notes)
    (tmp_path / "scene_day1_disc1.txt").write_text("", encoding="utf-8")

    fail_backend = FakeBackend(responder=lambda *a, **k: '{"message": ""}')
    o = orch_mod.Orchestrator(backend=fail_backend, config={"backend": "fake"})
    res = o.npc_speak_one()
    assert res["spoken"]
    assert res["fallback"]
    scene = (tmp_path / res["scene"]).read_text(encoding="utf-8")
    assert "「" in scene and "」" in scene


def test_76_vote_declaration_uses_grounded_reason():
    """投票宣言のフォールバックに機械決定の理由が含まれること。"""
    import orchestrator as orch_mod
    from llm_backend import FakeBackend

    state = _make_state([
        _p("アリス", "werewolf"),
        _p("ボブ", "villager"),
        _p("カール", "seer"),
        _p("ダナ", "villager"),
        _p("エミリー", "villager"),
    ], [], day=2)
    notes = {
        "public_co_claims": {"カール": {"role": "seer", "day": 1}},
        "public_seer_claims": [
            {"actor": "カール", "target": "アリス", "result": "人狼", "day": 2},
        ],
    }
    import engine as eng
    orig_load = eng.load_state
    orig_notes = eng.load_notes
    eng.load_state = lambda: state
    eng.load_notes = lambda: notes
    try:
        o = orch_mod.Orchestrator(
            backend=FakeBackend(responder=lambda *a, **k: '{"message": ""}'),
            config={"backend": "fake"},
        )
        line = o._vote_declaration(
            "ボブ", {"target": "アリス", "reason": "consensus"}, {})
        assert "アリス" in line
        assert "黒宣言" in line or "人狼" in line or "カール" in line
    finally:
        eng.load_state = orig_load
        eng.load_notes = orig_notes


def test_77_finalize_game_sets_epilogue_phase(tmp_path, monkeypatch):
    """epilogue 呼び出し後に phase が epilogue になること。"""
    import engine as eng
    import orchestrator as orch_mod
    from llm_backend import FakeBackend

    monkeypatch.setattr(eng, "BASE_DIR", tmp_path)
    monkeypatch.setattr(eng, "STATE_FILE", tmp_path / "game_state.json")
    monkeypatch.setattr(eng, "NOTES_FILE", tmp_path / ".gm_notes.json")
    monkeypatch.setattr(eng, "PLAYER_FILE", tmp_path / ".player_name")
    monkeypatch.setattr(orch_mod, "BASE_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "TYPING_FILE", tmp_path / ".typing.json")

    eng.setup_game()
    state = eng.load_state()
    state["phase"] = "night"
    eng.save_state(state)

    o = orch_mod.Orchestrator(backend=FakeBackend(), config={"backend": "fake"})
    o.epilogue("village")
    assert eng.load_state()["phase"] == "epilogue"


def test_78_epilogue_scene_is_template_without_speech(tmp_path, monkeypatch):
    """幕引き本体 scene_epilogue.txt にキャラ発言が無いこと。"""
    import engine as eng
    import orchestrator as orch_mod
    from llm_backend import FakeBackend
    import validator

    monkeypatch.setattr(eng, "BASE_DIR", tmp_path)
    monkeypatch.setattr(eng, "STATE_FILE", tmp_path / "game_state.json")
    monkeypatch.setattr(eng, "NOTES_FILE", tmp_path / ".gm_notes.json")
    monkeypatch.setattr(eng, "PLAYER_FILE", tmp_path / ".player_name")
    monkeypatch.setattr(orch_mod, "BASE_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "TYPING_FILE", tmp_path / ".typing.json")
    monkeypatch.setattr(validator, "BASE_DIR", tmp_path)

    eng.setup_game()
    o = orch_mod.Orchestrator(backend=FakeBackend(), config={"backend": "fake"})
    epi = o.epilogue("village")
    text = (tmp_path / epi["scenes"][0]).read_text(encoding="utf-8")
    assert "「" not in text
    assert "全役職公開" in text
    errs = validator.validate_file(tmp_path / epi["scenes"][0])
    assert not errs


def test_80_player_say_no_new_disc_when_queue_empty(tmp_path, monkeypatch):
    """NPC待ちが空のとき player_say は新しい disc を作らないこと。"""
    import engine as eng
    import orchestrator as orch_mod
    from llm_backend import FakeBackend

    monkeypatch.setattr(eng, "BASE_DIR", tmp_path)
    monkeypatch.setattr(eng, "STATE_FILE", tmp_path / "game_state.json")
    monkeypatch.setattr(eng, "NOTES_FILE", tmp_path / ".gm_notes.json")
    monkeypatch.setattr(eng, "PLAYER_FILE", tmp_path / ".player_name")
    monkeypatch.setattr(orch_mod, "BASE_DIR", tmp_path)
    monkeypatch.setattr(orch_mod, "TYPING_FILE", tmp_path / ".typing.json")

    eng.setup_game()
    state = eng.load_state()
    state["phase"] = "day_discussion"
    eng.save_state(state)
    player = eng.player_name()
    notes = eng.load_notes()
    notes["discussion_queue"] = []
    notes["active_disc_scene"] = "scene_day1_disc1.txt"
    notes["discussion_day"] = 1
    eng.save_notes(notes)
    (tmp_path / "scene_day1_disc1.txt").write_text("既存\n", encoding="utf-8")

    o = orch_mod.Orchestrator(backend=FakeBackend(), config={"backend": "fake"})
    res = o.player_say("もう一度聞きたい")
    assert res["scene"] == "scene_day1_disc1.txt"
    assert not (tmp_path / "scene_day1_disc2.txt").exists()
    text = (tmp_path / "scene_day1_disc1.txt").read_text(encoding="utf-8")
    assert "もう一度聞きたい" in text


def test_81_seer_target_question_gets_structured_reply():
    """占い先の質問に占い師CO者が具体的に答えるプランになること。"""
    import speech_planner

    players = [
        _p("リーザ", "villager"),
        _p("カタリナ", "madman"),
        _p("ニコラス", "seer"),
        _p("ジムゾン", "werewolf"),
        _p("アルビン", "villager"),
    ]
    state = _make_state(players, [], day=1)
    notes = {
        "public_co_claims": {
            "カタリナ": {"role": "seer", "day": 1},
            "ニコラス": {"role": "seer", "day": 1},
        },
    }
    plan = speech_planner.build_speech_plan(
        state, notes, "ニコラス", "リーザ",
        respond_to_player=True, disc_index=2,
        player_message="カタリナとニコラスは誰を占いたいか教えて",
    )
    types = [a["type"] for a in plan["acts"]]
    assert "answer_seer_target" in types
    assert any("占" in a["text_jp"] for a in plan["acts"])


def test_82_skip_duplicate_accuse_same_day():
    """同一日に同じ疑い先を繰り返さないこと。"""
    import speech_planner

    players = [
        _p("リーザ", "villager"),
        _p("カタリナ", "madman"),
        _p("ニコラス", "seer"),
        _p("ジムゾン", "werewolf"),
        _p("アルビン", "villager"),
    ]
    state = _make_state(players, [], day=1)
    notes = {
        "public_co_claims": {
            "カタリナ": {"role": "seer", "day": 1},
            "ニコラス": {"role": "seer", "day": 1},
        },
        "last_accuse": {
            "アルビン": {"day": 1, "target": "ニコラス"},
        },
        "spoken_topics": {"アルビン": ["rival_seer_co"]},
    }
    plan = speech_planner.build_speech_plan(
        state, notes, "アルビン", "リーザ", disc_index=3,
    )
    assert not any(a["type"] == "accuse" and a.get("target") == "ニコラス"
                   for a in plan["acts"])


def test_83_day1_vote_plan_prefers_rival_seers_over_grey():
    """初日・占い師CO対抗時はグレー村人よりCO者を村の投票先にすること。"""
    import engine as eng

    players = [
        _p("リーザ", "villager"),
        _p("オットー", "werewolf"),
        _p("ジムゾン", "madman"),
        _p("ニコラス", "seer"),
        _p("アルビン", "villager"),
    ]
    state = _make_state(players, [], day=1)
    notes = {
        "public_co_claims": {
            "ニコラス": {"role": "seer", "day": 1},
            "ジムゾン": {"role": "seer", "day": 1},
        },
        "npc_suspicion_avg": {
            "リーザ": 8.0,
            "ニコラス": 5.0,
            "ジムゾン": 6.0,
            "オットー": 3.0,
            "アルビン": 4.0,
        },
    }
    orig_state = eng.load_state
    orig_notes = eng.load_notes
    eng.load_state = lambda: state
    eng.load_notes = lambda: notes
    try:
        target = eng.compute_vote_plan()
        assert target in ("ニコラス", "ジムゾン"), f"グレー吊りになった: {target}"
    finally:
        eng.load_state = orig_state
        eng.load_notes = orig_notes


def test_79_resolve_night_no_day_advance_on_win(tmp_path, monkeypatch):
    """夜襲撃で決着した場合、日付を進めないこと。"""
    import engine as eng

    monkeypatch.setattr(eng, "BASE_DIR", tmp_path)
    monkeypatch.setattr(eng, "STATE_FILE", tmp_path / "game_state.json")
    monkeypatch.setattr(eng, "NOTES_FILE", tmp_path / ".gm_notes.json")
    monkeypatch.setattr(eng, "PLAYER_FILE", tmp_path / ".player_name")

    eng.setup_game()
    state = eng.load_state()
    alive = [p for p in state["players"] if p["alive"]]
    wolf = next(p for p in alive if p["role"] == "werewolf")
    villager = next(p for p in alive if p["role"] == "villager")
    for p in state["players"]:
        p["alive"] = p["name"] in (wolf["name"], villager["name"])
    state["day"] = 2
    state["phase"] = "night"
    eng.save_state(state)
    # プレイヤーが人狼だと襲撃入力が必要になるため村人に固定
    player = eng.player_name()
    wp = eng.get_player(state, player)
    if wp["role"] == "werewolf":
        wp["role"] = "villager"
        wolf["role"] = "werewolf"
        eng.save_state(state)

    result = eng.resolve_night()
    assert result["win"] == "werewolf"
    after = eng.load_state()
    assert after["day"] == 2, f"決着後に日付が進んだ: {after['day']}"
