#!/usr/bin/env python3
"""描写ファイルのバリデーター。

Usage: python3 validator.py <描写ファイル>

game_state.json を読み込み、描写ファイル中の発言が
ゲーム状態と整合しているかを検証する。
"""

import json
import re
import sys


def load_game_state(path="game_state.json"):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_narration(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def extract_speakers(text, player_names):
    """描写テキストから発言者名を抽出する。

    対応パターン:
      - 名前「セリフ」
      - 名前：「セリフ」
      - 名前:「セリフ」
    """
    speakers = []
    for name in player_names:
        pattern = re.escape(name) + r"[：:]?\s*「[^」]*」"
        if re.search(pattern, text):
            speakers.append(name)
    return speakers


ROLE_LABELS = [
    "人狼", "狂人", "占い師", "霊媒師", "狩人", "村人",
    "werewolf", "madman", "seer", "medium", "bodyguard", "villager",
]


def validate(game_state, narration_text, is_epilogue=False, executed_name=None):
    errors = []
    players = game_state["players"]
    player_names = [p["name"] for p in players]
    dead_names = [p["name"] for p in players if not p["alive"]]

    speakers = extract_speakers(narration_text, player_names)

    # 死人発言チェック（epilogueは除外、処刑シーンは処刑対象の最後の言葉を許可）
    if not is_epilogue:
        for name in speakers:
            if name in dead_names and name != executed_name:
                errors.append(f"[死人発言] {name} は死亡済みですが発言しています")

    # 存在チェック: 「」の直前にある名前がplayersに存在するか
    # player_names に含まれない名前が発言していないか検出する
    unknown_pattern = r"(\S+?)[：:]?\s*「[^」]*」"
    for m in re.finditer(unknown_pattern, narration_text):
        candidate = m.group(1)
        # 候補がプレイヤー名のいずれかで終わっている場合はOK
        matched = any(candidate.endswith(name) for name in player_names)
        if not matched and candidate:
            errors.append(f"[存在不明] {candidate} は players に登録されていません")

    # 役職付記チェック: 名前（役職）パターンの検出（epilogueは除外）
    if not is_epilogue:
        for name in player_names:
            for role in ROLE_LABELS:
                pattern = re.escape(name) + r"[（(]" + re.escape(role) + r"[）)]"
                if re.search(pattern, narration_text):
                    errors.append(
                        f"[役職漏洩] {name}（{role}）のように役職が付記されています"
                    )

    # フォーマット不正チェック: 名前重複・二重括弧パターン
    for name in player_names:
        # 名前「名前[：:] パターン（内部で名前が重複している）
        dup_pattern = re.escape(name) + r"「" + re.escape(name) + r"[：:]"
        if re.search(dup_pattern, narration_text):
            errors.append(
                f"[フォーマット不正] {name} の発言に名前重複パターン（{name}「{name}：）が検出されました"
            )
    # 二重かぎ括弧パターン
    if re.search(r"「「", narration_text):
        errors.append("[フォーマット不正] 二重開きかぎ括弧（「「）が検出されました")
    if re.search(r"」」", narration_text):
        errors.append("[フォーマット不正] 二重閉じかぎ括弧（」」）が検出されました")

    return errors


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 validator.py <描写ファイル>", file=sys.stderr)
        sys.exit(1)

    narration_path = sys.argv[1]
    game_state = load_game_state()
    narration_text = load_narration(narration_path)
    is_epilogue = "epilogue" in narration_path

    # 処刑シーン: 直近の処刑対象の最後の言葉を許可
    executed_name = None
    if "_execution" in narration_path and not is_epilogue:
        for e in reversed(game_state["log"]):
            if e["type"] == "execute":
                executed_name = e["target"]
                break

    errors = validate(game_state, narration_text, is_epilogue=is_epilogue, executed_name=executed_name)

    if errors:
        print("=== バリデーション失敗 ===")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print("=== バリデーション成功 ===")
        sys.exit(0)


if __name__ == "__main__":
    main()
