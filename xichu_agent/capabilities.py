"""Project-native execution profiles and the installed desktop tool bridge.

Only per-process configuration is produced. Account auth and global network settings
are never copied or edited. Skills remain authoritative in their original projects.
"""
from __future__ import annotations

import copy
import json
import os
import tomllib
from pathlib import Path


def toml_value(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(str(k)) + "=" + toml_value(v) for k, v in value.items()) + "}"
    raise ValueError("Unsupported configuration type")


def desktop_servers(source_home: str | Path) -> dict:
    """Reuse installed official runtimes, without touching another account's auth."""
    source = Path(source_home)
    config = tomllib.loads((source / "config.toml").read_text(encoding="utf-8"))
    original = config.get("mcp_servers", {}).get("node_repl")
    if not isinstance(original, dict) or not Path(original.get("command", "")).is_file():
        raise RuntimeError("原 Codex 的桌面工具运行时不可用，请先打开 Codex 桌面版并运行能力自检")
    node = {k: copy.deepcopy(v) for k, v in original.items() if k in {"command", "args", "env", "env_vars", "startup_timeout_sec", "tool_timeout_sec"}}
    node.update(startup_timeout_sec=60, tool_timeout_sec=180)
    result = {"node_repl": node}
    plugins = source / "plugins/cache/openai-bundled/unified-computer-use"
    manifests = list(plugins.glob("*/.mcp.json"))
    if manifests:
        manifest = max(manifests, key=lambda p:p.stat().st_mtime)
        data = json.loads(manifest.read_text(encoding="utf-8"))
        cua = data.get("mcpServers", {}).get("cua_repl")
        if isinstance(cua, dict) and Path(cua.get("command", "")).is_file():
            bridge = {k: copy.deepcopy(v) for k, v in cua.items() if k in {"command", "args", "env", "env_vars"}}
            bridge["env"] = {**node.get("env", {}), **bridge.get("env", {})}
            bridge.update(startup_timeout_sec=60, tool_timeout_sec=180)
            result["cua_repl"] = bridge
    return result


def project_profile(cfg, role: str, workdir: Path, *, readonly=False) -> dict:
    studio = Path(cfg.get("studio_root"))
    site = Path(cfg.get("site_repo"))
    forge = Path(cfg.get("forge_root", "C:/Projects/YourWebsiteOperations"))
    source_home = cfg.get("tools_source_home") or os.environ.get("CODEX_HOME") or Path.home() / ".codex"
    servers = desktop_servers(source_home)
    if role == "video":
        root = studio
        python = studio / ".venv/Scripts/python.exe"
        adapter = studio / "claude_adapter/xichugeek_studio_mcp.py"
        if python.is_file() and adapter.is_file():
            servers["xichugeek_studio"] = {"command": str(python), "args": [str(adapter)],
                                            "startup_timeout_sec": 60, "tool_timeout_sec": 300}
            servers["production_ops"] = {"command": str(python), "args": [str(Path(__file__).with_name("production_tools.py")),
                "--studio", str(studio), "--workdir", str(workdir)], "env": {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}, "startup_timeout_sec": 60, "tool_timeout_sec": 180}
        extra = [workdir, Path(cfg.get("jianying_drafts_root", "D:/Program Files/JianyingPro Drafts"))]
    elif role == "site":
        root = forge
        extra = [workdir, site, studio]
    else:
        root = studio
        extra = [workdir]
    if not root.is_dir():
        raise RuntimeError(f"原项目工作区不存在：{root}")
    overrides = {"mcp_servers": servers, "features.image_generation": True,
                 "web_search": "live" if not readonly else "cached"}
    # Explicit owner-approved, scoped application decisions. Never infer these from discovered windows.
    allowed = cfg.get("desktop_allowed_apps", [])
    if allowed:
        if not isinstance(allowed, list) or not all(isinstance(x, str) and x for x in allowed):
            raise ValueError("desktop_allowed_apps 必须是已获本人确认的应用标识列表")
        # App decisions are read by the Desktop client, not core ConfigReadResponse.
        # Preserve the Desktop-supported Boolean table rather than replacing it with a CLI array.
    return {"cwd": str(root), "sandbox": cfg.get("native_sandbox", "workspace-write"), "add_dirs": [str(p) for p in extra if p.is_dir()],
            "ignore_user_config": True, "network_access": not readonly,
            "codex_overrides": overrides,
            "resources": ["desktop", "production-workspaces"], "native_project": True}


def source_documents(cfg, role: str) -> list[Path]:
    studio = Path(cfg.get("studio_root"))
    site = Path(cfg.get("site_repo"))
    forge = Path(cfg.get("forge_root", "C:/Projects/YourWebsiteOperations"))
    if role == "video":
        return [studio / p for p in ("AGENTS.md", "README.md", "studio.config.yaml", "docs/CODEX_EDITION.md",
                ".agents/skills/xichugeek-video-production/SKILL.md",
                ".agents/skills/xichugeek-video-production/references/production_memory.md",
                "docs/WORKFLOW.md", "docs/CONTRACTS.md", "docs/SAFETY.md")]
    return [forge / "AGENTS.md", site / "AGENTS.md",
            Path(cfg.get("site_skill") or Path(cfg.get("tools_source_home") or os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "skills/xichugeek-deploy/SKILL.md"),
            site / "deploy/robot/skill/references/runbook.md",
            forge / "05_官网完整交接_新Codex窗口接续_2026-10-28.md"]


def tool_documents(cfg) -> dict:
    source = Path(cfg.get("tools_source_home") or os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    computer = list((source / "plugins/cache/openai-bundled/computer-use").glob("*/skills/computer-use/SKILL.md"))
    paths = {"browser": source / "skills/playwright/SKILL.md", "image_generation": source / "skills/.system/imagegen/SKILL.md"}
    if computer:
        paths["computer_use"] = max(computer, key=lambda p:p.stat().st_mtime)
    return {name: str(path) for name, path in paths.items() if path.is_file()}
