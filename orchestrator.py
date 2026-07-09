#!/usr/bin/env python3
"""ゲーム進行オーケストレーター。

viewer/server.py（HTTP API）から呼ばれ、engine（ルール）と
npc_agent（LLM発言生成）を束ねて1ゲームを進行させる。

シーンはファイル（scene_*.txt）に追記・生成され、ビューアが表示する。
進行状態は game_state.json の day/phase と、シーンファイルの有無から復元
できるため、orchestrator 自体は永続状態を持たない。

秘匿設計:
- discussion_brief() の内容（counter_co 等）はNPC戦略指示への変換のみに使い、
  API応答・シーンテキストへ直接出さない
- APIへ返す情報は viewer/server.py の視点フィルタを通る
"""

from __future__ import annotations

import datetime
import json
import random
import re
from pathlib import Path

import engine
import npc_agent
import speech_planner
import validator
import scene_checks
from llm_backend import create_backend, load_config, model_for, LLMError

BASE_DIR = Path(__file__).resolve().parent
TYPING_FILE = BASE_DIR / ".typing_now"

ROLE_JP = engine.ROLE_JP

# CO宣言の固定テンプレ。NPC・プレイヤーとも宣言はこの定型文で行い、
# 台帳（public_co_claims）へは直接記録する。正規表現検出（detect_role_co）は
# プレイヤーが自由文でCOした場合のフォールバックに格下げ。
CO_TEMPLATE = "COします、私は{role_jp}です。"
CO_ROLES = ("seer", "medium", "bodyguard")

# 結果発表もCO同様にテンプレ化する。発表文は機械挿入し台帳へ直接記録する
# （台帳 public_seer_claims / public_medium_results が全員の共通認識になる）。
SEER_RESULT_TEMPLATE = "占い結果を発表します。{target}は【{result}】でした。"
MEDIUM_RESULT_TEMPLATE = "霊媒結果を発表します。処刑された{target}は【{result}】でした。"

_MAX_SCENE_RETRIES = 3
MAX_DISC_ROUNDS_PER_DAY = 3

# プレイヤー応答の強制確率（キー: プレイヤー最新発言からの経過発言数）
# 直後は高確率で応答させ、離れるほど自然に会話が流れるようにする。
_RESPOND_PROB = {0: 0.9, 1: 0.5, 2: 0.25, 3: 0.1}
_RESPOND_PROB_FLOOR = 0.05

# 直前の発言で名前を呼ばれたNPCが次に話す重み倍率（100%にはしない）
_MENTION_BOOST = 3.0

_FIRST_PERSON = r"(?:私|わたし|あたし|俺|おら|僕|わたくし|わし|自分)"


def respond_probability(distance: int | None) -> float:
    """プレイヤー発言からの距離に応じた応答強制確率。未発言なら0。"""
    if distance is None:
        return 0.0
    return _RESPOND_PROB.get(distance, _RESPOND_PROB_FLOOR)


def speaker_weights(queue: list[str], last_line: str | None) -> list[float]:
    """発言待ちNPCの選出重み。直前の発言に名前が出た者をブーストする。

    重み付き抽選なのでブースト対象が確実に選ばれることはない（仕様）。
    """
    weights = []
    for name in queue:
        w = 1.0
        if last_line and name in last_line:
            w = _MENTION_BOOST
        weights.append(w)
    return weights


# CO宣言の断定語尾。「〜なら」「〜を信じる」等の非断定は含めない。
# だ は だべ/だよ/だね を、です は ですわ/ですね を前方一致で拾う。
_CO_TAIL = r"(?:です|だ|じゃ|よ(?![りうそ])|なの|CO|をCO)"


def detect_role_co(dialogue: str, role: str) -> bool:
    """シーン1行分のセリフから役職CO宣言を検出する（他者言及の誤検出を避ける）。"""
    if role == "seer":
        if re.search(r"占い師(?:が|は).{0,10}(?:二人|2人|両|どちら|嘘|対抗)", dialogue):
            if not re.search(_FIRST_PERSON + r"[がはも、,].{0,8}占い師", dialogue):
                return False
        patterns = [
            _FIRST_PERSON + r"[がはも](?:ここ)?(?:の)?(?:実は)?占い師" + _CO_TAIL,
            _FIRST_PERSON + r"[、,]\s*占い師" + _CO_TAIL,
            r"(?:^|[。！？\s])占い師(?:です|だ|をCO|COします|COする)(?:[。！？\s]|$)",
        ]
        return any(re.search(p, dialogue) for p in patterns)
    if role == "medium":
        if re.search(r"霊媒師(?:が|は).{0,10}(?:二人|2人|両|どちら|嘘|対抗)", dialogue):
            if not re.search(_FIRST_PERSON + r"[がはも、,].{0,8}霊媒師", dialogue):
                return False
        patterns = [
            _FIRST_PERSON + r"[がはも](?:ここ)?(?:の)?(?:実は)?霊媒師" + _CO_TAIL,
            _FIRST_PERSON + r"[、,]\s*霊媒師" + _CO_TAIL,
        ]
        return any(re.search(p, dialogue) for p in patterns)
    if role == "bodyguard":
        patterns = [
            _FIRST_PERSON + r"[がはも](?:ここ)?(?:の)?(?:実は)?狩人" + _CO_TAIL,
            _FIRST_PERSON + r"[、,]\s*狩人" + _CO_TAIL,
        ]
        return any(re.search(p, dialogue) for p in patterns)
    return False


def game_timeline(state: dict) -> str:
    """state['log'] から実際に起きた出来事の時系列テキストを作る。

    感想戦の事実台帳。これを渡さないとLLMが架空の占い・処刑・日数を
    捏造した振り返りを生成する。
    """
    lines: list[str] = []
    for e in state["log"]:
        day, typ = e.get("day"), e.get("type")
        if typ == "seer":
            res = "人狼" if e.get("result") == "werewolf" else "人間"
            lines.append(
                f"- {day}日目の夜: {e['actor']}が{e['target']}を占い、結果は【{res}】")
        elif typ == "guard":
            lines.append(f"- {day}日目の夜: {e['actor']}が{e['target']}を護衛")
        elif typ == "attack":
            if e.get("result") == "guarded":
                lines.append(f"- {day}日目の夜: 人狼が{e['target']}を襲撃したが護衛に阻まれた")
            else:
                lines.append(f"- {day}日目の夜: 人狼の襲撃で{e['target']}が死亡")
        elif typ == "execute":
            votes = e.get("votes", {})
            if votes:
                breakdown = engine.format_vote_breakdown(votes)
                lines.append(f"- {day}日目の昼: 投票（{breakdown}）で{e['target']}を処刑")
            else:
                tally = ", ".join(f"{n}{c}票" for n, c in e.get("tally", {}).items())
                lines.append(f"- {day}日目の昼: 投票（{tally}）で{e['target']}を処刑")
    return "\n".join(lines) if lines else "- 記録された出来事なし"


def format_vote_tally(votes: dict[str, str], executed: str, *,
                      runoff: bool = False) -> str:
    """投票結果をシーン冒頭用の固定フォーマットで返す（個票列挙）。"""
    return engine.format_execution_votes(votes, executed, runoff=runoff)


class OrchestratorError(Exception):
    pass


def _set_typing(npc: str | None, scene: str | None = None) -> None:
    with open(TYPING_FILE, "w", encoding="utf-8") as f:
        json.dump({"npc": npc, "scene": scene}, f, ensure_ascii=False)


def _clear_typing() -> None:
    _set_typing(None, None)


class Orchestrator:
    def __init__(self, backend=None, config: dict | None = None):
        self.config = config or load_config()
        self.backend = backend or create_backend(self.config)
        self.npc_model = model_for(self.config, "npc")
        self.narration_model = model_for(self.config, "narration")
        npc_agent.init(self.backend, self.npc_model)

    # ------------------------------------------------------------------
    # 共通ヘルパー
    # ------------------------------------------------------------------

    def _narrate(self, prompt: str, *, validate_as: str | None = None) -> str:
        """ナレーション生成。validator を通し、失敗したらリトライする。"""
        system = (
            "あなたは中世の村を舞台にした人狼ゲームの語り部。"
            "日本語で重厚かつ簡潔に描写する。"
            "キャラクターに発言させる場合は必ず行頭から『名前「セリフ」』形式。"
            "誰の役職も明かさない。存在しない人物を登場させない。"
        )
        state = engine.load_state()
        last_errors: list[str] = []
        for attempt in range(1, _MAX_SCENE_RETRIES + 1):
            extra = ""
            if last_errors:
                extra = (
                    "\n\n【前回の出力は以下の問題で却下された。修正して書き直せ】\n"
                    + "\n".join(f"- {e}" for e in last_errors)
                )
            text = self.backend.complete(
                prompt + extra, system=system, model=self.narration_model,
            ).strip()
            if not validate_as:
                return text
            errors = validator.validate(
                state, text,
                is_epilogue=(validate_as in ("epilogue", "epilogue_thread")),
                executed_name=self._last_executed() if validate_as == "execution" else None,
            )
            if not errors:
                return text
            last_errors = errors
            npc_agent._debug_log(f"NARRATE retry {attempt}", "\n".join(errors))
        raise OrchestratorError(
            f"ナレーション生成が{_MAX_SCENE_RETRIES}回失敗: {last_errors}"
        )

    def _last_executed(self) -> str | None:
        state = engine.load_state()
        for e in reversed(state["log"]):
            if e["type"] == "execute":
                return e["target"]
        return None

    @staticmethod
    def _scene_path(name: str) -> Path:
        return BASE_DIR / name

    def _write_scene(self, name: str, text: str) -> str:
        path = self._scene_path(name)
        path.write_text(text.rstrip() + "\n", encoding="utf-8")
        return name

    def _append_scene(self, name: str, text: str) -> str:
        path = self._scene_path(name)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        joined = (existing.rstrip() + "\n\n" if existing.strip() else "") + text.strip() + "\n"
        path.write_text(joined, encoding="utf-8")
        return name

    def _conversation_today(self, day: int) -> str:
        """当日の議論シーン全文を発言文脈として結合する。"""
        parts = []
        for p in sorted(BASE_DIR.glob(f"scene_day{day}_disc*.txt")):
            parts.append(p.read_text(encoding="utf-8").strip())
        return "\n\n".join(parts)

    def _current_disc_index(self, day: int) -> int:
        nums = [
            int(m.group(1))
            for p in BASE_DIR.glob(f"scene_day{day}_disc*.txt")
            if (m := re.search(r"disc(\d+)", p.name))
        ]
        return max(nums) if nums else 0

    def _npc_names(self, state: dict, player: str) -> list[str]:
        return [p["name"] for p in state["players"]
                if p["alive"] and p["name"] != player]

    def _char_map(self) -> dict:
        return {c["name"]: c for c in engine.load_characters()}

    def _get_view(self, name: str):
        state = engine.load_state()
        notes = engine.load_notes()
        return engine.get_player_view(state, name, notes)

    # ------------------------------------------------------------------
    # 新規ゲーム
    # ------------------------------------------------------------------

    def new_game(self, player_choice: str | None = None) -> dict:
        info = engine.setup_game(player_choice)
        # logs はゲームごとにリセットしない（追記継続）。scene は setup が削除済み。
        _clear_typing()
        return info

    # ------------------------------------------------------------------
    # 朝シーン
    # ------------------------------------------------------------------

    def morning_scene(self) -> dict:
        """朝の描写を生成する。夜明け直後（day_discussion 開始時）に呼ぶ。"""
        state = engine.load_state()
        day = state["day"]
        scene_name = f"scene_day{day}_morning.txt"
        if self._scene_path(scene_name).exists():
            return {"scene": scene_name, "already": True}

        victim = None
        for e in reversed(state["log"]):
            if e["type"] == "attack" and e["day"] == day - 1:
                if e.get("result") == "killed":
                    victim = e["target"]
                break

        alive_names = [p["name"] for p in engine.alive_players(state)]
        if day == 1:
            prompt = (
                "人狼ゲームの開幕。中世の村に「この中に人狼が潜んでいる」という"
                "疑心が広がった最初の朝を描写せよ。犠牲者はまだいない。"
                f"村人たち: {', '.join(alive_names)}。5行以内。"
            )
        elif victim:
            prompt = (
                f"Day{day} の朝。昨夜の犠牲者は【{victim}】。"
                "遺体発見と村の動揺を描写せよ。犠牲者の役職には触れない。"
                f"生存者: {', '.join(alive_names)}。5行以内。"
            )
        else:
            prompt = (
                f"Day{day} の朝。昨晩の犠牲者はなし。"
                "安堵と不気味さの入り混じる村の朝を描写せよ。"
                "護衛の成否・襲撃の詳細には一切触れない。"
                f"生存者: {', '.join(alive_names)}。5行以内。"
            )

        _set_typing(None, scene_name)
        try:
            text = self._narrate(prompt, validate_as="scene")
        finally:
            _clear_typing()
        self._write_scene(scene_name, text)
        return {"scene": scene_name, "victim": victim, "day": day}

    # ------------------------------------------------------------------
    # 議論
    # ------------------------------------------------------------------

    def _clear_disc_session(self, notes: dict | None = None) -> None:
        notes = notes if notes is not None else engine.load_notes()
        for key in ("discussion_queue", "active_disc_scene", "discussion_day",
                    "spoken_topics", "last_accuse", "vote_intent_stated"):
            notes.pop(key, None)
        engine.save_notes(notes)

    def _ensure_disc_session(self, state: dict, notes: dict, player: str,
                             *, force_new: bool = False) -> tuple[str, list[str]]:
        """議論セッション（シーン名 + NPC待ち行列）を返す。必要なら新規作成。"""
        day = state["day"]
        scene = notes.get("active_disc_scene")
        queue = list(notes.get("discussion_queue") or [])

        if (not force_new and scene and notes.get("discussion_day") == day
                and (queue or self._scene_path(scene).exists())):
            if notes.get("co_sync_day") != day:
                conv = self._conversation_today(day)
                self._record_public_events(conv, state, engine.load_notes())
                notes = engine.load_notes()
                notes["co_sync_day"] = day
                engine.save_notes(notes)
            return scene, queue

        disc_idx = self._current_disc_index(day) + 1
        if disc_idx > MAX_DISC_ROUNDS_PER_DAY:
            raise OrchestratorError(
                f"本日の議論は{MAX_DISC_ROUNDS_PER_DAY}回までです。"
                "投票へ進んでください。"
            )

        if notes.get("discussion_day") != day:
            for key in ("spoken_topics", "last_accuse", "vote_intent_stated"):
                notes.pop(key, None)

        scene_name = f"scene_day{day}_disc{disc_idx}.txt"
        queue = self._npc_names(state, player)
        random.shuffle(queue)
        notes["active_disc_scene"] = scene_name
        notes["discussion_day"] = day
        notes["discussion_queue"] = queue
        engine.save_notes(notes)
        return scene_name, queue

    def _append_disc_line(self, scene_name: str, line: str) -> None:
        if self._scene_path(scene_name).exists():
            self._append_scene(scene_name, line)
        else:
            self._write_scene(scene_name, line)

    def _strategy_hints(self, state: dict, notes: dict, player: str) -> dict[str, str]:
        """discussion_brief の内部情報をNPC個別の戦略指示に変換する。

        戻り値は名前→指示文字列。指示は各NPCのプロンプトにのみ入り、
        画面・シーンへ直接出ることはない。
        """
        day = state["day"]
        hints: dict[str, str] = {}

        brief = engine.discussion_brief()
        counter_co_new = set(brief["counter_co"])
        counter_co_actors = set(notes.get("counter_co_actors", []))

        co_claims = notes.get("public_co_claims", {})
        seer_cos = [
            n for n, info in co_claims.items()
            if isinstance(info, dict) and info.get("role") == "seer"
        ]

        # CO・結果発表はテンプレとして機械挿入される（_co_directives /
        # _result_directives）。ヒントでは挿入内容を予告し「続き」だけ指示する。
        result_dirs = self._result_directives(
            state, notes, player,
            assume_co=self._co_directives(state, notes, player),
        )

        def result_note(name: str) -> str:
            items = result_dirs.get(name, [])
            if not items:
                return ""
            rs = "、".join(f"{i['target']}→{i['result_jp']}" for i in items)
            return (
                f"発言の冒頭に結果の発表文（{rs}）が自動で挿入される。"
                "発表を自分で言い直さず、この結果を前提に考察を続けよ。"
            )

        # 真占い師: Day1 序盤に積極CO（経験者村のセオリー）
        seer_p = engine.find_role(state, "seer")
        if seer_p and seer_p["alive"] and seer_p["name"] != player:
            me = seer_p["name"]
            if me not in co_claims:
                co_note = (
                    "発言の冒頭に『COします、私は占い師です。』という宣言が自動で"
                    "挿入される。宣言を自分で言い直さず、その続きから話せ。"
                )
                rival_note = (
                    "既に他者が占い師COしているが、あなたが真の占い師である。"
                    if seer_cos else ""
                )
                hints[me] = (
                    co_note + rival_note
                    + ("初日のため占い結果はまだ無い、と正直に言うこと。" if day == 1
                       else result_note(me))
                )
            elif result_note(me):
                hints[me] = result_note(me)

        # 偽占い師CO: 初日割り当て + CO済みの維持指示
        for name in counter_co_actors:
            if co_claims.get(name, {}).get("role") == "seer":
                hints[name] = (
                    "あなたは既に占い師CO済み。今日も偽占い師として振る舞え。"
                    "COを否定・取り下げ・「占い師ではない」とは絶対に言わない。"
                    + ("初日のため結果はまだ無い、と言ってよい。" if day == 1
                       else result_note(name) or
                       "既に発表済みの占い結果と矛盾することを言わない。")
                )
            elif name in counter_co_new and day == 1:
                hints[name] = (
                    "あなたはこの発言で「占い師」を騙ってCOする（偽CO）。"
                    "発言の冒頭に『COします、私は占い師です。』という宣言が自動で"
                    "挿入される。宣言を自分で言い直さず、その続きから話せ。"
                    "初日のため結果はまだ無い、と言うこと。"
                    "自分が偽者だと悟られないよう堂々と振る舞え。"
                )

        # 霊媒師: CO済みなら結果発表（自動挿入）、未COなら判断を委ねる
        medium_p = engine.find_role(state, "medium")
        if medium_p and medium_p["alive"] and medium_p["name"] != player:
            me = medium_p["name"]
            if me in co_claims:
                if result_note(me):
                    hints[me] = result_note(me)
            elif day >= 2 and any(e["type"] == "execute" for e in state["log"]):
                hints[me] = (
                    "状況を見て霊媒師COを検討せよ。霊媒結果が村の役に立つなら"
                    "COして結果を発表してよい。"
                )

        # 疑惑スコア上位への言及を全員の緩い指針に
        suspicion = brief["suspicion"]
        if suspicion:
            top = list(suspicion.keys())[:2]
            base_hint = f"村の空気として {', '.join(top)} への疑いが強まっている。"
            for p in engine.alive_players(state):
                n = p["name"]
                if n == player:
                    continue
                hints.setdefault(n, "")
                hints[n] = (hints[n] + "\n" + base_hint).strip()

        # 初日: 占い結果の要求を全NPCに禁止（占い師本人の「結果なし」発言は別途ヒント済み）
        if day == 1:
            day1_rule = (
                "初日は夜の占い未実施（全員が知るルール）。"
                "占い師CO者に結果・占った相手・白黒を求めない。"
                "『結果は？』『誰を占った？』と詰めず、対抗COの真偽や発言の筋で疑え。"
            )
            for p in engine.alive_players(state):
                n = p["name"]
                if n == player:
                    continue
                if "初日のため占い結果" in hints.get(n, "") or "初日のため結果" in hints.get(n, ""):
                    continue
                hints.setdefault(n, "")
                hints[n] = (hints[n] + "\n" + day1_rule).strip() if hints[n] else day1_rule

        return hints

    def _co_directives(self, state: dict, notes: dict, player: str) -> dict[str, str]:
        """このターンにCOすべきNPC → 役職（EN）を返す。

        該当NPCの発言には CO_TEMPLATE が機械挿入され、台帳へ直接記録される。
        LLMの言い回し次第で検出に失敗する正規表現方式を使わない。
        前提: _strategy_hints（内部で discussion_brief）実行後に呼ぶこと
        （counter_co_actors の割り当てが済んでいる必要がある）。
        """
        co_claims = notes.get("public_co_claims", {})
        alive_names = {p["name"] for p in state["players"] if p["alive"]}
        directives: dict[str, str] = {}

        seer_p = engine.find_role(state, "seer")
        if (seer_p and seer_p["alive"] and seer_p["name"] != player
                and seer_p["name"] not in co_claims):
            directives[seer_p["name"]] = "seer"

        for name in notes.get("counter_co_actors", []):
            if name != player and name in alive_names and name not in co_claims:
                directives[name] = "seer"

        return directives

    def _result_directives(self, state: dict, notes: dict, player: str,
                           assume_co: dict[str, str] | None = None,
                           ) -> dict[str, list[dict]]:
        """このターンに発表すべき占い/霊媒結果 → NPC名ごとのリスト。

        各項目は {"kind": "seer"|"medium", "target": str, "result_jp": str}。
        発表内容はすべてエンジンが決定する（真占い師=logの実結果、
        偽占い師CO=定石の白進行を捏造、霊媒師=処刑者の実判定）。
        LLMに結果を発案させないことで、台帳とセリフの食い違いを構造的に防ぐ。

        assume_co: 同一ターンでCOテンプレが挿入されるNPC→役職。
        CO当日（例: 2日目に遅れてCOする真占い師）でも結果発表が同時に出る。
        """
        day = state["day"]
        co_claims = dict(notes.get("public_co_claims", {}))
        for n, r in (assume_co or {}).items():
            co_claims.setdefault(n, {"role": r})
        seer_claims = notes.get("public_seer_claims", [])
        alive_names = {p["name"] for p in state["players"] if p["alive"]}
        directives: dict[str, list[dict]] = {}

        # 真占い師: 未発表の実結果をすべて発表
        seer_p = engine.find_role(state, "seer")
        if (seer_p and seer_p["alive"] and seer_p["name"] != player
                and co_claims.get(seer_p["name"], {}).get("role") == "seer"):
            me = seer_p["name"]
            announced = {c["target"] for c in seer_claims if c["actor"] == me}
            items = [
                {"kind": "seer", "target": e["target"],
                 "result_jp": "人狼" if e["result"] == "werewolf" else "白（人間）"}
                for e in state["log"]
                if e["type"] == "seer" and e.get("actor") == me
                and e["target"] not in announced
            ]
            if items:
                directives[me] = items

        # 偽占い師CO: 2日目以降、その日の発表が無ければ白を1件捏造
        wolf_names = {p["name"] for p in state["players"] if p["role"] == "werewolf"}
        role_of = {p["name"]: p["role"] for p in state["players"]}
        for name in notes.get("counter_co_actors", []):
            if (name == player or name not in alive_names or day < 2
                    or co_claims.get(name, {}).get("role") != "seer"
                    or any(c["actor"] == name and c["day"] == day
                           for c in seer_claims)):
                continue
            done = {c["target"] for c in seer_claims if c["actor"] == name}
            targets = [n for n in alive_names if n != name and n not in done]
            if not targets:
                continue
            # 狼の騙りは仲間へ身内白、それ以外は疑惑最小の相手へ白（定石）
            mates = [t for t in targets if t in wolf_names] \
                if role_of.get(name) == "werewolf" else []
            if mates:
                target = mates[0]
            else:
                susp = notes.get("npc_suspicion_avg", {})
                scored = [t for t in targets if t in susp]
                target = min(scored, key=susp.get) if scored \
                    else random.choice(targets)
            directives.setdefault(name, []).append(
                {"kind": "seer", "target": target, "result_jp": "白（人間）"})

        # 霊媒師: 未発表の処刑結果を発表
        medium_p = engine.find_role(state, "medium")
        if (medium_p and medium_p["alive"] and medium_p["name"] != player
                and co_claims.get(medium_p["name"], {}).get("role") == "medium"):
            me = medium_p["name"]
            announced = {r["target"]
                         for r in notes.get("public_medium_results", [])
                         if r["actor"] == me}
            items = [
                {"kind": "medium", "target": e["target"],
                 "result_jp": "人狼" if e["alignment"] == "werewolf" else "人間"}
                for e in state["log"]
                if e["type"] == "execute" and e["target"] not in announced
            ]
            if items:
                directives.setdefault(me, []).extend(items)

        return directives

    def player_say(self, message: str, co_role: str | None = None,
                   result_target: str | None = None,
                   result_black: bool | None = None) -> dict:
        """プレイヤー発言のみをシーンに追加する（NPCは応答しない）。

        co_role: プルダウンCO。発言冒頭に CO_TEMPLATE を生成し台帳へ直接記録。
        result_target / result_black: 占い・霊媒結果の発表（CO済み役職に応じた
        テンプレを生成し台帳へ直接記録）。真偽は問わない＝騙りの黒出しも可。
        いずれも正規表現検出に依存しない。
        """
        state = engine.load_state()
        if state["phase"] != "day_discussion":
            raise OrchestratorError(f"議論フェーズではありません: {state['phase']}")

        player = engine.player_name()
        notes = engine.load_notes()
        day = state["day"]
        player_alive = any(p["name"] == player and p["alive"] for p in state["players"])
        if not player_alive:
            raise OrchestratorError("死亡したプレイヤーは発言できません")

        if co_role is not None and co_role not in CO_ROLES:
            raise OrchestratorError(f"CO可能な役職ではありません: {co_role}")

        announce_kind = None
        if result_target is not None:
            claimed = co_role or (notes.get("public_co_claims", {})
                                  .get(player) or {}).get("role")
            if claimed not in ("seer", "medium"):
                raise OrchestratorError("結果発表には占い師または霊媒師のCOが必要です")
            if result_black is None:
                raise OrchestratorError("結果（白/黒）が指定されていません")
            all_names = {p["name"] for p in state["players"]}
            if result_target not in all_names or result_target == player:
                raise OrchestratorError(f"発表対象が不正です: {result_target}")
            announce_kind = claimed

        msg = message.strip()
        prefix = ""
        if co_role:
            prefix += CO_TEMPLATE.format(role_jp=ROLE_JP[co_role])
        if announce_kind == "seer":
            result_jp = "人狼" if result_black else "白（人間）"
            prefix += SEER_RESULT_TEMPLATE.format(
                target=result_target, result=result_jp)
        elif announce_kind == "medium":
            result_jp = "人狼" if result_black else "人間"
            prefix += MEDIUM_RESULT_TEMPLATE.format(
                target=result_target, result=result_jp)
        msg = f"{prefix}{msg}" if msg else prefix
        if not msg:
            raise OrchestratorError("発言内容が空です")

        queue = list(notes.get("discussion_queue") or [])
        active = notes.get("active_disc_scene")
        if (not queue and active and notes.get("discussion_day") == day
                and self._scene_path(active).exists()):
            # NPC待ちが空なら現シーンへ追記のみ（新ラウンドは /api/continue）
            scene_name = active
        else:
            scene_name, queue = self._ensure_disc_session(state, notes, player)
        notes = engine.load_notes()
        queue = list(notes.get("discussion_queue") or [])

        player_line = f"{player}「{msg}」"
        self._append_disc_line(scene_name, player_line)
        if co_role:
            engine.record_public_co(player, co_role, day)
        if announce_kind == "seer":
            engine.record_public_seer_claim(
                player, result_target, "人狼" if result_black else "白（人間）", day)
        elif announce_kind == "medium":
            engine.record_public_medium_result(
                player, result_target,
                "werewolf" if result_black else "human", day)
        # プレイヤーのCO等を即座に台帳へ反映する
        # （次のNPC生成・CO事実検証が最新の公開情報を参照できるように）
        scene_text = self._scene_path(scene_name).read_text(encoding="utf-8")
        self._record_public_events(scene_text, state, engine.load_notes())

        disc_idx = int(re.search(r"disc(\d+)", scene_name).group(1))
        return {
            "scene": scene_name,
            "disc": disc_idx,
            "npc_queue_remaining": len(queue),
            "action": "player_say",
        }

    def npc_speak_one(self, on_progress=None) -> dict:
        """待ち行列の先頭NPCの発言を1件生成してシーンに追加する。"""
        state = engine.load_state()
        if state["phase"] != "day_discussion":
            raise OrchestratorError(f"議論フェーズではありません: {state['phase']}")

        player = engine.player_name()
        notes = engine.load_notes()
        day = state["day"]
        queue = list(notes.get("discussion_queue") or [])
        scene_name = notes.get("active_disc_scene")

        if not scene_name or notes.get("discussion_day") != day:
            scene_name, queue = self._ensure_disc_session(state, notes, player)
            notes = engine.load_notes()
            queue = list(notes.get("discussion_queue") or [])

        if not queue:
            raise OrchestratorError("NPCの発言待ちはありません。先に発言するか、投票へ進んでください。")

        conversation = self._conversation_today(day)

        # 次の話者: 直前の発言で名前を呼ばれたNPCを重み付きで優先（確定はしない）
        weights = speaker_weights(queue, npc_agent.last_speech_line(conversation))
        npc_name = random.choices(queue, weights=weights, k=1)[0]
        queue.remove(npc_name)
        notes["discussion_queue"] = queue
        engine.save_notes(notes)

        # プレイヤー応答の強制: 発言からの距離に応じた確率で課す
        distance = npc_agent.player_speech_distance(conversation, player)
        respond = random.random() < respond_probability(distance)

        disc_idx = int(re.search(r"disc(\d+)", scene_name).group(1))
        npc_agent.write_debug_header(
            f"discussion day{day} disc{disc_idx} npc={npc_name} "
            f"(respond_to_player={respond}, player_distance={distance})"
        )


        notes_now = engine.load_notes()
        # 対抗CO割り当ての副作用を維持（discussion_brief 内で決定・保存）
        engine.discussion_brief()
        notes_now = engine.load_notes()
        co_directives = self._co_directives(state, notes_now, player)
        co_role = co_directives.get(npc_name)
        results = self._result_directives(
            state, notes_now, player, assume_co=co_directives,
        ).get(npc_name, [])
        char_map = self._char_map()

        player_line = npc_agent.last_player_speech(conversation, player) or ""

        plan = speech_planner.build_speech_plan(
            state, notes_now, npc_name, player,
            respond_to_player=respond, disc_index=disc_idx,
            co_inserted=co_role, results_inserted=results,
            player_message=player_line if respond else "",
        )

        if on_progress:
            on_progress(npc_name)
        _set_typing(npc_name, scene_name)
        used_fallback = False
        try:
            res = npc_agent.render_speech(
                npc_name, self._get_view(npc_name), char_map.get(npc_name, {}),
                plan, conversation, player,
            )
        finally:
            _clear_typing()

        if not res["message"]:
            used_fallback = True
            res = {
                "name": npc_name,
                "thought": res.get("thought", ""),
                "message": f"{npc_name}「{plan['fallback_text']}」",
                "error": res.get("error"),
            }

        notes_after = engine.load_notes()
        speech_planner.mark_topics_spoken(
            notes_after, npc_name, plan["topic_keys"])
        speech_planner.record_speech_memory(
            notes_after, npc_name, plan, day)
        engine.save_notes(notes_after)

        skipped = None
        scene_text = ""
        if res["message"] and (co_role or results):
            # CO宣言・結果発表のテンプレをセリフ冒頭へ機械挿入し、台帳へ直接記録
            inserts = []
            if co_role:
                inserts.append(CO_TEMPLATE.format(role_jp=ROLE_JP[co_role]))
            for r in results:
                tmpl = SEER_RESULT_TEMPLATE if r["kind"] == "seer" \
                    else MEDIUM_RESULT_TEMPLATE
                inserts.append(tmpl.format(target=r["target"],
                                           result=r["result_jp"]))
            prefix = f"{npc_name}「"
            if res["message"].startswith(prefix):
                rest = res["message"][len(prefix):]
                res["message"] = f"{prefix}{''.join(inserts)}{rest}"
            if co_role:
                engine.record_public_co(npc_name, co_role, day)
            for r in results:
                if r["kind"] == "seer":
                    engine.record_public_seer_claim(
                        npc_name, r["target"], r["result_jp"], day)
                else:
                    engine.record_public_medium_result(
                        npc_name, r["target"],
                        "werewolf" if r["result_jp"] == "人狼" else "human", day)
        if res["message"]:
            self._append_disc_line(scene_name, res["message"])
            if res["thought"]:
                self._save_thought(scene_name, npc_name, res["thought"])
            scene_text = self._scene_path(scene_name).read_text(encoding="utf-8")
            self._record_public_events(scene_text, state, engine.load_notes())
        else:
            skipped = {"name": npc_name, "error": res["error"]}

        errors = validator.validate_file(self._scene_path(scene_name))
        if scene_text:
            errors += scene_checks.check_discussion_text(state, scene_text)

        return {
            "scene": scene_name,
            "disc": disc_idx,
            "npc": npc_name,
            "spoken": bool(res["message"]),
            "skipped": skipped,
            "npc_queue_remaining": len(queue),
            "validation_errors": errors,
            "action": "npc_speak",
            "fallback": used_fallback,
        }

    def npc_speak_all(self, on_progress=None) -> dict:
        """待ち行列のNPC全員が順番に発言する（autoplay・一括用）。"""
        spoken = []
        skipped = []
        last_result: dict = {}
        while True:
            notes = engine.load_notes()
            if not notes.get("discussion_queue"):
                break
            last_result = self.npc_speak_one(on_progress=on_progress)
            if last_result.get("skipped"):
                skipped.append(last_result["skipped"])
            elif last_result.get("spoken"):
                spoken.append(last_result["npc"])
        if not last_result:
            raise OrchestratorError("NPCの発言待ちはありません")
        last_result["spoken_npcs"] = spoken
        last_result["skipped_all"] = skipped
        last_result["action"] = "npc_speak_all"
        return last_result

    def _save_thought(self, scene_name: str, npc_name: str, thought: str) -> None:
        m = re.search(r"day(\d+)_disc(\d+)", scene_name)
        if not m:
            return
        day, disc = int(m.group(1)), int(m.group(2))
        path = npc_agent.LOG_DIR / f"npc_thoughts_day{day}_disc{disc}.json"
        thoughts = {}
        if path.exists():
            thoughts = json.loads(path.read_text(encoding="utf-8"))
        thoughts[npc_name] = thought
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(thoughts, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")

    def discussion_round(self, player_message: str | None,
                         on_progress=None) -> dict:
        """議論ラウンドを一括進める（autoplay 用。UI では player_say / npc_speak_one を使う）。"""
        state = engine.load_state()
        if state["phase"] != "day_discussion":
            raise OrchestratorError(f"議論フェーズではありません: {state['phase']}")

        player = engine.player_name()
        notes = engine.load_notes()
        player_alive = any(
            p["name"] == player and p["alive"] for p in state["players"]
        )
        if player_message and player_alive:
            self.player_say(player_message)
        else:
            self._ensure_disc_session(state, notes, player)

        result = self.npc_speak_all(on_progress=on_progress)
        return {
            "scene": result["scene"],
            "disc": result["disc"],
            "skipped": result.get("skipped_all", []),
            "validation_errors": result.get("validation_errors", []),
        }

    # ------------------------------------------------------------------
    # 公開イベントの検出・記録
    # ------------------------------------------------------------------

    def _record_public_events(self, text: str, state: dict, notes: dict) -> None:
        """シーンテキストからプレイヤーの自由文CO・結果発表を検出して記録する。

        regex 検出（CO・占い結果・霊媒結果）の対象はプレイヤーの行のみ。
        NPCのCO・結果発表はテンプレ挿入時に台帳（public_co_claims /
        public_seer_claims / public_medium_results）へ直接記録されるため、
        NPC行への regex 適用は誤検出（考察の「Xは人狼だと思う」を結果発表と
        誤認する等）しか生まない。ここでの記録が get_player_view の public_*
        と NPC 投票・襲撃の判断材料になる。
        """
        day = state["day"]
        player = engine.player_name()
        alive_names = [p["name"] for p in engine.alive_players(state)]
        all_names = [p["name"] for p in state["players"]]
        co_claims = notes.get("public_co_claims", {})

        for line in text.splitlines():
            m = re.match(r"^([^\s「」：:]+)[：:]?\s*「(.*)」\s*$", line.strip())
            if not m:
                continue
            speaker_raw, dialogue = m.group(1), m.group(2)
            speaker = next(
                (n for n in alive_names
                 if speaker_raw == n or speaker_raw.endswith(n)), None,
            )
            if not speaker:
                continue
            if speaker != player:
                continue

            if speaker not in co_claims:
                for role in ("seer", "medium", "bodyguard"):
                    if detect_role_co(dialogue, role):
                        engine.record_public_co(speaker, role, day)
                        co_claims = engine.load_notes().get("public_co_claims", {})
                        break

            co_claims = engine.load_notes().get("public_co_claims", {})

            # 占い結果発表の検出: 「Xは人狼」「Xは白/人間」（CO済み占い師のみ）
            if co_claims.get(speaker, {}).get("role") == "seer":
                for target in all_names:
                    if target == speaker or target not in dialogue:
                        continue
                    tp = re.escape(target)
                    if re.search(tp + r"(?:さん|君|様)?は?.{0,6}(?:人狼|黒)", dialogue):
                        engine.record_public_seer_claim(speaker, target, "人狼", day)
                    elif re.search(tp + r"(?:さん|君|様)?は?.{0,6}(?:白|人間|人狼ではな)", dialogue):
                        engine.record_public_seer_claim(speaker, target, "白（人間）", day)

            # 霊媒結果発表の検出（CO済み霊媒師のみ）
            if co_claims.get(speaker, {}).get("role") == "medium":
                for target in all_names:
                    if target == speaker or target not in dialogue:
                        continue
                    tp = re.escape(target)
                    if re.search(tp + r"(?:さん|君|様)?は?.{0,6}(?:人狼|黒)", dialogue):
                        engine.record_public_medium_result(speaker, target, "werewolf", day)
                    elif re.search(tp + r"(?:さん|君|様)?は?.{0,6}(?:人間|白)", dialogue):
                        engine.record_public_medium_result(speaker, target, "human", day)

        # wolf_accusations 更新: 議論中に狼を名指しで疑った生存者を記録
        self._update_wolf_accusations(text, state)

    def _update_wolf_accusations(self, text: str, state: dict) -> None:
        notes = engine.load_notes()
        wolf_names = [p["name"] for p in state["players"]
                      if p["role"] == "werewolf" and p["alive"]]
        alive_names = [p["name"] for p in engine.alive_players(state)]

        accusations: dict[str, list[str]] = {w: [] for w in wolf_names}
        suspect_pat = r"(?:怪しい|疑|吊|投票|人狼だと思)"

        for line in text.splitlines():
            m = re.match(r"^([^\s「」：:]+)[：:]?\s*「(.*)」\s*$", line.strip())
            if not m:
                continue
            speaker_raw, dialogue = m.group(1), m.group(2)
            speaker = next(
                (n for n in alive_names
                 if speaker_raw == n or speaker_raw.endswith(n)), None,
            )
            if not speaker:
                continue
            for wolf in wolf_names:
                if wolf == speaker:
                    continue
                if wolf in dialogue and re.search(suspect_pat, dialogue):
                    if speaker not in accusations[wolf]:
                        accusations[wolf].append(speaker)

        # 「現在時点」の状態として上書き（累積させない）
        notes["wolf_accusations"] = {w: a for w, a in accusations.items() if a}
        engine.save_notes(notes)

    # ------------------------------------------------------------------
    # 疑惑スコア収集（議論締め時に実行）
    # ------------------------------------------------------------------

    def collect_suspicion(self) -> dict:
        """疑惑スコアを機械計算して notes に保存する（LLM不使用）。

        旧実装は npc_agent.collect_suspicion で LLM に1体ずつ問い合わせて
        いたが、口調の翻訳以外のLLM関与を排除する方針に伴い、
        engine.compute_npc_suspicion による機械計算に置き換えた。
        """
        state = engine.load_state()
        notes = engine.load_notes()
        player = engine.player_name()
        result = engine.compute_npc_suspicion(state, notes, player)
        notes = engine.load_notes()
        notes["npc_suspicion_avg"] = result["avg"]
        engine.save_notes(notes)
        return result

    # ------------------------------------------------------------------
    # 投票・処刑
    # ------------------------------------------------------------------

    def vote_and_execute(self, player_vote: str | None) -> dict:
        """投票締切→NPC投票決定→処刑→投票/処刑シーン生成。"""
        state = engine.load_state()
        if state["phase"] not in ("day_discussion", "day_vote"):
            raise OrchestratorError(f"投票できるフェーズではありません: {state['phase']}")
        day = state["day"]

        # 議論の着地点を village_vote_target として記録（村NPCの投票先）
        vote_plan = engine.compute_vote_plan()
        notes = engine.load_notes()
        if vote_plan:
            notes["village_vote_target"] = vote_plan
            engine.save_notes(notes)

        result = engine.resolve_vote(player_vote)

        tally_block = format_vote_tally(
            result["votes"], result["executed"], runoff=result["runoff"],
        )

        # 議論キューをクリア（翌日の議論へ）
        notes = engine.load_notes()
        self._clear_disc_session(notes)

        # --- 投票宣言シーン ---
        scene_vote = f"scene_day{day}_vote.txt"
        player = engine.player_name()
        char_map = self._char_map()

        lines = []
        if player_vote and player in result["votes"]:
            lines.append(f"{player}「私は{player_vote}に投票する」")

        conversation = self._conversation_today(day)
        _set_typing(None, scene_vote)
        try:
            for voter, info in result["npc_votes"].items():
                line = self._vote_declaration(
                    voter, info, char_map.get(voter, {}), conversation,
                )
                lines.append(line)
        finally:
            _clear_typing()

        vote_text = tally_block + "\n\n" + "\n\n".join(lines)
        self._write_scene(scene_vote, vote_text)

        # 投票整合チェック（宣言セリフ vs 実投票）
        alive_names = [p["name"] for p in state["players"] if p["alive"]]
        vote_issues = scene_checks.check_vote_consistency(
            str(self._scene_path(scene_vote)),
            result["npc_votes"], player, alive_names,
        )
        if vote_issues:
            npc_agent._debug_log("VOTE_CHECK", "\n".join(vote_issues))

        # --- 処刑シーン ---
        scene_exec = f"scene_day{day}_execution.txt"
        executed = result["executed"]
        vote_text = engine.format_vote_breakdown(result["votes"])
        _set_typing(None, scene_exec)
        try:
            exec_text = self._narrate(
                f"Day{day} の投票: {vote_text}。"
                f"最多得票の【{executed}】が処刑されることになった。"
                + ("（同票のため決選の末の決定）" if result["runoff"] else "")
                + f"処刑の儀式と{executed}の最後の言葉、村の静寂を描写せよ。"
                f"{executed}の役職・陣営には一切触れない。8行以内。",
                validate_as="execution",
            )
        finally:
            _clear_typing()
        exec_text = tally_block + "\n\n" + exec_text
        self._write_scene(scene_exec, exec_text)

        return {
            "executed": executed,
            "tally": result["tally"],
            "runoff": result["runoff"],
            "win": result["win"],
            "scenes": [scene_vote, scene_exec],
            "vote_issues": vote_issues,
        }

    def _vote_declaration(self, voter: str, info: dict, char_data: dict,
                          conversation: str = "") -> str:
        """投票宣言セリフを1体分生成する。理由はエンジンが決定、LLMは口調変換のみ。"""
        target = info["target"]
        state = engine.load_state()
        notes = engine.load_notes()
        reason_jp = speech_planner._grounded_reason(
            state, notes, voter, target)

        ss = char_data.get("speech_style", {})
        prompt = (
            f"人狼ゲームの投票宣言。あなたは「{voter}」。\n"
            f"口調: {ss.get('tone', '')} / 一人称: {ss.get('first_person', '')} / "
            f"語尾: {ss.get('vocal_tics', '')}\n\n"
            f"投票先: {target}\n"
            f"理由（この内容を必ず含める）: {reason_jp}\n"
            f"「{target}に投票する」という意思が明確に伝わる宣言を1〜2文で。\n"
            "他の名前・事実を足すな。\n"
            '出力はJSONのみ: {"message": "セリフ本文"}'
        )
        try:
            raw = self.backend.complete(prompt, model=self.npc_model, expect_json=True)
            data = npc_agent.parse_json_bulletproof(raw, voter)
            msg = str(data.get("message", "")).strip()
            if msg and target in msg:
                return f"{voter}「{msg}」"
        except Exception as e:
            npc_agent._debug_log(f"VOTE_DECL {voter} ERROR", str(e))
        return f"{voter}「{reason_jp}。{target}に投票する」"

    # ------------------------------------------------------------------
    # 夜フェーズ
    # ------------------------------------------------------------------

    def night_requirements(self) -> dict:
        return engine.night_requirements()

    def resolve_night(self, seer: str | None = None, guard: str | None = None,
                      attack: str | None = None) -> dict:
        """夜を処理する。翌朝シーンは morning_scene() で別途生成する。"""
        # NPC占い師・狩人の行動先: 議論から明確な合意が取れないため
        # notes の npc_seer_target / npc_guard_target は疑惑スコアで更新する
        state = engine.load_state()
        notes = engine.load_notes()
        player = engine.player_name()

        suspicion = notes.get("npc_suspicion_avg", {})
        if suspicion:
            seer_p = engine.find_role(state, "seer")
            if seer_p and seer_p["alive"] and seer_p["name"] != player:
                already = {e["target"] for e in state["log"] if e["type"] == "seer"}
                cands = {n: s for n, s in suspicion.items()
                         if n != seer_p["name"] and n not in already}
                if cands:
                    notes["npc_seer_target"] = max(cands, key=cands.get)
                    engine.save_notes(notes)

        return engine.resolve_night(seer=seer, guard=guard, attack=attack)

    # ------------------------------------------------------------------
    # エンドゲーム
    # ------------------------------------------------------------------

    def epilogue(self, winner: str) -> dict:
        """全役職公開のエピローグと感想戦スレッドを生成する。"""
        state = engine.load_state()
        engine.finalize_game(winner)
        roles_text = "\n".join(
            f"- {p['name']}: {ROLE_JP[p['role']]}（{'生存' if p['alive'] else '死亡'}）"
            for p in state["players"]
        )
        winner_jp = "村人陣営" if winner == "village" else "人狼陣営"

        scene_epi = "scene_epilogue.txt"
        text = engine.format_epilogue_scene(winner, state)
        self._write_scene(scene_epi, text)

        # 感想戦スレッド（役職公開後の振り返り。幕引き本体はテンプレ固定）
        scene_thread = "scene_epilogue_thread.txt"
        char_map = self._char_map()
        chars_hint = "\n".join(
            f"- {p['name']}（{ROLE_JP[p['role']]}）: "
            f"{char_map.get(p['name'], {}).get('speech_style', {}).get('tone', '')}"
            for p in state["players"]
        )
        timeline = game_timeline(state)
        _set_typing(None, scene_thread)
        try:
            thread_text = self._narrate(
                f"人狼ゲームの感想戦。勝者は{winner_jp}。全役職は公開済み:\n{roles_text}\n"
                f"各キャラの口調:\n{chars_hint}\n"
                f"実際に起きた出来事の全記録（これがすべて。ゲームは{state['day']}日目で終了）:\n"
                f"{timeline}\n"
                "全員（死亡者含む）が役職公開の上でゲームを振り返るBBS風の"
                "感想戦を書け。振り返りは上記の記録にある出来事だけを話題にし、"
                "記録に無い占い・処刑・襲撃・日数を捏造しない"
                "（例: 記録に占いが無いなら誰も占い結果を語れない）。"
                "発言は必ず行頭から『名前「セリフ」』形式で、"
                "名前に役職を括弧書きしない。役職名をセリフに直接言うのは"
                "振り返りとして自然な場合のみ（例: 狂人が騙りを振り返る）。"
                "GMは発言しない。各自1〜2発言。",
                validate_as="epilogue_thread",
            )
        finally:
            _clear_typing()
        self._write_scene(scene_thread, thread_text)

        return {"scenes": [scene_epi, scene_thread], "winner": winner}
