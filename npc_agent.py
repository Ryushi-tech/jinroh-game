#!/usr/bin/env python3
"""マルチエージェント NPC 実行エンジン。

ウェーブ（グループ）方式:
  - グループ内は ThreadPoolExecutor で並列実行
  - グループ間は直列（前ウェーブの発言を最大 MAX_CONTEXT_LINES 件引き継ぐ）
  - 各 NPC は自分の視点フィルター済みデータのみを受け取る
"""

from __future__ import annotations

import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable

from logic_engine import get_player_view

MAX_CONTEXT_LINES = 5  # ウェーブ間で引き継ぐ直近発言数の上限

ROLE_JP = {
    "villager": "村人", "werewolf": "人狼", "seer": "占い師",
    "medium": "霊媒師", "bodyguard": "狩人", "madman": "狂人",
}

# ---------------------------------------------------------------------------
# デバッグログ（debug_view.log）— プレイヤー画面には絶対に出さない
# ---------------------------------------------------------------------------

_DEBUG_LOG_FILE = "debug_view.log"
_debug_log_lock = threading.Lock()


def _write_debug_log(npc_name: str, prompt: str, thought: str, raw: str = "") -> None:
    """生プロンプトと思考を debug_view.log に追記する。

    scene_*.txt パターンと一致しないためビューアには読み込まれない。
    ThreadPoolExecutor から並列呼び出しされるためロックで保護する。

    raw が渡された場合、名前「 形式の混入（JSON崩壊シグナル）を検出して記録する。
    """
    model_info = f"  [model={_npc_model_name}]" if _npc_model_name else ""

    # ★ JSON崩壊検出: raw の先頭が { 以外（かぎ括弧を含む行）で始まる場合はエラー
    contamination_msg = ""
    if raw:
        stripped = raw.lstrip()
        if not stripped.startswith("{"):
            # 先頭が { でない → 会話形式か説明文が混入している可能性
            contamination_msg = f"⚠ CONTAMINATION_ERROR: raw の冒頭が '{{' でない: {stripped[:80]!r}"
        elif re.search(r'^\s*\S+「', raw, re.MULTILINE):
            # { で始まっているが内部に 名前「 形式の行がある
            contamination_msg = f"⚠ CONTAMINATION_ERROR: raw に 名前「 形式が混入: {raw[:80]!r}"

    with _debug_log_lock:
        with open(_DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"[{npc_name}]{model_info}\n")
            if contamination_msg:
                f.write(f"{contamination_msg}\n")
                # stderr にも出力して即座に気づけるようにする
                print(f"[npc_agent] {npc_name}: {contamination_msg}", file=sys.stderr)
            f.write(f"{'─'*40}\nPROMPT:\n{prompt}\n")
            f.write(f"{'─'*40}\nTHOUGHT:\n{thought or '(空)'}\n")

# ---------------------------------------------------------------------------
# モジュールレベル状態
# ---------------------------------------------------------------------------

_call_fn: Callable[[str], str] | None = None
_call_fn_json: Callable[[str], str] | None = None  # NPC 発言生成専用（response_mime_type=JSON）
_npc_model_name: str = ""                          # debug_view.log に記録するモデル名
_thoughts_buffer: dict[str, str] = {}


def init(
    call_fn: Callable[[str], str],
    call_fn_json: Callable[[str], str] | None = None,
    npc_model_name: str = "",
) -> None:
    """gemini_gm.py の main() から _call_fn 確定後に呼ぶ。
    call_fn_json が None の場合は call_fn をフォールバックとして使う。
    """
    global _call_fn, _call_fn_json, _npc_model_name
    _call_fn = call_fn
    _call_fn_json = call_fn_json if call_fn_json is not None else call_fn
    _npc_model_name = npc_model_name


def write_debug_header(label: str) -> None:
    """新しいセクション（ゲーム開始・disc開始など）のヘッダーを debug_view.log に書く。"""
    import datetime
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _debug_log_lock:
        with open(_DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n{'#'*60}\n")
            f.write(f"# {label}  [{ts}]\n")
            f.write(f"{'#'*60}\n")


# ---------------------------------------------------------------------------
# コンテキストクレンジング
# ---------------------------------------------------------------------------

_DIALOGUE_PATTERN = re.compile(r'^(\S+)「(.+)」\s*$')


def _dialogue_lines_to_json(text: str) -> str:
    """名前「セリフ」形式のテキストを JSON 配列文字列に完全変換する。

    変換前: トーマス「疑っているのはシモンだ。」
    変換後: [{"speaker":"トーマス","speech":"疑っているのはシモンだ。"}, ...]

    かぎ括弧を一切含まない純粋なデータ形式にすることで、
    LLM が会話形式を模倣して JSON 崩壊（名前「{...}」）を起こすのを防ぐ。
    """
    entries = []
    for line in text.split("\n"):
        m = _DIALOGUE_PATTERN.match(line.strip())
        if m:
            entries.append({"speaker": m.group(1), "speech": m.group(2)})
    if not entries:
        return ""
    return json.dumps(entries, ensure_ascii=False)


# ---------------------------------------------------------------------------
# プロンプト構築
# ---------------------------------------------------------------------------

def _build_npc_prompt(
    npc_name: str,
    player_view: dict,
    char_data: dict,
    co_hint: str = "",
    context_discs: str = "",
) -> str:
    role    = player_view["self"]["role"]
    role_jp = player_view["self"]["role_jp"]
    day     = player_view["day"]

    alive = player_view["alive_players"]
    dead  = player_view["dead_players"]

    # 仲間の狼セクション（狼の場合のみ）
    wolf_section = ""
    if role == "werewolf" and player_view.get("wolf_teammates"):
        teammates = "、".join(player_view["wolf_teammates"])
        wolf_section = f"\n## 仲間の人狼（秘密情報）\n{teammates}\n"

    # 公開CO一覧
    co_claims = player_view.get("public_co_claims", {})
    co_lines = []
    for name, info in co_claims.items():
        if isinstance(info, dict):
            r_jp = ROLE_JP.get(info.get("role", ""), info.get("role", ""))
            co_lines.append(f"  - {name}: {r_jp}CO（Day{info.get('day', '?')}）")
    co_section = "\n".join(co_lines) if co_lines else "  なし"

    # 公開占い結果
    seer_results = player_view.get("public_seer_results", [])
    seer_lines = [
        f"  - Day{r['day']}夜 {r['actor']} → {r['target']}: {r['result']}"
        for r in seer_results
    ]
    seer_section = "\n".join(seer_lines) if seer_lines else "  なし"

    # 公開霊媒結果
    medium_results = player_view.get("public_medium_results", [])
    medium_lines = [
        f"  - Day{r.get('day', '?')} {r.get('actor', '?')} → {r.get('target', '?')}: {r.get('result', '?')}"
        for r in medium_results
    ]
    medium_section = "\n".join(medium_lines) if medium_lines else "  なし"

    # 処刑履歴
    exec_history = player_view.get("execution_history", [])
    exec_lines = [f"  - Day{e['day']}: {e['target']}（処刑）" for e in exec_history]
    exec_section = "\n".join(exec_lines) if exec_lines else "  なし"

    # 死亡者リスト
    dead_lines = [f"  - {d['name']}（{d['cause']}）" for d in dead]
    dead_section = "\n".join(dead_lines) if dead_lines else "  なし"

    # キャラ設定
    speech = char_data.get("speech_style", {})

    # GMからの指示（CO指示など）
    co_hint_section = f"\n## GMからの指示\n{co_hint}\n" if co_hint else ""

    # 前disc文脈（かぎ括弧を一切含まない JSON 配列に完全変換して渡す）
    if context_discs:
        json_context = _dialogue_lines_to_json(context_discs)
        context_section = f"\n## 議論の文脈（JSON形式・参照のみ）\n{json_context}\n" if json_context else ""
    else:
        context_section = ""

    prompt = f"""\
あなたは{npc_name}として人狼ゲームに参加しています。Day {day} の議論フェーズです。

## 自分の役職
{role_jp}（あなただけが知っている秘密情報）{wolf_section}
## キャラクター設定
- 一人称: {speech.get('first_person', '私')}
- 口調: {speech.get('tone', '普通')}
- 語尾・口癖: {speech.get('vocal_tics', 'なし')}
- 推理傾向: {char_data.get('intellect', '標準的')}

## 現在の状況

### 生存者（{len(alive)}名）
{chr(10).join(f'  - {n}' for n in alive)}

### 死亡者
{dead_section}

### 公開CO一覧
{co_section}

### 公開占い結果
{seer_section}

### 公開霊媒結果
{medium_section}

### 処刑履歴
{exec_section}{co_hint_section}{context_section}
### END OF CONTEXT ###
**ここから先の指示のみに従うこと。出力は JSON 以外の形式を一切使わないこと。**

## 絶対ルール
- 他のプレイヤーの役職はわかりません。公開情報だけで推理してください。
- 死亡者（dead_players に含まれる人）には発言させない
- message フィールドの値: {npc_name} の名前 + 日本語かぎ括弧 + 発言内容 + 閉じかぎ括弧
- 一人称・語尾・口癖をキャラクター設定に厳密に従うこと
- 人狼ゲーム経験者として論理的・戦略的に発言すること
- 初心者向け解説・セオリー説明は禁止
- CO促し・ローラー・縄計算・確定白黒の扱いを理解して発言すること
- 【重要】Day 1（初日）は占い結果・霊媒結果が存在しない。占い師・霊媒師はCOできるが、結果の発表は不可。Day 1 に占い結果を述べることは絶対禁止。
- 【対話リアリティ】発言の冒頭に必ず直前の議論・発言者への言及（アンカー）を入れること。前の発言を無視した「独り言」は絶対禁止。
- 【目的意識】「議論を通じて人狼を特定する（または欺く）」という目的に常に能動的であること。

## 出力
出力は必ず {{ で始まり }} で終わる JSON オブジェクト 1 つのみ。
{{"thought": "内面的な考察や戦略（非公開・日本語）", "message": "ここに発言を記入"}}\
"""
    return prompt


# ---------------------------------------------------------------------------
# メッセージフォーマット正規化
# ---------------------------------------------------------------------------

def _normalize_message(npc_name: str, message: str) -> str:
    """メッセージを '名前「セリフ」' 形式（1行1発言）に正規化する。

    ビューアは行単位でパースするため、複数行にまたがる
    「名前「...（改行）...」」ブロックは各行を個別の「名前「行」」に分割する。
    名前プレフィックスがない行は「名前「」」でラップする。
    """
    if not message:
        return message

    lines = message.strip().split('\n')
    result: list[str] = []
    open_block: list[str] = []   # 閉じ括弧がまだ来ていない行の蓄積

    def flush_block() -> None:
        for part in open_block:
            if part.strip():
                result.append(f'{npc_name}「{part.strip()}」')
        open_block.clear()

    for line in lines:
        stripped = line.strip()

        if not stripped:
            flush_block()
            continue

        if stripped.startswith(f'{npc_name}「'):
            flush_block()
            inner = stripped[len(f'{npc_name}「'):]
            if inner.endswith('」'):
                result.append(f'{npc_name}「{inner[:-1]}」')
            else:
                open_block.append(inner)

        elif open_block:
            # 開きブロックの続き行
            if stripped.endswith('」'):
                open_block.append(stripped[:-1])
                flush_block()
            else:
                open_block.append(stripped)

        else:
            # 名前プレフィックスなし → 1行ラップ
            result.append(f'{npc_name}「{stripped}」')

    flush_block()  # 閉じ忘れ処理

    return '\n'.join(result) if result else message


# ---------------------------------------------------------------------------
# JSON 抽出ヘルパー
# ---------------------------------------------------------------------------

def _parse_json_robust(raw: str, npc_name: str) -> dict:
    """複数の戦略で LLM 出力から JSON を抽出する。

    前処理: 名前「{...}」 ラッパーを剥がす（JSON崩壊パターン対応）
    戦略1: コードブロック除去後に直接パース
    戦略2: 最初の { から最後の } を抜き出してパース（ポストプロセッサ）
    戦略3: thought / message フィールドを正規表現で個別抽出
    """
    # 前処理: 名前「{...}」 ラッパーを剥がす
    stripped = raw.strip()
    wrapper_m = re.match(r'^\S+「([\s\S]+)」\s*$', stripped)
    if wrapper_m:
        print(f"[npc_agent] {npc_name} 名前「」ラッパー検出・剥離", file=sys.stderr)
        stripped = wrapper_m.group(1).strip()

    # 戦略1: コードブロック除去後に直接パース
    cleaned = re.sub(r"```(?:json)?", "", stripped).strip().rstrip("`").strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # 戦略2: 最初の { から最後の } を抜き出してパース
    start = cleaned.find("{")
    end   = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            pass

    # 戦略3: thought と message を個別に正規表現で抽出
    thought_m = re.search(r'"thought"\s*:\s*"((?:[^"\\]|\\.)*)"', cleaned, re.DOTALL)
    message_m = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', cleaned, re.DOTALL)
    if message_m:
        print(f"[npc_agent] {npc_name} JSON策略3(regex)で抽出", file=sys.stderr)
        return {
            "thought": thought_m.group(1) if thought_m else "",
            "message": message_m.group(1).replace("\\n", "\n"),
        }

    raise ValueError(f"JSON extraction failed for {npc_name}")


def _validate_npc_output(data: dict, npc_name: str) -> None:
    """NPC 出力スキーマを検証する。失敗時は ValueError を raise する。

    - thought: str（空文字列も許容）
    - message: 非空の str
    """
    thought = data.get("thought")
    message = data.get("message")
    if not isinstance(thought, str):
        raise ValueError(f"[schema] {npc_name}: thought が str でない ({type(thought).__name__})")
    if not isinstance(message, str) or not message.strip():
        raise ValueError(f"[schema] {npc_name}: message が空または str でない ({message!r})")


# ---------------------------------------------------------------------------
# NPC 1名の発言生成
# ---------------------------------------------------------------------------

def generate_npc_message(
    npc_name: str,
    player_view: dict,
    char_data: dict,
    co_hint: str = "",
    context_discs: str = "",
) -> dict:
    """NPC 1名の発言を生成する。
    Returns: {"name": str, "thought": str, "message": str, "error": str|None}
    """
    if _call_fn_json is None and _call_fn is None:
        return {
            "name": npc_name, "thought": "", "message": "",
            "error": "call_fn not initialized",
        }

    prompt = _build_npc_prompt(npc_name, player_view, char_data, co_hint, context_discs)

    # JSON 専用関数があればそちらを使う（response_mime_type=application/json）
    _fn = _call_fn_json if _call_fn_json is not None else _call_fn
    raw = ""
    try:
        raw = _fn(prompt).strip()
        data = _parse_json_robust(raw, npc_name)
        _validate_npc_output(data, npc_name)   # スキーマ検証
        thought = data.get("thought", "")
        msg = _normalize_message(npc_name, data.get("message", ""))
        _write_debug_log(npc_name, prompt, thought, raw=raw)
        return {
            "name":    npc_name,
            "thought": thought,
            "message": msg,
            "error":   None,
        }
    except Exception as e:
        # JSON 抽出完全失敗: raw テキスト全体をメッセージとして扱う
        print(f"[npc_agent] {npc_name} JSON extract failed: {e}", file=sys.stderr)
        print(f"[npc_agent] {npc_name} raw[:200]: {raw[:200]!r}", file=sys.stderr)
        _write_debug_log(npc_name, prompt, f"(JSON parse failed) raw={raw[:200]!r}", raw=raw)
        msg = _normalize_message(npc_name, raw)
        return {
            "name":    npc_name,
            "thought": "",
            "message": msg,
            "error":   str(e),
        }


# ---------------------------------------------------------------------------
# ウェーブ内並列実行ヘルパー
# ---------------------------------------------------------------------------

def _build_running_context(context_discs: str, completed_lines: list[str]) -> str:
    """ウェーブへ渡すコンテキスト文字列を構築する。
    completed_lines は直近 MAX_CONTEXT_LINES 件のみ含める（情報爆発防止）。
    """
    if not completed_lines:
        return context_discs
    recent = completed_lines[-MAX_CONTEXT_LINES:]
    # ★ かぎ括弧を一切含まない JSON 配列に完全変換して渡す（JSON崩壊防止）
    json_recent = _dialogue_lines_to_json("\n".join(recent))
    wave_section = f"## 今discのここまでの発言（JSON形式）\n{json_recent}" if json_recent else ""
    return (
        context_discs
        + ("\n\n" if context_discs else "")
        + wave_section
    )


def _run_wave(
    wave_names: list[str],
    state: dict,
    notes: dict,
    char_map: dict,
    co_hints: dict[str, str],
    running_context: str,
) -> list[dict]:
    """1ウェーブを ThreadPoolExecutor で並列実行し、wave_names 順で結果を返す。"""
    if not wave_names:
        return []

    if len(wave_names) == 1:
        name = wave_names[0]
        try:
            player_view = get_player_view(state, name, notes)
        except ValueError as e:
            print(f"[npc_agent] get_player_view failed for {name}: {e}", file=sys.stderr)
            return [{"name": name, "thought": "", "message": "", "error": str(e)}]
        char_data = char_map.get(name, {})
        return [generate_npc_message(name, player_view, char_data, co_hints.get(name, ""), running_context)]

    results_by_name: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=min(len(wave_names), 4)) as executor:
        futures: dict = {}
        for name in wave_names:
            try:
                player_view = get_player_view(state, name, notes)
            except ValueError as e:
                print(f"[npc_agent] get_player_view failed for {name}: {e}", file=sys.stderr)
                results_by_name[name] = {
                    "name": name, "thought": "", "message": "", "error": str(e),
                }
                continue
            future = executor.submit(
                generate_npc_message,
                name, player_view, char_map.get(name, {}), co_hints.get(name, ""), running_context,
            )
            futures[future] = name

        for future in as_completed(futures):
            name = futures[future]
            try:
                result = future.result()
            except Exception as e:
                result = {"name": name, "thought": "", "message": "", "error": str(e)}
            results_by_name[name] = result

    return [
        results_by_name.get(n, {"name": n, "thought": "", "message": "", "error": "missing"})
        for n in wave_names
    ]


# ---------------------------------------------------------------------------
# ウェーブ制NPC生成（メインAPI）
# ---------------------------------------------------------------------------

def generate_all_npc_messages(
    npc_names: list[str],
    state: dict,
    notes: dict,
    chars: list,
    co_hints: dict[str, str],
    context_discs: str = "",
    player_context: str = "",
    on_progress: Callable[[str, str | None], None] | None = None,
    waves: list[list[str]] | None = None,
) -> list[dict]:
    """ウェーブ方式で NPC 発言を生成する。

    waves: 各ウェーブの NPC 名リストのリスト。
           グループ内は並列、グループ間は直列
           （直近 MAX_CONTEXT_LINES 件のコンテキストを引き継ぐ）。
           None の場合は全員を 1 ウェーブ（並列）で実行。

    on_progress(name, None)    : ウェーブ開始（代表者名で入力中表示）
    on_progress(name, message) : NPC 1 名の生成完了（シーン更新に使う）
    """
    global _thoughts_buffer

    if waves is None:
        waves = [npc_names]  # フォールバック: 全員 1 ウェーブ（並列）

    char_map = {c["name"]: c for c in chars}
    all_results: list[dict] = []
    completed_lines: list[str] = []

    if player_context:
        completed_lines.append(player_context)

    for wave_idx, wave_names in enumerate(waves):
        if not wave_names:
            continue

        print(
            f"[npc_agent] Wave {wave_idx + 1}/{len(waves)}: {wave_names}",
            file=sys.stderr,
        )

        running_context = _build_running_context(context_discs, completed_lines)

        # ウェーブ開始通知（代表者を「入力中」として表示）
        if on_progress:
            on_progress(wave_names[0], None)

        wave_results = _run_wave(wave_names, state, notes, char_map, co_hints, running_context)

        for result in wave_results:
            all_results.append(result)
            msg = result.get("message", "").strip()
            if msg:
                completed_lines.append(msg)
            _thoughts_buffer[result["name"]] = result.get("thought", "")
            if on_progress:
                on_progress(result["name"], msg)

    return all_results


# ---------------------------------------------------------------------------
# 疑惑スコア収集
# ---------------------------------------------------------------------------

def _build_suspicion_prompt(
    npc_name: str,
    player_view: dict,
    context_discs: str = "",
) -> str:
    alive = player_view["alive_players"]
    others = [n for n in alive if n != npc_name]
    role_jp = player_view["self"]["role_jp"]
    context_section = f"\n## 今日の議論\n{context_discs}\n" if context_discs else ""
    example = json.dumps({n: 5 for n in others[:3]}, ensure_ascii=False)

    return f"""\
あなたは{npc_name}として人狼ゲームに参加しています。

## 自分の役職
{role_jp}（あなただけが知っている秘密情報）

## 生存者（自分を除く）
{chr(10).join(f'  - {n}' for n in others)}
{context_section}
## タスク
今日の議論を踏まえ、各生存者への疑惑度を 1〜10（10が最も怪しい）でJSONで返してください。
自分自身はリストに含めないこと。他のプレイヤーの役職は分かりません。公開情報だけで判断してください。

出力フォーマット（JSONのみ・コードブロック不要）:
{example}
（全員分を出力すること）\
"""


def _collect_one_suspicion(
    name: str,
    state: dict,
    notes: dict,
    char_map: dict,
    context_discs: str,
) -> dict[str, int]:
    try:
        player_view = get_player_view(state, name, notes)
    except ValueError as e:
        print(f"[npc_agent] suspicion get_player_view failed for {name}: {e}", file=sys.stderr)
        return {}
    prompt = _build_suspicion_prompt(name, player_view, context_discs)
    _fn = _call_fn_json if _call_fn_json is not None else _call_fn
    try:
        raw = _fn(prompt).strip()
        data = _parse_json_robust(raw, name)
        return {k: max(1, min(10, int(v))) for k, v in data.items() if isinstance(v, (int, float))}
    except Exception as e:
        print(f"[npc_agent] {name} suspicion parse failed: {e}", file=sys.stderr)
        return {}


def collect_all_suspicion_scores(
    npc_names: list[str],
    state: dict,
    notes: dict,
    chars: list,
    context_discs: str = "",
) -> dict[str, float]:
    """全 NPC の疑惑スコアを並列収集し、プレイヤーごとの平均値を返す。

    Returns: {player_name: avg_score (1.0–10.0)}
    """
    if _call_fn is None and _call_fn_json is None:
        return {}

    char_map = {c["name"]: c for c in chars}
    all_scores: list[dict[str, int]] = []

    with ThreadPoolExecutor(max_workers=min(len(npc_names), 4)) as executor:
        futures = {
            executor.submit(_collect_one_suspicion, name, state, notes, char_map, context_discs): name
            for name in npc_names
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                scores = future.result()
            except Exception as e:
                print(f"[npc_agent] suspicion future failed for {name}: {e}", file=sys.stderr)
                scores = {}
            if scores:
                all_scores.append(scores)

    if not all_scores:
        return {}

    totals: dict[str, list[int]] = {}
    for scores in all_scores:
        for player, score in scores.items():
            totals.setdefault(player, []).append(score)

    return {player: sum(vals) / len(vals) for player, vals in totals.items()}


# ---------------------------------------------------------------------------
# 思考ログ保存
# ---------------------------------------------------------------------------

def save_thoughts(day: int, disc_num: int) -> None:
    """_npc_thoughts_day{N}_disc{M}.json に思考ログを保存する。"""
    global _thoughts_buffer
    filename = f"_npc_thoughts_day{day}_disc{disc_num}.json"
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(_thoughts_buffer, f, ensure_ascii=False, indent=2)
    print(f"[npc_agent] 思考ログを保存しました: {filename}", file=sys.stderr)
    _thoughts_buffer = {}
