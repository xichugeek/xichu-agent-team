"""子进程运行：输出写文件、超时/取消时结束整个进程树（Windows 与 POSIX）。"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

IS_WINDOWS = os.name == "nt"


@dataclass
class ProcResult:
    returncode: int | None
    timed_out: bool
    cancelled: bool
    stdout_path: Path
    stderr_path: Path
    seconds: float

    def stdout(self) -> str:
        return self.stdout_path.read_text(encoding="utf-8", errors="replace") if self.stdout_path.exists() else ""

    def stderr(self) -> str:
        return self.stderr_path.read_text(encoding="utf-8", errors="replace") if self.stderr_path.exists() else ""

    def tail(self, n: int = 1200) -> str:
        return (self.stderr() or self.stdout())[-n:]


def _version_key(path: Path):
    try:
        out = subprocess.run([str(path), "--version"], capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.TimeoutExpired):
        return (-1,)
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else (-1,)


_CACHE: dict[str, str | None] = {}


def resolve_cli(name: str) -> str | None:
    """解析 CLI 路径。

    - 绝对路径：直接使用；
    - "auto:codex-app"：Codex 桌面版自带的 CLI，取 %LOCALAPPDATA%\\OpenAI\\Codex\\bin 下版本最高的 codex.exe；
    - "claude"：PATH 中的 claude，找不到时再看官方安装位置 %USERPROFILE%\\.local\\bin\\claude.exe。
    """
    if name in _CACHE:
        return _CACHE[name]
    found = None
    if os.path.isabs(name) and os.path.exists(name):
        found = name
    elif name == "auto:codex-app":
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI" / "Codex" / "bin"
        cands = list(base.glob("codex.exe")) + list(base.glob("*/codex.exe")) if base.is_dir() else []
        found = str(max(cands, key=_version_key)) if cands else shutil.which("codex")
    else:
        found = shutil.which(name)
        if not found and name == "claude":
            p = Path.home() / ".local" / "bin" / ("claude.exe" if IS_WINDOWS else "claude")
            found = str(p) if p.exists() else None
    if found:
        _CACHE[name] = found
    return found


def clean_env(drop: set[str], extra: dict | None = None) -> dict:
    env = {k: v for k, v in os.environ.items() if k.upper() not in drop}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra:
        env.update({k: v for k, v in extra.items() if v is not None})
    return env


def kill_tree(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except Exception:
        proc.kill()


def run(cmd: list[str], cwd: Path, log_prefix: Path, stdin_text: str | None, timeout: float,
        env: dict | None = None, cancel: threading.Event | None = None) -> ProcResult:
    out_path = log_prefix.with_name(log_prefix.name + ".stdout.log")
    err_path = log_prefix.with_name(log_prefix.name + ".stderr.log")
    kwargs: dict = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kwargs["start_new_session"] = True
    start = time.monotonic()
    timed_out = cancelled = False
    in_path = log_prefix.with_name(log_prefix.name + ".stdin.txt")
    in_path.write_text(stdin_text or "", encoding="utf-8")
    with open(out_path, "wb") as out, open(err_path, "wb") as err, open(in_path, "rb") as fin:
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdin=fin, stdout=out, stderr=err, env=env, **kwargs)
        while proc.poll() is None:
            if time.monotonic() - start > timeout:
                timed_out = True
                kill_tree(proc)
                break
            if cancel is not None and cancel.is_set():
                cancelled = True
                kill_tree(proc)
                break
            time.sleep(0.5)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    return ProcResult(proc.returncode, timed_out, cancelled, out_path, err_path, time.monotonic() - start)
