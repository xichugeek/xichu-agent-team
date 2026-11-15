"""Bounded, allowlisted metadata reads; no production writes or media hashing."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sys
from fractions import Fraction
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import yaml

CONTRACTS = {
    "brief": "brief.md", "facts": "facts.md", "evidence": "evidence_manifest.csv",
    "script": "script_locked.md", "shots": "shot_plan.csv", "assets": "asset_manifest.csv",
    "voice": "voice_manifest.csv", "captions": "captions.srt", "timeline": "timeline_manifest.json",
    "render": "render_report.json", "qa": "qa_report.md", "release": "release_gate.md",
}
FORMAL_GATES = ("SCRIPT_APPROVAL", "FINAL_REVIEW", "PUBLISH_APPROVAL")
STATES = {"planned", "blocked", "ready", "tested", "captured", "selected", "rendered",
          "review_required", "approved", "rejected", "obsolete", "published"}
EPISODE_REF = re.compile(r"(?:\d{3}(?:_[A-Za-z0-9_-]+)?|TEST_[A-Z0-9_]+)\Z")
SAFE_SUFFIXES = {".yaml", ".yml", ".json", ".csv", ".md", ".srt", ".txt", ".mp4",
                 ".webm", ".mkv", ".mov", ".avi", ".wav", ".mp3", ".aac", ".m4a",
                 ".flac", ".ogg", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".zip",
                 ".html", ".css", ".ts", ".tsx", ".js", ".py", ".exe"}
PRIVATE_PART = re.compile(r"(?:^|[_. -])(?:secret|secrets|token|tokens|credential|credentials|password|passwords|private|account|accounts|ssh|keys)(?:$|[_. -])", re.I)
MAX_DOCUMENT = 2 * 1024 * 1024
MAX_ROWS = 3000


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def timestamp(value) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def number(value, minimum=0, maximum=10**9):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and minimum <= value <= maximum:
        return value
    return None


def reported_number(value):
    if isinstance(value, str) and re.fullmatch(r"\d+(?:\.\d+)?(?:/\d+)?", value):
        try:
            return number(float(Fraction(value)))
        except (ValueError, ZeroDivisionError):
            return None
    return number(value)


def safe_relative(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 500:
        return None
    value = value.replace("\\", "/")
    p = PurePosixPath(value)
    if p.is_absolute() or any(c in value for c in (":", "\x00", "\r", "\n")):
        return None
    if any(part in {".", ".."} or part.startswith(".") or PRIVATE_PART.search(part)
           or part.lower() in {"localtts", "profiles", "id_rsa", "id_ed25519"} for part in p.parts):
        return None
    return p.as_posix()


def checked_path(base: Path, relative: object, *, directory=False) -> Path | None:
    rel = safe_relative(relative)
    if rel is None or (not directory and PurePosixPath(rel).suffix.lower() not in SAFE_SUFFIXES):
        return None
    try:
        target = base / rel
        # Reject links/junctions rather than following them into history or private folders.
        for ancestor in [target, *target.parents]:
            if ancestor == base.parent:
                break
            if ancestor.is_symlink() or (hasattr(ancestor, "is_junction") and ancestor.is_junction()):
                return None
        resolved = target.resolve()
        if not resolved.is_relative_to(base.resolve()):
            return None
        return target
    except (OSError, ValueError):
        return None


class Snapshot:
    def __init__(self, root: Path, episode: Path):
        self.root, self.episode = root, episode
        self.warnings: list[str] = []
        self.records: dict[str, tuple] = {}
        self.unsafe = False

    def metadata(self, relative: object, *, directory=False, base: Path | None = None) -> dict:
        origin = base or self.episode
        path = checked_path(origin, relative, directory=directory)
        if path is None:
            self.unsafe = True
            return {"path": "REDACTED", "exists": None, "status": "UNKNOWN", "restricted": True,
                    "modified_at": None, "size": None}
        rel = safe_relative(relative)
        key = ("studio/" if origin == self.root else "episode/") + rel
        try:
            exists = path.is_dir() if directory else path.is_file()
            stat = path.stat() if exists else None
        except OSError:
            exists, stat = False, None
        self.records[key] = (exists, stat.st_size if stat else None, stat.st_mtime_ns if stat else None)
        return {"path": rel, "exists": exists, "status": "FOUND" if exists else "MISSING",
                "restricted": False, "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat() if stat else None,
                "size": stat.st_size if stat and not directory else None}

    def document(self, relative: str, kind="json", *, base: Path | None = None) -> dict:
        meta = self.metadata(relative, base=base)
        if not meta["exists"]:
            return {}
        if meta["size"] > MAX_DOCUMENT:
            self.warnings.append(f"{relative} 超过状态文档读取上限")
            return {}
        path = checked_path(base or self.episode, relative)
        try:
            with path.open(encoding="utf-8-sig") as stream:
                body = stream.read(MAX_DOCUMENT + 1)
            if len(body) > MAX_DOCUMENT:
                raise ValueError("Document too large")
            data = yaml.safe_load(body) if kind == "yaml" else json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("Expected a mapping")
            return data
        except (OSError, UnicodeError, ValueError, yaml.YAMLError, RecursionError):
            self.warnings.append(f"{relative} 无法解析，相关字段显示 UNKNOWN")
            return {}

    def rows(self, relative: str) -> list[dict]:
        meta = self.metadata(relative)
        if not meta["exists"] or meta["size"] > MAX_DOCUMENT:
            return []
        try:
            path = checked_path(self.episode, relative)
            with path.open(encoding="utf-8-sig", newline="") as stream:
                body = stream.read(MAX_DOCUMENT + 1)
            if len(body) > MAX_DOCUMENT:
                raise ValueError("CSV too large")
            rows = list(csv.DictReader(io.StringIO(body)))
            if len(rows) > MAX_ROWS:
                raise ValueError("Too many rows")
            return rows
        except (OSError, UnicodeError, ValueError, csv.Error):
            self.warnings.append(f"{relative} 无法解析，素材统计显示 UNKNOWN")
            return []

    def fingerprint(self) -> str:
        # Hash only this small metadata map, never the referenced media bytes.
        return hashlib.sha256(json.dumps(self.records, sort_keys=True).encode()).hexdigest()


def mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def state(value) -> str:
    return value if isinstance(value, str) and value in STATES else "unknown"


class StatusReader:
    def __init__(self, root: Path):
        self.root = root.resolve()
        # External draft directories are metadata-only and explicitly allowlisted.
        self.draft_roots = [Path("D:/Program Files/JianyingPro Drafts")]

    def episode_dirs(self) -> list[Path]:
        base = self.root / "Episodes"
        if not base.is_dir():
            return []
        return sorted((p for p in base.iterdir() if EPISODE_REF.fullmatch(p.name)
                       and checked_path(base, p.name, directory=True) and p.is_dir()), key=lambda p: p.name, reverse=True)

    def resolve(self, ref: str) -> Path:
        if not EPISODE_REF.fullmatch(ref):
            raise ValueError("Invalid Episode reference")
        directories = self.episode_dirs()
        matches = [p for p in directories if p.name == ref]
        if not matches and re.fullmatch(r"\d{3}", ref):
            matches = [p for p in directories if p.name.startswith(ref + "_")]
        if len(matches) != 1:
            raise FileNotFoundError("Episode not found or ambiguous")
        return matches[0]

    def _draft(self, value: object) -> dict:
        result = {"exists": None, "status": "UNKNOWN", "modified_at": None, "size": None, "path": None}
        if not isinstance(value, str) or len(value) > 500:
            return result
        candidate = Path(value)
        for allowed in self.draft_roots:
            try:
                resolved, base = candidate.resolve(), allowed.resolve()
                if resolved.parent != base or safe_relative(resolved.name) is None:
                    continue
                if not checked_path(allowed, resolved.name, directory=True):
                    continue
                exists = resolved.is_dir()
                result.update(exists=exists, status="FOUND" if exists else "MISSING", path=str(resolved),
                              modified_at=datetime.fromtimestamp(resolved.stat().st_mtime, timezone.utc).isoformat() if exists else None)
                return result
            except (OSError, ValueError):
                continue
        return result

    def _delivery(self, snap: Snapshot, timeline: dict) -> tuple[dict, str | None]:
        candidates = ["delivery_scope.json", "Edit/Jianying/delivery_scope.json"]
        parent = checked_path(snap.episode, "Edit/Jianying", directory=True)
        if parent and parent.is_dir():
            for child in list(parent.iterdir())[:100]:
                rel = f"Edit/Jianying/{child.name}"
                checked = checked_path(snap.episode, rel, directory=True)
                if checked and checked.is_dir():
                    candidates.append(rel + "/delivery_scope.json")
        found = []
        for rel in candidates:
            meta = snap.metadata(rel)
            if meta["exists"]:
                data = snap.document(rel)
                if data:
                    matches = data.get("native_draft_name") == timeline.get("native_draft_name") and bool(timeline.get("native_draft_name"))
                    found.append((matches, meta["modified_at"] or "", data, rel))
        if not found:
            return {}, None
        found.sort(key=lambda row: (row[0], row[1]), reverse=True)
        return found[0][2], found[0][3]

    def read(self, ref: str) -> dict:
        ep = self.resolve(ref)
        snap = Snapshot(self.root, ep)
        config = snap.document("studio.config.yaml", "yaml", base=self.root)
        snap.metadata("scripts/studio.py", base=self.root)
        data = snap.document("production_status.yaml", "yaml")
        artifacts = mapping(data.get("artifacts"))
        declared_paths = mapping(config.get("paths"))
        deliveries = []
        contract_states = {}
        for name, default in CONTRACTS.items():
            item = mapping(artifacts.get(name))
            # Configured contract paths must remain safe; never fall back to arbitrary status paths.
            rel = declared_paths.get(name, default)
            meta = snap.metadata(rel)
            effective = state(item.get("status"))
            dependencies = mapping(config.get("dependencies"))
            parents = dependencies.get(name, [])
            parents = parents if isinstance(parents, list) else []
            stale = any(mapping(item.get("input_hashes")).get(p) != mapping(artifacts.get(p)).get("sha256")
                        for p in parents if isinstance(p, str) and p in CONTRACTS)
            blocked_by = [p for p in parents if isinstance(p, str) and p in CONTRACTS and state(mapping(artifacts.get(p)).get("status")) != "approved"]
            contract_states[name] = {"status": effective, "path": meta["path"], "exists": meta["exists"],
                                     "updated_at": timestamp(item.get("updated_at")), "stale_parent": stale,
                                     "blocked_by": blocked_by}
            deliveries.append({"id": name, "label": name.title(), **meta, "status": effective.upper(),
                               "source": "production_status.yaml", "state_updated_at": timestamp(item.get("updated_at"))})
            if not meta["exists"]:
                snap.warnings.append(f"{name} 契约文件缺失或受限")
            if item.get("path") != rel:
                snap.warnings.append(f"{name} 状态路径与配置不一致")
        # Only these state/report documents are parsed. No markdown, script body, media or voice profile reads.
        timeline = snap.document(CONTRACTS["timeline"])
        render = snap.document(CONTRACTS["render"])
        voice = snap.document("Voice/voice_report.json")
        cover = snap.document("Assets/Cover/cover_manifest.json")
        delivery, delivery_path = self._delivery(snap, timeline)
        native_qa_rel = safe_relative(delivery.get("native_qa") or timeline.get("native_qa"))
        native_qa_allowed = bool(native_qa_rel and re.fullmatch(r"Reviews/jianying[\w-]*qa[\w-]*\.json", native_qa_rel))
        native_qa = snap.document(native_qa_rel) if native_qa_allowed else {}
        shot_rows = snap.rows(CONTRACTS["shots"])
        for csv_name in ("shots", "assets", "voice"):
            rows = shot_rows if csv_name == "shots" else snap.rows(CONTRACTS[csv_name])
            for row in rows:
                if row.get("file"):
                    snap.metadata(row["file"])
        timeline_shots = timeline.get("shots") if isinstance(timeline.get("shots"), list) else []
        for shot in timeline_shots[:MAX_ROWS]:
            if isinstance(shot, dict) and shot.get("file"):
                snap.metadata(shot["file"])

        gates = {}
        for name in FORMAL_GATES:
            item = mapping(mapping(data.get("gates")).get(name))
            status = item.get("status") if item.get("status") in ("pending", "approved") else "unknown"
            evidence = snap.metadata(item["evidence"]) if item.get("evidence") else None
            gates[name] = {"status": status, "updated_at": timestamp(item.get("at")),
                           "evidence_exists": evidence["exists"] if evidence else None,
                           "source": "production_status.yaml · human attestation", "formal": True}
        facts_states = [contract_states[x]["status"] for x in ("facts", "evidence")]
        facts_exist = all(contract_states[x]["exists"] for x in ("facts", "evidence"))
        if facts_states == ["approved", "approved"] and facts_exist:
            fact_state = "approved"
        elif any(x in {"blocked", "obsolete", "rejected"} for x in facts_states):
            fact_state = "blocked"
        elif "unknown" in facts_states or not facts_exist:
            fact_state = "unknown"
        else:
            fact_state = "pending"
        gates["FACT_REVIEW"] = {"status": fact_state, "source": "由 facts + evidence 产物审批状态推导；非新增 Gate",
                               "formal": False, "updated_at": None, "evidence_exists": facts_exist}

        master_rel = timeline.get("main_audio") or delivery.get("master_audio") or voice.get("master") or "Voice/master_audio.wav"
        master = snap.metadata(master_rel)
        captions = snap.metadata(timeline.get("captions") or "captions.srt")
        cover_file = snap.metadata(cover.get("selected") or timeline.get("thumbnail") or delivery.get("thumbnail") or "Assets/Cover/cover.png")
        draft = self._draft(delivery.get("native_draft_path"))
        backup_rel = delivery.get("native_backup")
        backup = snap.metadata(backup_rel) if backup_rel else {"exists": None, "path": None, "status": "UNKNOWN", "modified_at": None, "size": None}
        native_recorded = delivery.get("native_saved_reopened") is True and native_qa.get("status") == "SAVED_AND_REOPENED"
        native_consistent = bool(delivery.get("native_draft_name")) and native_qa.get("draft_name") == delivery.get("native_draft_name")
        if timeline.get("native_draft_name") and timeline["native_draft_name"] != delivery.get("native_draft_name"):
            native_consistent = False
            snap.warnings.append("时间线与剪映交付记录不一致，需人工核对")
        native_ready = (native_recorded and native_consistent and draft["exists"] is True
                        and master["exists"] is True and captions["exists"] is True
                        and gates["SCRIPT_APPROVAL"]["status"] == "approved"
                        and all(contract_states[k]["status"] == "approved" and not contract_states[k]["stale_parent"]
                                for k in ("shots", "assets", "voice", "captions", "timeline")))
        # A report output is required. Never scan Export/*.mp4 and call an old silent preview the final candidate.
        render_output = snap.metadata(render.get("output")) if render.get("output") else {"exists": False, "path": None, "status": "NOT_RENDERED", "modified_at": None, "size": None}
        decoded = render.get("full_decode") is True or mapping(render.get("full_decode")).get("ok") is True
        video_ready = (render.get("status") != "not_rendered" and render_output["exists"] is True
                       and contract_states["render"]["status"] in {"rendered", "approved", "published"} and decoded)
        user_export = delivery.get("status") == "EDIT_READY_FOR_USER_EXPORT" or timeline.get("candidate_status") == "EDIT_READY_FOR_USER_EXPORT"
        if user_export and native_ready:
            candidate_state = "EDIT_READY_FOR_USER_EXPORT"
        elif video_ready:
            candidate_state = "RENDERED_CANDIDATE"
        elif user_export:
            candidate_state = "EDIT_RECORD_REQUIRES_VERIFICATION"
        else:
            candidate_state = "NOT_READY" if data else "UNKNOWN"
        if snap.unsafe:
            snap.warnings.append("检测到受限路径，已阻止读取；Validate 前需核对契约")

        required = [row for row in shot_rows if row.get("required", "true").lower() == "true"]
        selected = [row for row in required if row.get("status") in {"selected", "rendered", "approved"}]
        shot_files = sum(snap.metadata(row.get("file"))["exists"] is True for row in selected if row.get("file"))
        required_files = sum(snap.metadata(row.get("file"))["exists"] is True for row in required if row.get("file"))
        if native_ready and (not required or shot_files != len(required)):
            native_ready = False
            if candidate_state == "EDIT_READY_FOR_USER_EXPORT":
                candidate_state = "EDIT_RECORD_REQUIRES_VERIFICATION"
        picture = mapping(delivery.get("picture")) or mapping(native_qa.get("picture"))
        probe_data = mapping(render.get("probe"))
        probe_streams = probe_data.get("video") or probe_data.get("streams", [])
        probe_picture = next((x for x in probe_streams if isinstance(x, dict) and x.get("codec_type") == "video"), {}) if isinstance(probe_streams, list) else {}
        stats = {"shots": len(timeline_shots) or number(delivery.get("source_clip_count")),
                 "required_shots": len(required) if shot_rows else None,
                 "selected_shot_files": shot_files if shot_rows else None,
                 "required_shot_files": required_files if shot_rows else None,
                 "shot_row_status": state("captured") if required and all(row.get("status") == "captured" for row in required) else "unknown",
                 "width": number(picture.get("width") or probe_picture.get("width")),
                 "height": number(picture.get("height") or probe_picture.get("height")),
                 "fps": reported_number(picture.get("fps") or timeline.get("fps") or probe_picture.get("avg_frame_rate")),
                 "duration": reported_number(timeline.get("duration_seconds") or delivery.get("duration") or voice.get("duration") or mapping(probe_data.get("format")).get("duration")),
                 "captions": number(mapping(native_qa.get("captions")).get("count")),
                 "audio_tracks": number(mapping(native_qa.get("audio")).get("tracks")),
                 "cover_version": cover.get("version") if isinstance(cover.get("version"), str) and re.fullmatch(r"V\d+[A-Z0-9_-]{0,60}", cover["version"]) else "UNKNOWN",
                 "source": "当前时间线及报告记录，未在普通刷新中重新解码"}
        for label, key, meta in [
            ("Raw 素材目录", "raw_dir", snap.metadata("Raw", directory=True)),
            ("Assets 视觉目录", "assets_dir", snap.metadata("Assets", directory=True)),
            ("Cover 封面", "cover", cover_file), ("本人音色主音轨", "master_audio", master),
            ("可编辑 SRT", "final_srt", captions), ("剪映工程（仅目录检查）", "jianying", draft),
            ("剪映备份", "native_backup", backup), ("当前渲染报告引用输出", "candidate_video", render_output),
        ]:
            deliveries.append({"id": key, "label": label, **meta, "source": "文件 metadata / 交付记录"})
        if delivery_path:
            deliveries.append({"id": "delivery_scope", "label": "剪映交付记录", **snap.metadata(delivery_path), "source": "delivery_scope"})
        if native_qa_allowed:
            deliveries.append({"id": "native_qa", "label": "剪映保存重开 QA", **snap.metadata(native_qa_rel), "source": "QA 记录"})

        title = data.get("title") if isinstance(data.get("title"), str) else "UNKNOWN · 标题未登记"
        updates = [x["modified_at"] for x in deliveries if x.get("modified_at")]
        source_status = snap.metadata("production_status.yaml")
        result = {"ref": ep.name, "id": data.get("episode_id") if isinstance(data.get("episode_id"), str) else ep.name.split("_")[0],
                  "title": title[:180], "directory": str(ep), "test_only": data.get("test_only") is True or ep.name.startswith("TEST_"),
                  "status_exists": source_status["exists"], "status_parsed": bool(data), "status_modified_at": source_status["modified_at"],
                  "updated_at": max(updates, default=source_status["modified_at"]), "artifacts": contract_states,
                  "gates": gates, "deliveries": deliveries, "candidate": {"status": candidate_state,
                  "native_ready": native_ready, "native_directory_exists": draft["exists"], "saved_reopened_record": native_recorded,
                  "video_ready": video_ready, "render_status": "NOT_RENDERED" if render.get("status") == "not_rendered" else "UNKNOWN" if not render else "REPORTED",
                  "final_mp4_complete": video_ready and render.get("status") != "not_rendered",
                  "qa_record_exists": bool(native_qa), "cover_exists": cover_file["exists"], "master_exists": master["exists"],
                  "captions_exist": captions["exists"], "backup_exists": backup["exists"]},
                  "stats": stats, "warnings": list(dict.fromkeys(snap.warnings)), "restricted_paths": snap.unsafe,
                  "history": self._history(data.get("history")), "scanned_at": now()}
        result["fingerprint"] = snap.fingerprint()
        return result

    @staticmethod
    def _history(value) -> list[dict]:
        # No actor, free text, paths or credentials are exposed.
        if not isinstance(value, list):
            return []
        return [{"at": timestamp(x.get("at")), "artifact": x.get("artifact"), "to": state(x.get("to"))}
                for x in value if isinstance(x, dict) and x.get("action") == "mark" and x.get("artifact") in CONTRACTS]

    def listing(self) -> list[dict]:
        rows = []
        for ep in self.episode_dirs():
            snap = Snapshot(self.root, ep)
            data = snap.document("production_status.yaml", "yaml")
            rows.append({"ref": ep.name, "id": data.get("episode_id") if isinstance(data.get("episode_id"), str) else ep.name.split("_")[0],
                         "title": data.get("title", "UNKNOWN")[:180] if isinstance(data.get("title", "UNKNOWN"), str) else "UNKNOWN",
                         "test_only": data.get("test_only") is True or ep.name.startswith("TEST_"),
                         "updated_at": snap.metadata("production_status.yaml")["modified_at"],
                         "status": "PUBLISHED" if mapping(mapping(data.get("artifacts")).get("release")).get("status") == "published"
                         and all(mapping(mapping(data.get("gates")).get(g)).get("status") == "approved" for g in FORMAL_GATES) else "IN_PRODUCTION" if data else "UNKNOWN"})
        return rows

    def health(self, detail: dict | None = None) -> list[dict]:
        snap = Snapshot(self.root, self.root)
        config = snap.document("studio.config.yaml", "yaml", base=self.root)
        ffmpeg = snap.metadata(mapping(config.get("media")).get("ffmpeg_path"), base=self.root)
        result = [{"name": "Studio Root", "status": "OK" if self.root.is_dir() else "MISSING"},
                  {"name": "Episodes", "status": "OK" if (self.root / "Episodes").is_dir() else "MISSING"},
                  {"name": "Status File", "status": "OK" if detail and detail["status_parsed"] else "UNKNOWN"},
                  {"name": "Python", "status": "OK", "detail": sys.version.split()[0]},
                  {"name": "FFmpeg", "status": "FOUND" if ffmpeg["exists"] else "MISSING", "detail": "路径检查；未重复执行解码"},
                  {"name": "OBS", "status": "UNKNOWN", "detail": "未连接 OBS；不读取连接密码"},
                  {"name": "Remotion", "status": "OPTIONAL", "detail": "不作为面板启动依赖"},
                  {"name": "Jianying", "status": "FOUND" if detail and detail["candidate"]["native_directory_exists"] else "NOT_FOUND" if detail and detail["candidate"]["native_directory_exists"] is False else "UNKNOWN"}]
        if detail:
            ep = self.resolve(detail["ref"])
            source = Snapshot(self.root, ep).metadata("Assets/remotion-motion/package.json")
            if source["exists"]:
                result[6].update(status="SOURCE_FOUND", detail="本期源码存在；未检查渲染进程")
        return result
