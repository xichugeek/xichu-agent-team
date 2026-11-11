"""流水线定义：每个阶段的输入准备、提示词、产物核验、模拟产物和交接规则。

阶段只有三种：
- triage：项目经理拆解需求，决定派给哪个员工（或追问你）
- work：员工干活（岗位由工单决定，提示词在 config.json 的 roles.<岗位>.prompt）
- review：项目经理独立质检，PASS 结束，REJECT 自动退回返工

产物以工作目录中的 result.json + 真实文件为准；聊天文字不算交付。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .executors import Job

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

STAGE_LABEL = {"triage": "拆解需求", "work": "执行任务", "review": "独立质检"}


@dataclass
class Validation:
    ok: bool
    error: str | None = None
    summary: str = ""
    verdict: str | None = None
    reasons: list = field(default_factory=list)
    artifacts: list = field(default_factory=list)   # [{path, bytes, sha256}]
    warnings: list = field(default_factory=list)
    facts: dict = field(default_factory=dict)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def render(name: str, values: dict) -> str:
    path = PROMPTS / name
    if not path.is_file():
        path = PROMPTS / "work_generic.md"
    text = path.read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


class Workflows:
    def __init__(self, cfg):
        self.cfg = cfg

    def first_stage(self, kind: str) -> tuple[str, str]:
        """工单类型 → (首个阶段, 岗位)。"""
        if kind == "pm":
            return "triage", "pm"
        if kind == "review":
            return "review", "pm"
        return "work", kind

    def triage_routes(self) -> tuple:
        return (*self.cfg.workers, "review", "clarify")

    # ---------- 工作目录 ----------
    def workdir(self, ticket_id: str, stage: str, round_: int, attempt: int) -> Path:
        d = self.cfg.work_dir / ticket_id / f"{stage}-r{round_}-a{attempt}"
        if d.exists():
            shutil.rmtree(d)
        (d / "inputs").mkdir(parents=True)
        (d / "output").mkdir()
        return d

    def latest_ok_workdir(self, store, ticket_id: str, stage: str) -> Path | None:
        row = store.conn().execute(
            "SELECT workdir FROM stages WHERE ticket_id=? AND name=? AND status='ok' ORDER BY id DESC LIMIT 1",
            (ticket_id, stage)).fetchone()
        return Path(row["workdir"]) if row and row["workdir"] else None

    # ---------- 准备 ----------
    def prepare(self, store, ticket, stage_row, workdir: Path) -> Job:
        name = stage_row["name"]
        brief = ticket["brief"] if "brief" in ticket.keys() and ticket["brief"] else None
        request = ticket["text"] + (f"\n\n项目经理整理的任务说明：\n{brief}" if brief else "")
        common = {"ticket_id": ticket["id"], "request": request, "round": stage_row["round"],
                  "workdir": str(workdir)}
        (workdir / "inputs" / "request.md").write_text(
            f"# 工单 {ticket['id']}\n\n{request}\n", encoding="utf-8")
        getattr(self, f"_prep_{name}")(store, ticket, stage_row, workdir, common)
        prompt_name, profile = common.pop("_prompt"), common.pop("_profile")
        timeout = self.cfg.executor(stage_row["executor"]).get("timeout_sec", 3600)
        return Job(ticket["id"], name, stage_row["round"], workdir, render(prompt_name, common), timeout, profile)

    def _prep_triage(self, store, ticket, stage_row, workdir, common):
        lines = [f"- `{r}` —— **{self.cfg.role(r)['label']}**：{self.cfg.role(r).get('duty', '')}"
                 for r in self.cfg.workers]
        common["roles"] = "\n".join(lines)
        common["routes"] = " | ".join(self.triage_routes())
        common["_prompt"] = "triage.md"
        common["_profile"] = {"allowed_tools": ["Read", "Write"]}

    def _prep_work(self, store, ticket, stage_row, workdir, common):
        role = stage_row["role"]
        rconf = self.cfg.role(role)
        if stage_row["round"] > 1:
            prev = self.latest_ok_workdir(store, ticket["id"], "review")
            if prev and (prev / "output" / "review.md").exists():
                shutil.copy2(prev / "output" / "review.md", workdir / "inputs" / "review_feedback.md")
        common["feedback"] = "inputs/review_feedback.md（上一轮质检意见，必须逐条处理）" \
            if (workdir / "inputs" / "review_feedback.md").exists() else "无"
        common["role_label"] = rconf.get("label", role)
        common["duty"] = rconf.get("duty", "")
        project = rconf.get("project_dir")
        common["project_dir"] = project or "（未配置，只在当前工作目录内工作）"
        profile = {"sandbox": "workspace-write", "no_project_docs": True}
        if project:
            if not Path(project).is_dir():
                raise RuntimeError(f"{common['role_label']} 的 project_dir 不存在：{project}")
            profile["add_dirs"] = [project]
        common["_prompt"] = rconf.get("prompt") or "work_generic.md"
        common["_profile"] = profile

    def _prep_review(self, store, ticket, stage_row, workdir, common):
        src = self.latest_ok_workdir(store, ticket["id"], "work")
        if ticket["robot"] in self.cfg.workers and src:
            shutil.copytree(src / "output", workdir / "inputs" / "work", dirs_exist_ok=True)
            shutil.copy2(src / "result.json", workdir / "inputs" / "work_result.json")
            label = self.cfg.role(ticket["robot"])["label"]
            common["target"] = f"inputs/work/（{label} 的交付）和 inputs/work_result.json（它自己写的说明，不要直接采信）"
        else:
            common["target"] = "inputs/request.md 中描述的内容"
        common["_prompt"] = "review.md"
        common["_profile"] = {}

    # ---------- 核验 ----------
    def validate(self, ticket, stage_row, workdir: Path) -> Validation:
        result_path = workdir / "result.json"
        if not result_path.is_file():
            return Validation(False, "缺少 result.json（执行器未按约定交付）")
        try:
            result = json.loads(result_path.read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            return Validation(False, f"result.json 不是合法 JSON：{exc}")
        v = Validation(True, summary=str(result.get("summary", ""))[:800])
        if result.get("status") != "ok":
            v.ok, v.error = False, "执行器报告失败：" + "; ".join(map(str, result.get("reasons") or [v.summary]))[:800]
            return v
        root = workdir.resolve()
        for rel in result.get("artifacts") or []:
            p = (workdir / rel).resolve()
            if root not in p.parents or not p.is_file() or p.stat().st_size == 0:
                v.ok, v.error = False, f"声明的产物不存在、为空或不在工作目录内：{rel}"
                return v
            v.artifacts.append({"path": str(p), "rel": rel, "bytes": p.stat().st_size, "sha256": sha256(p)})
        getattr(self, f"_check_{stage_row['name']}")(ticket, workdir, result, v)
        (workdir / "_validation.json").write_text(json.dumps(v.__dict__, ensure_ascii=False, indent=2),
                                                  encoding="utf-8")
        return v

    def _check_triage(self, ticket, workdir, result, v):
        route = str(result.get("route", "")).lower()
        routes = self.triage_routes()
        if route not in routes:
            v.ok, v.error = False, f"项目经理的分派结果必须是 {'/'.join(routes)}，实际为 {route or '空'}"
            return
        brief = str(result.get("brief", "")).strip()
        if route != "clarify" and not brief:
            v.ok, v.error = False, "项目经理没有写任务说明 brief"
            return
        v.facts.update({"route": route, "brief": brief[:1500], "question": str(result.get("question", ""))[:500]})

    def _check_work(self, ticket, workdir, result, v):
        if not v.artifacts:
            v.ok, v.error = False, "员工没有交付任何产物（result.json 的 artifacts 为空）"

    def _check_review(self, ticket, workdir, result, v):
        p = workdir / "output" / "review.md"
        if not p.is_file() or p.stat().st_size == 0:
            v.ok, v.error = False, "缺少质检报告 output/review.md"
            return
        verdict = str(result.get("verdict", "")).upper()
        if verdict not in ("PASS", "REJECT"):
            v.ok, v.error = False, f"质检结论必须是 PASS 或 REJECT，实际为 {verdict or '空'}"
            return
        v.verdict, v.reasons = verdict, [str(r) for r in (result.get("reasons") or [])][:10]

    # ---------- 模拟产物（不调用模型；结果在回复中标【模拟】） ----------
    def simulate(self, job: Job) -> None:
        w, name = job.workdir, job.stage

        def write_result(**kw):
            kw.setdefault("status", "ok")
            kw["simulated"] = True
            (w / "result.json").write_text(json.dumps(kw, ensure_ascii=False, indent=2), encoding="utf-8")

        text = (w / "inputs" / "request.md").read_text(encoding="utf-8")
        if name == "triage":
            route = "clarify" if "不清楚" in text else None
            for r in self.cfg.workers:
                names = [self.cfg.role(r).get("label", r), *(self.cfg.role(r).get("aliases") or [])]
                if not route and any(n and n.split("(")[0] in text for n in names):
                    route = r
            route = route or self.cfg.workers[0]
            write_result(summary=f"【模拟】拆解：交给 {route}", route=route, brief=f"【模拟】{text.splitlines()[-1][:200]}",
                         question="【模拟】请说清楚要做什么" if route == "clarify" else "")
        elif name == "work":
            (w / "output" / "result.md").write_text(f"# 【模拟】{job.ticket_id} 第 {job.round} 轮交付\n\n{text}\n",
                                                    encoding="utf-8")
            (w / "progress.log").write_text("[开始] 读需求\n[完成] 写出交付\n", encoding="utf-8")
            write_result(summary="【模拟】完成", artifacts=["output/result.md"])
        elif name == "review":
            reject = "模拟驳回" in text and job.round == 1
            (w / "output" / "review.md").write_text(
                f"# 【模拟】质检 {job.ticket_id}\n\n结论：{'REJECT' if reject else 'PASS'}\n", encoding="utf-8")
            write_result(summary="【模拟】质检完成", verdict="REJECT" if reject else "PASS",
                         reasons=["【模拟】第二段需要补充数据来源"] if reject else [], artifacts=["output/review.md"])
