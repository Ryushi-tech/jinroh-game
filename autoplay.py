#!/usr/bin/env python3
"""自動テストプレイ v2: Orchestrator + LLMバックエンド上でゲームを最後まで回す。

Usage:
    python3 autoplay.py --runs 5              # FakeBackend（LLMなし・高速）
    python3 autoplay.py --runs 1 --backend cursor   # 実LLMで1ゲーム

各ゲームで以下の整合性を検査し、logs/autoplay_report.json に保存する:
- 生存者数の不変条件（初期人数 - 処刑 - 襲撃死）
- validator によるシーン検査（死人発言・役職漏洩・フォーマット）
- scene_checks による死者呼びかけ（DEAD_TALK）・幽霊（GHOST_TALK）検出
- 投票宣言セリフと実投票の一致（VOTE_CHECK）
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

import engine
import scene_checks
import validator
from llm_backend import FakeBackend, create_backend, load_config
from orchestrator import Orchestrator

BASE_DIR = Path(__file__).resolve().parent
REPORT_FILE = BASE_DIR / "logs" / "autoplay_report.json"

MAX_DAYS = 9


def _player_night_input(state: dict, player: str) -> dict:
    """プレイヤーが夜役職の場合の入力をランダムに決める。"""
    req = engine.night_requirements()
    alive_others = [p["name"] for p in engine.alive_players(state)
                    if p["name"] != player]
    kwargs: dict = {}
    if req["seer"]:
        already = {e["target"] for e in state["log"] if e["type"] == "seer"}
        cands = [n for n in alive_others if n not in already] or alive_others
        kwargs["seer"] = random.choice(cands)
    if req["guard"]:
        prev = engine.last_guard_target(state)
        cands = [n for n in alive_others if n != prev]
        if cands:
            kwargs["guard"] = random.choice(cands)
    if req["attack"]:
        wolf_names = {p["name"] for p in state["players"] if p["role"] == "werewolf"}
        cands = [n for n in alive_others if n not in wolf_names]
        if cands:
            kwargs["attack"] = random.choice(cands)
    return kwargs


def _check_invariants(state: dict, issues: list[str]) -> None:
    """生存者数 = 初期人数 - 処刑累計 - 襲撃死累計。"""
    executes = sum(1 for e in state["log"] if e["type"] == "execute")
    kills = sum(1 for e in state["log"]
                if e["type"] == "attack" and e.get("result") == "killed")
    alive_count = len(engine.alive_players(state))
    expected = len(state["players"]) - executes - kills
    if alive_count != expected:
        issues.append(
            f"INVARIANT: 生存者数 {alive_count} != 期待値 {expected}"
            f"（処刑{executes}・襲撃死{kills}）"
        )


def run_game(num: int, total: int, orch: Orchestrator, player: str) -> dict:
    print(f"\n[ゲーム {num}/{total}]")
    setup = orch.new_game(player)
    print(f"  setup: {setup['player']} / {setup['role_jp']}")
    orch.morning_scene()

    issues: list[str] = []
    winner = None
    day = 1

    for day in range(1, MAX_DAYS + 1):
        print(f"  --- Day {day} ---")
        state_at_start = engine.load_state()

        # 議論ラウンド（プレイヤーは簡易発言）
        disc = orch.discussion_round("状況を整理しましょう。怪しい人はいますか？")
        issues.extend(f"Day{day} {e}" for e in disc["validation_errors"])
        for s in disc["skipped"]:
            issues.append(f"Day{day} NPC_SKIPPED: {s['name']}: {s['error']}")

        disc_path = BASE_DIR / disc["scene"]
        disc_text = disc_path.read_text(encoding="utf-8")
        issues.extend(
            f"Day{day} {e}"
            for e in scene_checks.check_discussion_text(state_at_start, disc_text)
        )

        # 疑惑スコア収集 → 投票
        orch.collect_suspicion()
        state = engine.load_state()
        player_alive = any(p["name"] == player and p["alive"] for p in state["players"])
        vote_target = None
        if player_alive:
            vote_target = engine.compute_vote_plan()
            alive_others = [p["name"] for p in engine.alive_players(state)
                            if p["name"] != player]
            if vote_target not in alive_others:
                vote_target = random.choice(alive_others)

        result = orch.vote_and_execute(vote_target)
        issues.extend(f"Day{day} {i}" for i in result["vote_issues"]
                      if "ERROR" in i)
        print(f"  処刑: {result['executed']}  WIN={result['win']}")

        _check_invariants(engine.load_state(), issues)

        if result["win"] in ("village", "werewolf"):
            winner = result["win"]
            break

        # 夜フェーズ
        state = engine.load_state()
        night_kwargs = _player_night_input(state, player)
        night = orch.resolve_night(**night_kwargs)
        print(f"  夜: 犠牲={night['victim'] or 'none'}  WIN={night['win']}")

        _check_invariants(engine.load_state(), issues)

        if night["win"] in ("village", "werewolf"):
            winner = night["win"]
            break

        orch.morning_scene()

    if winner:
        epi = orch.epilogue(winner)
        for scene in epi["scenes"]:
            errs = validator.validate_file(BASE_DIR / scene)
            issues.extend(f"EPILOGUE {e}" for e in errs)
        print(f"  epilogue 完了  勝者={winner}")
    else:
        print(f"  {MAX_DAYS}日以内に決着せず（異常）")
        issues.append(f"NO_WINNER: {MAX_DAYS}日以内に決着しなかった")

    if issues:
        print(f"  ⚠ 検出された問題 {len(issues)}件:")
        for i in issues:
            print(f"    - {i}")
    else:
        print("  ✓ 整合性チェック全パス")

    return {"game": num, "winner": winner, "days": day, "issues": issues}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--backend", default="fake",
                        choices=["fake", "cursor", "anthropic", "gemini"],
                        help="LLMバックエンド（既定: fake）")
    parser.add_argument("--player", default="オットー")
    args = parser.parse_args()

    config = load_config()
    config["backend"] = args.backend
    backend = FakeBackend() if args.backend == "fake" else create_backend(config)
    orch = Orchestrator(backend=backend, config=config)

    results = [run_game(i, args.runs, orch, args.player)
               for i in range(1, args.runs + 1)]

    REPORT_FILE.parent.mkdir(exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
        f.write("\n")

    total_issues = sum(len(r["issues"]) for r in results)
    winners = [r["winner"] for r in results]
    print(f"\n=== 完了: {args.runs}ゲーム / 問題 {total_issues}件 / 勝者 {winners} ===")
    print(f"レポート: {REPORT_FILE}")
    sys.exit(1 if total_issues else 0)


if __name__ == "__main__":
    main()
