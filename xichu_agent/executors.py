"""Codex 账号 A/B（ChatGPT 订阅登录），以及显式标注的模拟执行器。

执行器只负责把提示词交给 CLI 并确认进程正常结束；产物是否合格由 workflows 根据
工作目录里的 result.json 和真实文件判定，不相信聊天文字。
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import procutil

# 子进程里一律去掉这些变量，避免误走按量付费的 API。
CODEX_DROP = {"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_ORG_ID",
              "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "XICHU_TG_BOT_TOKEN", "XG_MUSE_BRIDGE_TOKEN",
              "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
              "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_BEDROCK_BASE_URL",
              "ANTHROPIC_VERTEX_PROJECT_ID"}


@dataclass
class Job:
    ticket_id: str
    stage: str
    round: int
    workdir: Path
    prompt: str
    timeout: float
    profile: dict = field(default_factory=dict)
    cancel: threading.Event = field(default_factory=threading.Event)


@dataclass
class Outcome:
    ok: bool
    error: str | None = None
    simulated: bool = False
    detail: dict = field(default_factory=dict)
    quota_limited: bool = False   # 订阅额度受限：调度器会冷却该账号并换账号池里的下一个


def codex_account_id(codex_home: str | None) -> str | None:
    """只读取 auth.json 里 id_token 的 chatgpt_account_id，用来判断两个槽位是否同一账号；不输出任何令牌。"""
    import base64
    import os
    home = Path(codex_home) if codex_home else Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        data = json.loads((home / "auth.json").read_text(encoding="utf-8"))
        tok = (data.get("tokens") or {}).get("id_token") or ""
        if isinstance(tok, dict):
            tok = tok.get("raw_jwt", "")
        part = tok.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
        return (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_account_id") or None
    except (OSError, ValueError, IndexError, AttributeError):
        return None


class Executor:
    name = "base"
    label = "base"
    simulated = False

    def preflight(self) -> dict:
        return {"ok": True}

    def run(self, job: Job) -> Outcome:
        raise NotImplementedError


class _PreflightCache:
    def __init__(self, ttl=600):
        self.ttl, self.at, self.value = ttl, 0.0, None

    def get(self, fn):
        if self.value is None or not self.value.get("ok") or time.monotonic() - self.at > self.ttl:
            self.value, self.at = fn(), time.monotonic()
        return self.value


def _usage_limited(text: str) -> bool:
    low = text.lower()
    return bool(re.search(r"usage[ _]limit|rate[ _]limit|quota[ _](?:exceeded|limit)|too many requests|\b429\b|额度", low))


class CodexExecutor(Executor):
    def __init__(self, name: str, conf: dict):
        self.name, self.conf = name, conf
        self.label = conf.get("label", name)
        self._cache = _PreflightCache()

    def env(self) -> dict:
        extra = {}
        if self.conf.get("codex_home"):
            extra["CODEX_HOME"] = self.conf["codex_home"]
        return procutil.clean_env(CODEX_DROP, extra)

    def _check(self) -> dict:
        cli = procutil.resolve_cli(self.conf.get("cli", "codex"))
        if not cli:
            return {"ok": False, "error": "未找到 codex 命令（codex_cli_missing）"}
        home = self.conf.get("codex_home")
        if home and not Path(home).exists():
            return {"ok": False, "error": f"CODEX_HOME 不存在：{home}（需先用该账号 codex login）"}
        try:
            r = subprocess.run([cli, "login", "status"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=30, env=self.env(),
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "error": f"codex login status 失败：{exc}"}
        text = (r.stdout + r.stderr).strip()
        if r.returncode or "Logged in using ChatGPT" not in text:
            return {"ok": False, "error": f"未用 ChatGPT 订阅登录：{text[:200] or r.returncode}", "cli": cli}
        return {"ok": True, "cli": cli, "login": text.splitlines()[-1][:120], "codex_home": home or "(默认)"}

    def preflight(self) -> dict:
        return self._cache.get(self._check)

    def run(self, job: Job) -> Outcome:
        pre = self.preflight()
        if not pre.get("ok"):
            return Outcome(False, pre.get("error"))
        logs = job.workdir / "_logs"
        logs.mkdir(parents=True, exist_ok=True)
        last = logs / "codex_last_message.txt"
        p = job.profile
        from .capabilities import toml_value
        cwd = Path(p.get("cwd") or job.workdir)
        cmd = [pre["cli"], "--ask-for-approval", "never", "exec", "--skip-git-repo-check",
               "--sandbox", p.get("sandbox", "workspace-write"), "-C", str(cwd),
               "-c", 'forced_login_method="chatgpt"',
               "-c", "sandbox_workspace_write.network_access=" + ("true" if p.get("network_access") else "false"),
               "--json", "-o", str(last)]
        if p.get("output_schema"):
            cmd += ["--output-schema", str(p["output_schema"])]
        if p.get("ignore_user_config", True):
            cmd.append("--ignore-user-config")
        if self.conf.get("windows_sandbox"):
            # 忽略用户配置后需显式指定 Windows 沙盒，否则 workspace-write 会退化为只读并拒绝命令
            cmd += ["-c", f'windows.sandbox="{self.conf["windows_sandbox"]}"']
        if p.get("no_project_docs"):
            cmd += ["-c", "project_doc_max_bytes=0"]
        for d in p.get("add_dirs", []):
            cmd += ["--add-dir", str(d)]
        for key, value in p.get("codex_overrides", {}).items():
            cmd += ["-c", key + "=" + toml_value(value)]
        if self.conf.get("model"):
            cmd += ["-m", self.conf["model"]]
        effort = p.get("reasoning_effort") or self.conf.get("model_reasoning_effort")
        if effort:
            cmd += ["-c", "model_reasoning_effort=" + json.dumps(effort)]
        cmd.append("-")
        res = procutil.run(cmd, job.workdir, logs / "codex", job.prompt, job.timeout, self.env(), job.cancel)
        detail = {"returncode": res.returncode, "seconds": round(res.seconds, 1), "executor": self.label,
                  "model": self.conf.get("model"), "model_reasoning_effort": effort}
        if res.cancelled:
            return Outcome(False, "已取消", detail=detail)
        if res.timed_out:
            return Outcome(False, f"超时（{int(job.timeout)} 秒）", detail=detail)
        out = res.stdout()
        events = []
        for line in out.splitlines():
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
        detail["turn_completed"] = any(e.get("type") == "turn.completed" for e in events)
        causes = []
        for event in events:
            item = event.get("item") or {}
            if event.get("type") == "item.completed" and item.get("status") == "failed":
                error_text = str(item.get("error") or item.get("aggregated_output") or "")
                if "registered Core process has no package identity" in error_text:
                    causes.append("受限命令执行器启动失败（Core process无package identity）")
                if "MCP tool call requires approval" in error_text:
                    causes.append("工具要求交互审批，与当前无交互执行策略冲突")
        detail["execution_errors"] = list(dict.fromkeys(causes))
        fatal = [e for e in events if e.get("type") in ("error", "turn.failed")]
        if fatal and _usage_limited(json.dumps(fatal, ensure_ascii=False)):
            return Outcome(False, "Codex 订阅额度受限（结构化错误）", detail=detail, quota_limited=True)
        for e in events:
            if e.get("type") == "turn.completed" and isinstance(e.get("usage"), dict):
                detail["usage"] = e["usage"]
        if res.returncode:
            err = res.tail() + out[-600:]
            if _usage_limited(err):
                return Outcome(False, "Codex 订阅额度受限（usage limit）", detail=detail, quota_limited=True)
            return Outcome(False, f"Codex 退出码 {res.returncode}：{res.tail(500)}", detail=detail)
        if not detail["turn_completed"]:
            return Outcome(False, "Codex 未返回 turn.completed", detail=detail)
        if last.exists():
            final_text = last.read_text(encoding="utf-8", errors="replace")
            detail["last_message"] = final_text[-1500:]
            if p.get("structured_result"):
                try:
                    result = json.loads(final_text)
                    if not isinstance(result, dict):
                        raise ValueError("结果不是JSON对象")
                    (job.workdir / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                    detail["structured_delivery"] = True
                except (ValueError, OSError) as exc:
                    return Outcome(False, "结构化分派结果未交付：" + str(exc)[:300], detail=detail)
        if not (job.workdir / "result.json").is_file() and causes:
            return Outcome(False, "执行环境未就绪：" + "；".join(detail["execution_errors"]), detail=detail)
        return Outcome(True, detail=detail)


class SimulatedExecutor(Executor):
    """模拟执行器：不调用任何模型，按阶段写出确定性的模拟产物。所有结果都带 simulated 标记。"""

    simulated = True

    def __init__(self, name: str, label: str, fn: Callable[[Job], None]):
        self.name, self.label, self.fn = name, f"【模拟】{label}", fn

    def preflight(self) -> dict:
        return {"ok": True, "simulated": True}

    def run(self, job: Job) -> Outcome:
        if job.cancel.is_set():
            return Outcome(False, "已取消", simulated=True)
        self.fn(job)
        return Outcome(True, simulated=True, detail={"executor": self.label})


def build(name: str, conf: dict, simulate: bool, sim_fn: Callable[[Job], None]) -> Executor:
    if simulate:
        return SimulatedExecutor(name, conf.get("label", name), sim_fn)
    if conf["type"] == "codex":
        return CodexExecutor(name, conf)
    raise ValueError(f"unknown executor type {conf['type']}")
