#!/usr/bin/env python3
"""NPCエージェント v2: 全文脈プロンプト + 逐次ターン制 + 再生成リトライ。

旧版が捨てていた文脈（盤面・キャラ設定・会話ログ・役職戦略）を全て
プロンプトに復元した。生成は orchestrator が発言順に1体ずつ呼び、
各NPCは直前までの会話を見て発言する（逐次ターン制）。

防御は「置換」ではなく「再生成」:
- JSON崩壊・空メッセージ・死者名混入は最大 MAX_ATTEMPTS 回リトライ
- 超過したらそのNPCのターンをスキップ（error に理由を残す）

観測可能性:
- 全プロンプト/生レスポンスを logs/debug_view.log に記録
- 思考ログを logs/npc_thoughts_day{N}_disc{M}.json に保存
"""

from __future__ import annotations

import datetime
import json
import re
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
DEBUG_LOG_FILE = LOG_DIR / "debug_view.log"

MAX_ATTEMPTS = 3          # 生成リトライ上限
MAX_CONTEXT_CHARS = 4000  # 会話ログをプロンプトに入れる最大文字数

_debug_log_lock = threading.Lock()

# モジュール状態（init で注入）
_backend = None
_npc_model: str | None = None


class NPCGenerationError(Exception):
    pass


def init(backend, npc_model: str | None = None) -> None:
    """LLMバックエンドを注入する。"""
    global _backend, _npc_model
    _backend = backend
    _npc_model = npc_model


# ---------------------------------------------------------------------------
# ログ
# ---------------------------------------------------------------------------

def _debug_log(label: str, text: str) -> None:
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _debug_log_lock:
        with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n===== [{ts}] {label} =====\n{text}\n")


def write_debug_header(label: str) -> None:
    _debug_log(f"ACTION: {label}", "")


def save_thoughts(day: int, disc: int, thoughts: dict[str, str]) -> None:
    """思考ログを保存する（感想戦・デバッグ用）。"""
    LOG_DIR.mkdir(exist_ok=True)
    path = LOG_DIR / f"npc_thoughts_day{day}_disc{disc}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(thoughts, f, ensure_ascii=False, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# JSON抽出（多層防御）
# ---------------------------------------------------------------------------

def parse_json_bulletproof(raw: str, npc_name: str) -> dict:
    """LLM出力からJSONを抽出する。

    層1: コードフェンス除去 → 全体パース
    層2: 最初の { から最後の } までを抽出してパース
    層3: "message" フィールドだけを正規表現で救出
    """
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.MULTILINE).strip()

    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    m = re.search(r"(\{.*\})", cleaned, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass

    message_m = re.search(r'"message"\s*:\s*"((?:[^"\\]|\\.)*)"', cleaned, re.DOTALL)
    if message_m:
        return {"thought": "", "message": message_m.group(1).replace("\\n", "\n")}

    raise NPCGenerationError(f"JSON extraction failed for {npc_name}")


# ---------------------------------------------------------------------------
# プロンプト構築
# ---------------------------------------------------------------------------

def _format_character(char_data: dict) -> str:
    if not char_data:
        return ""
    ss = char_data.get("speech_style", {})
    lines = [
        f"性格: {char_data.get('personality', '')}",
        f"一人称: {ss.get('first_person', '')}",
        f"口調: {ss.get('tone', '')}",
        f"語尾・口癖: {ss.get('vocal_tics', '')}",
        f"思考タイプ: {char_data.get('intellect', '')}",
    ]
    return "\n".join(f"- {l}" for l in lines if l.split(": ", 1)[-1])


def _format_view(view: dict) -> str:
    """get_player_view の出力を読みやすい盤面テキストに整形する。"""
    parts = [f"今日は {view['day']} 日目。"]

    parts.append(f"生存者: {', '.join(view['alive_players'])}")
    if view["dead_players"]:
        dead = ", ".join(f"{d['name']}（{d['cause']}）" for d in view["dead_players"])
        parts.append(f"死亡者: {dead}")

    co = view.get("public_co_claims", {})
    if co:
        role_jp = {"seer": "占い師", "medium": "霊媒師", "bodyguard": "狩人"}
        cos = ", ".join(
            f"{n}（{role_jp.get(i.get('role'), i.get('role'))}CO・Day{i.get('day', '?')}）"
            for n, i in co.items() if isinstance(i, dict)
        )
        parts.append(f"公開CO: {cos}")

    seer_rs = view.get("public_seer_results", [])
    if seer_rs:
        rs = " / ".join(
            f"Day{r['day']}夜 {r['actor']}→{r['target']}: {r['result']}"
            for r in seer_rs
        )
        parts.append(f"公開済み占い結果: {rs}")

    med_rs = view.get("public_medium_results", [])
    if med_rs:
        rs = " / ".join(
            f"Day{r['day']}処刑 {r['target']}: {'人狼' if r['result'] == 'werewolf' else '人間'}"
            for r in med_rs
        )
        parts.append(f"公開済み霊媒結果: {rs}")

    for e in view.get("execution_history", []):
        tally = ", ".join(f"{n}:{c}票" for n, c in e.get("tally", {}).items())
        parts.append(f"Day{e['day']} 処刑: {e['target']}（{tally}）")

    private = view.get("private", {})
    if private.get("seer_results"):
        rs = " / ".join(
            f"Day{r['day']}夜 {r['target']}: {r['result']}"
            for r in private["seer_results"]
        )
        parts.append(f"【あなただけが知る占い結果】{rs}")
    if private.get("medium_results"):
        rs = " / ".join(
            f"Day{r['day']}処刑 {r['target']}: {r['result']}"
            for r in private["medium_results"]
        )
        parts.append(f"【あなただけが知る霊媒結果】{rs}")
    if private.get("guard_history"):
        rs = " / ".join(
            f"Day{r['day']}夜 {r['target']}" for r in private["guard_history"]
        )
        parts.append(f"【あなただけが知る護衛履歴】{rs}")

    if view.get("wolf_teammates"):
        parts.append(f"【仲間の人狼】{', '.join(view['wolf_teammates'])}")

    return "\n".join(parts)


def build_npc_prompt(npc_name: str, view: dict, char_data: dict,
                     strategy_hint: str = "", conversation: str = "") -> str:
    role_jp = view["self"]["role_jp"]
    dead_names = [d["name"] for d in view["dead_players"]]

    convo = conversation.strip()
    if len(convo) > MAX_CONTEXT_CHARS:
        convo = "…（前略）…\n" + convo[-MAX_CONTEXT_CHARS:]

    sections = [
        f"あなたは人狼ゲーム参加者の「{npc_name}」。真の役職は【{role_jp}】。",
        "## あなたのキャラクター設定\n" + _format_character(char_data),
        "## 現在の盤面\n" + _format_view(view),
    ]
    if strategy_hint:
        sections.append("## 今回のあなたの戦略指示（誰にも明かさないこと）\n" + strategy_hint)
    if convo:
        sections.append("## 今日のここまでの議論\n" + convo)

    rules = [
        "全員が人狼ゲーム経験者。用語解説やセオリーの長文説明はしない。",
        "直前の議論の流れ・他の生存者の発言に具体的に反応し、議論を停滞させない。",
        f"死亡者（{', '.join(dead_names) or 'なし'}）の名前は発言に一切出さない。",
        "自分の真の役職は、CO指示がない限り明かさない（村人陣営を装う/グレーとして振る舞う）。",
        "発言は1〜3文に凝縮する。キャラクターの口調を厳守する。",
        '出力は次のJSONオブジェクトのみ: {"thought": "内心の戦略・分析（日本語）", "message": "セリフ本文（かぎ括弧は含めない）"}',
    ]
    sections.append("## 遵守事項\n" + "\n".join(f"{i+1}. {r}" for i, r in enumerate(rules)))
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# 発言生成（1ターン）
# ---------------------------------------------------------------------------

def _validate_message(msg: str, dead_names: list[str]) -> str | None:
    """問題があれば理由文字列を、なければ None を返す。"""
    if not msg or not msg.strip():
        return "empty message"
    for dname in dead_names:
        if dname in msg:
            return f"dead name mentioned: {dname}"
    if "「" in msg or "」" in msg:
        return "quote brackets inside message"
    return None


def generate_npc_message(npc_name: str, view: dict, char_data: dict,
                         strategy_hint: str = "", conversation: str = "") -> dict:
    """NPC1体の発言を生成する。

    Returns: {name, thought, message, error}
        message は「名前「セリフ」」形式。失敗時は "" と error 理由。
    """
    if _backend is None:
        return {"name": npc_name, "thought": "", "message": "",
                "error": "backend not initialized"}

    prompt = build_npc_prompt(npc_name, view, char_data, strategy_hint, conversation)
    dead_names = [d["name"] for d in view["dead_players"]]

    last_reason = "unknown"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = _backend.complete(prompt, model=_npc_model, expect_json=True)
        except Exception as e:
            last_reason = f"llm error: {e}"
            _debug_log(f"NPC {npc_name} attempt {attempt} LLM_ERROR", str(e))
            continue

        _debug_log(f"NPC {npc_name} attempt {attempt} PROMPT", prompt)
        _debug_log(f"NPC {npc_name} attempt {attempt} RAW", raw)

        try:
            data = parse_json_bulletproof(raw, npc_name)
        except NPCGenerationError as e:
            last_reason = str(e)
            continue

        msg = str(data.get("message", "")).strip()
        problem = _validate_message(msg, dead_names)
        if problem:
            last_reason = problem
            _debug_log(f"NPC {npc_name} attempt {attempt} REJECTED", problem)
            # リトライ時に問題点をプロンプトへ明示（再生成の質を上げる）
            prompt = build_npc_prompt(
                npc_name, view, char_data,
                strategy_hint
                + f"\n【前回の出力は却下された。理由: {problem}。修正して出力し直せ】",
                conversation,
            )
            continue

        return {
            "name": npc_name,
            "thought": str(data.get("thought", "")),
            "message": f"{npc_name}「{msg}」",
            "error": None,
        }

    _debug_log(f"NPC {npc_name} SKIPPED", last_reason)
    return {"name": npc_name, "thought": "", "message": "",
            "error": f"skipped after {MAX_ATTEMPTS} attempts: {last_reason}"}


# ---------------------------------------------------------------------------
# 議論ラウンド（逐次ターン制）
# ---------------------------------------------------------------------------

def run_discussion_round(npc_names: list[str], get_view, char_map: dict,
                         strategy_hints: dict[str, str],
                         conversation_so_far: str,
                         on_progress=None) -> list[dict]:
    """NPC発言を発言順に逐次生成する。各NPCは直前までの発言を見る。

    Args:
        npc_names: 発言順のNPC名リスト
        get_view: name -> view を返す callable
        char_map: name -> characters.json エントリ
        strategy_hints: name -> 戦略指示文字列
        conversation_so_far: 本日ここまでの会話テキスト
        on_progress: 各NPC開始時に呼ばれる callable(name)（typing表示用）

    Returns: generate_npc_message の結果リスト（発言順）
    """
    results = []
    convo = conversation_so_far
    for name in npc_names:
        if on_progress:
            on_progress(name)
        res = generate_npc_message(
            name, get_view(name), char_map.get(name, {}),
            strategy_hints.get(name, ""), convo,
        )
        results.append(res)
        if res["message"]:
            convo = (convo + "\n\n" + res["message"]).strip()
    return results


# ---------------------------------------------------------------------------
# 疑惑スコア収集
# ---------------------------------------------------------------------------

def collect_suspicion(npc_names: list[str], get_view,
                      conversation: str = "") -> dict:
    """各NPCに「怪しい順スコア(1-10)」を出させる。

    狼は仲間（狼）を最低スコアに補正する（身内票の偽装）。

    Returns:
        {"avg": {target: 平均スコア}, "by_rater": {rater: {target: score}}}
    """
    if _backend is None:
        return {"avg": {}, "by_rater": {}}

    by_rater: dict[str, dict[str, int]] = {}
    for name in npc_names:
        view = get_view(name)
        alive_others = [n for n in view["alive_players"] if n != name]
        if not alive_others:
            continue

        convo = conversation.strip()
        if len(convo) > MAX_CONTEXT_CHARS:
            convo = convo[-MAX_CONTEXT_CHARS:]

        prompt = (
            f"あなたは人狼ゲーム参加者「{name}」（{view['self']['role_jp']}）。\n"
            f"盤面:\n{_format_view(view)}\n\n"
            + (f"今日の議論:\n{convo}\n\n" if convo else "")
            + f"生存者 {', '.join(alive_others)} について、人狼として怪しい順に"
            "1〜10のスコアを付けよ（10=最も怪しい）。\n"
            '出力はJSONのみ: {"名前": スコア, ...}'
        )
        try:
            raw = _backend.complete(prompt, model=_npc_model, expect_json=True)
            scores = parse_json_bulletproof(raw, name)
        except Exception as e:
            _debug_log(f"SUSPICION {name} ERROR", str(e))
            continue

        role = view["self"]["role"]
        teammates = view.get("wolf_teammates", [])
        rated: dict[str, int] = {}
        for target, v in scores.items():
            if target not in alive_others:
                continue
            try:
                iv = max(1, min(10, int(v)))
            except (TypeError, ValueError):
                continue
            if role == "werewolf" and target in teammates:
                iv = 1
            rated[target] = iv
        if rated:
            by_rater[name] = rated

    totals: dict[str, list[int]] = {}
    for rated in by_rater.values():
        for target, iv in rated.items():
            totals.setdefault(target, []).append(iv)
    avg = {t: round(sum(vs) / len(vs), 2) for t, vs in totals.items()}
    return {"avg": avg, "by_rater": by_rater}
