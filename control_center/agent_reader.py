"""Allowlisted, read-only view of Telegram work orders, with evidence-based links."""
from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import struct
from contextlib import closing
from pathlib import Path

from .codex_quota import QuotaCache, stamp

STATES = {"queued": "排队中", "running": "执行中", "partial": "部分完成，仍有阻塞", "waiting_approval": "等待本人确认", "waiting_publish_link": "等待发布链接", "review_rejected": "质检未过", "draft_ready": "官网草稿就绪", "needs_info": "待补充", "done": "完成", "failed": "失败", "cancelled": "已取消"}
STAGES = {"triage": "拆解需求", "produce": "视频制作", "review": "独立质检", "site_draft": "官网草稿", "site_ops": "官网运维"}
ROLES = {"pm": "项目经理", "video": "视频制作", "site": "官网运维"}
TICKET = re.compile(r"T-\d{4,10}\Z")
SITE_STAGES = {"site_ops", "site_draft"}
SITE_QUERY = "(t.robot='site' OR t.stage IN ('site_ops','site_draft') OR EXISTS (SELECT 1 FROM stages sx WHERE sx.ticket_id=t.id AND (sx.role='site' OR sx.name IN ('site_ops','site_draft'))))"


def agent_config(root: Path) -> dict:
    """Read public/local settings only; never open the Telegram secrets file."""
    config = object_json((root / "config.json").read_text(encoding="utf-8"))
    local = root / "config.local.json"
    if local.is_file():
        def merge(base, extra):
            out = dict(base)
            for key, value in extra.items():
                out[key] = merge(out[key], value) if isinstance(out.get(key), dict) and isinstance(value, dict) else value
            return out
        config = merge(config, object_json(local.read_text(encoding="utf-8")))
    return config


def scheduler_listening(port: int) -> bool | None:
    """Read the TCP table, without connecting to the scheduler's unaccepted lock socket."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            api = ctypes.WinDLL("iphlpapi").GetExtendedTcpTable
            size = wintypes.DWORD(0)
            api(None, ctypes.byref(size), False, 2, 3, 0)  # AF_INET, OWNER_PID_LISTENER
            buf = ctypes.create_string_buffer(size.value)
            if api(buf, ctypes.byref(size), False, 2, 3, 0) != 0:
                return None
            count = struct.unpack_from("<I", buf.raw)[0]
            for i in range(min(count, (len(buf.raw) - 4) // 24)):
                state, address, local_port, _, _, pid = struct.unpack_from("<6I", buf.raw, 4 + i * 24)
                if state == 2 and socket.ntohs(local_port & 0xffff) == port and address == 0x0100007f:
                    return bool(pid)
            return False
        tcp = Path("/proc/net/tcp")
        if tcp.is_file():
            return any(line.split()[1].upper() == f"0100007F:{port:04X}" and line.split()[3] == "0A" for line in tcp.read_text().splitlines()[1:])
    except (OSError, ValueError, AttributeError, struct.error):
        pass
    return None


def safe_text(value, limit=6000):
    text = str(value or "")[:limit]
    text = re.sub(r"\b\d{6,12}:[A-Za-z0-9_-]{25,}\b|\bsk-[A-Za-z0-9_-]{12,}\b|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[已隐藏凭据]", text)
    return re.sub(r"(?i)((?:access_token|refresh_token|api_key|authorization|bot_token)\s*[\"']?\s*[:=]\s*)[^\s,}]+", r"\1[已隐藏凭据]", text)


def object_json(value):
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def link_episodes(text: str, episodes: list[dict]) -> list[dict]:
    exact = [ep for ep in episodes if re.search(r"(?<![\w-])" + re.escape(ep["ref"]) + r"(?![\w-])", text, re.I)]
    numbers = re.findall(r"(?:第\s*(\d{1,3})\s*期|\bepisode\s*[-#:]?\s*(\d{1,3})(?!\d))", text, re.I)
    ids = {str(int(a or b)).zfill(3) for a, b in numbers}
    linked = {ep["ref"]: {"ref": ep["ref"], "id": ep["id"], "title": ep["title"], "source": "明确目录"} for ep in exact}
    for number in ids:
        matches = [ep for ep in episodes if ep["id"] == number and not ep["test_only"]]
        if len(matches) == 1:
            ep = matches[0]
            linked.setdefault(ep["ref"], {"ref": ep["ref"], "id": ep["id"], "title": ep["title"], "source": "明确期号"})
    return list(linked.values())


class AgentReader:
    def __init__(self, studio_root: Path, quotas=None):
        self.studio_root = studio_root
        self.quotas = quotas or QuotaCache()

    def account_configs(self) -> dict:
        integration = object_json((self.studio_root / "control_center.integration.json").read_text(encoding="utf-8"))
        root = Path(integration["agent_root"]).resolve()
        config = agent_config(root)
        return {k: v for k, v in config.get("executors", {}).items() if isinstance(v, dict) and v.get("type", "codex") == "codex"}

    def snapshot(self, episodes: list[dict], selected: str | None = None, *, site_only: bool = False) -> dict:
        if selected and not TICKET.fullmatch(selected):
            raise ValueError("Invalid ticket")
        result = {"status": "unconfigured", "message": "尚未配置电报工单数据源", "tickets": [], "ticket": None,
                  "counts": {}, "total": 0, "site_total": 0, "accounts": [], "scanned_at": stamp(), "read_only": True}
        if not (self.studio_root / "control_center.integration.json").is_file():
            return result
        try:
            integration = object_json((self.studio_root / "control_center.integration.json").read_text(encoding="utf-8"))
            if not integration.get("agent_root"):
                return result
            root = Path(integration["agent_root"]).resolve()
            config = agent_config(root)
            data = Path(config.get("data_dir", "runtime"))
            data = (data if data.is_absolute() else root / data).resolve()
            accounts = {k: v for k, v in config.get("executors", {}).items() if isinstance(v, dict) and v.get("type", "codex") == "codex"}
            result["accounts"] = self.quotas.snapshot(accounts, config.get("roles", {}))
            result["video_mode"] = config.get("video_mode", "unknown")
            result["role_pools"] = [{"role": ROLES.get(k, k), "pool": v.get("pool", [])} for k, v in config.get("roles", {}).items()]
            result["scheduler_reachable"] = scheduler_listening(int(config.get("lock_port", 47231)))
            # mode=ro never creates/migrates agent.db, even when the scheduler is offline.
            with closing(sqlite3.connect((data / "agent.db").as_uri() + "?mode=ro", uri=True, timeout=.3)) as db:
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA query_only=ON")
                db.execute("BEGIN")
                where = " WHERE " + SITE_QUERY if site_only else ""
                result["counts"] = {row["state"]: row["n"] for row in db.execute("SELECT t.state,COUNT(*) n FROM tickets t" + where + " GROUP BY t.state")}
                result["total"] = sum(result["counts"].values())
                result["site_total"] = db.execute("SELECT COUNT(*) FROM tickets t WHERE " + SITE_QUERY).fetchone()[0]
                selection = "SELECT t.id FROM tickets t" + where + " ORDER BY t.seq DESC LIMIT 200"
                rows = [dict(r) for r in db.execute("SELECT t.id,t.seq,t.robot,t.text,t.brief,t.state,t.stage,t.round,t.created_at,t.updated_at FROM tickets t" + where + " ORDER BY t.seq DESC LIMIT 200")]
                stages = [dict(r) for r in db.execute("SELECT s.* FROM stages s JOIN (" + selection + ") chosen ON chosen.id=s.ticket_id ORDER BY s.id")]
                if selected and selected not in {r["id"] for r in rows}:
                    older = db.execute("SELECT t.id,t.seq,t.robot,t.text,t.brief,t.state,t.stage,t.round,t.created_at,t.updated_at FROM tickets t WHERE t.id=?" + (" AND " + SITE_QUERY if site_only else ""), (selected,)).fetchone()
                    if older:
                        rows.append(dict(older))
                        stages += [dict(r) for r in db.execute("SELECT * FROM stages WHERE ticket_id=? ORDER BY id", (selected,))]
                by_ticket = {r["id"]: [] for r in rows}
                texts = {r["id"]: (r["text"] or "") + "\n" + (r["brief"] or "") for r in rows}
                for s in stages:
                    if s["ticket_id"] not in by_ticket:
                        continue
                    by_ticket[s["ticket_id"]].append(s)
                    validation = object_json(s["result"]).get("validation") or {}
                    texts[s["ticket_id"]] += "\n" + str(validation.get("summary") or "") + "\n" + json.dumps(validation.get("facts") or {}, ensure_ascii=False)
                    # Only inspect the explicit episode field, never raw executor logs.
                    for filename in ("result.json", "output/result.json"):
                        output = self._work_path(data, s.get("workdir"), filename)
                        if output and output.is_file() and output.stat().st_size <= 262144:
                            produced = object_json(output.read_text(encoding="utf-8-sig"))
                            reported = str(produced.get("episode") or "")
                            texts[s["ticket_id"]] += "\n" + ("Episode " + reported if re.fullmatch(r"\d{1,3}", reported) else reported)
                links = {key: link_episodes(text, episodes) for key, text in texts.items()}
                # Only inherit explicitly referenced tickets; never guess from the active Episode.
                def linked(key, visiting):
                    if key in visiting:
                        return []
                    if links.get(key):
                        return links[key]
                    found = {}
                    for parent in re.findall(r"\bT-\d{4,10}\b", texts.get(key, "")):
                        for ep in linked(parent, visiting | {key}):
                            found[ep["ref"]] = {**ep, "source": "关联工单 " + parent}
                    return list(found.values())
                for row in rows:
                    site_history = [s for s in by_ticket[row["id"]] if s.get("role") == "site" or s["name"] in SITE_STAGES]
                    history = site_history if site_only else by_ticket[row["id"]]
                    latest = history[-1] if history else {}
                    row.update(episodes=linked(row["id"], set()), title=safe_text(row["text"], 120).replace("\n", " "),
                               state_label=STATES.get(row["state"], row["state"]), stage_label=STAGES.get(row["stage"], row["stage"] or "等待分配"),
                               role_label=ROLES.get(latest.get("role") or row["robot"], row["robot"]), executor=latest.get("executor"),
                               completed_stages=sum(s["status"] == "ok" for s in history), attempts=len(history),
                               simulated=any(s.get("simulated") for s in history),
                               site_member=bool(site_history or row["robot"] == "site" or row["stage"] in SITE_STAGES))
                    if site_only:
                        row.update(stage_label=STAGES.get(latest.get("name") or row["stage"], "官网任务待分派"),
                                   role_label="官网运维", attempts=sum(max(1, s.get("attempt") or 1) for s in history))
                    row.pop("text")
                    row.pop("brief")
                result["tickets"] = rows
                selected = selected or next((r["id"] for r in rows if r["state"] == "running"), rows[0]["id"] if rows else None)
                chosen = next((r for r in rows if r["id"] == selected), None)
                if chosen:
                    detail = {**chosen, "request": safe_text(texts[selected].split("\n", 1)[0], 6000), "stages": []}
                    # Full original user request (not model reasoning or raw executor logs).
                    request = db.execute("SELECT text FROM tickets WHERE id=?", (selected,)).fetchone()
                    detail["request"] = safe_text(request[0])
                    for s in by_ticket[selected]:
                        if site_only and s.get("role") != "site" and s["name"] not in SITE_STAGES:
                            continue
                        validation = object_json(s["result"]).get("validation") or {}
                        item = {k: s.get(k) for k in ("name", "status", "executor", "role", "round", "attempt", "started_at", "finished_at", "last_heartbeat", "simulated")}
                        item.update(label=STAGES.get(s["name"], s["name"]), summary=safe_text(validation.get("summary"), 1800), error=safe_text(s.get("error"), 1200),
                                    artifacts=[{"name": safe_text(a.get("rel"), 300), "bytes": a.get("bytes")} for a in validation.get("artifacts", []) if isinstance(a, dict)][:40])
                        progress = self._work_path(data, s.get("workdir"), "progress.log")
                        if progress and progress.is_file():
                            with progress.open("rb") as stream:
                                stream.seek(max(0, progress.stat().st_size - 8192))
                                item["progress"] = safe_text(stream.read(8192).decode("utf-8", errors="replace"), 8192).splitlines()[-12:]
                        detail["stages"].append(item)
                    result["ticket"] = detail
            result.update(status="ok", message="读取电报工单数据库；每 4 秒刷新")
        except (OSError, ValueError, TypeError, sqlite3.Error):
            result.update(status="unavailable", message="电报数据源暂不可读；Studio 制作记录仍可使用")
        return result

    def site_snapshot(self, episodes: list[dict], selected: str | None = None) -> dict:
        result = self.snapshot(episodes, selected, site_only=True)
        result["account_pool"] = []
        result["project"] = {"configured": False, "exists": False, "wordpress": False, "frontend": False}
        try:
            integration = object_json((self.studio_root / "control_center.integration.json").read_text(encoding="utf-8"))
            config = agent_config(Path(integration["agent_root"]))
            result["account_pool"] = config.get("roles", {}).get("site", {}).get("pool", [])
            if config.get("site_repo"):
                root = Path(config["site_repo"]).resolve()
                result["project"] = {"configured": True, "path": str(root), "exists": root.is_dir(),
                                     "wordpress": (root / "wp-includes/version.php").is_file(),
                                     "frontend": (root / "wp-content/mu-plugins/xichugeek-site-v2.php").is_file()}
        except (OSError, ValueError, KeyError):
            pass
        result["message"] = "官网工单与本地交付记录，每 4 秒刷新" if result["status"] == "ok" else "官网工单数据源暂不可读"
        return result

    @staticmethod
    def _work_path(data: Path, workdir: str | None, filename: str):
        if not workdir:
            return None
        try:
            path = (Path(workdir) / filename).resolve()
            return path if path.is_relative_to((data / "work").resolve()) else None
        except (OSError, ValueError):
            return None
