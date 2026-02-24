#!/usr/bin/env python3
"""Gemini GM: Gemini API を使ったナラティブ生成エンジン

Usage:
    python3 gemini_gm.py morning           # 朝・夜明けシーン
    python3 gemini_gm.py discussion        # 議論シーン（連番自動）
    python3 gemini_gm.py vote              # 投票宣言シーン
    python3 gemini_gm.py execution         # 処刑シーン
    python3 gemini_gm.py epilogue          # エピローグ（全役職公開）
    python3 gemini_gm.py epilogue-thread   # 感想戦（BBS風）

環境変数:
    GEMINI_API_KEY  Gemini API キー（必須）。.env ファイルも参照する。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

STATE_FILE  = "game_state.json"
CHAR_FILE   = "characters.json"
PLAYER_FILE = ".player_name"
NOTES_FILE  = ".gm_notes.json"
MODEL_NAME  = "gemini-2.5-pro"
MAX_RETRIES = 3

ROLE_JP = {
    "villager": "村人", "werewolf": "人狼", "seer": "占い師",
    "medium": "霊媒師", "bodyguard": "狩人", "madman": "狂人",
}


# ---------------------------------------------------------------------------
# .env ローダー（python-dotenv 不要）
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()


# ---------------------------------------------------------------------------
# Gemini クライアント初期化
# ---------------------------------------------------------------------------

def init_gemini():
    try:
        from google import genai
    except ImportError:
        print("ERROR: google-genai が未インストールです。", file=sys.stderr)
        print("  python3.11 -m pip install google-genai", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("ERROR: 環境変数 GEMINI_API_KEY が設定されていません。", file=sys.stderr)
        print("  .env ファイルに GEMINI_API_KEY=... を記述するか、", file=sys.stderr)
        print("  export GEMINI_API_KEY=... を実行してください。", file=sys.stderr)
        sys.exit(1)

    return genai.Client(api_key=api_key)


# ---------------------------------------------------------------------------
# ファイル読み込み
# ---------------------------------------------------------------------------

def load_state() -> dict:
    with open(STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_chars() -> list:
    with open(CHAR_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_notes() -> dict:
    try:
        with open(NOTES_FILE, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def pname() -> str:
    return Path(PLAYER_FILE).read_text(encoding="utf-8").strip()


# ---------------------------------------------------------------------------
# gm_helper 呼び出し
# ---------------------------------------------------------------------------

def run_discussion_brief() -> dict:
    """gm_helper.py discussion_brief を実行し KEY=VALUE 辞書で返す。"""
    result = subprocess.run(
        ["python3", "gm_helper.py", "discussion_brief"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: discussion_brief が失敗しました\n{result.stderr}", file=sys.stderr)
        sys.exit(1)
    brief: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            brief[k.strip()] = v.strip()
    return brief


# ---------------------------------------------------------------------------
# ゲーム状態サマリー（秘密情報なし）
# ---------------------------------------------------------------------------

def build_state_summary(state: dict, player: str) -> str:
    alive = [p for p in state["players"] if p["alive"]]
    dead  = [p for p in state["players"] if not p["alive"]]

    lines = [
        f"## ゲーム状態",
        f"- 日付: Day {state['day']}  フェーズ: {state['phase']}",
        f"- プレイヤー操作キャラ: {player}",
        f"",
        f"### 生存者 {len(alive)} 名",
        *[f"  - {p['name']}" for p in alive],
    ]
    if dead:
        lines += ["", f"### 死亡者 {len(dead)} 名"]
        for p in dead:
            lines.append(f"  - {p['name']}（{_death_cause(state, p['name'])}）")

    # 公開済み占い結果（生存占い師のみ）
    alive_names = {p["name"] for p in alive}
    seer_logs = [
        e for e in state["log"]
        if e["type"] == "seer" and e.get("actor", "") in alive_names
    ]
    if seer_logs:
        lines += ["", "### 公開済み占い結果"]
        for e in seer_logs:
            result_jp = "人狼" if e["result"] == "werewolf" else "白（人間）"
            lines.append(f"  - Day{e['day']}夜: {e['actor']} → {e['target']}: {result_jp}")

    return "\n".join(lines)


def _death_cause(state: dict, name: str) -> str:
    for e in reversed(state["log"]):
        if e["type"] == "execute" and e["target"] == name:
            return f"Day{e['day']} 処刑"
        if e["type"] == "attack" and e["target"] == name and e.get("result") == "killed":
            return f"Day{e['day']} 夜・襲撃死"
    return "死亡"


# ---------------------------------------------------------------------------
# キャラクター設定サマリー
# ---------------------------------------------------------------------------

def build_char_info(chars: list, names: list) -> str:
    char_map = {c["name"]: c for c in chars}
    lines = ["## キャラクター設定（口調・一人称を厳守すること）"]
    for name in names:
        c = char_map.get(name)
        if not c:
            continue
        lines += [
            f"\n### {name}",
            f"- 一人称: {c['speech_style']['first_person']}",
            f"- 口調: {c['speech_style']['tone']}",
            f"- 語尾・口癖: {c['speech_style']['vocal_tics']}",
            f"- 推理傾向: {c['intellect']}",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# システムインストラクション
# ---------------------------------------------------------------------------

SYSTEM_INSTRUCTION = """\
あなたは人狼ゲームのゲームマスター兼ナレーターです。

## 絶対守るべきルール

### 死人は喋らない
alive: false のキャラクターに一切発言させない。

### 役職透け防止
- NPCの役職を絶対に出力しない。
- 発言フォーマットに役職を付記しない。
  NG: カタリナ（村人）「〜」  OK: カタリナ「〜」
- 処刑・襲撃死者の役職は公開しない。霊媒師が自らCOして初めて「人狼だった/人間だった」を公表できる。
  NG: 「正体は占い師だった」  OK: 霊媒師COによる公表のみ

### 出力フォーマット
- キャラクターの発言は「名前「セリフ」」の形式のみ。
- ナレーションは地の文として書く。
- 一人称・語尾・口癖はキャラクター設定に厳密に従う。

### NPCの議論スタイル（経験者モード）
- CO促し・ローラー・縄計算・確定白黒の扱いを全員が理解している。
- 曖昧な返答には「それでは答えになっていない」と追及する。
- 初心者向け解説・セオリー説明は絶対に行わない。

### 情報管理
- ゲーム終了前（epilogue以外）は誰の役職も明かさない。
- 占い・霊媒結果は「COして発表する」形式でのみ公開できる。\
"""


# ---------------------------------------------------------------------------
# ファイル名ユーティリティ
# ---------------------------------------------------------------------------

def next_disc_file(day: int) -> str:
    i = 1
    while Path(f"scene_day{day}_disc{i}.txt").exists():
        i += 1
    return f"scene_day{day}_disc{i}.txt"


# ---------------------------------------------------------------------------
# Gemini API 呼び出し
# ---------------------------------------------------------------------------

def call_gemini(client, prompt: str) -> str:
    from google.genai import types
    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
        ),
    )
    return response.text


# ---------------------------------------------------------------------------
# バリデーション
# ---------------------------------------------------------------------------

def run_validator(filepath: str) -> tuple[bool, str]:
    result = subprocess.run(
        ["python3", "validator.py", filepath],
        capture_output=True, text=True,
    )
    return result.returncode == 0, (result.stdout + result.stderr).strip()


# ---------------------------------------------------------------------------
# シーン生成（リトライ付き）
# ---------------------------------------------------------------------------

def generate_scene(client, filepath: str, prompt: str, prefix: str = "") -> bool:
    """Gemini でシーンを生成し validator を通す。最大 MAX_RETRIES 回リトライ。"""
    errors: str | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        if errors:
            retry_prompt = (
                f"{prompt}\n\n"
                f"## 前回のバリデーションエラー（必ず修正してください）\n{errors}"
            )
        else:
            retry_prompt = prompt

        print(f"[Gemini] 生成中... (試行 {attempt}/{MAX_RETRIES})", file=sys.stderr)
        text = call_gemini(client, retry_prompt)
        if prefix:
            text = prefix + "\n" + text
        Path(filepath).write_text(text, encoding="utf-8")

        ok, output = run_validator(filepath)
        if ok:
            print(f"[validator] OK → {filepath}", file=sys.stderr)
            return True

        errors = output
        print(f"[validator] FAIL (試行 {attempt}):\n{output}", file=sys.stderr)

    print(f"ERROR: {MAX_RETRIES} 回試行してもバリデーションを通過できませんでした。", file=sys.stderr)
    return False


# ---------------------------------------------------------------------------
# CO インストラクション構築
# ---------------------------------------------------------------------------

def build_co_instruction(state: dict, player: str, brief: dict, disc_num: int) -> str:
    """disc1 用の占い師CO指示文を生成する（Gemini プロンプト内部用）。"""
    if disc_num != 1:
        return ""

    counter_co_raw = brief.get("COUNTER_CO", "none")
    co_order       = brief.get("CO_ORDER", "real_first")
    day            = state["day"]

    fake_co_names = [n.strip() for n in counter_co_raw.split(",")
                     if n.strip() and n.strip() != "none"]

    # 真占い師（NPC）を特定
    real_seer = next(
        (p["name"] for p in state["players"]
         if p["role"] == "seer" and p["alive"] and p["name"] != player
         and p["name"] not in fake_co_names),
        None,
    )

    # 真占い師の公開済み占い結果（生存しているときのみ意味がある）
    seer_results_text = ""
    if real_seer:
        results = [
            e for e in state["log"]
            if e["type"] == "seer" and e.get("actor") == real_seer
        ]
        if results:
            parts = []
            for e in results:
                result_jp = "人狼" if e["result"] == "werewolf" else "白（人間）"
                parts.append(f"Day{e['day']}夜 → {e['target']}: {result_jp}")
            seer_results_text = f"（占い結果: {' / '.join(parts)}）"
        elif day == 1:
            seer_results_text = "（初日のため結果なし）"

    lines = ["## [GMの内部指示] disc1 での占いCO（出力には役職名を書かないこと）"]

    def real_seer_line():
        if real_seer:
            return f"  - {real_seer} が占い師としてCOする {seer_results_text}"
        return ""

    def fake_seer_line():
        if fake_co_names:
            names = "、".join(fake_co_names)
            return f"  - {names} が占い師として対抗COする（村を混乱させる虚偽のCO）"
        return ""

    if co_order == "counter_first":
        if fake_seer_line():
            lines.append(fake_seer_line())
        if real_seer_line():
            lines.append(real_seer_line())
    else:  # real_first
        if real_seer_line():
            lines.append(real_seer_line())
        if fake_seer_line():
            lines.append(fake_seer_line())

    if len(lines) == 1:
        return ""  # COなし
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# シーンタイプ別プロンプト生成
# ---------------------------------------------------------------------------

def cmd_morning(client, state: dict, chars: list, player: str) -> None:
    day      = state["day"]
    filepath = f"scene_day{day}_morning.txt"

    # 前夜の結果をログから取得
    victim  = None
    guarded = False
    for e in reversed(state["log"]):
        if e["type"] == "attack" and e["day"] == day - 1:
            if e.get("result") == "killed":
                victim = e["target"]
            elif e.get("result") == "guarded":
                guarded = True
            break

    alive_names = [p["name"] for p in state["players"] if p["alive"]]
    state_summary = build_state_summary(state, player)
    char_info     = build_char_info(chars, alive_names)

    if victim:
        victim_desc = f"昨夜の犠牲者: {victim}（死体が発見される）"
    else:
        victim_desc = "昨夜の犠牲者: なし（「昨晩の犠牲者はなし」とだけ告知する。護衛の有無は描写しない）"

    prompt = f"""\
{state_summary}

{char_info}

## タスク: Day {day} 朝シーンを生成してください

{victim_desc}

生成ルール:
- 夜明けの村、前夜の出来事が明らかになる場面を描写する
- 犠牲者がいる場合: {victim} の死体発見と村人の動揺を描写する
- 犠牲者がいない場合: 「昨晩の犠牲者はなし」とだけ告知する（護衛成功かどうかは触れない）
- 生存者数名が反応してよい（全員でなくてもよい）
- 200〜400 文字程度
- プレイヤー（{player}）の発言を生成してもよいが、自然な範囲で

出力: {filepath} のテキストのみ（余分な説明・コードブロック不要）\
"""
    if generate_scene(client, filepath, prompt):
        print(filepath)


def _load_prev_discs(day: int, disc_num: int) -> str:
    """同日の disc1〜disc(N-1) の内容を読み込んで返す。"""
    parts = []
    for i in range(1, disc_num):
        p = Path(f"scene_day{day}_disc{i}.txt")
        if p.exists():
            parts.append(f"=== disc{i} ===\n{p.read_text(encoding='utf-8')}")
    if not parts:
        return ""
    return "## 本日の議論（これまでの流れ・必ず把握して続けること）\n" + "\n\n".join(parts)


def cmd_discussion(client, state: dict, chars: list, player: str, context: str = "") -> None:
    day      = state["day"]
    brief    = run_discussion_brief()
    filepath = next_disc_file(day)
    disc_num = int(re.search(r"disc(\d+)", filepath).group(1))

    alive_names    = [p["name"] for p in state["players"] if p["alive"]]
    state_summary  = build_state_summary(state, player)
    char_info      = build_char_info(chars, alive_names)
    co_instruction = build_co_instruction(state, player, brief, disc_num)
    prev_discs     = _load_prev_discs(day, disc_num)

    brief_text = "\n".join(f"- {k}: {v}" for k, v in brief.items())

    confirmed_black = brief.get("CONFIRMED_BLACK", "none")
    confirmed_white = brief.get("CONFIRMED_WHITE", "none")
    vote_plan       = brief.get("VOTE_PLAN", "none")

    prompt = f"""\
{state_summary}

{char_info}

## GMブリーフィング（内部情報・出力には含めないこと）
{brief_text}

{co_instruction}

{prev_discs}

## タスク: Day {day} 議論シーン {disc_num} を生成してください
{f"## プレイヤーの直前の発言（この直後からシーンを続けること）{chr(10)}{player}「{context}」{chr(10)}" if context else ""}
生成ルール:
- 生存者のみが発言する（死亡者は絶対に発言させない）
- これまでの議論（上記）で出た情報・CO・発言を必ず踏まえて続けること
- 確定黒 [{confirmed_black}] が存在する場合: 吊り最優先として議論を誘導する
- 確定白 [{confirmed_white}] が存在する場合: 信頼できる存在として扱う
- 村の多数派の投票予測 [{vote_plan}] を踏まえた議論にする
- NPCは人狼ゲーム経験者として論理的・戦略的に議論する
- 各キャラクターの一人称・語尾・口癖を厳守する
- 発言フォーマット: 名前「セリフ」（役職付記禁止）
- プレイヤー（{player}）の発言は最小限でよい（ユーザーが続きを担当する）

出力: {filepath} のテキストのみ（余分な説明・コードブロック不要）\
"""
    prefix = f'{player}「{context}」' if context else ""
    if generate_scene(client, filepath, prompt, prefix=prefix):
        print(filepath)


def cmd_vote(client, state: dict, chars: list, player: str) -> None:
    day      = state["day"]
    filepath = f"scene_day{day}_vote.txt"
    notes    = load_notes()

    alive_names   = [p["name"] for p in state["players"] if p["alive"]]
    state_summary = build_state_summary(state, player)
    char_info     = build_char_info(chars, alive_names)
    vote_target   = notes.get("village_vote_target", "未定")

    prompt = f"""\
{state_summary}

{char_info}

## タスク: Day {day} 投票宣言シーンを生成してください

村の多数派の投票先（NPC村人陣営はここに投票する）: {vote_target}

生成ルール:
- 各 NPC が投票先を宣言する場面を描写する
- プレイヤー（{player}）の投票宣言は含めない（ユーザーが後で入力する）
- 生存者のみが発言する
- 各キャラクターの口調を維持する
- 発言フォーマット: 名前「セリフ」（役職付記禁止）

出力: {filepath} のテキストのみ（余分な説明・コードブロック不要）\
"""
    if generate_scene(client, filepath, prompt):
        print(filepath)


def cmd_execution(client, state: dict, chars: list, player: str) -> None:
    day = state["day"]
    filepath = f"scene_day{day}_execution.txt"

    # 処刑者をログから取得
    executed = None
    for e in reversed(state["log"]):
        if e["type"] == "execute" and e["day"] == day:
            executed = e["target"]
            break
    if not executed:
        print("ERROR: 処刑ログが見つかりません", file=sys.stderr)
        sys.exit(1)

    alive_names   = [p["name"] for p in state["players"] if p["alive"]]
    state_summary = build_state_summary(state, player)
    # 処刑者も発言できる（最後の言葉）
    char_info = build_char_info(chars, alive_names + [executed])

    prompt = f"""\
{state_summary}

{char_info}

## タスク: Day {day} 処刑シーンを生成してください

処刑対象: {executed}

生成ルール:
- {executed} が処刑される場面を描写する
- {executed} は処刑前に最後の言葉を一言述べてもよい
- 処刑後、{executed} の役職は絶対に明かさない（霊媒師COがない限り）
  NG: 「正体は〇〇だった」 NG: 「村人/人狼だったのか」という確定的表現
- 村人たちの反応を短く描写する
- 発言フォーマット: 名前「セリフ」（役職付記禁止）

出力: {filepath} のテキストのみ（余分な説明・コードブロック不要）\
"""
    if generate_scene(client, filepath, prompt):
        print(filepath)


def cmd_epilogue(client, state: dict, chars: list, player: str) -> None:
    filepath = "scene_epilogue.txt"

    # 全役職公開（エピローグのみ許可）
    role_lines = ["## 全役職（エピローグで公開）"]
    for p in state["players"]:
        status   = "生存" if p["alive"] else "死亡"
        role_jp  = ROLE_JP.get(p["role"], p["role"])
        role_lines.append(f"  - {p['name']}: {role_jp}（{status}）")
    roles_text = "\n".join(role_lines)

    all_names     = [p["name"] for p in state["players"]]
    char_info     = build_char_info(chars, all_names)
    alive_wolves  = [p for p in state["players"] if p["alive"] and p["role"] == "werewolf"]
    winner        = "人狼陣営" if alive_wolves else "村人陣営"

    prompt = f"""\
{roles_text}

{char_info}

## タスク: エピローグシーンを生成してください

勝者: {winner}

生成ルール:
- 勝敗を宣告し、全員の役職を明かす（エピローグでは役職公開が許可されている）
- 村人たちの驚き・納得・安堵などの反応を描写する
- 各キャラクターが自分の役職と行動を振り返ってもよい
- 発言フォーマット: 名前「セリフ」

出力: {filepath} のテキストのみ（余分な説明・コードブロック不要）\
"""
    if generate_scene(client, filepath, prompt):
        print(filepath)


def cmd_epilogue_thread(client, state: dict, chars: list, player: str) -> None:
    filepath = "scene_epilogue_thread.txt"

    role_lines = ["## 全役職（感想戦用）"]
    for p in state["players"]:
        role_jp = ROLE_JP.get(p["role"], p["role"])
        role_lines.append(f"  - {p['name']}: {role_jp}")
    roles_text = "\n".join(role_lines)

    all_names    = [p["name"] for p in state["players"]]
    char_info    = build_char_info(chars, all_names)
    alive_wolves = [p for p in state["players"] if p["alive"] and p["role"] == "werewolf"]
    winner       = "人狼陣営" if alive_wolves else "村人陣営"

    prompt = f"""\
{roles_text}

{char_info}

## タスク: 感想戦スレッド（BBS風）を生成してください

勝者: {winner}

生成ルール:
- ゲーム終了後の感想戦。死亡者を含む全員が役職公開の上で振り返る
- 「あの時こうすればよかった」「あれが失敗だった」など本音で語る
- BBS 投稿風に全員がバランスよく発言する
- 各キャラクターの口調・一人称・語尾を維持する
- 発言フォーマット: 名前「セリフ」
- 情報秘匿ルールはゲーム終了後のため適用外

出力: {filepath} のテキストのみ（余分な説明・コードブロック不要）\
"""
    if generate_scene(client, filepath, prompt):
        print(filepath)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Gemini GM: ナラティブ生成エンジン")
    parser.add_argument(
        "scene",
        choices=["morning", "discussion", "vote", "execution", "epilogue", "epilogue-thread"],
        help="生成するシーンタイプ",
    )
    parser.add_argument(
        "--context", default="",
        help="プレイヤーの直前の発言（discシーンに組み込まれる）",
    )
    args = parser.parse_args()

    client = init_gemini()
    state  = load_state()
    chars  = load_chars()
    player = pname()

    dispatch = {
        "morning":         cmd_morning,
        "discussion":      cmd_discussion,
        "vote":            cmd_vote,
        "execution":       cmd_execution,
        "epilogue":        cmd_epilogue,
        "epilogue-thread": cmd_epilogue_thread,
    }
    if args.scene == "discussion":
        cmd_discussion(client, state, chars, player, context=args.context)
    else:
        dispatch[args.scene](client, state, chars, player)


if __name__ == "__main__":
    main()
