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

from llm_backend import join_prompt
import engine

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
DEBUG_LOG_FILE = LOG_DIR / "debug_view.log"

MAX_ATTEMPTS = 3          # 生成リトライ上限
MAX_CONTEXT_CHARS = 4000  # 会話ログをプロンプトに入れる最大文字数

_SPEECH_LINE = re.compile(r"^([^\s「」：:]+)[：:]?\s*「(.*)」\s*$")


def _parse_speech_line(line: str) -> tuple[str, str] | None:
    m = _SPEECH_LINE.match(line.strip())
    if not m:
        return None
    text = m.group(2)
    if text.endswith("」"):
        text = text[:-1]
    return m.group(1), text


def player_spoke_last(conversation: str, player_name: str) -> bool:
    """議論の最終行がプレイヤー発言か。"""
    lines = [ln.strip() for ln in conversation.splitlines() if ln.strip()]
    if not lines:
        return False
    parsed = _parse_speech_line(lines[-1])
    return parsed is not None and parsed[0] == player_name


def last_player_speech(conversation: str, player_name: str) -> str | None:
    """プレイヤーの最新発言本文。なければ None。"""
    for line in reversed(conversation.splitlines()):
        line = line.strip()
        if not line:
            continue
        parsed = _parse_speech_line(line)
        if parsed and parsed[0] == player_name:
            return parsed[1]
    return None


def player_speech_distance(conversation: str, player_name: str) -> int | None:
    """プレイヤーの最新発言から何発言経過したか（0 = 直前がプレイヤー）。

    発言（名前「…」形式）のみを数え、ナレーション行は無視する。
    プレイヤーが未発言なら None。
    """
    speeches = []
    for line in conversation.splitlines():
        parsed = _parse_speech_line(line.strip())
        if parsed:
            speeches.append(parsed[0])
    for i, name in enumerate(reversed(speeches)):
        if name == player_name:
            return i
    return None


def last_speech_line(conversation: str) -> str | None:
    """会話の最後の発言行（名前「…」形式の生テキスト）。なければ None。"""
    for line in reversed(conversation.splitlines()):
        line = line.strip()
        if line and _parse_speech_line(line):
            return line
    return None


def _trim_conversation(conversation: str, player_name: str) -> str:
    convo = conversation.strip()
    if len(convo) <= MAX_CONTEXT_CHARS:
        return convo
    tail = convo[-MAX_CONTEXT_CHARS:]
    last_pl = last_player_speech(convo, player_name)
    if last_pl and last_pl not in tail:
        anchor = f"{player_name}「{last_pl}」"
        combined = anchor + "\n\n" + tail
        tail = combined[-MAX_CONTEXT_CHARS:]
    return "…（前略）…\n" + tail


def _is_hiragana(ch: str) -> bool:
    return "\u3040" <= ch <= "\u309f"


def _addresses_player_message(msg: str, player_line: str, player_name: str) -> bool:
    """プレイヤー発言への最低限の応答があるか（名前または内容語の一致）。

    機能語（「です」「ます」等）の偶然一致で応答扱いになるのを防ぐため、
    共通部分文字列には内容語を要求する:
    - 非ひらがな文字（漢字・カタカナ・英数字）を1文字以上含む場合は長さ3以上
    - ひらがなのみの場合は長さ6以上
    """
    if player_name in msg:
        return True
    for n in range(min(len(player_line), 12), 2, -1):
        for i in range(len(player_line) - n + 1):
            chunk = player_line[i:i + n]
            if not chunk.strip() or chunk not in msg:
                continue
            if any(not _is_hiragana(ch) for ch in chunk):
                return True
            if n >= 6:
                return True
    return False

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


_COMP_ROLE_JP = {
    "werewolf": "人狼", "madman": "狂人", "seer": "占い師",
    "medium": "霊媒師", "bodyguard": "狩人", "villager": "村人",
}
_COMP_ORDER = ["werewolf", "madman", "seer", "medium", "bodyguard", "villager"]


def _format_view(view: dict) -> str:
    """get_player_view の出力を読みやすい盤面テキストに整形する。"""
    parts = [f"今日は {view['day']} 日目。"]
    if view["day"] == 1:
        parts.append(
            "【初日ルール】夜の占いはまだ一度も行われていない。"
            "占い師は占い結果を持たない（全員が知る公開ルール）。"
        )

    comp = view.get("role_composition", {})
    if comp:
        comp_str = "・".join(
            f"{_COMP_ROLE_JP[r]}{comp[r]}" for r in _COMP_ORDER if comp.get(r)
        )
        parts.append(
            f"役職構成（全員が知る公開情報）: {comp_str}（計{sum(comp.values())}人）"
        )

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
        votes = e.get("votes", {})
        if votes:
            breakdown = engine.format_vote_breakdown(votes)
            parts.append(
                f"Day{e['day']} 投票: {breakdown} → {e['target']}を処刑")
        else:
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
                     strategy_hint: str = "", conversation: str = "",
                     player_name: str = "",
                     respond_to_player: bool | None = None) -> list[dict]:
    """プロンプトをセグメントlistで返す（llm_backend の共通IF形式）。

    プロンプトキャッシュのため「変わらないもの → 変わるもの」の順に並べ、
    安定部分に cache=True を付ける:
      seg1 (cache): 人物・キャラ設定・遵守事項 … ゲーム中ほぼ不変
      seg2 (cache): 盤面 … フェーズ内は不変（CO記録・死亡で変化）
      seg3        : 戦略指示・プレイヤー応答指示・会話ログ … 毎ターン変化
    """
    role_jp = view["self"]["role_jp"]
    dead_names = [d["name"] for d in view["dead_players"]]

    if player_name:
        convo = _trim_conversation(conversation, player_name)
    else:
        convo = conversation.strip()
        if len(convo) > MAX_CONTEXT_CHARS:
            convo = "…（前略）…\n" + convo[-MAX_CONTEXT_CHARS:]

    rules = [
        "全員が人狼ゲーム経験者。用語解説やセオリーの長文説明はしない。",
        "直前の議論の流れ・他の生存者の発言に具体的に反応し、議論を停滞させない。"
        "全員が同じ相手ばかり追及するのは避け、盤面全体に目を配ること。",
        "疑いを口にするときは、根拠を相手の発言内容・投票行動・役職内訳のいずれかに"
        "紐付けること。態度や口調・心情の憶測だけを根拠にしない。",
        "投票について話すときは盤面の「DayN 投票: A→B、…」の個票記録だけを真実とする。"
        "誰が誰に入れたかを得票数から推測したり捏造したりするな（各人1票）。",
        f"死亡者（{', '.join(dead_names) or 'なし'}）を生存中の相手として扱わない"
        "（呼びかけ・質問・投票・現在の容疑者にしない）。"
        "死亡者が残した情報（CO・占い結果・生前の発言）に言及して推理するのはよい。",
        "誰がCOしたかは盤面の「公開CO」欄だけを真実とする。そこに無いCOを事実として語らない"
        "（会話中の他者の要約が間違っていることもある）。",
        "自分の真の役職は、CO指示がない限り明かさない（村人陣営を装う/グレーとして振る舞う）。",
        "発言は1〜3文に凝縮する。キャラクターの口調を厳守する。",
        '出力は次のJSONオブジェクトのみ: {"thought": "内心の戦略・分析（日本語）", "message": "セリフ本文（かぎ括弧は含めない）"}',
    ]
    if view["day"] == 1:
        rules.insert(1,
            "初日は夜の占いが未実施。占い師CO者に占い結果・占った相手・白黒の"
            "発表や提示を求めたり『結果は？』と詰めたりしない（全員が知るルール）。"
            "対抗COの真偽・発言の筋・投票の動きで議論せよ。"
        )
    stable = "\n\n".join([
        f"あなたは人狼ゲーム参加者の「{npc_name}」。真の役職は【{role_jp}】。",
        "## あなたのキャラクター設定\n" + _format_character(char_data),
        "## 遵守事項\n" + "\n".join(f"{i+1}. {r}" for i, r in enumerate(rules)),
    ])

    board = "## 現在の盤面\n" + _format_view(view)

    dynamic_sections = []
    if strategy_hint:
        dynamic_sections.append(
            "## 今回のあなたの戦略指示（誰にも明かさないこと）\n" + strategy_hint)

    if respond_to_player is None:
        respond_to_player = bool(
            player_name and player_spoke_last(conversation, player_name))
    player_line = last_player_speech(conversation, player_name) if player_name else None
    if respond_to_player and player_line:
        dynamic_sections.append(
            "## 【最優先】プレイヤー（人間）の発言に応答せよ\n"
            f"プレイヤー「{player_name}」が返答を待っている:\n"
            f"「{player_line}」\n"
            "- 上記の論点・質問・提案に**最初の1文で**答えるか受け止めること（スルー禁止）\n"
            "- その後で自分の疑いや戦略を述べてよい\n"
            "- 村の空気や他論点だけを繰り返してプレイヤーを無視するな"
        )

    if convo:
        dynamic_sections.append("## 今日のここまでの議論\n" + convo)

    segments = [
        {"text": stable, "cache": True},
        {"text": board, "cache": True},
    ]
    if dynamic_sections:
        segments.append({"text": "\n\n".join(dynamic_sections), "cache": False})
    return segments


# ---------------------------------------------------------------------------
# 発言生成（1ターン）
# ---------------------------------------------------------------------------

# 死者名の後に挟んでよい敬称
_HONORIFIC = r"(?:さん|君|様|ちゃん)?"


def dead_as_alive_check(msg: str, dead_names: list[str]) -> str | None:
    """死亡済みの人物を「生存中の相手」として扱う発言を検出する。

    死者への言及そのもの（「ジムゾンの出した黒」「昨夜パメラが噛まれた」）は
    人狼の議論の本体なので許容する。禁止するのは呼びかけ・質問・投票・
    現在形の疑い対象化など、死者がまだ生きているかのような扱いのみ。
    問題があれば理由文字列を、なければ None を返す。
    """
    for dname in dead_names:
        n = re.escape(dname) + _HONORIFIC
        patterns = [
            # 呼びかけ（「ジムゾンさん、どう思う？」）
            rf"{n}[、,]",
            # 質問・発言要求
            rf"{n}(?:はどう思|に聞きたい|に質問|、?答えて|の意見を聞)",
            # 処刑・投票対象化
            rf"{n}(?:に投票|を吊|に一票|を処刑)",
            # 現在形の疑い対象化（過去形「怪しかった」「疑っていた」は許容。
            # 「怪しかった」は「怪しい」を含まないため自然に除外される）
            rf"{n}(?:が|は)(?:今)?(?:一番)?怪しい",
            rf"{n}を疑(?!ってい)",
        ]
        for pat in patterns:
            if re.search(pat, msg):
                return (
                    f"dead player treated as alive: 死亡済みの{dname}を"
                    "生存中の相手として扱っている（呼びかけ・質問・投票・"
                    "現在の容疑者化は不可。死者が残した情報への言及は可）"
                )
    return None


def _validate_message(msg: str, dead_names: list[str]) -> str | None:
    """問題があれば理由文字列を、なければ None を返す。"""
    if not msg or not msg.strip():
        return "empty message"
    problem = dead_as_alive_check(msg, dead_names)
    if problem:
        return problem
    if "「" in msg or "」" in msg:
        return "quote brackets inside message"
    return None


_CO_ROLE = r"(?:占い師|霊媒師|狩人)"
_ROLE_JP2EN = {"占い師": "seer", "霊媒師": "medium", "狩人": "bodyguard"}
_ROLE_EN2JP = {v: k for k, v in _ROLE_JP2EN.items()}

# 名詞形「XのCO」の直後がこれらなら「COの要求・待望・仮定」であり事実主張ではない
_CO_NOUN_EXEMPT = re.compile(
    r"^(?:を待|を求|を促|を要求|に期待|があれば|が出|が欲し|してほし|するなら|なら|を見てから)"
)


def check_co_misattribution(msg: str, npc_name: str, view: dict) -> str | None:
    """発言中のCO言及を公開CO台帳（public_co_claims）と突き合わせる。

    LLMの誤読（例: COしていない人物への「Xの占い師CO」発言）は
    プロンプトでは防げないため、生成後に機械検証して再生成させる。
    検出するのは客観的事実として偽と判定できる主張のみ:
      - COしていない人物へのCO帰属（完了形・名詞形・騙り指摘）
    仮定（〜なら）・要求（COを待つ/求める）・自分自身のCOは対象外。

    問題があれば正解付きの理由文字列を、なければ None を返す。
    """
    claims = view.get("public_co_claims", {})
    names = list(view.get("alive_players", [])) + \
        [d["name"] for d in view.get("dead_players", [])]

    def co_summary() -> str:
        if not claims:
            return "現時点で公開COは1件もない"
        return "公開COは " + ", ".join(
            f"{n}（{_ROLE_EN2JP.get(i.get('role'), i.get('role'))}）"
            for n, i in claims.items() if isinstance(i, dict)
        ) + " のみ"

    for name in names:
        if name == npc_name:
            continue  # 自分のCOはこの発言自体で成立するため対象外
        esc = re.escape(name)
        suffix = r"(?:さん|君|様|ちゃん)?"

        hits: list[str] = []
        # 完了形: 「Xが占い師COした/している」「XはCO済みだ」
        # ただし助言・要求・仮定（COした方がいい/COしてほしい/COしたら等）は除外
        m = re.search(
            esc + suffix + r"(?:が|は|も)(?:対抗)?" + _CO_ROLE +
            r"?(?:を|と)?CO(?:した(?!方|ら|とし)|して(?!ほし|くれ|もら|欲し|から)|し、|済|だ|です)",
            msg)
        if m:
            hits.append(m.group(0))
        # 名詞形: 「Xの占い師CO」「XのCO」（要求・待望・仮定の文脈は除外）
        m = re.search(
            esc + suffix + r"の(?:対抗)?" + _CO_ROLE + r"?CO", msg)
        if m and not _CO_NOUN_EXEMPT.match(msg[m.end():]):
            hits.append(m.group(0))
        # 騙り指摘: 「Xは占い師を騙っている」はXのCOが存在する前提
        m = re.search(
            esc + suffix + r"(?:が|は|も)(?:対抗)?" + _CO_ROLE +
            r"(?:を|だと)?騙", msg)
        if m:
            hits.append(m.group(0))

        if not hits:
            continue

        seg = hits[0]
        claimed_role = next(
            (en for jp, en in _ROLE_JP2EN.items() if jp in seg), None)
        actual = claims.get(name)
        if actual is None:
            return (
                f"事実誤認: {name} はCOしていない（{co_summary()}）。"
                "COの有無は盤面の公開CO欄だけを真実とし、"
                "会話中の他者の要約や自分の推測でCOを捏造するな"
            )
        if claimed_role and actual.get("role") != claimed_role:
            actual_jp = _ROLE_EN2JP.get(actual.get("role"), actual.get("role"))
            return (
                f"事実誤認: {name} の公開COは{actual_jp}であり"
                f"{_ROLE_EN2JP[claimed_role]}ではない（{co_summary()}）"
            )
    return None


def check_day1_seer_result_demand(msg: str, view: dict) -> str | None:
    """初日に占い師へ結果を求める発言を検出する（夜の占い未実施のため存在しない）。

    結果が無いことを述べる発言（占い師本人・ルール説明）は許容する。
    """
    if view.get("day", 0) != 1:
        return None
    if re.search(r"結果(?:は|が)(?:まだ)?(?:無|な)い|結果ゼロ|占い(?:は|が)まだ|未実施", msg):
        return None
    patterns = [
        r"占い結果を(?:聞|教|出|発表|提示|待)",
        r"結果を(?:聞|教|出|発表|提示|待ち|急か|確認)",
        r"(?:誰|誰を)占った",
        r"白か黒|黒か白",
        r"占った(?:のは|相手|結果)",
        r"占い結果[はが].{0,8}(?:聞|教|出|発表|待|確認)",
        r"(?:まず|先に).{0,12}占い結果",
    ]
    for pat in patterns:
        if re.search(pat, msg):
            return (
                "初日ルール違反: 夜の占いはまだ未実施のため占い結果は存在しない。"
                "結果の要求・催促はしない（対抗COの真偽や発言内容で議論せよ）"
            )
    return None


def _recent_speech_lines(conversation: str, limit: int = 3) -> str:
    """会話末尾の発言行を抽出（口調の連続性用）。"""
    lines = []
    for line in conversation.splitlines():
        if _parse_speech_line(line.strip()):
            lines.append(line.strip())
    return "\n".join(lines[-limit:]) if lines else ""


def _build_render_prompt(npc_name: str, char_data: dict, plan: dict,
                         conversation: str, player_name: str) -> list[dict]:
    """プラン内容を口調変換するための短いプロンプト（盤面全文は渡さない）。"""
    rules = [
        "下記の「今回あなたが言う内容」を、この順番で、あなたの口調の自然な1〜3文に変換する。",
        "新しい事実・名前・占い結果・投票先を追加しない。内容の削除もしない。",
        "語尾・一人称はキャラ設定に従う。",
        '出力はJSONのみ: {"message": "セリフ本文（かぎ括弧は含めない）"}',
    ]
    stable = "\n\n".join([
        f"あなたは人狼ゲーム参加者の「{npc_name}」。",
        "## あなたのキャラクター設定\n" + _format_character(char_data),
        "## 遵守事項\n" + "\n".join(f"{i+1}. {r}" for i, r in enumerate(rules)),
    ])

    dynamic_parts = []
    recent = _recent_speech_lines(conversation)
    if recent:
        dynamic_parts.append("## 直前の議論（口調の参考）\n" + recent)

    has_respond = any(a.get("type") == "respond_player" for a in plan.get("acts", []))
    if has_respond and player_name:
        pline = last_player_speech(conversation, player_name)
        if pline:
            dynamic_parts.append(
                "## プレイヤーへの応答（最初の1文で直接答えよ）\n"
                f"プレイヤー「{player_name}」: 「{pline}」"
            )

    content_lines = [
        f"{i+1}. {a['text_jp']}"
        for i, a in enumerate(plan.get("acts", []))
        if a.get("text_jp")
    ]
    if content_lines:
        dynamic_parts.append(
            "## 今回あなたが言う内容（変換元。これ以外を言うな）\n"
            + "\n".join(content_lines)
        )
    if plan.get("must_mention"):
        names = "、".join(plan["must_mention"])
        dynamic_parts.append(f"## 必須言及\n次の名前を必ずセリフに含める: {names}")

    segments = [{"text": stable, "cache": True}]
    if dynamic_parts:
        segments.append({"text": "\n\n".join(dynamic_parts), "cache": False})
    return segments


def render_speech(npc_name: str, view: dict, char_data: dict, plan: dict,
                  conversation: str = "", player_name: str = "") -> dict:
    """プランの内容をキャラ口調のセリフに変換する（LLMは翻訳器）。

    Returns: {name, thought, message, error}
        message は「名前「セリフ」」形式。失敗時は "" と error。
        フォールバックは orchestrator が plan["fallback_text"] で適用する。
    """
    if _backend is None:
        return {"name": npc_name, "thought": "", "message": "",
                "error": "backend not initialized"}

    dead_names = [d["name"] for d in view["dead_players"]]
    must = list(plan.get("must_mention") or [])
    thought = json.dumps(plan.get("acts", []), ensure_ascii=False)
    prompt = _build_render_prompt(
        npc_name, char_data, plan, conversation, player_name)

    last_reason = "unknown"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = _backend.complete(prompt, model=_npc_model, expect_json=True)
        except Exception as e:
            last_reason = f"llm error: {e}"
            _debug_log(f"RENDER {npc_name} attempt {attempt} LLM_ERROR", str(e))
            continue

        _debug_log(f"RENDER {npc_name} attempt {attempt} PROMPT", join_prompt(prompt))
        _debug_log(f"RENDER {npc_name} attempt {attempt} RAW", raw)

        try:
            data = parse_json_bulletproof(raw, npc_name)
        except NPCGenerationError as e:
            last_reason = str(e)
            continue

        msg = str(data.get("message", "")).strip()
        problem = _validate_message(msg, dead_names)
        if not problem and must:
            missing = [n for n in must if n not in msg]
            if missing:
                problem = f"must_mention missing: {', '.join(missing)}"
        if problem:
            last_reason = problem
            _debug_log(f"RENDER {npc_name} attempt {attempt} REJECTED", problem)
            continue

        return {
            "name": npc_name,
            "thought": thought,
            "message": f"{npc_name}「{msg}」",
            "error": None,
        }

    _debug_log(f"RENDER {npc_name} SKIPPED", last_reason)
    return {"name": npc_name, "thought": thought, "message": "",
            "error": f"skipped after {MAX_ATTEMPTS} attempts: {last_reason}"}


def generate_npc_message(npc_name: str, view: dict, char_data: dict,
                         strategy_hint: str = "", conversation: str = "",
                         player_name: str = "",
                         respond_to_player: bool | None = None) -> dict:
    """NPC1体の発言を生成する。

    respond_to_player:
        True  = プレイヤーの最新発言への応答を必須にする（プロンプト指示+検証）
        False = 応答を強制しない
        None  = 従来動作（プレイヤーが直前に発言していれば必須）

    Returns: {name, thought, message, error}
        message は「名前「セリフ」」形式。失敗時は "" と error 理由。
    """
    if _backend is None:
        return {"name": npc_name, "thought": "", "message": "",
                "error": "backend not initialized"}

    if respond_to_player is None:
        respond_to_player = bool(
            player_name and player_spoke_last(conversation, player_name))
    pending_player = bool(player_name and respond_to_player)
    player_line = last_player_speech(conversation, player_name) if pending_player else None

    prompt = build_npc_prompt(
        npc_name, view, char_data, strategy_hint, conversation, player_name,
        respond_to_player=pending_player,
    )
    dead_names = [d["name"] for d in view["dead_players"]]

    last_reason = "unknown"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            raw = _backend.complete(prompt, model=_npc_model, expect_json=True)
        except Exception as e:
            last_reason = f"llm error: {e}"
            _debug_log(f"NPC {npc_name} attempt {attempt} LLM_ERROR", str(e))
            continue

        _debug_log(f"NPC {npc_name} attempt {attempt} PROMPT", join_prompt(prompt))
        _debug_log(f"NPC {npc_name} attempt {attempt} RAW", raw)

        try:
            data = parse_json_bulletproof(raw, npc_name)
        except NPCGenerationError as e:
            last_reason = str(e)
            continue

        msg = str(data.get("message", "")).strip()
        problem = _validate_message(msg, dead_names)
        if not problem:
            problem = check_co_misattribution(msg, npc_name, view)
        if not problem:
            problem = check_day1_seer_result_demand(msg, view)
        if not problem and pending_player and player_line:
            if not _addresses_player_message(msg, player_line, player_name):
                if attempt >= MAX_ATTEMPTS:
                    # 応答チェックのみの失敗は最終試行では許容する
                    # （不完全な返答 > NPCの沈黙）
                    _debug_log(
                        f"NPC {npc_name} attempt {attempt} ACCEPT_ANYWAY",
                        f"player-address check failed but accepting: {msg[:60]}",
                    )
                else:
                    problem = (
                        f"player '{player_name}' awaits response but message ignored their point: "
                        f"{player_line[:40]}"
                    )
        if problem:
            last_reason = problem
            _debug_log(f"NPC {npc_name} attempt {attempt} REJECTED", problem)
            extra_hint = strategy_hint + f"\n【前回の出力は却下された。理由: {problem}。修正して出力し直せ】"
            if pending_player and player_line:
                extra_hint += (
                    f"\n【必須】プレイヤー「{player_name}」の"
                    f"「{player_line}」に必ず触れよ。"
                )
            prompt = build_npc_prompt(
                npc_name, view, char_data, extra_hint, conversation, player_name,
                respond_to_player=pending_player,
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
