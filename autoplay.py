#!/usr/bin/env python3
"""自動テストプレイ: AIがすべての決定を行い整合性チェックを実行する

Usage:
    python3.11 autoplay.py           # 10ゲーム実行
    python3.11 autoplay.py --runs 3  # 3ゲーム実行
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

MAX_DAYS    = 9   # 無限ループ防止
REPORT_FILE = "autoplay_report.json"

# レート制限リトライ設定（通常ウェイトは 0）
# NOTE: "429" は数値として出力に紛れ込む可能性があるため除外し、
#       より具体的なAPIエラー文字列のみで検知する
_RATE_LIMIT_SIGNALS = [
    "RESOURCE_EXHAUSTED",
    "ResourceExhausted",
    "quota exceeded",
    "rate limit exceeded",
    "Too Many Requests",
]
_RATE_LIMIT_WAIT_BASE = 30   # 初回待機秒数（リトライごとに +30s）
_RATE_LIMIT_MAX_TRIES = 10   # 最大リトライ回数

# ---------------------------------------------------------------------------
# 投票シーン整合性チェック
# ---------------------------------------------------------------------------

# 「{name}[さん君殿]?に投票」「{name}[さん君殿]?に一票」「{name}[さん君殿]?を処刑/吊」など
_VOTE_SUFFIX = r'(?:さん|君|殿|様)?'
# autoplay.py の _VOTE_PHRASES を強化
_VOTE_PHRASES = [
    r'に投票', r'に一票', r'に入れ', r'を処刑', r'を吊', r'吊り',
    r'に投じ', r'にします', r'を選', r'を疑う', r'へ投票' # 自然な表現を追加
]


def _extract_vote_target(dialogue: str, candidate_names: list[str]) -> str | None:
    """セリフ文字列から投票先の名前を抽出する。見つからなければ None。"""
    for name in candidate_names:
        escaped = re.escape(name)
        for phrase in _VOTE_PHRASES:
            if re.search(f'{escaped}{_VOTE_SUFFIX}{phrase}', dialogue):
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
        m = re.match(r'^(.+?)「(.+)」\s*$', line.strip())
        if m:
            speaker  = m.group(1).strip()
            dialogue = m.group(2).strip()
            speaker_dialogues.setdefault(speaker, []).append(dialogue)

    for npc, info in npc_votes.items():
        if npc == player:
            continue
        expected  = info["target"] if isinstance(info, dict) else str(info)
        dialogues = speaker_dialogues.get(npc, [])

        if not dialogues:
            issues.append(
                f"VOTE_CHECK_WARN: {npc} のセリフが scene に見つからない "
                f"(expected={expected})"
            )
            continue

        combined  = " ".join(dialogues)
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


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def run(cmd: list[str]) -> tuple[str, str]:
    """サブプロセス実行。レート制限検出時のみ待機リトライ（通常ウェイト 0）。"""
    for attempt in range(1, _RATE_LIMIT_MAX_TRIES + 1):
        r = subprocess.run(cmd, capture_output=True, text=True)
        combined = r.stdout + r.stderr
        if any(sig.lower() in combined.lower() for sig in _RATE_LIMIT_SIGNALS):
            wait = _RATE_LIMIT_WAIT_BASE * attempt
            tag  = cmd[-1] if cmd else "?"
            print(
                f"  [rate_limit] {tag} → {wait}s 待機後リトライ"
                f" ({attempt}/{_RATE_LIMIT_MAX_TRIES})",
                flush=True,
            )
            time.sleep(wait)
            continue
        return r.stdout.strip(), r.stderr.strip()
    return "", f"[rate_limit] {_RATE_LIMIT_MAX_TRIES}回リトライ失敗: {cmd[-1]}"


def parse_kv(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def parse_npc_votes(text: str) -> dict[str, dict]:
    """vote_decide 出力の NPC_VOTES_START...NPC_VOTES_END ブロックを解析する。

    出力フォーマット: voter=target:reason_category:role_hint
    戻り値: {voter: {"target": str, "reason": str, "role_hint": str}}
    """
    result: dict[str, dict] = {}
    in_block = False
    for line in text.splitlines():
        if line.strip() == "NPC_VOTES_START":
            in_block = True
        elif line.strip() == "NPC_VOTES_END":
            in_block = False
        elif in_block and "=" in line:
            voter, _, rest = line.partition("=")
            parts = rest.split(":")
            target    = parts[0].strip()
            reason    = parts[1].strip() if len(parts) > 1 else "consensus"
            role_hint = parts[2].strip() if len(parts) > 2 else "villager"
            result[voter.strip()] = {
                "target": target, "reason": reason, "role_hint": role_hint,
            }
    return result


def load_state() -> dict:
    with open("game_state.json", encoding="utf-8") as f:
        return json.load(f)


def load_notes() -> dict:
    try:
        with open(".gm_notes.json", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save_notes(notes: dict) -> None:
    with open(".gm_notes.json", "w", encoding="utf-8") as f:
        json.dump(notes, f, ensure_ascii=False, indent=2)


def player_name() -> str:
    return Path(".player_name").read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# セットアップ（任意の役職を受け入れる）
# ---------------------------------------------------------------------------

def setup_game() -> dict[str, str]:
    out, _ = run(["python3.11", "gm_helper.py", "setup", "--player", "オットー"])
    return parse_kv(out)


# ---------------------------------------------------------------------------
# 夜行動の引数決定
# ---------------------------------------------------------------------------

def decide_night_args(state: dict) -> list[str]:
    pname = player_name()
    pdata = next((p for p in state["players"] if p["name"] == pname), None)
    if not pdata or not pdata["alive"]:
        return []

    role  = pdata["role"]
    alive = [p for p in state["players"] if p["alive"] and p["name"] != pname]

    if role == "seer":
        checked = {e["target"] for e in state["log"] if e["type"] == "seer"}
        cands = [p["name"] for p in alive if p["name"] not in checked]
        if cands:
            return ["--seer", cands[0]]

    elif role == "bodyguard":
        notes    = load_notes()
        co_alive = {
            name for name, info in notes.get("public_co_claims", {}).items()
            if isinstance(info, dict) and info.get("role") == "seer"
            and any(p["name"] == name and p["alive"] for p in state["players"])
        }
        target = next(iter(co_alive), None) or (alive[0]["name"] if alive else None)
        if target:
            return ["--guard", target]

    elif role == "werewolf":
        wolf_names = {p["name"] for p in state["players"] if p["role"] in ("werewolf", "madman")}
        targets = [p["name"] for p in alive if p["name"] not in wolf_names]
        if targets:
            return ["--attack", targets[0]]

    return []


# ---------------------------------------------------------------------------
# 1ゲーム完走
# ---------------------------------------------------------------------------

def run_game(game_num: int, verbose: bool = True) -> dict:
    log: list[str] = []

    def note(msg: str) -> None:
        log.append(msg)
        if verbose:
            print(f"  {msg}", flush=True)

    result: dict = {
        "game": game_num,
        "player_role": None,
        "days": 0,
        "winner": None,
        "errors": [],
        "log": log,
    }

    try:
        kv = setup_game()
        result["player_role"] = kv.get("PLAYER_ROLE")
        note(f"setup: {kv.get('PLAYER_NAME')} / {kv.get('PLAYER_ROLE')}")

        for day in range(1, MAX_DAYS + 1):
            result["days"] = day
            note(f"--- Day {day} ---")

            # 朝シーン
            out, err = run(["python3.11", "gemini_gm.py", "morning"])
            if not out:
                result["errors"].append(f"Day{day} morning 生成失敗\n{err[:200]}")
                note(f"morning 失敗")

            # discussion_brief → vote_plan を取得
            brief_out, _ = run(["python3.11", "gm_helper.py", "discussion_brief"])
            brief = parse_kv(brief_out)
            vote_plan = brief.get("VOTE_PLAN", "none")

            # village_vote_target をセット
            notes = load_notes()
            notes["village_vote_target"] = vote_plan
            save_notes(notes)

            # 議論シーン（1ターンのみ）
            out, err = run(["python3.11", "gemini_gm.py", "discussion"])
            if not out:
                result["errors"].append(f"Day{day} disc 生成失敗\n{err[:200]}")
                note(f"disc 失敗")

            # 疑惑スコア収集（議論終了後・vote_decide 前）
            run(["python3.11", "gemini_gm.py", "suspicion-json"])

            # vote_decide 前に alive_names と game_state を保存
            # （vote_decide が処刑を実行して状態を変えるため、vote scene 生成前に退避する）
            pre_vote_state = load_state()
            alive_names    = [p["name"] for p in pre_vote_state["players"] if p["alive"]]
            pre_vote_state_json = Path("game_state.json").read_text(encoding="utf-8")

            # vote_decide を先に実行して実際の投票結果を取得
            vote_out, _ = run([
                "python3.11", "gm_helper.py", "vote_decide",
                "--player-vote", vote_plan,
            ])
            vkv = parse_kv(vote_out)
            executed = vkv.get("EXECUTED", "?")
            note(f"処刑: {executed}  WIN={vkv.get('WIN', 'none')}")
            post_vote_state_json = Path("game_state.json").read_text(encoding="utf-8")

            # 実際の投票結果を使って投票シーンを生成
            # validator が「被処刑者は死亡済み」と判定しないよう、処刑前の状態に一時復元する
            npc_votes = parse_npc_votes(vote_out)
            votes_json = json.dumps(npc_votes, ensure_ascii=False)
            Path("game_state.json").write_text(pre_vote_state_json, encoding="utf-8")
            out, err = run(["python3.11", "gemini_gm.py", "vote", "--votes", votes_json])
            Path("game_state.json").write_text(post_vote_state_json, encoding="utf-8")
            if not out:
                result["errors"].append(f"Day{day} vote 生成失敗\n{err[:200]}")

            # 投票シーン整合性チェック
            if out:
                pname = player_name()
                vote_issues = check_vote_consistency(
                    out, npc_votes, pname, alive_names,
                )
                for issue in vote_issues:
                    note(issue)
                    if "ERROR" in issue:
                        result["errors"].append(issue)

            # 処刑シーン
            out, err = run(["python3.11", "gemini_gm.py", "execution"])
            if not out:
                result["errors"].append(f"Day{day} execution 生成失敗\n{err[:200]}")

            if vkv.get("WIN") in ("village", "werewolf"):
                result["winner"] = vkv["WIN"]
                break

            # 夜行動
            state     = load_state()
            night_args = decide_night_args(state)
            night_out, _ = run(
                ["python3.11", "gm_helper.py", "night_actions"] + night_args
            )
            nkv    = parse_kv(night_out)
            victim = nkv.get("VICTIM", "none")
            note(f"夜: 犠牲={victim}  WIN={nkv.get('WIN', 'none')}")

            if nkv.get("WIN") in ("village", "werewolf"):
                result["winner"] = nkv["WIN"]
                break

            # Day 終了ごとの mid-game チェック
            day_issues = check_day_state(day)
            if day_issues:
                note(f"  ⚠ Day{day} 整合性問題:")
                for di in day_issues:
                    note(f"    • {di}")
                result["errors"].extend([f"[Day{day}] {di}" for di in day_issues])
            else:
                note(f"  ✓ Day{day} 整合性 OK")
        else:
            result["errors"].append(f"MAX_DAYS({MAX_DAYS})超過")

        # エピローグ
        run(["python3.11", "gemini_gm.py", "epilogue"])
        run(["python3.11", "gemini_gm.py", "epilogue-thread"])
        note(f"epilogue 完了  勝者={result['winner']}")

        # 論理破綻チェック（ゲーム終了後）
        try:
            final_state = load_state()
            logic_failures = check_logic_failures(final_state, result["errors"])
            result["logic_failures"] = logic_failures
        except Exception as e2:
            result["logic_failures"] = [
                {"type": "CHECK_ERROR", "day": None, "detail": str(e2)}
            ]

    except Exception as e:
        result["errors"].append(f"例外: {type(e).__name__}: {e}")
        result.setdefault("logic_failures", [])
        if verbose:
            import traceback
            traceback.print_exc()

    return result


# ---------------------------------------------------------------------------
# mid-game 日次チェック
# ---------------------------------------------------------------------------

def check_day_state(day: int) -> list[str]:
    """1日終了後に実行する軽量チェック（ゲーム途中用）。"""
    issues: list[str] = []
    try:
        state = load_state()
    except Exception as e:
        return [f"game_state.json 読み込みエラー: {e}"]

    players = state["players"]
    alive   = [p for p in players if p["alive"]]
    dead    = [p for p in players if not p["alive"]]
    log     = state["log"]

    # 死者数の整合
    n_exec   = len([e for e in log if e["type"] == "execute"])
    n_killed = len([e for e in log if e["type"] == "attack" and e.get("result") == "killed"])
    if len(dead) != n_exec + n_killed:
        issues.append(f"死者数不一致: dead={len(dead)}, execute={n_exec}+kill={n_killed}")

    # 同日シーンファイルのバリデーション
    for sf in sorted(Path(".").glob(f"scene_day{day}_*.txt")):
        out, err = run(["python3.11", "validator.py", str(sf)])
        combined = (out + err).strip()
        if combined and "エラー" in combined:
            issues.append(f"validator [{sf.name}]: {combined[:120]}")

    # 重複占い
    seer_targets = [e["target"] for e in log if e["type"] == "seer"]
    if len(seer_targets) != len(set(seer_targets)):
        dupes = [t for t in set(seer_targets) if seer_targets.count(t) > 1]
        issues.append(f"重複占い: {dupes}")

    return issues


# ---------------------------------------------------------------------------
# 整合性チェック（ゲーム終了後）
# ---------------------------------------------------------------------------

def check_consistency(result: dict) -> list[str]:
    issues: list[str] = []

    try:
        state = load_state()
    except Exception as e:
        return [f"game_state.json 読み込みエラー: {e}"]

    players = state["players"]
    alive   = [p for p in players if p["alive"]]
    dead    = [p for p in players if not p["alive"]]
    log     = state["log"]

    # 1. 死者数 = 処刑数 + 襲撃死数
    n_exec    = len([e for e in log if e["type"] == "execute"])
    n_killed  = len([e for e in log if e["type"] == "attack" and e.get("result") == "killed"])
    if len(dead) != n_exec + n_killed:
        issues.append(
            f"死者数不一致: dead={len(dead)}, execute={n_exec}+kill={n_killed}={n_exec+n_killed}"
        )

    # 2. 勝者と盤面の整合
    winner        = result.get("winner")
    alive_wolves  = [p for p in alive if p["role"] == "werewolf"]
    alive_village = [p for p in alive if p["role"] not in ("werewolf", "madman")]

    if winner == "village" and alive_wolves:
        issues.append(f"勝者=village なのに人狼が生存: {[w['name'] for w in alive_wolves]}")
    elif winner == "werewolf" and not alive_wolves:
        issues.append("勝者=werewolf なのに人狼が全滅")
    elif winner is None and result["days"] < MAX_DAYS:
        issues.append("勝者が決まらずゲームが終了")

    # 3. 生存者数 = 全員 - 死者数
    if len(alive) + len(dead) != len(players):
        issues.append(f"プレイヤー総数不一致: alive={len(alive)} dead={len(dead)} total={len(players)}")

    # 4. 同一ターゲットへの重複占い
    seer_targets = [e["target"] for e in log if e["type"] == "seer"]
    if len(seer_targets) != len(set(seer_targets)):
        dupes = [t for t in set(seer_targets) if seer_targets.count(t) > 1]
        issues.append(f"同一対象を複数回占い: {dupes}")

    # 5. 処刑ログと実際の死者の整合
    exec_targets = {e["target"] for e in log if e["type"] == "execute"}
    actual_dead_names = {p["name"] for p in dead}
    if not exec_targets.issubset(actual_dead_names):
        issues.append(f"処刑ログの対象が死亡リストにない: {exec_targets - actual_dead_names}")

    # 6. シーンファイルの validator 実行
    scene_files = sorted(Path(".").glob("scene_*.txt"))
    for sf in scene_files:
        out, err = run(["python3.11", "validator.py", str(sf)])
        combined = (out + err).strip()
        if combined and "エラー" in combined:
            issues.append(f"validator エラー [{sf.name}]: {combined[:150]}")

    # 7. 日付の進みが log と一致するか
    days_in_log = max((e.get("day", 0) for e in log), default=0)
    if abs(days_in_log - result["days"]) > 1:
        issues.append(f"log の最終 day={days_in_log} が result.days={result['days']} と乖離")

    return issues


# ---------------------------------------------------------------------------
# 論理破綻チェック（ゲーム終了後）
# ---------------------------------------------------------------------------

def check_logic_failures(state: dict, errors: list[str]) -> list[dict]:
    """論理破綻を検出し、タイプ別のリストで返す。

    検出項目:
      FRIENDLY_FIRE  : 人狼が人狼仲間に投票
      DEAD_SPEECH    : 死亡者の発言（validator エラーから抽出）
      DUPLICATE_SEER : 同一ターゲットへの重複占い
      VOTE_MISMATCH  : 投票シーンと実投票ロジックの矛盾（VOTE_CHECK_ERROR）
    """
    failures: list[dict] = []
    players  = state.get("players", [])
    log      = state.get("log", [])
    wolf_set = {p["name"] for p in players if p["role"] == "werewolf"}

    # 1. 身内への誤爆: 人狼が人狼仲間に処刑投票
    for e in log:
        if e.get("type") != "execute":
            continue
        for voter, target in e.get("votes", {}).items():
            if voter in wolf_set and target in wolf_set:
                failures.append({
                    "type":   "FRIENDLY_FIRE",
                    "day":    e["day"],
                    "detail": f"{voter}(人狼) が {target}(人狼) に投票",
                })

    # 2. 死人の発言: validator エラーから検出
    _dead_kw = ["死亡者", "死人", "alive: false", "死んでいる", "DEAD"]
    for err in errors:
        if any(kw in err for kw in _dead_kw):
            failures.append({
                "type":   "DEAD_SPEECH",
                "day":    None,
                "detail": err[:120],
            })

    # 3. 重複占い
    seer_targets = [e["target"] for e in log if e.get("type") == "seer"]
    for t in {x for x in seer_targets if seer_targets.count(x) > 1}:
        failures.append({
            "type":   "DUPLICATE_SEER",
            "day":    None,
            "detail": f"{t} を複数回占い（{seer_targets.count(t)} 回）",
        })

    # 4. 投票シーンと実ロジックの矛盾（run_game が errors に VOTE_CHECK_ERROR として記録済み）
    for err in errors:
        if "VOTE_CHECK_ERROR" in err:
            failures.append({
                "type":   "VOTE_MISMATCH",
                "day":    None,
                "detail": err[:120],
            })

    return failures


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()

    print(f"{'='*64}")
    print(f"自動テストプレイ  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {args.runs}ゲーム")
    print(f"{'='*64}")

    all_results: list[dict] = []
    total_issues = 0

    for i in range(1, args.runs + 1):
        print(f"\n[ゲーム {i}/{args.runs}]")
        result  = run_game(i)
        issues  = check_consistency(result)
        result["issues"] = issues
        all_results.append(result)
        total_issues += len(issues)

        if issues:
            print(f"  ⚠ 整合性問題 {len(issues)} 件:", flush=True)
            for iss in issues:
                print(f"    • {iss}", flush=True)
        else:
            print("  ✓ 整合性 OK", flush=True)

        if result["errors"]:
            print(f"  ✗ エラー:", flush=True)
            for e in result["errors"]:
                print(f"    • {e[:120]}", flush=True)

        # 論理破綻サマリー（ゲームごと）
        logic_failures = result.get("logic_failures", [])
        print(f"\n  ── 論理破綻チェック ({'%d件' % len(logic_failures) if logic_failures else '問題なし'}) ──", flush=True)
        if logic_failures:
            for lf in logic_failures:
                day_str = f"Day{lf['day']} " if lf.get("day") else ""
                print(f"    [{lf['type']}] {day_str}{lf['detail']}", flush=True)
        else:
            print("    FRIENDLY_FIRE  : 0件", flush=True)
            print("    DEAD_SPEECH    : 0件", flush=True)
            print("    DUPLICATE_SEER : 0件", flush=True)
            print("    VOTE_MISMATCH  : 0件", flush=True)

    # 全体サマリー
    print(f"\n{'='*64}")
    winners = [r.get("winner") for r in all_results]
    days    = [r.get("days", 0) for r in all_results]
    print(f"結果サマリー ({args.runs}ゲーム)")
    print(f"  勝利: 村人={winners.count('village')}  人狼={winners.count('werewolf')}  未決={winners.count(None)}")
    print(f"  平均日数: {sum(days)/len(days):.1f}  最大={max(days)}  最小={min(days)}")
    print(f"  整合性問題: 合計 {total_issues} 件  ({total_issues/args.runs:.1f} 件/ゲーム)")

    # 論理破綻タイプ別集計
    type_counts: dict[str, int] = {}
    total_logic = 0
    for r in all_results:
        for lf in r.get("logic_failures", []):
            type_counts[lf["type"]] = type_counts.get(lf["type"], 0) + 1
            total_logic += 1
    print(f"  論理破綻: 合計 {total_logic} 件  ({total_logic/args.runs:.1f} 件/ゲーム)")
    for t in ["FRIENDLY_FIRE", "DEAD_SPEECH", "DUPLICATE_SEER", "VOTE_MISMATCH", "CHECK_ERROR"]:
        cnt = type_counts.get(t, 0)
        mark = "✓" if cnt == 0 else "⚠"
        print(f"    {mark} {t:<20}: {cnt} 件")
    print(f"{'='*64}")

    # レポート保存
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    print(f"詳細レポート → {REPORT_FILE}")


if __name__ == "__main__":
    main()
