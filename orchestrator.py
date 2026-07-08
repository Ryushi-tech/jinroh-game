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
import validator
import scene_checks
from llm_backend import create_backend, load_config, model_for, LLMError

BASE_DIR = Path(__file__).resolve().parent
TYPING_FILE = BASE_DIR / ".typing_now"

ROLE_JP = engine.ROLE_JP

_MAX_SCENE_RETRIES = 3


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
                is_epilogue=(validate_as == "epilogue"),
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

    def _strategy_hints(self, state: dict, notes: dict, player: str) -> dict[str, str]:
        """discussion_brief の内部情報をNPC個別の戦略指示に変換する。

        戻り値は名前→指示文字列。指示は各NPCのプロンプトにのみ入り、
        画面・シーンへ直接出ることはない。
        """
        day = state["day"]
        hints: dict[str, str] = {}

        brief = engine.discussion_brief()
        counter_co = brief["counter_co"]

        # 真占い師: Day1 序盤に積極CO（経験者村のセオリー）
        seer_p = engine.find_role(state, "seer")
        co_claims = notes.get("public_co_claims", {})
        if seer_p and seer_p["alive"] and seer_p["name"] != player:
            if seer_p["name"] not in co_claims:
                hints[seer_p["name"]] = (
                    "今日の議論で占い師COせよ。"
                    + ("初日のため占い結果はまだ無い、と正直に言うこと。" if day == 1
                       else "これまでの占い結果があれば正確に発表すること。")
                )
            else:
                # CO済み: 未発表の占い結果を発表させる
                announced = {
                    (c["target"], c["day"])
                    for c in notes.get("public_seer_claims", [])
                    if c["actor"] == seer_p["name"]
                }
                unannounced = [
                    e for e in state["log"]
                    if e["type"] == "seer" and e.get("actor") == seer_p["name"]
                    and (e["target"], e["day"]) not in announced
                ]
                if unannounced:
                    rs = " / ".join(
                        f"Day{e['day']}夜 {e['target']}→"
                        f"{'人狼' if e['result'] == 'werewolf' else '白（人間）'}"
                        for e in unannounced
                    )
                    hints[seer_p["name"]] = f"占い結果を発表せよ: {rs}"

        # 対抗CO指示（狂人・狼の偽占い師CO）
        for name in counter_co:
            hints[name] = (
                "今日の議論で「占い師」を騙ってCOせよ（偽CO）。"
                + ("初日のため結果はまだ無い、と言うこと。" if day == 1
                   else "もっともらしい捏造の占い結果を1件発表してよい"
                     "（生存者から対象を選び、村人陣営らしい相手に白を出すのが定石）。")
                + "自分が偽者だと悟られないよう堂々と振る舞え。"
            )

        # 霊媒師: 前日処刑があれば結果発表を促す（CO済みの場合）
        medium_p = engine.find_role(state, "medium")
        if medium_p and medium_p["alive"] and medium_p["name"] != player:
            if medium_p["name"] in co_claims:
                announced = {
                    (r["target"], r["day"])
                    for r in notes.get("public_medium_results", [])
                    if r["actor"] == medium_p["name"]
                }
                for e in state["log"]:
                    if e["type"] == "execute" and (e["target"], e["day"]) not in announced:
                        result_jp = "人狼" if e["alignment"] == "werewolf" else "人間"
                        hints[medium_p["name"]] = (
                            f"霊媒結果を発表せよ: Day{e['day']}処刑の"
                            f"{e['target']}は【{result_jp}】だった。"
                        )
            elif day >= 2 and any(e["type"] == "execute" for e in state["log"]):
                hints[medium_p["name"]] = (
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

        return hints

    def discussion_round(self, player_message: str | None,
                         on_progress=None) -> dict:
        """議論ラウンドを1回進める。

        player_message があればシーン冒頭に追加し、NPC全員が
        逐次ターン制で応答する。生成後にシーンを validator でチェックする。
        """
        state = engine.load_state()
        if state["phase"] != "day_discussion":
            raise OrchestratorError(f"議論フェーズではありません: {state['phase']}")

        player = engine.player_name()
        notes = engine.load_notes()
        day = state["day"]
        disc_idx = self._current_disc_index(day) + 1
        scene_name = f"scene_day{day}_disc{disc_idx}.txt"

        npc_agent.write_debug_header(f"discussion day{day} disc{disc_idx}")

        # 発言順: ランダム（毎ラウンド変える）
        npc_names = self._npc_names(state, player)
        random.shuffle(npc_names)

        hints = self._strategy_hints(state, notes, player)
        char_map = self._char_map()
        conversation = self._conversation_today(day)

        lines = []
        player_alive = any(p["name"] == player and p["alive"] for p in state["players"])
        if player_message and player_alive:
            player_line = f"{player}「{player_message.strip()}」"
            lines.append(player_line)
            conversation = (conversation + "\n\n" + player_line).strip()

        def _progress(name):
            _set_typing(name, scene_name)
            if on_progress:
                on_progress(name)

        try:
            results = npc_agent.run_discussion_round(
                npc_names, self._get_view, char_map, hints,
                conversation, on_progress=_progress,
            )
        finally:
            _clear_typing()

        thoughts = {}
        skipped = []
        for r in results:
            if r["message"]:
                lines.append(r["message"])
                thoughts[r["name"]] = r["thought"]
            else:
                skipped.append({"name": r["name"], "error": r["error"]})

        if not lines:
            raise OrchestratorError("全NPCの発言生成に失敗しました")

        text = "\n\n".join(lines)
        self._write_scene(scene_name, text)
        npc_agent.save_thoughts(day, disc_idx, thoughts)

        # 生成後チェック（観測: 結果は返すが、破綻時は既にリトライ済みの残余）
        errors = validator.validate_file(self._scene_path(scene_name))
        errors += scene_checks.check_discussion_text(state, text)

        # CO・結果発表の自動検出 → 公開情報に記録
        self._record_public_events(text, state, notes)

        return {
            "scene": scene_name,
            "disc": disc_idx,
            "skipped": skipped,
            "validation_errors": errors,
        }

    # ------------------------------------------------------------------
    # 公開イベントの検出・記録
    # ------------------------------------------------------------------

    def _record_public_events(self, text: str, state: dict, notes: dict) -> None:
        """シーンテキストからCO・結果発表を検出して公開情報に記録する。

        検出は保守的（明確なパターンのみ）。ここでの記録が
        get_player_view の public_* と NPC 投票・襲撃の判断材料になる。
        """
        day = state["day"]
        alive_names = [p["name"] for p in engine.alive_players(state)]
        all_names = [p["name"] for p in state["players"]]
        co_claims = notes.get("public_co_claims", {})

        role_patterns = {
            "seer": r"占い師",
            "medium": r"霊媒師",
            "bodyguard": r"狩人",
        }

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

            # CO検出: 「私が占い師」「占い師をCO」等
            for role, pat in role_patterns.items():
                if speaker in co_claims:
                    break
                if re.search(
                    pat + r"(?:だ|です|をCO|CO|として|は(?:私|わたし|あたし|俺|おら|僕|わたくし))",
                    dialogue,
                ) and re.search(
                    r"(?:私|わたし|あたし|俺|おら|僕|わたくし|自分)[がはも]?.{0,6}" + pat
                    + "|" + pat + r".{0,4}(?:CO|です|だ)\b?",
                    dialogue,
                ):
                    engine.record_public_co(speaker, role, day)
                    co_claims = engine.load_notes().get("public_co_claims", {})
                    break

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
        state = engine.load_state()
        player = engine.player_name()
        npc_names = self._npc_names(state, player)
        conversation = self._conversation_today(state["day"])

        _set_typing(None, "suspicion")
        try:
            result = npc_agent.collect_suspicion(
                npc_names, self._get_view, conversation,
            )
        finally:
            _clear_typing()

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

        # --- 投票宣言シーン ---
        scene_vote = f"scene_day{day}_vote.txt"
        player = engine.player_name()
        char_map = self._char_map()

        lines = []
        if player_vote and player in result["votes"]:
            lines.append(f"{player}「私は{player_vote}に投票する」")

        _set_typing(None, scene_vote)
        try:
            for voter, info in result["npc_votes"].items():
                line = self._vote_declaration(
                    voter, info, char_map.get(voter, {}),
                )
                lines.append(line)
        finally:
            _clear_typing()

        vote_text = "\n\n".join(lines)
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
        tally_text = ", ".join(f"{n}: {c}票" for n, c in result["tally"].items())
        _set_typing(None, scene_exec)
        try:
            exec_text = self._narrate(
                f"Day{day} の投票結果: {tally_text}。"
                f"最多得票の【{executed}】が処刑されることになった。"
                + ("（同票のため決選の末の決定）" if result["runoff"] else "")
                + f"処刑の儀式と{executed}の最後の言葉、村の静寂を描写せよ。"
                f"{executed}の役職・陣営には一切触れない。8行以内。",
                validate_as="execution",
            )
        finally:
            _clear_typing()
        self._write_scene(scene_exec, exec_text)

        return {
            "executed": executed,
            "tally": result["tally"],
            "runoff": result["runoff"],
            "win": result["win"],
            "scenes": [scene_vote, scene_exec],
            "vote_issues": vote_issues,
        }

    def _vote_declaration(self, voter: str, info: dict, char_data: dict) -> str:
        """投票宣言セリフを1体分生成する。失敗時は定型文にフォールバック。"""
        target = info["target"]
        reason = info["reason"]
        reason_hint = {
            "consensus": "村の議論の流れに沿った自然な投票",
            "pivot": "議論の多数派とは異なる投票。翻意の理由を自分の言葉で語る",
            "conviction": "自分の推理に基づく投票",
        }.get(reason, "")

        ss = char_data.get("speech_style", {})
        prompt = (
            f"人狼ゲームの投票宣言。あなたは「{voter}」。\n"
            f"口調: {ss.get('tone', '')} / 一人称: {ss.get('first_person', '')} / "
            f"語尾: {ss.get('vocal_tics', '')}\n"
            f"投票先: {target}（{reason_hint}）\n"
            f"「{target}に投票する」という意思が明確に伝わる宣言セリフを1〜2文で。\n"
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
        return f"{voter}「……{target}に投票する」"

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
        roles_text = "\n".join(
            f"- {p['name']}: {ROLE_JP[p['role']]}（{'生存' if p['alive'] else '死亡'}）"
            for p in state["players"]
        )
        winner_jp = "村人陣営" if winner == "village" else "人狼陣営"

        scene_epi = "scene_epilogue.txt"
        _set_typing(None, scene_epi)
        try:
            text = self._narrate(
                f"人狼ゲーム終了。勝者は【{winner_jp}】。\n"
                f"全役職公開:\n{roles_text}\n"
                "勝敗発表と全役職の開示を、物語の幕引きとして描写せよ。10行以内。",
                validate_as="epilogue",
            )
        finally:
            _clear_typing()
        self._write_scene(scene_epi, text)

        # 感想戦スレッド
        scene_thread = "scene_epilogue_thread.txt"
        char_map = self._char_map()
        chars_hint = "\n".join(
            f"- {p['name']}（{ROLE_JP[p['role']]}）: "
            f"{char_map.get(p['name'], {}).get('speech_style', {}).get('tone', '')}"
            for p in state["players"]
        )
        _set_typing(None, scene_thread)
        try:
            thread_text = self._narrate(
                f"人狼ゲームの感想戦。勝者は{winner_jp}。全役職は公開済み:\n{roles_text}\n"
                f"各キャラの口調:\n{chars_hint}\n"
                "全員（死亡者含む）が役職公開の上でゲームを振り返るBBS風の"
                "感想戦を書け。発言は必ず行頭から『名前「セリフ」』形式で、"
                "名前に役職を括弧書きしない。GMは発言しない。各自1〜2発言。",
                validate_as="epilogue",
            )
        finally:
            _clear_typing()
        self._write_scene(scene_thread, thread_text)

        return {"scenes": [scene_epi, scene_thread], "winner": winner}
