#!/usr/bin/env python3
"""人狼ゲーム 表示専用Webビューア — HTTPサーバー

Usage: python3 viewer/server.py [--port PORT]

標準ライブラリのみ使用。localhost:8080 でビューアを配信する。
"""

import hashlib
import json
import os
import re
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import unquote

# プロジェクトルート（viewer/ の親ディレクトリ）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VIEWER_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(PROJECT_ROOT, "game_state.json")
CHARACTERS_FILE = os.path.join(PROJECT_ROOT, "characters.json")
PLAYER_NAME_FILE = os.path.join(PROJECT_ROOT, ".player_name")
CHARA_IMAGE_DIR = os.path.join(PROJECT_ROOT, "chara_image")

ROLE_JP = {
    "villager": "村人",
    "werewolf": "人狼",
    "seer": "占い師",
    "bodyguard": "狩人",
    "madman": "狂人",
    "medium": "霊媒師",
}

PHASE_JP = {
    "night": "夜",
    "day_discussion": "昼・議論",
    "day_vote": "昼・投票",
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
    """公開情報としての死亡者リスト。
    処刑者: 霊媒結果は霊媒師のCOによってのみ公開されるため、陣営は非表示。
    襲撃死者: 人狼に喰われた＝人狼ではないことが確定（「人間」と表示）。
    """
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
    """player_status.py と同等の役職固有秘密情報。"""
    role = player["role"]
    info = []

    if role == "seer":
        for entry in state["log"]:
            if entry["type"] == "seer" and entry["actor"] == player["name"]:
                result_jp = "人狼" if entry["result"] == "werewolf" else "人狼ではない"
                info.append(f"Night {entry['day']}: {entry['target']} → {result_jp}")

    elif role == "bodyguard":
        for entry in state["log"]:
            if entry["type"] == "guard" and entry["actor"] == player["name"]:
                success = any(
                    e["type"] == "attack"
                    and e["day"] == entry["day"]
                    and e["target"] == entry["target"]
                    and e.get("result") == "guarded"
                    for e in state["log"]
                )
                mark = " ★護衛成功" if success else ""
                info.append(f"Night {entry['day']}: {entry['target']} を護衛{mark}")

    elif role == "werewolf":
        allies = [p["name"] for p in state["players"]
                  if p["role"] == "werewolf" and p["name"] != player["name"]]
        if allies:
            info.append(f"仲間の人狼: {', '.join(allies)}")
        for entry in state["log"]:
            if entry["type"] == "attack":
                result_jp = "護衛された" if entry.get("result") == "guarded" else "成功"
                info.append(f"Night {entry['day']}: {entry['target']} を襲撃 → {result_jp}")

    elif role == "medium":
        for entry in state["log"]:
            if entry["type"] == "execute":
                alignment = entry.get("alignment")
                if alignment:
                    result_jp = "人狼" if alignment == "werewolf" else "人間"
                else:
                    target = get_player(state, entry["target"])
                    result_jp = "人狼" if target["role"] == "werewolf" else "人間"
                info.append(f"Day {entry['day']} 処刑: {entry['target']} → {result_jp}")

    elif role == "madman":
        info.append("※ 人狼が誰かは分かりません。勘と推理で人狼陣営を勝利に導いてください。")

    return info


def is_game_over():
    """エピローグシーンが存在すればゲーム終了とみなす。"""
    for fname in os.listdir(PROJECT_ROOT):
        if fname.startswith("scene_epilogue") and fname.endswith(".txt"):
            return True
    return False


def build_filtered_state():
    """プレイヤー視点でフィルタしたゲーム状態を返す。"""
    state = load_json(STATE_FILE)
    player_name = get_player_name()
    player = get_player(state, player_name) if player_name else None
    game_over = is_game_over()

    alive = []
    for p in state["players"]:
        if p["alive"]:
            entry = {"name": p["name"]}
            if game_over:
                entry["role"] = p["role"]
                entry["role_jp"] = ROLE_JP.get(p["role"], p["role"])
            alive.append(entry)

    deaths = public_death_info(state)
    if game_over:
        # ゲーム終了後: 死者にも具体的な役職を付与
        for d in deaths:
            target = get_player(state, d["name"])
            if target:
                d["role"] = target["role"]
                d["role_jp"] = ROLE_JP.get(target["role"], target["role"])

    result = {
        "day": state["day"],
        "phase": state["phase"],
        "phase_jp": PHASE_JP.get(state["phase"], state["phase"]),
        "game_over": game_over,
        "player": None,
        "alive": alive,
        "deaths": deaths,
        "private_info": [],
    }

    if player:
        result["player"] = {
            "name": player["name"],
            "role": player["role"],
            "role_jp": ROLE_JP.get(player["role"], player["role"]),
            "alive": player["alive"],
        }
        result["private_info"] = private_info(state, player)

    return result


def list_scene_files():
    """scene_day* と scene_epilogue* のファイル名を時系列順で返す。
    scene_night* は GM 視点情報が含まれるため除外。
    """
    files = []
    for fname in os.listdir(PROJECT_ROOT):
        if not fname.endswith(".txt"):
            continue
        if fname.startswith("scene_day") or fname.startswith("scene_epilogue"):
            files.append(fname)
    files.sort(key=_scene_sort_key)
    return files


def _scene_sort_key(fname):
    """シーンファイルをゲーム内時系列順にソートするキー。"""
    # scene_epilogue は最後
    if fname.startswith("scene_epilogue"):
        return (999, 999, "")

    # scene_dayN_xxx.txt からday番号とフェーズ名を抽出
    m = re.match(r"scene_day(\d+)(?:_(.+))?\.txt", fname)
    if not m:
        return (0, 0, fname)

    day = int(m.group(1))
    phase = m.group(2) or ""

    # フェーズの順序
    phase_order = {
        "morning": 0,
        "discussion": 1,
    }

    # disc1, disc2, ... のパターン
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
        # scene_dayN.txt (フェーズ名なし) — 日冒頭として扱う
        order = 5
    else:
        order = 30
    return (day, order, phase)


def read_scene_file(name):
    """シーンファイル本文を読み取る。名前を検証してパストラバーサルを防止。"""
    # ファイル名バリデーション
    if not re.match(r"^scene_(day\d+(_\w+)?|epilogue\w*)\.txt$", name):
        return None
    path = os.path.join(PROJECT_ROOT, name)
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        return f.read()


def compute_hash():
    """変更検知用ハッシュ。state mtime + scene ファイル一覧。"""
    h = hashlib.md5()
    try:
        mtime = os.path.getmtime(STATE_FILE)
        h.update(str(mtime).encode())
    except OSError:
        h.update(b"no-state")

    scenes = list_scene_files()
    h.update(json.dumps(scenes).encode())
    # 各シーンファイルの mtime も含める
    for s in scenes:
        try:
            mt = os.path.getmtime(os.path.join(PROJECT_ROOT, s))
            h.update(str(mt).encode())
        except OSError:
            pass
    return h.hexdigest()


class ViewerHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = unquote(self.path).split("?")[0]

        # API エンドポイント
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
        elif path == "/api/hash":
            self._json_response({"hash": compute_hash()})
        elif path == "/api/characters":
            try:
                chars = load_json(CHARACTERS_FILE)
                # raw_description のみ返す（設定詳細は秘匿）
                result = {}
                for c in chars:
                    result[c["name"]] = c.get("raw_description", "")
                self._json_response(result)
            except FileNotFoundError:
                self._json_response({})
        elif path.startswith("/chara_image/"):
            self._serve_chara_image(path)
        else:
            # 静的ファイル配信
            self._serve_static(path)

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
        # viewer ディレクトリ内のファイルのみ配信
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
        """chara_image/ ディレクトリから画像を配信。"""
        fname = path[len("/chara_image/"):]
        # ファイル名バリデーション（日本語文字 + 拡張子のみ許可）
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
        if "/api/hash" in (args[0] if args else ""):
            return
        super().log_message(format, *args)


def main():
    port = 8080
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        if idx + 1 < len(sys.argv):
            port = int(sys.argv[idx + 1])

    server = HTTPServer(("localhost", port), ViewerHandler)
    print(f"人狼ビューア起動: http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nサーバー停止")
        server.server_close()


if __name__ == "__main__":
    main()
