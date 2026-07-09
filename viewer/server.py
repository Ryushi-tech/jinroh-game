#!/usr/bin/env python3
"""人狼ゲーム Webサーバー（表示 + 対話）

Usage: python3 viewer/server.py [--port PORT]

GET  (表示系・従来どおり):
    /api/state /api/scenes /api/scene/<name> /api/typing /api/hash /api/characters

POST (対話系・ゲーム進行):
    /api/new_game      {"player": "オットー"}     新規ゲーム開始
    /api/say           {"message": "..."}          発言して議論ラウンドを回す
    /api/continue      {}                          発言せず議論ラウンドを回す（死亡時等）
    /api/vote          {"target": "ヤコブ"}        投票を締め切り処刑まで進める
    /api/night_action  {"seer"|"guard"|"attack": 名前}  夜行動（不要な役職は空JSON）

進行中のPOSTは1件のみ受け付ける（409 busy）。フロントは /api/typing と
/api/hash のポーリングで生成中表示・画面更新を行う。
"""

import hashlib
import json
import os
import re
import sys
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote

# プロジェクトルート（viewer/ の親ディレクトリ）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

VIEWER_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(PROJECT_ROOT, "game_state.json")
CHARACTERS_FILE = os.path.join(PROJECT_ROOT, "characters.json")
PLAYER_NAME_FILE = os.path.join(PROJECT_ROOT, ".player_name")
CHARA_IMAGE_DIR = os.path.join(PROJECT_ROOT, "chara_image")
TYPING_FILE = os.path.join(PROJECT_ROOT, ".typing_now")

import engine
from engine import GameError
from orchestrator import Orchestrator, OrchestratorError, MAX_DISC_ROUNDS_PER_DAY

PHASE_JP = {
    "night": "夜",
    "day_discussion": "昼・議論",
    "day_vote": "昼・投票",
    "epilogue": "エピローグ",
}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

# ゲーム進行の排他制御
_game_lock = threading.Lock()
_orchestrator: Orchestrator | None = None
_orchestrator_lock = threading.Lock()


def get_orchestrator() -> Orchestrator:
    global _orchestrator
    with _orchestrator_lock:
        if _orchestrator is None:
            _orchestrator = Orchestrator()
        return _orchestrator


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_player_name():
    try:
        with open(PLAYER_NAME_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def get_player(state, name):
    for p in state["players"]:
        if p["name"] == name:
            return p
    return None


def public_death_info(state):
    """公開情報としての死亡者リスト。"""
    deaths = []
    for entry in state["log"]:
        if entry["type"] == "execute":
            deaths.append({
                "name": entry["target"],
                "day": entry["day"],
                "cause": "処刑",
            })
        elif entry["type"] == "attack" and entry.get("result") == "killed":
            deaths.append({
                "name": entry["target"],
                "day": entry["day"] + 1,
                "cause": "襲撃",
                "alignment": "人間",
            })
    return deaths


def private_info(state, player):
    """役職固有の秘密情報（プレイヤー本人にのみ返す）。

    視点フィルタは engine.get_player_view() に一元化されており、
    ここではその出力を表示用文字列に整形するだけ。
    """
    view = engine.get_player_view(state, player["name"], engine.load_notes())
    role = view["self"]["role"]
    private = view["private"]
    info = []

    if role == "seer":
        for r in private.get("seer_results", []):
            info.append(f"Night {r['day']}: {r['target']} → {r['result']}")

    elif role == "bodyguard":
        for g in private.get("guard_history", []):
            mark = " ★護衛成功" if g["success"] else ""
            info.append(f"Night {g['day']}: {g['target']} を護衛{mark}")

    elif role == "werewolf":
        if view["wolf_teammates"]:
            info.append(f"仲間の人狼: {', '.join(view['wolf_teammates'])}")
        for a in private.get("attack_history", []):
            result_jp = "護衛された" if a["result"] == "guarded" else "成功"
            info.append(f"Night {a['day']}: {a['target']} を襲撃 → {result_jp}")

    elif role == "medium":
        for r in private.get("medium_results", []):
            info.append(f"Day {r['day']} 処刑: {r['target']} → {r['result']}")

    elif role == "madman":
        info.append("※ 人狼が誰かは分かりません。勘と推理で人狼陣営を勝利に導いてください。")

    return info


def is_game_over():
    for fname in os.listdir(PROJECT_ROOT):
        if fname.startswith("scene_epilogue") and fname.endswith(".txt"):
            return True
    return False


def _ui_hint(state, player, game_over):
    """フロントが表示すべき入力UIの種類を返す。"""
    if game_over:
        return {"mode": "game_over"}
    if state is None or player is None:
        return {"mode": "setup", "characters": engine.ALL_NAMES}

    phase = state["phase"]
    player_alive = player["alive"]
    win = engine.win_status(state)
    if win != "none":
        return {"mode": "epilogue_pending", "winner": win}

    if phase == "day_discussion":
        alive_names = [p["name"] for p in state["players"]
                       if p["alive"] and p["name"] != player["name"]]
        notes = engine.load_notes()
        queue_remaining = len(notes.get("discussion_queue") or [])
        # CO宣言プルダウン: 村の構成に存在する特殊役職のみ選択肢に出す
        # （真偽は問わない＝騙りCOも同じUIから行う）
        comp_roles = {p["role"] for p in state["players"]}
        co_options = [r for r in ("seer", "medium", "bodyguard") if r in comp_roles]
        player_co = (notes.get("public_co_claims", {})
                     .get(player["name"]) or {}).get("role")
        # 結果発表UI: 占い師/霊媒師でCO済みのときのみ。対象は死者含む他全員
        # （夜に占った相手が朝死んでいることがある）
        can_announce = player_alive and player_co in ("seer", "medium")
        announce_candidates = [p["name"] for p in state["players"]
                               if p["name"] != player["name"]]
        disc_count = len([
            f for f in os.listdir(PROJECT_ROOT)
            if f.startswith(f"scene_day{state['day']}_disc") and f.endswith(".txt")
        ])
        can_new_disc = disc_count < MAX_DISC_ROUNDS_PER_DAY
        return {
            "mode": "discussion",
            "can_speak": player_alive,
            "can_npc_speak": queue_remaining > 0,
            "npc_queue_remaining": queue_remaining,
            "can_start_new_disc": can_new_disc,
            "disc_rounds": disc_count,
            "max_disc_rounds": MAX_DISC_ROUNDS_PER_DAY,
            "vote_candidates": alive_names if player_alive else [],
            "co_options": co_options if player_alive else [],
            "player_co": player_co,
            "can_announce_result": can_announce,
            "announce_candidates": announce_candidates if can_announce else [],
        }
    if phase == "day_vote":
        alive_names = [p["name"] for p in state["players"]
                       if p["alive"] and p["name"] != player["name"]]
        return {
            "mode": "vote",
            "can_vote": player_alive,
            "vote_candidates": alive_names if player_alive else [],
        }
    if phase == "night":
        req = engine.night_requirements()
        need = None
        candidates = []
        alive_others = [p["name"] for p in state["players"]
                        if p["alive"] and p["name"] != player["name"]]
        if req["seer"]:
            need = "seer"
            already = {e["target"] for e in state["log"] if e["type"] == "seer"}
            candidates = [n for n in alive_others if n not in already] or alive_others
        elif req["guard"]:
            need = "guard"
            prev = engine.last_guard_target(state)
            candidates = [n for n in alive_others if n != prev]
        elif req["attack"]:
            need = "attack"
            wolf_names = {p["name"] for p in state["players"] if p["role"] == "werewolf"}
            candidates = [n for n in alive_others if n not in wolf_names]
        return {
            "mode": "night",
            "need": need,
            "candidates": candidates,
        }
    return {"mode": "unknown", "phase": phase}


def build_filtered_state():
    """プレイヤー視点でフィルタしたゲーム状態を返す。"""
    try:
        state = load_json(STATE_FILE)
    except (FileNotFoundError, json.JSONDecodeError):
        state = None

    player_name = get_player_name()
    player = get_player(state, player_name) if (state and player_name) else None
    game_over = is_game_over()

    alive = []
    deaths = []
    if state:
        for p in state["players"]:
            if p["alive"]:
                entry = {"name": p["name"]}
                if game_over:
                    entry["role"] = p["role"]
                    entry["role_jp"] = engine.ROLE_JP.get(p["role"], p["role"])
                alive.append(entry)

        deaths = public_death_info(state)
        if game_over:
            for d in deaths:
                target = get_player(state, d["name"])
                if target:
                    d["role"] = target["role"]
                    d["role_jp"] = engine.ROLE_JP.get(target["role"], target["role"])

    execution_history = []
    if state:
        for e in state["log"]:
            if e["type"] == "execute":
                execution_history.append({
                    "day": e["day"],
                    "target": e["target"],
                    "votes": e.get("votes", {}),
                    "tally": e.get("tally", {}),
                })

    result = {
        "day": state["day"] if state else 0,
        "phase": state["phase"] if state else "none",
        "phase_jp": PHASE_JP.get(state["phase"], state["phase"]) if state else "--",
        "game_over": game_over,
        "player": None,
        "alive": alive,
        "deaths": deaths,
        "execution_history": execution_history,
        "private_info": [],
        "busy": _game_lock.locked(),
        "ui": _ui_hint(state, player, game_over),
    }

    if state and state.get("phase") == "day_discussion":
        try:
            notes = engine.load_notes()
            active = notes.get("active_disc_scene")
            if active:
                result["discussion_scene"] = active
        except Exception:
            pass

    if player:
        result["player"] = {
            "name": player["name"],
            "role": player["role"],
            "role_jp": engine.ROLE_JP.get(player["role"], player["role"]),
            "alive": player["alive"],
        }
        result["private_info"] = private_info(state, player)

    return result


def list_scene_files():
    files = []
    for fname in os.listdir(PROJECT_ROOT):
        if not fname.endswith(".txt"):
            continue
        if fname.startswith("scene_day") or fname.startswith("scene_epilogue"):
            files.append(fname)
    files.sort(key=_scene_sort_key)
    return files


def _scene_sort_key(fname):
    if fname.startswith("scene_epilogue"):
        order = 1 if "_thread" in fname else 0
        return (999, order, "")

    m = re.match(r"scene_day(\d+)(?:_(.+))?\.txt", fname)
    if not m:
        return (0, 0, fname)

    day = int(m.group(1))
    phase = m.group(2) or ""

    phase_order = {
        "morning": 0,
        "discussion": 1,
    }

    disc_match = re.match(r"disc(\d+)", phase)
    if disc_match:
        order = 10 + int(disc_match.group(1))
    elif phase in phase_order:
        order = phase_order[phase]
    elif phase.startswith("vote"):
        order = 50
    elif phase.startswith("execution"):
        order = 60
    elif phase.startswith("final"):
        order = 70
    elif phase == "":
        order = 5
    else:
        order = 30
    return (day, order, phase)


def read_scene_file(name):
    if not re.match(r"^scene_(day\d+(_\w+)?|epilogue\w*)\.txt$", name):
        return None
    path = os.path.join(PROJECT_ROOT, name)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def compute_hash():
    h = hashlib.md5()
    try:
        mtime = os.path.getmtime(STATE_FILE)
        h.update(str(mtime).encode())
    except OSError:
        h.update(b"no-state")
    try:
        h.update(str(os.path.getmtime(TYPING_FILE)).encode())
    except OSError:
        pass
    h.update(b"busy" if _game_lock.locked() else b"idle")

    scenes = list_scene_files()
    h.update(json.dumps(scenes).encode())
    for s in scenes:
        try:
            mt = os.path.getmtime(os.path.join(PROJECT_ROOT, s))
            h.update(str(mt).encode())
        except OSError:
            pass
    return h.hexdigest()


# ---------------------------------------------------------------------------
# ゲーム進行アクション
# ---------------------------------------------------------------------------

def _finish_if_won(orch, win: str) -> dict | None:
    if win in ("village", "werewolf"):
        return orch.epilogue(win)
    return None


def action_new_game(body: dict) -> dict:
    orch = get_orchestrator()
    info = orch.new_game(body.get("player"))
    orch.morning_scene()
    return {"ok": True, "setup": info}


def action_say(body: dict) -> dict:
    message = (body.get("message") or "").strip()
    co_role = (body.get("co") or "").strip() or None
    result_target = (body.get("result_target") or "").strip() or None
    result_value = (body.get("result") or "").strip() or None  # "white" | "black"
    if not message and not co_role and not result_target:
        raise GameError("発言内容が空です")
    result_black = None
    if result_target:
        if result_value not in ("white", "black"):
            raise GameError("結果（白/黒）を選んでください")
        result_black = result_value == "black"
    orch = get_orchestrator()
    result = orch.player_say(
        message, co_role=co_role,
        result_target=result_target, result_black=result_black,
    )
    return {"ok": True, **result}


def action_npc_speak(body: dict) -> dict:
    orch = get_orchestrator()
    mode = (body.get("mode") or "one").strip()
    if mode == "all":
        result = orch.npc_speak_all()
    else:
        result = orch.npc_speak_one()
    return {"ok": True, **result}


def action_continue(body: dict) -> dict:
    """発言せずにNPC全員の発言を一括生成（プレイヤー死亡時・様子見）。

    前ラウンドのキューを消費済みなら新しい議論ラウンドを開始する。
    """
    orch = get_orchestrator()
    state = engine.load_state()
    notes = engine.load_notes()
    player = engine.player_name()
    queue_exhausted = (
        notes.get("discussion_day") == state["day"]
        and not notes.get("discussion_queue")
    )
    orch._ensure_disc_session(state, notes, player, force_new=queue_exhausted)
    result = orch.npc_speak_all()
    return {"ok": True, **result}


def action_vote(body: dict) -> dict:
    orch = get_orchestrator()
    state = engine.load_state()
    player = engine.player_name()
    player_alive = any(p["name"] == player and p["alive"] for p in state["players"])

    target = body.get("target")
    if player_alive and not target:
        raise GameError("投票先が必要です")

    # 議論を締める前に疑惑スコアを収集（NPC投票・夜行動の判断材料）
    orch.collect_suspicion()

    result = orch.vote_and_execute(target if player_alive else None)
    epilogue = _finish_if_won(orch, result["win"])
    resp = {"ok": True, **result}
    if epilogue:
        resp["epilogue"] = epilogue
    return resp


def action_night(body: dict) -> dict:
    orch = get_orchestrator()
    result = orch.resolve_night(
        seer=body.get("seer"),
        guard=body.get("guard"),
        attack=body.get("attack"),
    )
    epilogue = _finish_if_won(orch, result["win"])
    resp = {"ok": True, **{k: v for k, v in result.items() if k != "seer_result"}}
    if result.get("seer_result"):
        resp["seer_result"] = result["seer_result"]
    if epilogue:
        resp["epilogue"] = epilogue
    else:
        orch.morning_scene()
    return resp


POST_ACTIONS = {
    "/api/new_game": action_new_game,
    "/api/say": action_say,
    "/api/npc_speak": action_npc_speak,
    "/api/continue": action_continue,
    "/api/vote": action_vote,
    "/api/night_action": action_night,
}


# ---------------------------------------------------------------------------
# HTTPハンドラ
# ---------------------------------------------------------------------------

class ViewerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = unquote(self.path).split("?")[0]

        if path == "/api/state":
            self._json_response(build_filtered_state())
        elif path == "/api/scenes":
            self._json_response(list_scene_files())
        elif path.startswith("/api/scene/"):
            name = path[len("/api/scene/"):]
            content = read_scene_file(name)
            if content is None:
                self._error(404, "Scene not found")
            else:
                self._json_response({"name": name, "content": content})
        elif path == "/api/typing":
            try:
                with open(TYPING_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                data["busy"] = _game_lock.locked()
                self._json_response(data)
            except (FileNotFoundError, json.JSONDecodeError):
                self._json_response({"npc": None, "scene": None,
                                     "busy": _game_lock.locked()})
        elif path == "/api/hash":
            self._json_response({"hash": compute_hash()})
        elif path == "/api/characters":
            try:
                chars = load_json(CHARACTERS_FILE)
                result = {}
                for c in chars:
                    result[c["name"]] = c.get("raw_description", "")
                self._json_response(result)
            except FileNotFoundError:
                self._json_response({})
        elif path.startswith("/chara_image/"):
            self._serve_chara_image(path)
        else:
            self._serve_static(path)

    def do_POST(self):
        path = unquote(self.path).split("?")[0]
        action = POST_ACTIONS.get(path)
        if action is None:
            self._error(404, "Unknown action")
            return

        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError:
            self._error(400, "Invalid JSON body")
            return

        if not _game_lock.acquire(blocking=False):
            self._error(409, "処理中です。完了までお待ちください")
            return
        try:
            result = action(body)
            self._json_response(result)
        except (GameError, OrchestratorError) as e:
            self._error(400, str(e))
        except Exception as e:
            import traceback
            traceback.print_exc()
            self._error(500, f"内部エラー: {e}")
        finally:
            _game_lock.release()

    def _json_response(self, data):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _serve_static(self, path):
        if path == "/":
            path = "/index.html"
        safe_path = os.path.normpath(path.lstrip("/"))
        if ".." in safe_path:
            self._error(403, "Forbidden")
            return
        full_path = os.path.join(VIEWER_DIR, safe_path)
        if not os.path.isfile(full_path):
            self._error(404, "Not found")
            return
        ext = os.path.splitext(full_path)[1]
        content_type = CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(full_path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _serve_chara_image(self, path):
        fname = path[len("/chara_image/"):]
        if not re.match(r"^[\w\u3000-\u9fff\uff00-\uffef]+\.(png|jpg|jpeg|webp)$", fname):
            self._error(404, "Not found")
            return
        full_path = os.path.join(CHARA_IMAGE_DIR, fname)
        if not os.path.isfile(full_path):
            self._error(404, "Not found")
            return
        ext = os.path.splitext(full_path)[1]
        content_type = CONTENT_TYPES.get(ext, "application/octet-stream")
        with open(full_path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", len(body))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, message):
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # API ポーリングのログを抑制
        first = args[0] if args else ""
        if isinstance(first, str) and ("/api/hash" in first or "/api/typing" in first):
            return
        super().log_message(format, *args)


def main():
    port = 8080
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    server = ThreadingHTTPServer(("localhost", port), ViewerHandler)
    print(f"人狼ゲームサーバー起動: http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nサーバー停止")
        server.server_close()


if __name__ == "__main__":
    main()
