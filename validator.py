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


def validate(game_state, narration_text):
    errors = []
    players = game_state["players"]
    player_names = [p["name"] for p in players]
    dead_names = [p["name"] for p in players if not p["alive"]]

    speakers = extract_speakers(narration_text, player_names)

    # 死人発言チェック
    for name in speakers:
        if name in dead_names:
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

    return errors


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 validator.py <描写ファイル>", file=sys.stderr)
        sys.exit(1)

    narration_path = sys.argv[1]
    game_state = load_game_state()
    narration_text = load_narration(narration_path)

    errors = validate(game_state, narration_text)

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
