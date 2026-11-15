"""Explain file states as a UI pipeline; never perform state transitions."""
from __future__ import annotations

ROLES = [
    ("director", "Director", "制作总监", ("brief", "release")),
    ("research", "Research", "事实研究", ("facts", "evidence")),
    ("script", "Script", "脚本与镜头", ("script", "shots")),
    ("recorder", "Recorder", "素材录制", ("shots",)),
    ("visual", "Visual", "视觉与封面", ("assets",)),
    ("voice", "Voice", "本人音色与字幕", ("voice", "captions")),
    ("editor", "Editor", "剪辑与交付", ("timeline", "render")),
    ("qa", "QA", "质量检查", ("qa",)),
]
WEIGHTS = {"director": 5, "research": 10, "FACT_REVIEW": 5, "script": 10,
           "SCRIPT_APPROVAL": 5, "recorder": 10, "visual": 10, "voice": 10,
           "editor": 10, "qa": 10, "FINAL_REVIEW": 10, "PUBLISH_APPROVAL": 5}
CREDIT = {"approved": 1, "published": 1, "selected": .75, "rendered": .85,
          "captured": .5, "tested": .25, "ready": .1, "review_required": .5}
FAILURES = {"blocked", "rejected", "obsolete", "validate_error"}
STATUS_LABELS = {"approved": "已批准", "published": "已发布", "selected": "已选用",
                 "rendered": "已渲染", "captured": "已采集", "tested": "小样已测试",
                 "review_required": "待复核", "obsolete": "上游失效", "blocked": "阻塞",
                 "rejected": "已拒绝", "planned": "未开始", "ready": "待执行",
                 "pending": "等待人工", "unknown": "UNKNOWN", "validate_error": "Validate 错误"}


def combine(values: list[str]) -> str:
    for priority in ("obsolete", "blocked", "rejected", "unknown", "review_required", "planned", "ready", "tested", "captured", "selected", "rendered"):
        if priority in values:
            return priority
    return "approved" if values and all(x in {"approved", "published"} for x in values) else "unknown"


def enrich(detail: dict, validation: dict | None = None) -> dict:
    artifacts, gates, candidate = detail["artifacts"], detail["gates"], detail["candidate"]

    def artifact_state(names):
        states = []
        for name in names:
            item = artifacts[name]
            if not item["exists"]:
                states.append("blocked" if item["status"] != "unknown" else "unknown")
            elif item["stale_parent"]:
                states.append("obsolete")
            else:
                states.append(item["status"])
        return combine(states)

    steps = []

    def step(key, label, kind, status, owner, outputs=(), note=""):
        steps.append({"id": key, "label": label, "kind": kind, "status": status,
                      "status_label": STATUS_LABELS.get(status, "UNKNOWN"), "owner": owner,
                      "outputs": list(outputs), "note": note, "current": False, "weight": WEIGHTS.get(key, 0)})

    step("director", "Director · 选题与范围", "role", artifact_state(("brief",)), "Director", ("brief",))
    step("research", "Research · 事实与证据", "role", artifact_state(("facts", "evidence")), "Research", ("facts", "evidence"))
    step("FACT_REVIEW", "FACT_REVIEW", "human", gates["FACT_REVIEW"]["status"], "Director / 人工", note=gates["FACT_REVIEW"]["source"])
    step("script", "Script · 脚本", "role", artifact_state(("script",)), "Script", ("script",))
    step("SCRIPT_APPROVAL", "SCRIPT_APPROVAL", "human", gates["SCRIPT_APPROVAL"]["status"], "Director / 人工")
    recorder_state = artifact_state(("shots",))
    if recorder_state == "approved" and detail["stats"]["required_shots"] is not None:
        if detail["stats"]["selected_shot_files"] != detail["stats"]["required_shots"] or detail["stats"]["required_shots"] == 0:
            if detail["stats"]["required_shot_files"] == detail["stats"]["required_shots"] and detail["stats"]["required_shots"] > 0:
                recorder_state = "captured" if detail["stats"]["shot_row_status"] == "captured" else "review_required"
            else:
                recorder_state = "blocked"
    step("recorder", "Recorder · 素材", "production", recorder_state, "Recorder", ("shots",))
    visual_state = artifact_state(("assets",))
    if visual_state == "approved" and candidate["cover_exists"] is not True:
        visual_state = "review_required"
    step("visual", "Visual · 动画与封面", "production", visual_state, "Visual", ("assets",), "封面为每期默认交付")
    voice_state = artifact_state(("voice", "captions"))
    if voice_state == "approved" and (not candidate["master_exists"] or not candidate["captions_exist"]):
        voice_state = "blocked"
    step("voice", "Voice · 本人音色与 SRT", "production", voice_state, "Voice", ("voice", "captions"))
    editor_state = artifact_state(("timeline", "render"))
    if candidate["native_ready"] and artifact_state(("timeline",)) == "approved":
        editor_state = "approved"
    step("editor", "Editor · 剪辑交付", "role", editor_state, "Editor", ("timeline", "render"),
         "剪映工程就绪；用户自行导出，render 未完成" if candidate["status"] == "EDIT_READY_FOR_USER_EXPORT" else "工程与 MP4 分别核对")
    step("qa", "QA · 独立检查", "role", artifact_state(("qa",)), "QA", ("qa",), "技术检查记录不自动批准人工终审")
    step("FINAL_REVIEW", "FINAL_REVIEW", "human", gates["FINAL_REVIEW"]["status"], "Director / 人工")
    step("PUBLISH_APPROVAL", "PUBLISH_APPROVAL", "human", gates["PUBLISH_APPROVAL"]["status"], "Director / 人工")
    published = artifacts["release"]["status"] == "published" and all(gates[g]["status"] == "approved" for g in ("SCRIPT_APPROVAL", "FINAL_REVIEW", "PUBLISH_APPROVAL"))
    step("published", "Published", "terminal", "published" if published else "planned", "用户", ("release",))
    validation = validation or {"status": "NOT_RUN", "checked_at": None, "issues": []}
    current = next((x for x in steps if x["status"] not in {"approved", "published"}), steps[-1])
    if validation["status"] in {"FAIL", "ERROR", "UNSAFE"}:
        current = dict(current)
        current.update(status="validate_error", status_label="Validate 错误")
    for item in steps:
        item["current"] = item["id"] == current["id"]
        if item["current"] and current["status"] == "validate_error":
            item["source_status"] = item["status"]
            item.update(status="validate_error", status_label="Validate 错误")
    if current["id"] == "PUBLISH_APPROVAL" and artifacts["release"]["status"] != "approved":
        owner = "Director"
        action = "先完成并验收 release_gate.md 发布前检查，再等待用户单独批准 PUBLISH_APPROVAL。"
    elif current["id"] == "published" and not published:
        owner = "用户" if artifacts["release"]["status"] == "approved" else "Director"
        action = ("发布批准已登记；由用户完成实际发布后，通过现有流程记录 release=published。面板不执行发布。"
                  if artifacts["release"]["status"] == "approved" else "先核对并验收发布前材料；实际发布完成后才可记录 Published。")
    elif current["id"] == "qa" and candidate["status"] == "EDIT_READY_FOR_USER_EXPORT":
        owner = "QA / Director"
        action = "复核 QA 报告并按现有流程登记 QA 验收；随后由你自行导出、完整观看本人音色候选片，完成 FINAL_REVIEW。"
    elif current["id"] == "recorder" and current["status"] in {"captured", "review_required"}:
        owner = "Recorder / Script"
        action = "复核 shot_plan.csv 与已有素材，验收并选用当前镜头；需要补录的部分再按全屏和隐私要求采集。"
    else:
        owner = current["owner"]
        actions = {
            "director": "确认本期选题、观众目标与素材权限，并完成 brief.md。",
            "research": "核对 facts.md / evidence_manifest.csv，补足或复核事实证据。",
            "FACT_REVIEW": "请人工审核 facts.md 与 evidence_manifest.csv；按现有流程登记两份产物批准。",
            "script": "根据已批准事实完成或复核 script_locked.md 与对应镜头表。",
            "SCRIPT_APPROVAL": "请人工审核完整旁白与镜头表，明确批准 SCRIPT_APPROVAL。",
            "recorder": "完成隐私检查，采集批准镜头；桌面按需全屏并检查 1920×1080 实际画面。",
            "visual": "完成或复核视觉素材、动画及符合频道风格的封面。",
            "voice": "复用批准的本人音色，以适中语速生成主音轨，再对齐真实音频 SRT。",
            "editor": "按最终主音轨调整画面，完成剪映工程或候选渲染，并核对素材链接。",
            "qa": "对当前候选工程/视频执行独立 QA，复核事实、隐私、音画与字幕并登记验收。",
            "FINAL_REVIEW": "等待用户完整观看当前候选片，并明确完成 FINAL_REVIEW。",
            "PUBLISH_APPROVAL": "等待用户对本次发布单独明确批准 PUBLISH_APPROVAL。",
            "published": "本期已有合法发布记录，可查看交付与归档；面板不执行发布。",
        }
        action = actions[current["id"]]
    if current["status"] == "validate_error":
        owner = "Director"
        action = "先处理 Studio Validate 的错误，再按 production_status.yaml 接续；面板不会自动修复或 sync。"
    elif current["status"] in {"obsolete", "rejected", "blocked"}:
        action = "先处理当前步骤的失效、拒绝或缺失交付；" + action
    elif current["status"] == "unknown":
        action = "状态或字段不完整，请先核对本期契约；" + action
    if validation["status"] == "STALE":
        action = "文件 metadata 已变化，先手动重新运行 Studio Validate；" + action
    breakdown = [{"id": x["id"], "label": x["label"], "weight": x["weight"],
                  "status": x["status"], "credit": CREDIT.get(x["status"], 0),
                  "points": round(x["weight"] * CREDIT.get(x["status"], 0), 2)} for x in steps if x["weight"]]
    obsolete = [k for k, v in artifacts.items() if v["status"] == "obsolete" or v["stale_parent"]]
    reviews = [k for k, v in artifacts.items() if v["status"] == "review_required"]
    blockers = [k for k, v in artifacts.items() if v["status"] in {"blocked", "rejected", "obsolete"} or not v["exists"] or v["stale_parent"]]
    if detail["restricted_paths"]:
        blockers.append("restricted_path")
    if validation["status"] in {"FAIL", "ERROR", "UNSAFE"}:
        blockers.append("studio_validate")
    if blockers or current["status"] in FAILURES:
        overall = "BLOCKED"
    elif published:
        overall = "PUBLISHED"
    elif validation["status"] == "STALE":
        overall = "REVIEW_REQUIRED"
    elif candidate["status"] == "EDIT_READY_FOR_USER_EXPORT":
        overall = "EDIT_READY_FOR_USER_EXPORT"
    elif current["kind"] == "human":
        overall = "WAITING_FOR_HUMAN_REVIEW"
    elif current["status"] == "review_required":
        overall = "REVIEW_REQUIRED"
    elif current["status"] == "unknown":
        overall = "UNKNOWN"
    else:
        overall = "READY_TO_CONTINUE"
    cards = []
    for role_id, english, chinese, names in ROLES:
        associated = [artifacts[n] for n in names]
        node = next(x for x in steps if x["id"] == role_id)
        delivery_history = [x for x in detail["history"] if x["artifact"] in names and x["to"] in {"approved", "selected", "rendered", "captured", "tested"}]
        execution = "有验收交付记录" if any(x["status"] in {"approved", "published"} for x in associated) else "有历史制作记录" if delivery_history else "有制作记录" if any(x["status"] in {"tested", "captured", "selected", "rendered"} for x in associated) else "未确认执行"
        if role_id == "qa" and candidate["qa_record_exists"] and execution == "未确认执行":
            execution = "有检查记录，待验收"
        last = max((x["at"] for x in delivery_history if x["at"]), default=None)
        cards.append({"id": "xichugeek-" + role_id, "name": english, "label": chinese,
                      "status": node["status"], "status_label": node["status_label"], "current": node["current"],
                      "execution": execution, "last_delivery": last,
                      "last_state_update": max((x["updated_at"] for x in associated if x["updated_at"]), default=None),
                      "outputs": [artifacts[n]["path"] for n in names],
                      "needs_redo": any(x["status"] in {"obsolete", "rejected"} or x["stale_parent"] for x in associated),
                      "needs_review": any(x["status"] == "review_required" for x in associated)})
    detail.update(pipeline=steps, team=cards, stage=current["id"], current_role=owner,
                  stage_label=current["label"], overall_status=overall, next_action=action,
                  progress=round(sum(x["points"] for x in breakdown)), progress_breakdown=breakdown,
                  blockers=list(dict.fromkeys(blockers)), obsolete=obsolete, review_required=reviews,
                  validate=validation, published=published, activity="由契约推导当前待处理岗位；没有监测机器人执行进程")
    detail.pop("history", None)
    return detail
