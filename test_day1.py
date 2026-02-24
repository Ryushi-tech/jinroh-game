#!/usr/bin/env python3
"""Day 1 ベンチマーク: Gemini vs Claude のトークン数比較

Usage:
    python3.11 test_day1.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

PLAYER = "オットー"
SCENES = ["morning", "discussion", "vote"]   # execution は vote_decide が必要なので省略


def run(cmd: list[str]) -> tuple[str, str]:
    """コマンドを実行して (stdout, stderr) を返す。"""
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout, r.stderr


def setup_game() -> None:
    """村側の役職が出るまで setup を繰り返す。"""
    for _ in range(30):
        out, _ = run(["python3.11", "gm_helper.py", "setup", "--player", PLAYER])
        for line in out.splitlines():
            if line.startswith("PLAYER_ROLE="):
                role = line.split("=", 1)[1]
                if role not in ("werewolf", "madman"):
                    print(f"  セットアップ完了: {line}")
                    return
    sys.exit("ERROR: 30回試行しても村側役職が出ませんでした")


def parse_token_line(stderr: str) -> dict[str, int]:
    """[tokens] backend=...  input=N  output=N  total=N を解析する。"""
    m = re.search(r"input=(\d+)\s+output=(\d+)\s+total=(\d+)", stderr)
    if not m:
        return {"input": 0, "output": 0, "total": 0}
    return {"input": int(m.group(1)), "output": int(m.group(2)), "total": int(m.group(3))}


def run_day1(backend: str) -> dict[str, dict[str, int]]:
    """指定バックエンドで Day 1 を実行してシーンごとのトークン数を返す。"""
    results: dict[str, dict[str, int]] = {}
    # vote には village_vote_target が必要
    for scene in SCENES:
        if scene == "vote":
            # discussion_brief で vote_plan を取得して village_vote_target をセット
            out, _ = run(["python3.11", "gm_helper.py", "discussion_brief"])
            vote_plan = "none"
            for line in out.splitlines():
                if line.startswith("VOTE_PLAN="):
                    vote_plan = line.split("=", 1)[1]
            notes_path = Path(".gm_notes.json")
            notes = json.loads(notes_path.read_text()) if notes_path.exists() else {}
            notes["village_vote_target"] = vote_plan
            notes_path.write_text(json.dumps(notes, ensure_ascii=False, indent=2))

        _, stderr = run(["python3.11", "gemini_gm.py", scene, "--backend", backend])
        tokens = parse_token_line(stderr)
        results[scene] = tokens
        print(f"  [{backend}] {scene}: input={tokens['input']} output={tokens['output']} total={tokens['total']}")

    return results


def print_table(gemini: dict, claude: dict) -> None:
    print()
    print("=" * 62)
    print(f"{'Scene':<12} {'Gemini input':>12} {'Gemini out':>10} {'Claude input':>12} {'Claude out':>10}")
    print("-" * 62)
    g_total = c_total = 0
    for scene in SCENES:
        g = gemini.get(scene, {})
        c = claude.get(scene, {})
        print(f"{scene:<12} {g.get('input',0):>12,} {g.get('output',0):>10,} {c.get('input',0):>12,} {c.get('output',0):>10,}")
        g_total += g.get("total", 0)
        c_total += c.get("total", 0)
    print("-" * 62)
    print(f"{'TOTAL':<12} {sum(gemini[s].get('input',0) for s in SCENES):>12,} "
          f"{sum(gemini[s].get('output',0) for s in SCENES):>10,} "
          f"{sum(claude[s].get('input',0) for s in SCENES):>12,} "
          f"{sum(claude[s].get('output',0) for s in SCENES):>10,}")
    print(f"{'(input+output)':<12} {g_total:>23,} {c_total:>23,}")
    print("=" * 62)
    if g_total and c_total:
        ratio = c_total / g_total
        print(f"Claude/Gemini 比率: {ratio:.2f}x")


if __name__ == "__main__":
    print("=== Gemini バックエンド ===")
    setup_game()
    gemini_result = run_day1("gemini")

    print("\n=== Claude バックエンド ===")
    setup_game()
    claude_result = run_day1("claude")

    print_table(gemini_result, claude_result)
