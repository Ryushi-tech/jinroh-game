#!/usr/bin/env python3
"""GM ナラティブ生成エンジン（Gemini / Claude 切り替え対応）

Usage:
    python3 gemini_gm.py morning                        # Gemini（デフォルト）
    python3 gemini_gm.py morning --backend claude       # Claude に切り替え
    python3 gemini_gm.py discussion --context "..."

環境変数:
    GEMINI_API_KEY    Gemini API キー
    ANTHROPIC_API_KEY Claude API キー
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import npc_agent as _npc_agent

STATE_FILE        = "game_state.json"
CHAR_FILE         = "characters.json"
PLAYER_FILE       = ".player_name"
NOTES_FILE        = ".gm_notes.json"
MODEL_NAME        = "gemini-2.5-flash"
CLAUDE_MODEL_NAME = "claude-haiku-4-5-20251001"
MAX_RETRIES       = 3
TYPING_FILE       = Path(".typing_now")

# ---------------------------------------------------------------------------
# バックエンド切り替え用グローバル
# ---------------------------------------------------------------------------
_call_fn   = None   # call_gemini or call_claude（main で設定）
_token_log: dict[str, int] = {"input": 0, "output": 0}

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
# クライアント初期化
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
        sys.exit(1)

    return genai.Client(api_key=api_key)


def init_claude():
    try:
        import anthropic
    except ImportError:
        print("ERROR: anthropic が未インストールです。", file=sys.stderr)
        print("  python3.11 -m pip install anthropic", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: 環境変数 ANTHROPIC_API_KEY が設定されていません。", file=sys.stderr)
        sys.exit(1)

    return anthropic.Anthropic(api_key=api_key)


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
    notes = load_notes()

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

    # 公開済みCO一覧（.gm_notes.json の public_co_claims から）
    co_claims = notes.get("public_co_claims", {})
    if co_claims:
        role_jp_map = {"seer": "占い師", "medium": "霊媒師", "bodyguard": "狩人"}
        by_role: dict[str, list[str]] = {}
        for name, info in co_claims.items():
            if isinstance(info, dict):
                role = info.get("role", "")
                day  = info.get("day", "?")
            else:
                role = str(info)
                day  = "?"
            role_jp = role_jp_map.get(role, role)
            by_role.setdefault(role_jp, []).append(f"{name}（Day{day}）")
        lines += ["", "### 公開済みCO一覧（重要: 再COさせないこと）"]
        for role_jp, names in by_role.items():
            lines.append(f"  - {role_jp}CO済み: {'、'.join(names)}")

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

    # 公開済み霊媒結果（.gm_notes.json の public_medium_results から）
    medium_results = notes.get("public_medium_results", [])
    if medium_results:
        lines += ["", "### 公開済み霊媒結果"]
        for r in medium_results:
            result_jp = "人狼" if r.get("result") == "werewolf" else "白（人間）"
            lines.append(f"  - Day{r['day']}: {r['actor']} → {r['target']}: {result_jp}")

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
# LLM 呼び出し（Gemini / Claude）
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
    um = getattr(response, "usage_metadata", None)
    if um:
        _token_log["input"]  += getattr(um, "prompt_token_count", 0) or 0
        _token_log["output"] += getattr(um, "candidates_token_count", 0) or 0
    return response.text


def call_claude(client, prompt: str) -> str:
    import anthropic
    response = client.messages.create(
        model=CLAUDE_MODEL_NAME,
        max_tokens=2048,
        system=SYSTEM_INSTRUCTION,
        messages=[{"role": "user", "content": prompt}],
    )
    _token_log["input"]  += response.usage.input_tokens
    _token_log["output"] += response.usage.output_tokens
    return response.content[0].text


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

def generate_scene(filepath: str, prompt: str, prefix: str = "") -> bool:
    """LLM でシーンを生成し validator を通す。最大 MAX_RETRIES 回リトライ。"""
    errors: str | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        if errors:
            retry_prompt = (
                f"{prompt}\n\n"
                f"## 前回のバリデーションエラー（必ず修正してください）\n{errors}"
            )
        else:
            retry_prompt = prompt

        print(f"[LLM] 生成中... (試行 {attempt}/{MAX_RETRIES})", file=sys.stderr)
        text = _call_fn(retry_prompt)
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
# CO ヒント構築（マルチエージェント用）
# ---------------------------------------------------------------------------

def _build_co_hints(state: dict, player: str, brief: dict, disc_num: int) -> dict[str, str]:
    """disc1 用の NPC ごとの CO 指示を生成する。
    Returns: {npc_name: co_hint_text} — disc_num != 1 の場合は空dict。
    """
    if disc_num != 1:
        return {}

    counter_co_raw = brief.get("COUNTER_CO", "none")
    fake_co_names = [
        n.strip() for n in counter_co_raw.split(",")
        if n.strip() and n.strip() != "none"
    ]

    hints: dict[str, str] = {}

    # 偽COするNPCへの指示
    for name in fake_co_names:
        hints[name] = "disc1で占い師としてCOしてください（村を混乱させるための偽CO戦略）。ただし占い結果は絶対に出さないこと（Day1は初日占いなしのため結果が存在しない）"

    # 真占い師NPCへの指示（プレイヤーが占い師でない場合のみ）
    player_is_seer = next(
        (p["role"] == "seer" for p in state["players"] if p["name"] == player),
        False,
    )
    if not player_is_seer:
        real_seer = next(
            (p for p in state["players"]
             if p["role"] == "seer" and p["alive"] and p["name"] != player
             and p["name"] not in fake_co_names),
            None,
        )
        if real_seer:
            results = [
                e for e in state["log"]
                if e["type"] == "seer" and e.get("actor") == real_seer["name"]
            ]
            if results:
                parts = []
                for e in results:
                    result_jp = "人狼" if e["result"] == "werewolf" else "白（人間）"
                    parts.append(f"Day{e['day']}夜 → {e['target']}: {result_jp}")
                results_text = f"（占い結果: {' / '.join(parts)}）"
            else:
                results_text = "（初日のため結果なし）"
            hints[real_seer["name"]] = (
                f"disc1で占い師としてCOしてください {results_text}"
            )

    return hints


# ---------------------------------------------------------------------------
# シーン組み立て（マルチエージェント用）
# ---------------------------------------------------------------------------

def _build_waves(npc_names: list[str], co_hints: dict[str, str]) -> list[list[str]]:
    """NPC をウェーブに分割する。

    アナウンス担当（co_hints あり）がいる場合:
      Wave 1 = アナウンス担当（占い師CO・偽CO）
      Wave 2 = 残り全員（Wave 1 の発言を見てから生成）

    アナウンス担当がいない場合（通常 disc）:
      4 名以上なら先頭 2 名を Wave 1（種まき）、残りを Wave 2（リアクター）
      3 名以下は 1 ウェーブ（全並列）
    """
    announcers = [n for n in npc_names if n in co_hints]
    reactors   = [n for n in npc_names if n not in co_hints]

    if announcers:
        return [announcers, reactors] if reactors else [announcers]

    if len(npc_names) >= 4:
        return [npc_names[:2], npc_names[2:]]

    return [npc_names]


def _clear_typing() -> None:
    """入力中インジケータファイルを削除する。"""
    try:
        TYPING_FILE.unlink()
    except FileNotFoundError:
        pass


def _assemble_scene(results: list[dict], player: str, context: str = "") -> str:
    """NPC 発言結果リストからシーンテキストを組み立てる。"""
    lines: list[str] = []
    if context:
        lines += [f'{player}「{context}」', ""]
    for r in results:
        msg = r.get("message", "").strip()
        if msg:
            lines += [msg, ""]
    return "\n".join(lines).strip() + "\n"


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
    if generate_scene(filepath, prompt):
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


def extract_co_from_scene(scene_text: str, day: int) -> None:
    """生成済みシーンから新規COを抽出して .gm_notes.json の public_co_claims / public_medium_results を更新する。"""
    role_jp_map = {"seer": "占い師", "medium": "霊媒師", "bodyguard": "狩人"}
    prompt = f"""\
以下の人狼ゲームシーンを読み、このシーン内で初めて役職をCOした発言を抽出してください。

対象:
- 占い師CO（「私が占い師」「占い師です」等）
- 霊媒師CO（「霊媒師です」「霊媒師だ」等）
- 狩人CO（「狩人です」「狩人だ」等）

出力形式（JSONのみ。COがなければ空リスト）:
{{"co_claims": [{{"name": "キャラ名", "role": "seer|medium|bodyguard"}}]}}

---
{scene_text}
"""
    try:
        raw = _call_fn(prompt).strip()
        # コードブロック除去
        raw = re.sub(r"```(?:json)?", "", raw).strip().rstrip("`").strip()
        data = json.loads(raw)
        claims = data.get("co_claims", [])
    except Exception:
        return  # 抽出失敗は無視（次のシーン生成に影響しない）

    if not claims:
        return

    notes = load_notes()
    existing = notes.setdefault("public_co_claims", {})
    changed = False
    for c in claims:
        name = c.get("name", "")
        role = c.get("role", "")
        if name and role and name not in existing:
            existing[name] = {"role": role, "day": day}
            changed = True
            print(f"[CO追跡] {name} → {role_jp_map.get(role, role)} CO (Day{day})", file=sys.stderr)

    if changed:
        notes["public_co_claims"] = existing
        with open(NOTES_FILE, "w", encoding="utf-8") as f:
            json.dump(notes, f, ensure_ascii=False, indent=2)


def cmd_discussion(client, state: dict, chars: list, player: str, context: str = "") -> None:
    day      = state["day"]
    brief    = run_discussion_brief()
    filepath = next_disc_file(day)
    disc_num = int(re.search(r"disc(\d+)", filepath).group(1))
    notes    = load_notes()

    # NPC名リスト（生存かつプレイヤー除外）
    npc_names = [p["name"] for p in state["players"] if p["alive"] and p["name"] != player]

    # CO指示を NPC ごとに構築
    co_hints = _build_co_hints(state, player, brief, disc_num)

    # CO順序制御（disc1 のみ）
    if disc_num == 1:
        counter_co_raw = brief.get("COUNTER_CO", "none")
        co_order = brief.get("CO_ORDER", "real_first")
        fake_co_names = [
            n.strip() for n in counter_co_raw.split(",")
            if n.strip() and n.strip() != "none"
        ]
        if fake_co_names:
            fake_set = set(fake_co_names)
            others   = [n for n in npc_names if n not in fake_set]
            fakes    = [n for n in npc_names if n in fake_set]
            if co_order == "counter_first":
                npc_names = fakes + others
            else:  # real_first
                npc_names = others + fakes

    # ウェーブ分割（アナウンス担当が先行、残りが反応）
    waves = _build_waves(npc_names, co_hints)

    # 前disc文脈
    context_discs = _load_prev_discs(day, disc_num)

    errors_str: str | None = None
    scene_text = ""

    try:
        for attempt in range(1, MAX_RETRIES + 1):
            # リトライ時はエラーを全NPCの指示に追記
            current_hints = dict(co_hints)
            if errors_str:
                for name in npc_names:
                    existing = current_hints.get(name, "")
                    current_hints[name] = (
                        (existing + "\n\n" if existing else "")
                        + f"前回のバリデーションエラー（必ず修正してください）:\n{errors_str}"
                    )

            print(
                f"[LLM] NPC発言を直列生成中... (試行 {attempt}/{MAX_RETRIES})",
                file=sys.stderr,
            )

            # 途中経過をシーンファイルに書き出しながら生成するコールバック
            _partial_results: list[dict] = []

            def _on_progress(name: str, message: str | None, _fp: str = filepath) -> None:
                if message is None:
                    # 入力中: typing ファイルを更新
                    TYPING_FILE.write_text(
                        json.dumps({"npc": name, "scene": _fp}, ensure_ascii=False),
                        encoding="utf-8",
                    )
                else:
                    # 完了: typing をクリアして部分シーンを書き出す
                    _clear_typing()
                    if message:
                        _partial_results.append({"name": name, "message": message})
                    partial_text = _assemble_scene(_partial_results, player, context)
                    Path(_fp).write_text(partial_text, encoding="utf-8")

            results = _npc_agent.generate_all_npc_messages(
                npc_names=npc_names,
                state=state,
                notes=notes,
                chars=chars,
                co_hints=current_hints,
                context_discs=context_discs,
                player_context=f'{player}「{context}」' if context else "",
                on_progress=_on_progress,
                waves=waves,
            )

            scene_text = _assemble_scene(results, player, context)
            Path(filepath).write_text(scene_text, encoding="utf-8")

            ok, output = run_validator(filepath)
            if ok:
                print(f"[validator] OK → {filepath}", file=sys.stderr)
                break

            errors_str = output
            print(f"[validator] FAIL (試行 {attempt}):\n{output}", file=sys.stderr)
            if attempt == MAX_RETRIES:
                print(
                    f"ERROR: {MAX_RETRIES} 回試行してもバリデーションを通過できませんでした。",
                    file=sys.stderr,
                )
                return
    finally:
        _clear_typing()  # 例外・早期returnでも必ずクリア

    _npc_agent.save_thoughts(day, disc_num)
    extract_co_from_scene(scene_text, day)
    print(filepath)


_REASON_HINT = {
    "consensus":  "議論の流れ・多数意見に沿った投票。これまでの発言と一貫した動機で宣言する。",
    "pivot":      "議論中の自分の発言とは異なる相手に投票する。役職・裏の思惑は絶対に言わない。"
                  "キャラクターに合った自然な「翻意の言い訳」を考えること。"
                  "例: 直感が変わった / 最後の一手として / あえて流れを変えたい / 念のため など。",
    "conviction": "自分の判断に基づく投票。根拠を一言添える。",
}


def _load_disc_context(day: int) -> str:
    """当日の disc テキストを結合して返す（vote scene の文脈として使用）。"""
    import glob as _glob
    texts = []
    for path in sorted(_glob.glob(f"scene_day{day}_disc*.txt")):
        try:
            with open(path, encoding="utf-8") as f:
                texts.append(f.read().strip())
        except OSError:
            pass
    return "\n\n".join(texts)


def cmd_vote(client, state: dict, chars: list, player: str, npc_votes: dict | None = None) -> None:
    """投票宣言シーンを生成する。

    npc_votes: {voter_name: {"target": str, "reason": str}} の実際の投票結果。
               vote_decide 済みの値。None の場合は village_vote_target のみで生成（後方互換）。
    """
    day      = state["day"]
    filepath = f"scene_day{day}_vote.txt"
    notes    = load_notes()

    alive_names   = [p["name"] for p in state["players"] if p["alive"]]
    state_summary = build_state_summary(state, player)
    char_info     = build_char_info(chars, alive_names)
    disc_context  = _load_disc_context(day)

    if npc_votes:
        lines = []
        for voter, info in npc_votes.items():
            if voter == player:
                continue
            # info は {"target": ..., "reason": ...} または後方互換で str
            if isinstance(info, dict):
                target = info.get("target", "")
                reason = info.get("reason", "consensus")
            else:
                target, reason = str(info), "consensus"
            hint = _REASON_HINT.get(reason, _REASON_HINT["consensus"])
            lines.append(f"- {voter} → {target}（演技指示: {hint}）")
        vote_instruction = (
            "## 各NPCの投票先と演技指示\n"
            "投票先は決定済み。これまでの議論の流れを踏まえ、不自然にならない台詞を書くこと。\n"
            "役職・裏の思惑は台詞に絶対に出さない。\n\n"
            + "\n".join(lines)
        )
    else:
        vote_target = notes.get("village_vote_target", "未定")
        vote_instruction = f"村の多数派の投票先（NPC村人陣営はここに投票する）: {vote_target}"

    context_section = f"\n## 本日の議論（参考: 各NPCの直前の発言）\n{disc_context}\n" if disc_context else ""

    prompt = f"""\
{state_summary}

{char_info}
{context_section}
## タスク: Day {day} 投票宣言シーンを生成してください

{vote_instruction}

生成ルール:
- 各 NPC が投票先を宣言する場面を描写する
- プレイヤー（{player}）の投票宣言は含めない（ユーザーが後で入力する）
- 生存者のみが発言する
- 各キャラクターの口調・一人称・語尾を維持する
- 発言フォーマット: 名前「セリフ」（役職付記禁止）
- 「pivot」の NPC は翻意の理由を自然に語らせること。「なんとなく」「直感です」のみの一行は禁止。

出力: {filepath} のテキストのみ（余分な説明・コードブロック不要）\
"""
    if generate_scene(filepath, prompt):
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
    if generate_scene(filepath, prompt):
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
    if generate_scene(filepath, prompt):
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
    if generate_scene(filepath, prompt):
        print(filepath)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    global _call_fn

    parser = argparse.ArgumentParser(description="GM ナラティブ生成エンジン")
    parser.add_argument(
        "scene",
        choices=["morning", "discussion", "vote", "execution", "epilogue", "epilogue-thread"],
        help="生成するシーンタイプ",
    )
    parser.add_argument(
        "--context", default="",
        help="プレイヤーの直前の発言（discシーンに組み込まれる）",
    )
    parser.add_argument(
        "--backend", choices=["gemini", "claude"], default="gemini",
        help="LLM バックエンド（デフォルト: gemini）",
    )
    parser.add_argument(
        "--votes", default="",
        help="vote シーン用: vote_decide が出力した NPC投票 JSON ({voter:target})",
    )
    args = parser.parse_args()

    if args.backend == "claude":
        client  = init_claude()
        _call_fn = lambda p: call_claude(client, p)
        backend_label = f"Claude ({CLAUDE_MODEL_NAME})"
    else:
        client  = init_gemini()
        _call_fn = lambda p: call_gemini(client, p)
        backend_label = f"Gemini ({MODEL_NAME})"

    # NPC エージェントに call_fn を渡す
    _npc_agent.init(_call_fn)

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
    elif args.scene == "vote":
        npc_votes = json.loads(args.votes) if args.votes else None
        cmd_vote(client, state, chars, player, npc_votes=npc_votes)
    else:
        dispatch[args.scene](client, state, chars, player)

    # トークンサマリー
    total = _token_log["input"] + _token_log["output"]
    print(
        f"[tokens] backend={backend_label}  "
        f"input={_token_log['input']}  output={_token_log['output']}  total={total}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
