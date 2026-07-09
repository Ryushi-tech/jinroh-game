#!/usr/bin/env python3
"""描写ファイルのバリデーター。

Usage: python3 validator.py <描写ファイル>

game_state.json を読み込み、描写ファイル中の発言が
ゲーム状態と整合しているかを検証する。

ライブラリとしても使用可: validate(game_state, text, ...) -> list[str]

発言者の判定は「行頭の 名前「セリフ」」のみを対象とする。
地の文に埋め込まれた引用（例: 彼は「静かに」と言った）は発言と見なさない。
これにより旧版の [存在不明] 誤検知を解消している。
"""

import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# 行頭の発言行: 名前「セリフ」 / 名前：「セリフ」 / 名前:「セリフ」
_SPEAKER_LINE = re.compile(r"^([^\s「」：:]+)[：:]?\s*「", re.MULTILINE)

ROLE_LABELS = [
    "人狼", "狂人", "占い師", "霊媒師", "狩人", "村人",
    "werewolf", "madman", "seer", "medium", "bodyguard", "villager",
]


def load_game_state(path=None):
    with open(path or (BASE_DIR / "game_state.json"), encoding="utf-8") as f:
        return json.load(f)


def load_narration(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def extract_speakers(text, player_names):
    """行頭の発言行から発言者名を抽出する（重複除去・順序維持）。"""
    speakers = []
    for m in _SPEAKER_LINE.finditer(text):
        candidate = m.group(1)
        for name in player_names:
            if candidate == name or candidate.endswith(name):
                if name not in speakers:
                    speakers.append(name)
                break
    return speakers


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

    # 存在チェック: 行頭の発言行の名前が players に存在するか
    # （地の文中の引用はチェック対象外 — 行頭アンカーで誤検知を防止）
    for m in _SPEAKER_LINE.finditer(narration_text):
        candidate = m.group(1)
        matched = any(candidate == name or candidate.endswith(name)
                      for name in player_names)
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
        dup_pattern = re.escape(name) + r"「" + re.escape(name) + r"[：:]"
        if re.search(dup_pattern, narration_text):
            errors.append(
                f"[フォーマット不正] {name} の発言に名前重複パターン（{name}「{name}：）が検出されました"
            )
    if re.search(r"「「", narration_text):
        errors.append("[フォーマット不正] 二重開きかぎ括弧（「「）が検出されました")
    if re.search(r"」」", narration_text):
        errors.append("[フォーマット不正] 二重閉じかぎ括弧（」」）が検出されました")

    return errors


def validate_file(narration_path, game_state=None):
    """ファイルを検証してエラーリストを返す（orchestrator 用）。"""
    if game_state is None:
        game_state = load_game_state()
    narration_text = load_narration(narration_path)
    name = Path(narration_path).name
    is_epilogue = "epilogue" in name

    executed_name = None
    if "_execution" in name and not is_epilogue:
        for e in reversed(game_state["log"]):
            if e["type"] == "execute":
                executed_name = e["target"]
                break

    errors = validate(game_state, narration_text,
                      is_epilogue=is_epilogue, executed_name=executed_name)

    # 幕引き本体は語り部の地の文のみ（キャラ自白形式の役職漏洩を防ぐ）
    if name == "scene_epilogue.txt":
        player_names = [p["name"] for p in game_state["players"]]
        if extract_speakers(narration_text, player_names):
            errors.append(
                "[エピローグ] 幕引きシーンにキャラクター発言（名前「」形式）を含めない"
            )

    return errors


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 validator.py <描写ファイル>", file=sys.stderr)
        sys.exit(1)

    errors = validate_file(sys.argv[1])

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
