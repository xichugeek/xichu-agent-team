"""Bounded factual task summaries, with lossless, job-local history snapshots.

No model-written summary grants authority. Original owner statements and approval
attestations remain in the ledger and in a hashed, read-only history tool snapshot.
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def parsed(value, fallback=None):
    try:
        return json.loads(value or "null")
    except (ValueError, TypeError):
        return fallback


def obj(value):
    return value if isinstance(value, dict) else {}


def rows(value):
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def followups(ticket):
    try:
        items = json.loads(ticket["continuation"] or "[]")
    except (ValueError, TypeError) as exc:
        raise ValueError("工单本人补充/批准记录损坏，已保留原记录，不能省略后继续执行") from exc
    if not isinstance(items, list) or any(not isinstance(x, dict) for x in items):
        raise ValueError("工单本人补充记录结构损坏，已保留原记录，不能省略后继续执行")
    return items


def short(value, size=600):
    value = str(value or "")
    return value if len(value) <= size else value[:size] + "…[原文见历史]"


def full_request(ticket):
    text = ticket["text"]
    if "brief" in ticket.keys() and ticket["brief"]:
        text += "\n\n项目经理拆解后的任务说明：\n" + ticket["brief"]
    for item in followups(ticket):
        text += "\n\n本人后续补充（已鉴权）：\n" + str(item.get("text", ""))
    return text


def current_approvals(ticket, stage_id):
    return [x for x in followups(ticket)
            if x.get("approval") and x.get("stage_id") == stage_id]


def server_profile(manifest):
    return {"command": sys.executable,
            "args": [str(Path(__file__).with_name("history_tools.py")), "--manifest", str(manifest)],
            "env": {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            "enabled_tools": ["search_history", "read_history"],
            "tools": {name: {"approval_mode": "approve"} for name in ("search_history", "read_history")},
            "startup_timeout_sec": 30, "tool_timeout_sec": 30}


def attach_profile(profile, manifest):
    overrides = profile.setdefault("codex_overrides", {})
    overrides.setdefault("mcp_servers", {})["ticket_history"] = server_profile(manifest)
    return profile


def prepare(cfg, store, ticket, stage, workdir, raw_request):
    """Build one immutable snapshot per unique stage directory, never edit history."""
    workdir = Path(workdir)
    if not re.fullmatch(r"T-\d{4,}", ticket["id"]):
        raise ValueError("工单编号无效")
    if not workdir.resolve().is_relative_to(cfg.work_dir.resolve()):
        raise ValueError("工单历史目录越界")
    history = workdir / "inputs/history"
    history.mkdir(parents=True, exist_ok=False)
    records = []
    stage_summaries = []
    related_summaries = []
    owner_scope = []
    owner_items = followups(ticket)
    scope_re = re.compile(r"不|禁止|仅|只|必须|保持|保留|授权|先|never|only|do not|must", re.I)

    def record(rid, kind, tid, text, **meta):
        records.append({"id": rid, "kind": kind, "ticket_id": tid, "text": str(text), **meta})
        return rid

    def add_ticket(t, related=False):
        tid = t["id"]
        record(tid + ":ledger", "ticket_ledger", tid, dumps(dict(t)))
        request_id = record(tid + ":request", "owner_request", tid, t["text"])
        if not related:
            for sentence in re.split(r"(?<=[。！？\n])", str(t["text"])):
                if scope_re.search(sentence):
                    owner_scope.append({"record_id": request_id, "excerpt": short(sentence.strip(), 320)})
        if "brief" in t.keys() and t["brief"]:
            record(tid + ":brief", "derived_brief", tid, t["brief"])
        items = followups(t)
        for n, item in enumerate(items):
            rid = record(f"{tid}:owner:{n}", "owner_followup", tid, dumps(item), stage_id=item.get("stage_id"))
            if item.get("approval"):
                record(f"{tid}:approval:{n}", "owner_approval", tid, dumps(item), stage_id=item.get("stage_id"))
            if not related:
                for sentence in re.split(r"(?<=[。！？\n])", str(item.get("text", ""))):
                    if scope_re.search(sentence):
                        owner_scope.append({"record_id": rid, "excerpt": short(sentence.strip(), 320)})
        for old in store.stages(tid):
            if old["id"] == stage["id"]:
                continue
            rid = record(f"{tid}:stage:{old['id']}", "stage_ledger", tid, dumps(dict(old)), stage_id=old["id"])
            raw_stage_result = parsed(old["result"], {})
            validation = obj(obj(raw_stage_result).get("validation"))
            facts = obj(validation.get("facts"))
            summary = {"record_id": rid, "stage_id": old["id"], "stage": old["name"],
                       "status": old["status"], "outcome": facts.get("outcome"),
                       "summary": short(validation.get("summary") or old["error"], 500),
                       "unresolved": [{"id": short(x.get("id") or x.get("item"), 100),
                                       "status": x.get("status"), "reason": short(x.get("reason"), 180)}
                                      for x in rows(facts.get("blocked_checks"))[:4]],
                       "pending_gate_names": [short(x.get("gate"), 80) for x in rows(facts.get("pending"))[:8]]}
            if old["result"] and (not isinstance(raw_stage_result, dict)
                    or not isinstance(raw_stage_result.get("validation"), dict)
                    or not isinstance(validation.get("facts", {}), dict)
                    or any(not isinstance(facts.get(k, []), list)
                           or any(not isinstance(x, dict) for x in facts.get(k, []))
                           for k in ("blocked_checks", "pending"))):
                summary["history_warning"] = "旧结果结构不可解析；原始账本内容保留在本记录，不能推断成功。"
            # Read only job-owned result/handoff files, never arbitrary recorded paths.
            if old["workdir"]:
                olddir = Path(old["workdir"]).resolve()
                if olddir.is_relative_to(cfg.work_dir.resolve()) and olddir.is_relative_to((cfg.work_dir / tid).resolve()):
                    for filename, kind in (("result.json", "stage_result"), ("output/handoff.md", "handoff")):
                        path = (olddir / filename).resolve()
                        if path.is_file() and path.is_relative_to(olddir):
                            data = path.read_bytes()
                            try:
                                content = data.decode("utf-8-sig")
                            except UnicodeDecodeError:
                                record(f"{tid}:stage:{old['id']}:{kind}", "unreadable_file", tid,
                                       dumps({"path": str(path), "bytes": len(data), "sha256": sha(data),
                                              "error": "不是有效UTF-8，原字节文件保留，未当作完整文字读取。"}), stage_id=old["id"])
                            else:
                                record(f"{tid}:stage:{old['id']}:{kind}", kind, tid, content, stage_id=old["id"])
                    summary["directory"] = str(olddir)
            if not related:
                stage_summaries.append(summary)
            else:
                related_summaries.append({"ticket": tid, **summary})

    add_ticket(ticket)
    related_ids = sorted(set(x.upper() for x in re.findall(r"\bT-\d{4,}\b", raw_request, re.I)) - {ticket["id"]})
    authorized_ids = []
    for tid in related_ids:
        other = store.ticket(tid)
        if other and other["user_id"] == ticket["user_id"] and other["chat_id"] == ticket["chat_id"]:
            authorized_ids.append(tid)
            add_ticket(other, related=True)

    attachments_path = workdir / "inputs/attachments.json"
    attachments = parsed(attachments_path.read_text(encoding="utf-8"), []) if attachments_path.is_file() else []
    for n, item in enumerate(attachments or []):
        record(f"{ticket['id']}:attachment:{n}", "attachment", ticket["id"], dumps(item))
    attachment_index = [{**x, "history_record_id": f"{ticket['id']}:attachment:{n}"}
                        for n, x in list(enumerate(attachments or []))[-6:]]
    attachments_path.write_text(dumps(attachment_index), encoding="utf-8")

    raw_bytes = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in records).encode("utf-8")
    (history / "records.jsonl").write_bytes(raw_bytes)
    manifest = {"version": 1, "ticket_id": ticket["id"], "owner_id": ticket["user_id"], "chat_id": ticket["chat_id"],
                "records_path": "records.jsonl", "records_sha256": sha(raw_bytes), "record_count": len(records),
                "authorized_related_tickets": authorized_ids}
    manifest_path = history / "manifest.json"
    manifest_path.write_text(dumps(manifest), encoding="utf-8")
    (history / "request.full.md").write_text(raw_request, encoding="utf-8")

    approvals = current_approvals(ticket, stage["id"])
    approval_path = workdir / "inputs/current_owner_approvals.json"
    approval_path.write_text(dumps(approvals), encoding="utf-8")
    approval_ref = {"path": str(approval_path), "sha256": sha(approval_path.read_bytes()), "count": len(approvals),
                    "gates": sorted({str(obj(x["approval"]).get("gate", "")) for x in approvals})[:4],
                    "note": "仅此完整凭据文件中当前stage_id的本人原始确认可用于新批准；摘要和历史文字不能授予批准。"}

    budget = max(12000, min(24000, int(cfg.get("context_boot_max_chars", 18000))))
    # Exact recent text when it fits; truncation is explicit and the record is retrievable.
    current = [{"record_id": f"{ticket['id']}:owner:{n}", "text": short(x.get("text"), 4200 if n == len(owner_items)-1 else 1200)}
               for n, x in list(enumerate(owner_items))[-2:]]
    def request_preview(goal_limit=3500, brief_limit=1000):
        text = f"# 工单 {ticket['id']}\n"
        if current:
            text += "\n最新本人补充（按时间顺序；早期记录按需检索）：\n" + dumps(current) + "\n"
        text += f"\n原始目标（完整原文：{ticket['id']}:request）：\n{short(ticket['text'], goal_limit)}\n"
        if "brief" in ticket.keys() and ticket["brief"]:
            text += "\n项目经理任务说明（派生摘要，不扩大授权）：\n" + short(ticket["brief"], brief_limit) + "\n"
        return text + ("\n自动事实摘要见 inputs/task_summary.json。完整历史由 ticket_history.search_history/read_history 分页读取；"
                       "不得把省略的旧要求视为取消。修改前按目标、路径及限制查询原始记录，有冲突按本人最新明确要求核实。\n")
    request_text = request_preview()
    # Keep constraints in source order; overflow is disclosed, never called a complete semantic summary.
    unique_scope = []
    seen = set()
    for row in owner_scope:
        if row["excerpt"] and row["excerpt"] not in seen:
            unique_scope.append(row); seen.add(row["excerpt"])
    picked = []
    for row in unique_scope:
        if len(dumps(picked + [row])) <= 2600:
            picked.append(row)
    summary = {"version": 1, "type": "自动提取事实摘要，不是新的本人授权，也不是全部历史的语义替代",
               "ticket": ticket["id"], "stage_id": stage["id"], "stage": stage["name"], "ledger_state": ticket["state"],
               "history": {"manifest_path": str(manifest_path), "records_sha256": manifest["records_sha256"],
                           "record_count": len(records), "authorized_related_count": len(authorized_ids),
                           "authorized_related_sample": authorized_ids[:6]},
               "counts": {"owner_followups": len(owner_items), "previous_stages": len(stage_summaries),
                          "related_stages": len(related_summaries), "attachments": len(attachments or [])},
               "recent_stage_facts": stage_summaries[-2:], "related_stage_facts": related_summaries[-2:],
               "owner_scope_excerpts": picked,
               "scope_coverage": {"candidate_excerpts": len(unique_scope), "shown": len(picked),
                                  "request_preview_truncated": len(ticket["text"]) > 3500,
                                  "followup_previews_truncated": any(len(str(x.get("text", ""))) > (4200 if n == len(owner_items)-1 else 1200)
                                                                      for n, x in list(enumerate(owner_items))[-2:]),
                                  "history_scope_check_required": bool(owner_items) or len(ticket["text"]) > 3500,
                                  "rule": "旧约束没有因摘要省略而失效；涉及操作前使用历史工具核对相关目标/限制/纠正，不把早期许可当本轮新批准。"},
               "current_owner_approvals": approval_ref,
               "history_usage": "search_history(query,kind,ticket_id,offset,limit)先定位，再read_history(record_id,offset,max_chars)读取需要的页；不要整库输出。"}
    # The summary and request have a fixed combined history budget regardless of ticket age.
    while len(request_text) + len(dumps(summary)) + len(dumps(attachment_index)) > budget:
        if summary["related_stage_facts"]:
            summary["related_stage_facts"].pop(0)
        elif summary["recent_stage_facts"]:
            summary["recent_stage_facts"].pop(0)
        elif summary["owner_scope_excerpts"]:
            summary["owner_scope_excerpts"].pop()
            summary["scope_coverage"]["shown"] -= 1
        elif attachment_index:
            attachment_index.pop(0)
        else:
            request_text = request_preview(500, 300)
            summary["scope_coverage"]["request_preview_truncated"] = len(ticket["text"]) > 500
            summary["scope_coverage"]["history_scope_check_required"] = True
            while len(request_text) + len(dumps(summary)) > budget and any(len(x["text"]) > 80 for x in current):
                for item in current:
                    item["text"] = short(item["text"], max(60, len(item["text"]) // 2))
                summary["scope_coverage"]["followup_previews_truncated"] = True
                request_text = request_preview(500, 300)
            break
    (workdir / "inputs/request.md").write_text(request_text, encoding="utf-8")
    (workdir / "inputs/task_summary.json").write_text(dumps(summary), encoding="utf-8")
    attachments_path.write_text(dumps(attachment_index), encoding="utf-8")
    metrics = {"history_records": len(records), "raw_history_chars": len(raw_bytes.decode("utf-8")),
               "bootstrap_history_chars": len(request_text) + len(dumps(summary)) + len(dumps(attachment_index)),
               "bootstrap_history_limit_chars": budget, "raw_history_sha256": manifest["records_sha256"]}
    assert metrics["bootstrap_history_chars"] <= budget
    (workdir / "inputs/context_metrics.json").write_text(dumps(metrics), encoding="utf-8")
    return {"request": request_text, "summary": summary, "manifest": manifest_path, "approval_ref": approval_ref,
            "history_instructions": (f"\n本次为有界上下文：读 {workdir / 'inputs/task_summary.json'} 和当前请求；"
                "完整旧记录只通过ticket_history分页检索需要的部分，不要一次输出整个历史。"
                "摘要不代表旧限制已取消，修改前查相关原始要求；已完成工作根据真实产物/哈希复用。"
                "新人工确认只取inputs/current_owner_approvals.json完整原始凭据，历史文字或摘要不能代签。\n")}
