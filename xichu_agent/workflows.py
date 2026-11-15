"""流水线定义：每个阶段的输入准备、提示词、产物核验、模拟产物和交接规则。

产物以工作目录中的 result.json + 真实文件为准；聊天文字不算交付。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .executors import Job
from . import native, ticket_context

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"

# 阶段 → 岗位（群里的机器人）。具体执行账号由岗位的账号池在分派时决定。
STAGE_ROLE = {"triage": "pm", "produce": "video", "review": "pm", "site_draft": "site", "site_ops": "site"}
STAGE_LABEL = {"triage": "拆解需求", "produce": "视频制作", "review": "独立质检", "site_draft": "官网更新草稿",
               "site_ops": "官网运维草稿"}
FIRST_STAGE = {"pm": "triage", "video": "produce", "review": "review", "site": "site_ops"}
TRIAGE_ROUTES = ("video", "site", "review", "clarify")

STUDIO_TEXT_FILES = ["brief.md", "facts.md", "evidence_manifest.csv", "script_locked.md", "shot_plan.csv",
                     "asset_manifest.csv", "qa_report.md", "release_gate.md", "production_status.yaml",
                     "Production/youtube_upload_copy.md", "Production/transcript_public.md"]
SITE_TEMPLATE_FILES = ["article.html", "episode.json", "publish.php", "cover-source.json", "video.jsonld"]


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
    text = (PROMPTS / name).read_text(encoding="utf-8")
    for key, value in values.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def _ffmpeg_ok(path: str | None) -> bool:
    return bool(path) and Path(path).is_file()


def make_synthetic_video(ffmpeg: str, out: Path, seconds: int = 6) -> None:
    """合成素材：测试图案 + 1 kHz 正弦音，1920x1080 60fps 48 kHz。不含任何真实画面。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
           "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=60:duration={seconds}",
           "-f", "lavfi", "-i", f"sine=frequency=1000:sample_rate=48000:duration={seconds}",
           "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", "-shortest", str(out)]
    subprocess.run(cmd, check=True, capture_output=True, timeout=180,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def probe(ffprobe: str | None, path: Path) -> dict:
    if not _ffmpeg_ok(ffprobe):
        return {"checked": False, "reason": "未配置 ffprobe"}
    r = subprocess.run([ffprobe, "-v", "error", "-show_entries",
                        "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate",
                        "-of", "json", str(path)], capture_output=True, text=True, timeout=120,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode:
        return {"checked": True, "decodable": False, "error": r.stderr[-300:]}
    data = json.loads(r.stdout or "{}")
    return {"checked": True, "decodable": True, "duration": float(data.get("format", {}).get("duration", 0) or 0),
            "streams": data.get("streams", [])}


class Workflows:
    def __init__(self, cfg):
        self.cfg = cfg

    # ---------- 工作目录 ----------
    def workdir(self, ticket_id: str, stage: str, round_: int, attempt: int) -> Path:
        # Continuations/retries retain every previous delivery and log.
        d = self.cfg.work_dir / ticket_id / f"{stage}-r{round_}-a{attempt}-{uuid.uuid4().hex[:10]}"
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
        raw_request = ticket_context.full_request(ticket)
        pack = ticket_context.prepare(self.cfg, store, ticket, stage_row, workdir, raw_request)
        common = {"ticket_id": ticket["id"], "request": pack["request"], "round": stage_row["round"],
                  "workdir": str(workdir)}
        effort = (self.cfg.get("stage_reasoning_effort") or {}).get(name)
        if native.enabled(self.cfg, name):
            prompt, profile = native.prepare(self.cfg, store, ticket, stage_row, workdir, raw_request, pack)
            ticket_context.attach_profile(profile, pack["manifest"])
            if effort:
                profile["reasoning_effort"] = effort
            return Job(ticket["id"], name, stage_row["round"], workdir,
                       render("network_rules.md", {}) + pack["history_instructions"] + "\n\n" + prompt,
                       self.cfg.executor(stage_row["executor"]).get("timeout_sec", 3600), profile)
        getattr(self, f"_prep_{name}")(store, ticket, stage_row, workdir, common)
        prompt_name, profile = common.pop("_prompt"), common.pop("_profile")
        ticket_context.attach_profile(profile, pack["manifest"])
        if effort:
            profile["reasoning_effort"] = effort
        timeout = self.cfg.executor(stage_row["executor"]).get("timeout_sec", 3600)
        history_guide = ("\n本阶段为纯分派，当前请求及摘要已内嵌，直接返回JSON；无需shell或文件读写。必要历史查询才使用只读ticket_history。\n"
                         if name == "triage" else pack["history_instructions"])
        prompt = render("network_rules.md", {}) + history_guide + "\n\n" + render(prompt_name, common)
        return Job(ticket["id"], name, stage_row["round"], workdir, prompt, timeout, profile)

    def _prep_produce(self, store, ticket, stage_row, workdir, common):
        mode = self.cfg.get("video_mode", "synthetic")
        if stage_row["round"] > 1:
            prev = self.latest_ok_workdir(store, ticket["id"], "review")
            if prev and (prev / "output" / "review.md").exists():
                shutil.copy2(prev / "output" / "review.md", workdir / "inputs" / "review_feedback.md")
        ffmpeg = self.cfg.get("ffmpeg_path")
        common["ffmpeg"] = ffmpeg
        common["feedback"] = "inputs/review_feedback.md（上一轮审核意见，必须逐条处理）" \
            if (workdir / "inputs" / "review_feedback.md").exists() else "无"
        if mode == "studio":
            common["studio_root"] = self.cfg.get("studio_root")
            common["_prompt"] = "produce_studio.md"
            common["_profile"] = {"sandbox": "workspace-write", "add_dirs": [self.cfg.get("studio_root")],
                                  "ignore_user_config": False}
            return
        if not _ffmpeg_ok(ffmpeg):
            raise RuntimeError(f"找不到 ffmpeg：{ffmpeg}")
        make_synthetic_video(ffmpeg, workdir / "inputs" / "synthetic_source.mp4")
        (workdir / "inputs" / "SYNTHETIC.txt").write_text(
            "本目录素材由调度器用 ffmpeg testsrc2 + 正弦音合成，不含任何真实画面、声音或客户资料。\n",
            encoding="utf-8")
        common["_prompt"] = "produce_synthetic.md"
        common["_profile"] = {"sandbox": "workspace-write", "no_project_docs": True}

    def _copy_produce_outputs(self, store, ticket, workdir) -> None:
        src = self.latest_ok_workdir(store, ticket["id"], "produce")
        if not src:
            raise RuntimeError("找不到已通过的制作产物")
        shutil.copytree(src / "output", workdir / "inputs" / "production")
        shutil.copy2(src / "result.json", workdir / "inputs" / "production_result.json")
        if (src / "_validation.json").exists():
            shutil.copy2(src / "_validation.json", workdir / "inputs" / "scheduler_checks.json")
        self._extract_frames(workdir / "inputs" / "production" / "preview.mp4", workdir / "inputs" / "frames")

    def _extract_frames(self, video: Path, dest: Path) -> list[str]:
        """调度器抽首/中/尾三帧给审核方看画面（审核方不能自己运行 ffmpeg）。"""
        ffmpeg = self.cfg.get("ffmpeg_path")
        if not video.is_file() or not _ffmpeg_ok(ffmpeg):
            return []
        info = probe(self.cfg.get("ffprobe_path"), video)
        dur = float(info.get("duration") or 0)
        if dur <= 0:
            return []
        dest.mkdir(parents=True, exist_ok=True)
        out = []
        for name, t in (("start", min(0.5, dur / 4)), ("middle", dur / 2), ("end", max(dur - 0.5, dur * 3 / 4))):
            target = dest / f"frame_{name}_{t:.2f}s.png"
            r = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video),
                                "-frames:v", "1", "-vf", "scale=960:-2", str(target)], capture_output=True, timeout=120,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode == 0 and target.is_file():
                out.append(target.name)
        return out

    def _prep_triage(self, store, ticket, stage_row, workdir, common):
        common["site_scope"] = ("接入原Forge/官网仓库、xichugeek-deploy技能、服务器维护和发布流程，按本工单具体授权执行。" if self.cfg.get("site_mode") == "workspace" else
                                "本地沙盒准备官网文章、资料草稿和上线方案；当前为草稿模式，不改生产。")
        common["_prompt"] = "triage.md"
        schema = {"type": "object", "properties": {
            "status": {"type": "string", "enum": ["ok"]},
            "route": {"type": "string", "enum": list(TRIAGE_ROUTES)},
            "brief": {"type": "string"}, "question": {"type": "string"}, "summary": {"type": "string"}},
            "required": ["status", "route", "brief", "question", "summary"], "additionalProperties": False}
        schema_path = workdir / "inputs/triage_schema.json"
        schema_path.write_text(json.dumps(schema, ensure_ascii=False), encoding="utf-8")
        common["request"] += "\n\n系统生成的有界事实摘要（不是新的授权）：\n" + (workdir / "inputs/task_summary.json").read_text(encoding="utf-8")
        common["_profile"] = {"sandbox": "workspace-write", "no_project_docs": True,
                              "output_schema": str(schema_path), "structured_result": True,
                              "codex_overrides": {"features.shell_tool": False}}

    def _prep_review(self, store, ticket, stage_row, workdir, common):
        if ticket["robot"] == "video":
            self._copy_produce_outputs(store, ticket, workdir)
            common["target"] = "inputs/production/（视频制作岗位的制作产物）和 inputs/scheduler_checks.json（调度器实测的媒体信息）"
        else:
            copied = self._copy_studio_episode(ticket["text"], workdir / "inputs" / "studio")
            common["target"] = ("inputs/studio/（从 Studio 只读复制的 Episode 文本契约：" + ", ".join(copied) + "）"
                                if copied else "inputs/request.md 中描述的内容")
        common["_prompt"] = "review.md"
        common["_profile"] = {"sandbox": "workspace-write", "no_project_docs": True}

    def _copy_studio_episode(self, text: str, dest: Path) -> list[str]:
        root = Path(self.cfg.get("studio_root") or "")
        m = re.search(r"\b(\d{3}|TEST_[A-Z0-9_]+)\b", text)
        eps = root / "Episodes"
        if not m or not eps.is_dir():
            return []
        matches = sorted(p for p in eps.iterdir() if p.is_dir() and (p.name == m.group(1) or p.name.startswith(m.group(1) + "_")))
        if len(matches) != 1:
            return []
        copied = []
        for rel in STUDIO_TEXT_FILES:
            src = matches[0] / rel
            if src.is_file() and src.stat().st_size < 2_000_000:
                (dest / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dest / rel)
                copied.append(rel)
        (dest / "SOURCE.txt").write_text(f"只读复制自 {matches[0]}\n", encoding="utf-8")
        return copied

    def _copy_site_template(self, workdir: Path) -> str:
        tpl = self.cfg.get("site_template_episode", "018")
        src = Path(self.cfg.get("site_repo") or "") / "content" / "episodes" / tpl
        dest = workdir / "inputs" / "site_template" / tpl
        dest.mkdir(parents=True, exist_ok=True)
        for rel in SITE_TEMPLATE_FILES:
            if (src / rel).is_file():
                shutil.copy2(src / rel, dest / rel)
        if not any(dest.iterdir()):
            fixture = Path(__file__).resolve().parent.parent / "fixtures" / "site_template"
            shutil.copytree(fixture, dest, dirs_exist_ok=True)
            return f"{tpl}（官网仓库不可读，改用本地合成模板）"
        return tpl

    def _prep_site_draft(self, store, ticket, stage_row, workdir, common):
        tpl = self._copy_site_template(workdir)
        self._copy_produce_outputs(store, ticket, workdir)
        rv = self.latest_ok_workdir(store, ticket["id"], "review")
        if rv and (rv / "output" / "review.md").exists():
            shutil.copy2(rv / "output" / "review.md", workdir / "inputs" / "review.md")
        info = json.loads(ticket["youtube_info"] or "{}")
        (workdir / "inputs" / "published.json").write_text(json.dumps(info, ensure_ascii=False, indent=2),
                                                         encoding="utf-8")
        common.update({"template": tpl, "video_id": info.get("video_id"), "youtube_url": info.get("url"),
                       "episode_dir": f"draft/content/episodes/TEST_{ticket['id']}"})
        common["_prompt"] = "site_draft.md"
        common["_profile"] = {"sandbox": "workspace-write", "no_project_docs": True}

    def _prep_site_ops(self, store, ticket, stage_row, workdir, common):
        common["template"] = self._copy_site_template(workdir)
        common["_prompt"] = "site_ops.md"
        common["_profile"] = {"sandbox": "workspace-write", "no_project_docs": True}

    # ---------- 核验 ----------
    def validate(self, ticket, stage_row, workdir: Path) -> Validation:
        result_path = workdir / "result.json"
        if not result_path.is_file():
            return Validation(False, "缺少 result.json（执行器未按契约交付）")
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
            # A successful command normally has an empty stderr stream. It is
            # diagnostic evidence, unlike an empty video, report or receipt.
            empty_stderr = p.name.lower().endswith((".stderr.txt", ".stderr.log"))
            if root not in p.parents or not p.is_file() or (p.stat().st_size == 0 and not empty_stderr):
                v.ok, v.error = False, f"声明的产物不存在、为空或越界：{rel}"
                return v
            if p.stat().st_size == 0:
                v.facts.setdefault("empty_stderr_logs", []).append(rel)
            v.artifacts.append({"path": str(p), "rel": rel, "bytes": p.stat().st_size, "sha256": sha256(p)})
        if native.enabled(self.cfg, stage_row["name"]):
            native.validate(self.cfg, ticket, stage_row["name"], workdir, result, v)
        else:
            getattr(self, f"_check_{stage_row['name']}")(ticket, workdir, result, v)
        (workdir / "_validation.json").write_text(json.dumps(v.__dict__, ensure_ascii=False, indent=2),
                                                  encoding="utf-8")
        return v

    def _require(self, v: Validation, workdir: Path, rels: list[str]) -> bool:
        for rel in rels:
            p = workdir / rel
            if not p.is_file() or p.stat().st_size == 0:
                v.ok, v.error = False, f"缺少必需产物 {rel}"
                return False
        return True

    def _check_produce(self, ticket, workdir, result, v):
        if self.cfg.get("video_mode", "synthetic") == "studio":
            return
        if not self._require(v, workdir, ["output/script.md", "output/shot_plan.csv", "output/preview.mp4"]):
            return
        info = probe(self.cfg.get("ffprobe_path"), workdir / "output" / "preview.mp4")
        v.facts["preview_probe"] = info
        if not info.get("checked"):
            v.warnings.append("未能用 ffprobe 核验 preview.mp4")
        elif not info.get("decodable") or info.get("duration", 0) < 1:
            v.ok, v.error = False, "preview.mp4 无法解码或时长不足 1 秒"
            return
        try:
            with open(workdir / "output" / "shot_plan.csv", encoding="utf-8-sig") as f:
                rows = list(csv.DictReader(f))
            v.facts["shots"] = len(rows)
            if not rows:
                v.warnings.append("shot_plan.csv 没有镜头行")
        except (OSError, csv.Error) as exc:
            v.warnings.append(f"shot_plan.csv 读取失败：{exc}")

    def _check_triage(self, ticket, workdir, result, v):
        route = str(result.get("route", "")).lower()
        if route not in TRIAGE_ROUTES:
            v.ok, v.error = False, f"项目经理的分派结果必须是 {'/'.join(TRIAGE_ROUTES)}，实际为 {route or '空'}"
            return
        brief = str(result.get("brief", "")).strip()
        if route != "clarify" and not brief:
            v.ok, v.error = False, "项目经理没有写任务说明 brief"
            return
        v.facts.update({"route": route, "brief": brief[:1500], "question": str(result.get("question", ""))[:500]})

    def _check_review(self, ticket, workdir, result, v):
        if not self._require(v, workdir, ["output/review.md"]):
            return
        verdict = str(result.get("verdict", "")).upper()
        if verdict not in ("PASS", "REJECT"):
            v.ok, v.error = False, f"审核结论必须是 PASS 或 REJECT，实际为 {verdict or '空'}"
            return
        v.verdict, v.reasons = verdict, [str(r) for r in (result.get("reasons") or [])][:10]

    def _check_site_draft(self, ticket, workdir, result, v):
        info = json.loads(ticket["youtube_info"] or "{}")
        rel_ep = f"draft/content/episodes/TEST_{ticket['id']}"
        ep = workdir / rel_ep
        if not self._require(v, workdir, [f"{rel_ep}/article.html", f"{rel_ep}/episode.json"]):
            return
        try:
            meta = json.loads((ep / "episode.json").read_text(encoding="utf-8-sig"))
        except ValueError as exc:
            v.ok, v.error = False, f"episode.json 不是合法 JSON：{exc}"
            return
        vid = info.get("video_id")
        if meta.get("video_id") != vid:
            v.ok, v.error = False, f"episode.json 的 video_id={meta.get('video_id')} 与确认链接 {vid} 不一致"
            return
        html = (ep / "article.html").read_text(encoding="utf-8", errors="replace")
        if vid not in html:
            v.ok, v.error = False, "article.html 没有嵌入确认的视频"
            return
        lowered = html.lower() + json.dumps(meta).lower()
        for bad in ("wp-config", "password", "ssh ", "cloudcone"):
            if bad in lowered:
                v.ok, v.error = False, f"草稿含不应出现的内容：{bad}"
                return
        v.facts.update({"draft_dir": str(ep), "title": meta.get("title"), "video_id": vid})

    def _check_site_ops(self, ticket, workdir, result, v):
        draft = workdir / "draft"
        if not draft.is_dir() or not any(p.is_file() for p in draft.rglob("*")):
            v.ok, v.error = False, "draft/ 目录没有任何草稿文件"

    # ---------- 模拟产物（不调用模型；结果在回复中标【模拟】） ----------
    def simulate(self, job: Job) -> None:
        w, name = job.workdir, job.stage
        def write_result(**kw):
            kw.setdefault("status", "ok")
            kw["simulated"] = True
            (w / "result.json").write_text(json.dumps(kw, ensure_ascii=False, indent=2), encoding="utf-8")
        if name == "triage":
            text = (w / "inputs" / "request.md").read_text(encoding="utf-8")
            route = "video" if "视频" in text else "site" if "官网" in text else "clarify" if "不清楚" in text else "review"
            write_result(summary=f"【模拟】拆解：交给 {route}", route=route, brief=f"【模拟】{text.splitlines()[-1][:200]}",
                         question="【模拟】请说明要做视频还是官网" if route == "clarify" else "")
            return
        if name == "produce":
            src = w / "inputs" / "synthetic_source.mp4"
            shutil.copy2(src, w / "output" / "preview.mp4")
            (w / "output" / "script.md").write_text(f"# 【模拟】脚本 {job.ticket_id} 第 {job.round} 轮\n\n合成测试旁白。\n",
                                                    encoding="utf-8")
            (w / "output" / "shot_plan.csv").write_text(
                "shot_id,script_id,mode,status,required,file,evidence_ids,privacy,notes\n"
                "S01,L01,generated_visual,selected,true,output/preview.mp4,,synthetic,模拟\n", encoding="utf-8")
            write_result(summary="【模拟】用合成素材生成 6 秒预览",
                         artifacts=["output/preview.mp4", "output/script.md", "output/shot_plan.csv"])
        elif name == "review":
            reject = "模拟驳回" in (w / "inputs" / "request.md").read_text(encoding="utf-8") and job.round == 1
            (w / "output" / "review.md").write_text(
                f"# 【模拟】审核 {job.ticket_id}\n\n结论：{'REJECT' if reject else 'PASS'}\n", encoding="utf-8")
            write_result(summary="【模拟】审核完成", verdict="REJECT" if reject else "PASS",
                         reasons=["【模拟】字幕安全区需调整"] if reject else [], artifacts=["output/review.md"])
        elif name == "site_draft":
            info = json.loads((w / "inputs" / "published.json").read_text(encoding="utf-8"))
            ep = w / "draft" / "content" / "episodes" / f"TEST_{job.ticket_id}"
            ep.mkdir(parents=True)
            vid = info["video_id"]
            (ep / "episode.json").write_text(json.dumps({"episode": f"TEST_{job.ticket_id}", "title": "【模拟】草稿",
                                                         "video_id": vid, "video_url": info["url"]},
                                                        ensure_ascii=False, indent=2), encoding="utf-8")
            (ep / "article.html").write_text(
                f'<p>【模拟】草稿</p>\n<iframe src="https://www.youtube-nocookie.com/embed/{vid}"></iframe>\n',
                encoding="utf-8")
            rel = ep.relative_to(w).as_posix()
            write_result(summary="【模拟】官网草稿", artifacts=[f"{rel}/episode.json", f"{rel}/article.html"])
        elif name == "site_ops":
            (w / "draft").mkdir(exist_ok=True)
            (w / "draft" / "PLAN.md").write_text("# 【模拟】官网运维草稿\n", encoding="utf-8")
            write_result(summary="【模拟】运维草稿", artifacts=["draft/PLAN.md"])
