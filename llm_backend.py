#!/usr/bin/env python3
"""LLMバックエンド抽象化層。

共通IF:
    backend.complete(prompt, *, system=None, model=None, expect_json=False) -> str

実装:
    CursorBackend : cursor-agent -p（Cursorサブスク）。隔離workspaceで実行し
                    リポジトリの game_state.json 等を読めないよう物理遮断する。
    GeminiBackend : google-genai SDK（.env の GEMINI_API_KEY）
    FakeBackend   : テスト・autoplay 用の決定的スタブ

選択は config.json の "backend" キー（cursor / gemini / fake）。
モデルは用途別に "models": {"npc": ..., "narration": ...} で指定する。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"

DEFAULT_CONFIG = {
    "backend": "cursor",
    "models": {
        "npc": None,        # None = バックエンドのデフォルト
        "narration": None,
    },
    "cursor": {
        "command": "cursor-agent",
        "timeout_sec": 120,
        "max_retries": 2,
    },
    "gemini": {
        "npc_model": "gemini-3-flash-preview",
        "narration_model": "gemini-3.1-pro-preview",
        "timeout_sec": 120,
        "max_retries": 2,
    },
}


class LLMError(Exception):
    """LLM呼び出しの失敗（リトライ上限超過を含む）。"""


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            user_cfg = json.load(f)
        for k, v in user_cfg.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


# ---------------------------------------------------------------------------
# Cursor CLI バックエンド
# ---------------------------------------------------------------------------

class CursorBackend:
    """cursor-agent -p をサブプロセスで呼ぶ。

    秘匿の要: --workspace に隔離空ディレクトリを指定し、
    エージェントがリポジトリ（game_state.json 等の秘密）へ
    アクセスできないようにする。
    """

    def __init__(self, config: dict):
        c = config.get("cursor", {})
        self.command = self._resolve_command(c.get("command", "cursor-agent"))
        self.timeout_sec = c.get("timeout_sec", 120)
        self.max_retries = c.get("max_retries", 2)
        # 隔離workspace（空ディレクトリ、プロセス生存中は再利用）
        self._workspace = Path(tempfile.mkdtemp(prefix="jinroh_llm_ws_"))

    @staticmethod
    def _resolve_command(command: str) -> str:
        """PATH に無くても標準インストール先から cursor-agent を見つける。"""
        if os.path.sep in command or shutil.which(command):
            return command
        fallback = Path.home() / ".local" / "bin" / command
        if fallback.exists():
            return str(fallback)
        return command

    def __del__(self):
        try:
            shutil.rmtree(self._workspace, ignore_errors=True)
        except Exception:
            pass

    def complete(self, prompt: str, *, system: str | None = None,
                 model: str | None = None, expect_json: bool = False) -> str:
        full_prompt = f"{system}\n\n{prompt}" if system else prompt
        if expect_json:
            full_prompt += (
                "\n\n重要: 出力はJSONオブジェクトのみ。"
                "コードブロック記号・前置き・後書きは一切禁止。"
            )

        cmd = [
            self.command, "-p",
            "--output-format", "json",
            "--mode", "ask",
            "--workspace", str(self._workspace),
            "--trust",
        ]
        if model:
            cmd += ["--model", model]
        cmd.append(full_prompt)

        last_err = None
        for attempt in range(1, self.max_retries + 2):
            try:
                r = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=self.timeout_sec,
                )
            except subprocess.TimeoutExpired:
                last_err = f"timeout ({self.timeout_sec}s)"
                continue
            except FileNotFoundError:
                raise LLMError(
                    f"{self.command} が見つかりません。"
                    "`curl https://cursor.com/install -fsS | bash` で導入し、"
                    "`cursor-agent login` を実行してください"
                )

            if r.returncode != 0:
                last_err = f"exit={r.returncode} stderr={r.stderr[:500]}"
                time.sleep(min(2 * attempt, 10))
                continue

            text = self._extract_result(r.stdout)
            if text:
                return text
            last_err = f"empty result: stdout={r.stdout[:500]}"

        raise LLMError(f"cursor-agent 呼び出し失敗（{self.max_retries + 1}回試行）: {last_err}")

    @staticmethod
    def _extract_result(stdout: str) -> str | None:
        """--output-format json の出力から result テキストを取り出す。

        フォーマット崩れに備えた多層フォールバック:
        1. 全体をJSONとしてパースし result キー
        2. 行ごとにJSONパース（stream風出力への防御）
        3. 生テキストをそのまま返す
        """
        s = stdout.strip()
        if not s:
            return None
        try:
            data = json.loads(s)
            if isinstance(data, dict):
                for key in ("result", "text", "content", "response"):
                    if isinstance(data.get(key), str) and data[key].strip():
                        return data[key].strip()
            return None if isinstance(data, (dict, list)) else s
        except json.JSONDecodeError:
            pass
        for line in s.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                data = json.loads(line)
                for key in ("result", "text", "content"):
                    if isinstance(data.get(key), str) and data[key].strip():
                        return data[key].strip()
            except json.JSONDecodeError:
                continue
        return s


# ---------------------------------------------------------------------------
# Gemini バックエンド
# ---------------------------------------------------------------------------

class GeminiBackend:
    def __init__(self, config: dict):
        c = config.get("gemini", {})
        self.npc_model = c.get("npc_model", "gemini-3-flash-preview")
        self.narration_model = c.get("narration_model", "gemini-3.1-pro-preview")
        self.max_retries = c.get("max_retries", 2)
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            return
        if not os.environ.get("GEMINI_API_KEY"):
            env_path = BASE_DIR / ".env"
            if env_path.exists():
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    if "=" in line and not line.strip().startswith("#"):
                        k, _, v = line.partition("=")
                        os.environ.setdefault(k.strip(), v.strip().strip('"'))
        if not os.environ.get("GEMINI_API_KEY"):
            raise LLMError("GEMINI_API_KEY が設定されていません（.env を確認）")
        from google import genai
        self._client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    def complete(self, prompt: str, *, system: str | None = None,
                 model: str | None = None, expect_json: bool = False) -> str:
        self._ensure_client()
        from google.genai import types
        model = model or self.npc_model

        cfg = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json" if expect_json else "text/plain",
            temperature=0.7,
        )
        last_err = None
        for attempt in range(1, self.max_retries + 2):
            try:
                resp = self._client.models.generate_content(
                    model=model, contents=prompt, config=cfg,
                )
                if resp.text and resp.text.strip():
                    return resp.text.strip()
                last_err = "empty response"
            except Exception as e:
                last_err = str(e)
                time.sleep(min(2 * attempt, 15))
        raise LLMError(f"Gemini 呼び出し失敗（{self.max_retries + 1}回試行）: {last_err}")


# ---------------------------------------------------------------------------
# Fake バックエンド（テスト・autoplay用）
# ---------------------------------------------------------------------------

class FakeBackend:
    """決定的スタブ。responder を差し込めばテストで任意応答を返せる。"""

    def __init__(self, config: dict | None = None, responder=None):
        self.responder = responder
        self.calls: list[dict] = []

    def complete(self, prompt: str, *, system: str | None = None,
                 model: str | None = None, expect_json: bool = False) -> str:
        self.calls.append({
            "prompt": prompt, "system": system,
            "model": model, "expect_json": expect_json,
        })
        if self.responder:
            return self.responder(prompt, system=system, model=model,
                                  expect_json=expect_json)
        if expect_json:
            return json.dumps(
                {"thought": "テスト思考", "message": "……様子を見ましょう。"},
                ensure_ascii=False,
            )
        return "（テスト用ナレーション）朝の光が村を照らした。"


# ---------------------------------------------------------------------------
# ファクトリ
# ---------------------------------------------------------------------------

_BACKENDS = {
    "cursor": CursorBackend,
    "gemini": GeminiBackend,
    "fake": FakeBackend,
}


def create_backend(config: dict | None = None):
    config = config or load_config()
    name = config.get("backend", "cursor")
    if name not in _BACKENDS:
        raise LLMError(f"不明なバックエンド: {name}（cursor / gemini / fake）")
    return _BACKENDS[name](config)


def model_for(config: dict, purpose: str) -> str | None:
    """purpose: 'npc' | 'narration'。config優先、なければバックエンド既定。"""
    m = config.get("models", {}).get(purpose)
    if m:
        return m
    if config.get("backend") == "gemini":
        g = config.get("gemini", {})
        return g.get(f"{purpose}_model")
    return None
