#!/usr/bin/env python3
"""シーンテキストの論理整合チェック。

- check_vote_consistency: 投票シーンのセリフと投票ロジックの突き合わせ
- check_discussion_text: 死者への呼びかけ / 存在しない人物（マリア現象）の検出

orchestrator（生成直後の自己チェック）と autoplay（回帰テスト）の双方が使う。
"""

from __future__ import annotations

import re

# 「{name}[さん君殿様]?に投票」「{name}を吊」など
_VOTE_SUFFIX = r"(?:さん|君|殿|様)?"
_VOTE_PHRASES = [
    r"に投票", r"に一票", r"に入れ", r"を処刑", r"を吊", r"吊り",
    r"に投じ", r"にします", r"を選", r"を疑う", r"へ投票",
]


def _extract_vote_target(dialogue: str, candidate_names: list[str]) -> str | None:
    """セリフ文字列から投票先の名前を抽出する。見つからなければ None。"""
    for name in candidate_names:
        escaped = re.escape(name)
        for phrase in _VOTE_PHRASES:
            if re.search(f"{escaped}{_VOTE_SUFFIX}{phrase}", dialogue):
                return name
    return None


def check_vote_consistency(
    scene_path: str,
    npc_votes: dict,
    player: str,
    alive_names: list[str],
) -> list[str]:
    """vote scene のセリフと npc_votes の投票先を突き合わせる。

    Returns:
        不一致・読取不能の場合はメッセージのリスト（整合していれば空リスト）。
        VOTE_CHECK_ERROR: セリフが logic と矛盾（最重要）
        VOTE_CHECK_WARN:  セリフから投票先を読み取れなかった（確認推奨）
    """
    issues: list[str] = []

    try:
        with open(scene_path, encoding="utf-8") as f:
            raw_lines = f.readlines()
    except OSError as e:
        return [f"VOTE_CHECK_ERR: シーンファイルを開けない {scene_path}: {e}"]

    # スピーカー別に全セリフを結合（1NPCが複数行ある場合も対応）
    speaker_dialogues: dict[str, list[str]] = {}
    for line in raw_lines:
        m = re.match(r"^(.+?)「(.+)」\s*$", line.strip())
        if m:
            speaker = m.group(1).strip()
            dialogue = m.group(2).strip()
            speaker_dialogues.setdefault(speaker, []).append(dialogue)

    for npc, info in npc_votes.items():
        if npc == player:
            continue
        expected = info["target"] if isinstance(info, dict) else str(info)
        dialogues = speaker_dialogues.get(npc, [])

        if not dialogues:
            issues.append(
                f"VOTE_CHECK_WARN: {npc} のセリフが scene に見つからない "
                f"(expected={expected})"
            )
            continue

        combined = " ".join(dialogues)
        extracted = _extract_vote_target(combined, alive_names)

        if extracted is None:
            issues.append(
                f"VOTE_CHECK_WARN: {npc} のセリフから投票先を読み取れず "
                f"(expected={expected}) | {combined[:80]!r}"
            )
        elif extracted != expected:
            issues.append(
                f"VOTE_CHECK_ERROR: {npc} セリフ={extracted} vs logic={expected} [矛盾] "
                f"| {combined[:80]!r}"
            )

    return issues


def check_discussion_text(state_at_start: dict, disc_text: str) -> list[str]:
    """議論テキストの死者呼びかけ（DEAD_TALK）・幽霊呼びかけ（GHOST_TALK）を検出する。

    state_at_start: 議論開始時点の game_state（生死判定の基準）
    """
    errors = []
    players = state_at_start["players"]
    all_known_names = [p["name"] for p in players]
    dead_at_start = [p["name"] for p in players if not p["alive"]]

    def resolve(katakana_run: str) -> str | None:
        """カタカナ連続列から既知の名前を解決する。

        貪欲マッチで前置きのカタカナ語が混入するため（例: リーダーのヴァルター）、
        末尾一致で既知名を探す。見つからなければ None（=幽霊）。
        """
        if katakana_run in all_known_names:
            return katakana_run
        for n in all_known_names:
            if katakana_run.endswith(n):
                return n
        return None

    # 名前 + 敬称(さん/君/様) で呼ばれている固有名詞を抽出
    # 「ヴ」(U+30F4) や小書き文字を含む名前に対応するため ァ-ヴ の範囲を使う
    #
    # GHOST_TALK: 存在しない名前は敬称付きで出てくること自体が幻覚なので言及でも検出。
    # DEAD_TALK: 死者への正当な言及（「◯◯さんの遺した結果」）は許容し、
    #            「生存者扱い」（呼びかけ・質問・発言要求）が続く場合のみ検出する。
    for m in re.finditer(r"([ァ-ヴー]{2,})((?:さん|君|様)?)", disc_text):
        called_run, honorific = m.group(1), m.group(2)
        called_name = resolve(called_run)
        if called_name is None:
            if honorific:
                errors.append(f"GHOST_TALK: 存在しない {called_run} に話しかけています")
            continue
        if called_name not in dead_at_start:
            continue
        rest = disc_text[m.end():]
        if re.match(r"[、,]|はどう思|に聞|に質問|答えて", rest):
            errors.append(f"DEAD_TALK: 既に死んでいる {called_name} に話しかけています")

    # 存在しない人物・死者への「投票宣言」チェック
    vote_matches = re.findall(r"([ァ-ヴー]{2,})(?:に投票|にします|に一票)", disc_text)
    for target_run in vote_matches:
        target_name = resolve(target_run)
        if target_name is None:
            errors.append(f"GHOST_TALK: 存在しない {target_run} への投票を宣言しています")
        elif target_name in dead_at_start:
            errors.append(f"DEAD_TALK: 死者 {target_name} への投票を宣言しています")

    return sorted(set(errors))
