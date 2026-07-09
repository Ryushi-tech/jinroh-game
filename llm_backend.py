#!/usr/bin/env python3
"""LLMバックエンド抽象化層。

共通IF:
    backend.complete(prompt, *, system=None, model=None, expect_json=False) -> str

    prompt は str のほか、セグメントの list も受け付ける:
        [{"text": "...", "cache": True}, {"text": "...", "cache": False}]
    cache=True のセグメントは AnthropicBackend でプロンプトキャッシュの
    breakpoint になる（他バックエンドでは単に連結される）。

実装:
    CursorBackend    : cursor-agent -p（Cursorサブスク）。隔離workspaceで実行し
                       リポジトリの game_state.json 等を読めないよう物理遮断する。
    AnthropicBackend : Claude API（.env の ANTHROPIC_API_KEY）
    GeminiBackend    : google-genai SDK（.env の GEMINI_API_KEY）
    FakeBackend      : テスト・autoplay 用の決定的スタブ

選択は config.json の "backend" キー（cursor / anthropic / gemini / fake）。
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
    "anthropic": {
        "npc_model": "claude-haiku-4-5-20251001",
        "narration_model": "claude-sonnet-5",
        "max_tokens": 1024,
        "timeout_sec": 120,
        "max_retries": 2,
        "cache": True,
        "cache_ttl": "5m",
    },
    "gemini": {
        "npc_model": "gemini-3-flash-preview",
        "narration_model": "gemini-3.1-pro-preview",
        "timeout_sec": 120,
        "max_retries": 2,
    },
}


def _load_env_file() -> None:
    """.env のキーを環境変数へ読み込む（既存の環境変数を優先）。"""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


def join_prompt(prompt) -> str:
    """セグメントlist形式のプロンプトを単一文字列に落とす（str はそのまま）。"""
    if isinstance(prompt, str):
        return prompt
    return "\n\n".join(seg["text"] for seg in prompt if seg.get("text"))


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

    def complete(self, prompt, *, system: str | None = None,
                 model: str | None = None, expect_json: bool = False) -> str:
        prompt = join_prompt(prompt)
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
# Anthropic (Claude API) バックエンド
# ---------------------------------------------------------------------------

class AnthropicBackend:
    """Claude API を直接呼ぶ（.env の ANTHROPIC_API_KEY）。

    プロンプトのみを渡す（リポジトリへのアクセス経路が存在しないため
    CursorBackend のような workspace 隔離は不要）。

    プロンプトキャッシュ（config: anthropic.cache, 既定 true）:
    - segments形式プロンプトの cache=True 部分に明示 breakpoint を置き、
      さらにリクエスト全体へ自動キャッシュ（top-level cache_control）を併用する。
    - 同一NPCの連続ターン・再生成リトライで安定プレフィックスが再利用される。
    - モデルごとの最小キャッシュ長未満は API 側で黙って素通しされる（無害）。
    - 効果は logs/debug_view.log の LLM_USAGE 行
      （cache_write / cache_read トークン）で観測できる。
    """

    def __init__(self, config: dict):
        c = config.get("anthropic", {})
        self.npc_model = c.get("npc_model", "claude-haiku-4-5-20251001")
        self.narration_model = c.get("narration_model", "claude-sonnet-5")
        self.max_tokens = c.get("max_tokens", 1024)
        self.timeout_sec = c.get("timeout_sec", 120)
        self.max_retries = c.get("max_retries", 2)
        self.cache_enabled = c.get("cache", True)
        # "5m"（既定・追加指定なし）または "1h"（書き込み2倍コスト）
        self.cache_ttl = c.get("cache_ttl", "5m")
        self._client = None

    def _cache_control(self) -> dict:
        cc = {"type": "ephemeral"}
        if self.cache_ttl == "1h":
            cc["ttl"] = "1h"
        return cc

    @staticmethod
    def _log_usage(model: str, usage) -> None:
        """キャッシュ効果の観測用に usage を debug ログへ追記する。"""
        try:
            line = (
                f"model={model} input={getattr(usage, 'input_tokens', '?')} "
                f"cache_write={getattr(usage, 'cache_creation_input_tokens', 0) or 0} "
                f"cache_read={getattr(usage, 'cache_read_input_tokens', 0) or 0} "
                f"output={getattr(usage, 'output_tokens', '?')}"
            )
            log_dir = BASE_DIR / "logs"
            log_dir.mkdir(exist_ok=True)
            with open(log_dir / "debug_view.log", "a", encoding="utf-8") as f:
                f.write(f"\n===== LLM_USAGE =====\n{line}\n")
        except Exception:
            pass  # 観測ログの失敗で本処理を止めない（呼び出し自体は成功している）

    def _ensure_client(self):
        if self._client is not None:
            return
        _load_env_file()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise LLMError("ANTHROPIC_API_KEY が設定されていません（.env を確認）")
        try:
            import anthropic
        except ImportError:
            raise LLMError(
                "anthropic パッケージが未導入です: pip install anthropic"
            )
        # SDK 内リトライは自前リトライと重複するため無効化
        self._client = anthropic.Anthropic(
            timeout=self.timeout_sec, max_retries=0,
        )

    def _build_content(self, prompt, expect_json: bool) -> list[dict]:
        """プロンプトを content ブロック列に変換する。

        segments形式なら cache=True ブロックへ明示 breakpoint を付与
        （明示は最大3個。自動キャッシュ分と合わせ上限4を超えないため）。
        """
        json_suffix = (
            "\n\n重要: 出力はJSONオブジェクトのみ。"
            "コードブロック記号・前置き・後書きは一切禁止。"
        ) if expect_json else ""

        if isinstance(prompt, str):
            return [{"type": "text", "text": prompt + json_suffix}]

        blocks = []
        explicit = 0
        for seg in prompt:
            text = seg.get("text", "")
            if not text:
                continue
            block: dict = {"type": "text", "text": text}
            if self.cache_enabled and seg.get("cache") and explicit < 3:
                block["cache_control"] = self._cache_control()
                explicit += 1
            blocks.append(block)
        if not blocks:
            blocks = [{"type": "text", "text": ""}]
        blocks[-1]["text"] += json_suffix
        return blocks

    def complete(self, prompt, *, system: str | None = None,
                 model: str | None = None, expect_json: bool = False) -> str:
        self._ensure_client()
        model = model or self.npc_model

        kwargs: dict = {
            "model": model,
            "max_tokens": self.max_tokens,
            "messages": [{
                "role": "user",
                "content": self._build_content(prompt, expect_json),
            }],
            "temperature": 0.7,
        }
        if system:
            kwargs["system"] = system
        if self.cache_enabled:
            # 自動キャッシュ: 最後の cacheable ブロックへ breakpoint を自動付与
            kwargs["extra_body"] = {"cache_control": self._cache_control()}

        last_err = None
        for attempt in range(1, self.max_retries + 2):
            try:
                resp = self._client.messages.create(**kwargs)
                self._log_usage(model, resp.usage)
                text = "".join(
                    b.text for b in resp.content if getattr(b, "type", "") == "text"
                ).strip()
                if text:
                    return text
                last_err = "empty response"
            except Exception as e:
                last_err = str(e)
                # 新しいモデルは temperature 非対応 → 外して即時リトライ
                if "temperature" in last_err and "temperature" in kwargs:
                    kwargs.pop("temperature")
                    continue
                # レート制限・過負荷は長めに待つ
                wait = 10 if ("429" in last_err or "overloaded" in last_err.lower()) \
                    else min(2 * attempt, 15)
                time.sleep(wait)
        raise LLMError(f"Claude API 呼び出し失敗（{self.max_retries + 1}回試行）: {last_err}")


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
        _load_env_file()
        if not os.environ.get("GEMINI_API_KEY"):
            raise LLMError("GEMINI_API_KEY が設定されていません（.env を確認）")
        from google import genai
        self._client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    def complete(self, prompt, *, system: str | None = None,
                 model: str | None = None, expect_json: bool = False) -> str:
        self._ensure_client()
        from google.genai import types
        prompt = join_prompt(prompt)
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

    def complete(self, prompt, *, system: str | None = None,
                 model: str | None = None, expect_json: bool = False) -> str:
        prompt = join_prompt(prompt)
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
    "anthropic": AnthropicBackend,
    "gemini": GeminiBackend,
    "fake": FakeBackend,
}


def create_backend(config: dict | None = None):
    config = config or load_config()
    name = config.get("backend", "cursor")
    if name not in _BACKENDS:
        raise LLMError(f"不明なバックエンド: {name}（cursor / anthropic / gemini / fake）")
    return _BACKENDS[name](config)


def model_for(config: dict, purpose: str) -> str | None:
    """purpose: 'npc' | 'narration'。config優先、なければバックエンド既定。"""
    m = config.get("models", {}).get(purpose)
    if m:
        return m
    backend = config.get("backend")
    if backend in ("anthropic", "gemini"):
        b = config.get(backend, {})
        return b.get(f"{purpose}_model")
    return None
