"""执行器：Codex（ChatGPT 订阅登录）、Claude Code（订阅登录），以及显式标注的模拟执行器。

执行器只负责把提示词交给 CLI 并确认进程正常结束；产物是否合格由 workflows 根据
工作目录里的 result.json 和真实文件判定，不相信聊天文字。
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import procutil

# 子进程里一律去掉这些变量，避免误走按量付费的 API。
CODEX_DROP = {"OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_ORG_ID",
              "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "AGENT_TEAM_TG_TOKEN"}
CLAUDE_DROP = {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK",
               "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "ANTHROPIC_BEDROCK_BASE_URL",
               "ANTHROPIC_VERTEX_PROJECT_ID", "AGENT_TEAM_TG_TOKEN", "CODEX_HOME"}


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
    quota_limited: bool = False   # 订阅额度受限：调度器会冷却该执行器，排队等待恢复


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
    # 例：Codex "usage limit"；Claude Code "You've hit your 5-hour limit · resets 3:20am"
    return any(k in low for k in ("usage limit", "rate limit", "429", "quota", "额度",
                                  "hit your", "limit reached", "limit · resets", "weekly limit"))


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
        cmd = [pre["cli"], "--ask-for-approval", "never", "exec", "--skip-git-repo-check",
               "--sandbox", p.get("sandbox", "workspace-write"), "-C", str(job.workdir),
               "-c", 'forced_login_method="chatgpt"',
               "-c", "sandbox_workspace_write.network_access=false",
               "--json", "-o", str(last)]
        if p.get("ignore_user_config", True):
            cmd.append("--ignore-user-config")
        if self.conf.get("windows_sandbox"):
            # 忽略用户配置后需显式指定 Windows 沙盒，否则 workspace-write 会退化为只读并拒绝命令
            cmd += ["-c", f'windows.sandbox="{self.conf["windows_sandbox"]}"']
        if p.get("no_project_docs"):
            cmd += ["-c", "project_doc_max_bytes=0"]
        for d in p.get("add_dirs", []):
            cmd += ["--add-dir", str(d)]
        if self.conf.get("model"):
            cmd += ["-m", self.conf["model"]]
        cmd.append("-")
        res = procutil.run(cmd, job.workdir, logs / "codex", job.prompt, job.timeout, self.env(), job.cancel)
        detail = {"returncode": res.returncode, "seconds": round(res.seconds, 1), "executor": self.label}
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
            detail["last_message"] = last.read_text(encoding="utf-8", errors="replace")[-1500:]
        return Outcome(True, detail=detail)


class ClaudeExecutor(Executor):
    def __init__(self, name: str, conf: dict):
        self.name, self.conf = name, conf
        self.label = conf.get("label", name)
        self._cache = _PreflightCache()

    def env(self) -> dict:
        return procutil.clean_env(CLAUDE_DROP)

    def _check(self) -> dict:
        cli = procutil.resolve_cli(self.conf.get("cli", "claude"))
        if not cli:
            return {"ok": False, "error": "未找到 claude 命令（需安装官方 Claude Code）"}
        try:
            r = subprocess.run([cli, "auth", "status", "--json"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60, env=self.env(),
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            info = json.loads(r.stdout or "{}")
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            return {"ok": False, "error": f"claude auth status 失败：{exc}"}
        method = str(info.get("authMethod", ""))
        if not info.get("loggedIn"):
            return {"ok": False, "error": "Claude Code 未登录（在终端运行 claude 后 /login 选订阅账号）", "cli": cli}
        if "api" in method.lower() or info.get("apiProvider") not in (None, "firstParty"):
            return {"ok": False, "error": f"Claude Code 当前是 {method}/{info.get('apiProvider')}，不是订阅登录，拒绝执行",
                    "cli": cli}
        return {"ok": True, "cli": cli, "authMethod": method, "subscription": info.get("subscriptionType")}

    def preflight(self) -> dict:
        return self._cache.get(self._check)

    def run(self, job: Job) -> Outcome:
        pre = self.preflight()
        if not pre.get("ok"):
            return Outcome(False, pre.get("error"))
        logs = job.workdir / "_logs"
        logs.mkdir(parents=True, exist_ok=True)
        p = job.profile
        cmd = [pre["cli"], "-p", "--output-format", "json",
               "--permission-mode", "acceptEdits",
               "--allowedTools", ",".join(p.get("allowed_tools", ["Read", "Glob", "Grep", "Write", "Edit"])),
               "--disallowedTools", ",".join(p.get("disallowed_tools", ["Bash", "WebFetch", "WebSearch"])),
               "--strict-mcp-config", "--no-session-persistence",
               "--max-turns", str(self.conf.get("max_turns", 40))]
        if self.conf.get("model"):
            cmd += ["--model", self.conf["model"]]
        res = procutil.run(cmd, job.workdir, logs / "claude", job.prompt, job.timeout, self.env(), job.cancel)
        detail = {"returncode": res.returncode, "seconds": round(res.seconds, 1), "executor": self.label}
        if res.cancelled:
            return Outcome(False, "已取消", detail=detail)
        if res.timed_out:
            return Outcome(False, f"超时（{int(job.timeout)} 秒）", detail=detail)
        try:
            data = json.loads(res.stdout().strip().splitlines()[-1])
        except (ValueError, IndexError):
            data = {}
        detail.update({k: data.get(k) for k in ("subtype", "num_turns", "duration_ms", "is_error")})
        if res.returncode or data.get("is_error") or data.get("subtype") not in ("success", None):
            msg = data.get("result") or res.tail(500)
            if _usage_limited(str(msg)):
                return Outcome(False, "Claude 订阅额度受限", detail=detail, quota_limited=True)
            return Outcome(False, f"Claude 执行失败（{data.get('subtype') or res.returncode}）：{str(msg)[:500]}",
                           detail=detail)
        detail["last_message"] = str(data.get("result", ""))[-1500:]
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
    if conf["type"] == "claude":
        return ClaudeExecutor(name, conf)
    raise ValueError(f"unknown executor type {conf['type']}")
