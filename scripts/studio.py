"""Episode contracts, state transitions and dependency invalidation."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = yaml.safe_load((ROOT / "studio.config.yaml").read_text(encoding="utf-8"))
DEPS: dict[str, list[str]] = CONFIG["dependencies"]
PATHS: dict[str, str] = CONFIG["paths"]
STATES = set(CONFIG["status_values"])
GATES = CONFIG["gates"]
EPISODES = ROOT / CONFIG["episodes_dir"]
DIRS = ("Raw", "Assets", "Voice", "ProjectShots", "DemoWorkspace", "Preview", "Export")
TRANSITIONS = {
    "planned": {"ready", "blocked", "rejected"},
    "blocked": {"ready", "rejected"},
    "ready": {"tested", "captured", "selected", "rendered", "review_required", "approved", "blocked", "rejected"},
    "tested": {"captured", "selected", "review_required", "approved", "rejected"},
    "captured": {"selected", "review_required", "approved", "rejected"},
    "selected": {"rendered", "review_required", "approved", "rejected"},
    "rendered": {"review_required", "approved", "rejected"},
    "review_required": {"ready", "approved", "rejected"},
    "approved": {"obsolete", "published"},
    "rejected": {"ready"},
    "obsolete": {"ready", "review_required"},
    "published": {"obsolete"},
}


def utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inside_episode(ep: Path, rel: str) -> Path:
    target = (ep / rel).resolve()
    if not target.is_relative_to(ep.resolve()):
        raise ValueError(f"Path escapes Episode: {rel}")
    return target


def artifact_hash(ep: Path, name: str) -> str | None:
    """Digest the contract and any referenced media, not just the manifest text."""
    path = ep / PATHS[name]
    base = sha256(path)
    if base is None:
        return None
    refs: list[str] = []
    if name in {"shots", "assets", "voice"}:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            refs = [row.get("file", "") for row in csv.DictReader(stream) if row.get("file")]
    elif name == "timeline":
        data = json.loads(path.read_text(encoding="utf-8"))
        refs = [row.get("file", "") for row in data.get("shots", []) if row.get("file")]
        if data.get("main_audio"):
            refs.append(data["main_audio"])
    elif name == "render":
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("output"):
            refs.append(data["output"])
    if not refs:
        return base
    digest = hashlib.sha256(base.encode("ascii"))
    for rel in sorted(set(refs)):
        target = inside_episode(ep, rel)
        digest.update(rel.encode("utf-8"))
        digest.update((sha256(target) or "MISSING").encode("ascii"))
    return digest.hexdigest()


def episode_dir(ref: str) -> Path:
    if not re.fullmatch(r"(?:\d{3}|TEST_[A-Z0-9_]+|\d{3}_[A-Za-z0-9_-]+)", ref):
        raise ValueError("Invalid episode ID; use 003, 003_slug, or TEST_P0")
    direct = EPISODES / ref
    if direct.is_dir():
        return direct
    matches = list(EPISODES.glob(f"{ref}_*")) if re.fullmatch(r"\d{3}", ref) else []
    if len(matches) == 1 and matches[0].is_dir():
        return matches[0]
    raise FileNotFoundError(f"Episode not found: {ref}")


def read_status(ep: Path) -> dict:
    data = yaml.safe_load((ep / "production_status.yaml").read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("production_status.yaml must be a mapping")
    return data


def write_status(ep: Path, data: dict) -> None:
    target = ep / "production_status.yaml"
    temp = ep / "production_status.yaml.tmp"
    temp.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    temp.replace(target)


def descendants(name: str) -> set[str]:
    found: set[str] = set()
    pending = [name]
    while pending:
        current = pending.pop()
        for child, parents in DEPS.items():
            if current in parents and child not in found:
                found.add(child)
                pending.append(child)
    return found


def new_episode(ref: str, title: str, test: bool = False) -> Path:
    if test:
        if not re.fullmatch(r"TEST_[A-Z0-9_]+", ref):
            raise ValueError("--test requires TEST_... ID")
        dirname = ref
    else:
        if not re.fullmatch(r"\d{3}", ref):
            raise ValueError("Formal Episode ID must be exactly three digits")
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "episode"
        dirname = f"{ref}_{slug}"
    ep = EPISODES / dirname
    if ep.exists():
        raise FileExistsError(f"Refusing to overwrite {ep}")
    ep.mkdir(parents=True)
    for directory in DIRS:
        (ep / directory).mkdir()
    for artifact, rel in PATHS.items():
        source = ROOT / "templates" / rel
        if not source.is_file():
            raise FileNotFoundError(f"Missing template: {source}")
        body = source.read_text(encoding="utf-8").replace("{{EPISODE_ID}}", ref).replace("{{TITLE}}", title)
        (ep / rel).write_text(body, encoding="utf-8")
    artifacts = {}
    for name, rel in PATHS.items():
        artifacts[name] = {
            "path": rel,
            "status": "planned",
            "sha256": artifact_hash(ep, name),
            "input_hashes": {parent: artifact_hash(ep, parent) for parent in DEPS[name]},
            "updated_at": utc(),
        }
    data = {
        "schema_version": "0.1", "episode_id": ref, "title": title, "test_only": test,
        "created_at": utc(), "artifacts": artifacts,
        "gates": {gate: {"status": "pending", "actor": None, "evidence": None, "at": None} for gate in GATES},
        "history": [],
    }
    write_status(ep, data)
    return ep


def sync(ep: Path) -> list[str]:
    data = read_status(ep)
    changed = []
    for name, item in data["artifacts"].items():
        current = artifact_hash(ep, name)
        if current != item.get("sha256"):
            changed.append(name)
            item["sha256"] = current
            item["status"] = "review_required" if current else "blocked"
            item["updated_at"] = utc()
    for name in changed:
        for downstream in descendants(name):
            item = data["artifacts"][downstream]
            item["status"] = "obsolete"
            item["updated_at"] = utc()
            item["input_hashes"] = {parent: data["artifacts"][parent]["sha256"] for parent in DEPS[downstream]}
        if name in {"brief", "facts", "evidence", "script"}:
            data["gates"]["SCRIPT_APPROVAL"] = {"status": "pending", "actor": None, "evidence": None, "at": None}
        data["gates"]["FINAL_REVIEW"] = {"status": "pending", "actor": None, "evidence": None, "at": None}
        data["gates"]["PUBLISH_APPROVAL"] = {"status": "pending", "actor": None, "evidence": None, "at": None}
        data["history"].append({"at": utc(), "action": "content_changed", "artifact": name, "invalidated": sorted(descendants(name))})
    if changed:
        write_status(ep, data)
    return changed


def mark(ep: Path, name: str, target: str) -> None:
    sync(ep)
    data = read_status(ep)
    if name not in data["artifacts"] or target not in STATES:
        raise ValueError("Unknown artifact or status")
    item = data["artifacts"][name]
    old = item["status"]
    if target not in TRANSITIONS[old]:
        raise ValueError(f"Illegal transition {old} -> {target}")
    if target not in {"blocked", "rejected", "obsolete", "review_required"}:
        if item["sha256"] is None:
            raise ValueError(f"Missing artifact file: {item['path']}")
        waiting = [p for p in DEPS[name] if data["artifacts"][p]["status"] != "approved"]
        if waiting:
            raise ValueError(f"Parents not approved: {waiting}")
        if name in {"voice", "captions"} and data["gates"]["SCRIPT_APPROVAL"]["status"] != "approved":
            raise ValueError("SCRIPT_APPROVAL required")
        if name == "release" and data["gates"]["FINAL_REVIEW"]["status"] != "approved":
            raise ValueError("FINAL_REVIEW required before release gate")
        if name == "release" and target == "published" and data["gates"]["PUBLISH_APPROVAL"]["status"] != "approved":
            raise ValueError("PUBLISH_APPROVAL required before publish action")
    item["status"] = target
    item["input_hashes"] = {p: data["artifacts"][p]["sha256"] for p in DEPS[name]}
    item["updated_at"] = utc()
    data["history"].append({"at": utc(), "action": "mark", "artifact": name, "from": old, "to": target})
    write_status(ep, data)


def approve_gate(ep: Path, gate: str, actor: str, evidence: str) -> None:
    if gate not in GATES:
        raise ValueError(f"Unknown Gate: {gate}")
    if not actor.strip() or not evidence.strip():
        raise ValueError("Human actor and evidence path are required")
    sync(ep)
    data = read_status(ep)
    if gate == "SCRIPT_APPROVAL" and data["artifacts"]["script"]["status"] != "approved":
        raise ValueError("Script artifact must be approved first")
    if gate == "FINAL_REVIEW" and data["artifacts"]["qa"]["status"] != "approved":
        raise ValueError("QA must be approved first")
    if gate == "PUBLISH_APPROVAL":
        if data["gates"]["FINAL_REVIEW"]["status"] != "approved":
            raise ValueError("FINAL_REVIEW required")
        if data["artifacts"]["release"]["status"] != "approved":
            raise ValueError("Release gate artifact must be approved first")
    ev = (ep / evidence).resolve()
    if not ev.is_relative_to(ep.resolve()) or not ev.is_file():
        raise ValueError("Gate evidence must be an existing Episode file")
    data["gates"][gate] = {"status": "approved", "actor": actor.strip(), "evidence": evidence, "sha256": sha256(ev), "at": utc()}
    data["history"].append({"at": utc(), "action": "human_gate", "gate": gate, "actor": actor.strip()})
    write_status(ep, data)


def validate(ep: Path, stage: str = "general") -> list[str]:
    data = read_status(ep)
    issues = []
    if data.get("schema_version") != "0.1" or not isinstance(data.get("artifacts"), dict):
        issues.append("Invalid production_status root structure")
        return issues
    if not isinstance(data.get("episode_id"), str) or not isinstance(data.get("title"), str) or not isinstance(data.get("test_only"), bool):
        issues.append("Invalid episode identity/title/test_only")
    if not isinstance(data.get("history"), list):
        issues.append("Invalid status history")
    if set(data["artifacts"]) != set(DEPS):
        issues.append("Artifact names differ from dependency graph")
    for name in DEPS:
        item = data["artifacts"].get(name)
        if not isinstance(item, dict):
            issues.append(f"Missing artifact state: {name}")
            continue
        if item.get("path") != PATHS[name] or item.get("status") not in STATES:
            issues.append(f"Invalid artifact path/status: {name}")
        if not isinstance(item.get("input_hashes"), dict) or set(item["input_hashes"]) != set(DEPS[name]):
            issues.append(f"Invalid input_hashes: {name}")
        if item.get("sha256") is not None and not re.fullmatch(r"[a-f0-9]{64}", str(item["sha256"])):
            issues.append(f"Invalid sha256: {name}")
        current = artifact_hash(ep, name)
        if current != item.get("sha256"):
            issues.append(f"Un-synced file/hash: {name}")
        if item.get("status") in {"approved", "selected", "rendered", "published"}:
            for p in DEPS[name]:
                if data["artifacts"][p]["status"] != "approved":
                    issues.append(f"{name} used unapproved parent {p}")
                if item.get("input_hashes", {}).get(p) != data["artifacts"][p]["sha256"]:
                    issues.append(f"{name} has stale parent hash {p}")
    if set(data.get("gates", {})) != set(GATES):
        issues.append("Missing human Gate")
    for gate, item in data.get("gates", {}).items():
        if not isinstance(item, dict) or item.get("status") not in {"pending", "approved"}:
            issues.append(f"Invalid Gate structure: {gate}")
            continue
        if item.get("status") == "approved":
            if not item.get("actor") or not item.get("at"):
                issues.append(f"Missing Gate actor/time: {gate}")
            rel = item.get("evidence")
            ev = (ep / rel).resolve() if isinstance(rel, str) else ep
            if not ev.is_relative_to(ep.resolve()) or sha256(ev) != item.get("sha256"):
                issues.append(f"Invalid/stale Gate evidence: {gate}")
    if stage == "render":
        if data["gates"]["SCRIPT_APPROVAL"]["status"] != "approved":
            issues.append("SCRIPT_APPROVAL missing")
        for name in ("shots", "assets", "voice", "captions", "timeline"):
            if data["artifacts"][name]["status"] != "approved":
                issues.append(f"Render input not approved: {name}")
        shots = list(csv.DictReader((ep / PATHS["shots"]).open(encoding="utf-8-sig", newline="")))
        for shot in shots:
            if shot.get("required", "true").lower() != "true":
                continue
            if shot.get("status") not in {"selected", "rendered", "approved"}:
                issues.append(f"Required shot not selected: {shot.get('shot_id')}")
            media = shot.get("file", "")
            if not media or not inside_episode(ep, media).is_file():
                issues.append(f"Required shot missing file: {shot.get('shot_id')}")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("new_episode")
    init.add_argument("id")
    init.add_argument("title")
    init.add_argument("--test", action="store_true")
    for command in ("sync", "validate"):
        p = sub.add_parser(command)
        p.add_argument("episode")
        if command == "validate":
            p.add_argument("--stage", choices=("general", "render"), default="general")
    state = sub.add_parser("mark")
    state.add_argument("episode")
    state.add_argument("artifact")
    state.add_argument("status")
    gate = sub.add_parser("approve_gate")
    gate.add_argument("episode")
    gate.add_argument("gate", choices=GATES)
    gate.add_argument("--actor", required=True)
    gate.add_argument("--evidence", required=True)
    sub.add_parser("graph")
    args = parser.parse_args()
    try:
        if args.command == "new_episode":
            print(new_episode(args.id, args.title, args.test))
        elif args.command == "graph":
            for name, parents in DEPS.items():
                print(f"{name} <- {', '.join(parents) if parents else '(root)'}")
        else:
            ep = episode_dir(args.episode)
            if args.command == "sync":
                print("Changed:", ", ".join(sync(ep)) or "none")
            elif args.command == "mark":
                mark(ep, args.artifact, args.status)
                print(f"{args.artifact}: {args.status}")
            elif args.command == "approve_gate":
                approve_gate(ep, args.gate, args.actor, args.evidence)
                print(f"Gate recorded: {args.gate}")
            else:
                issues = validate(ep, args.stage)
                print("PASS" if not issues else "FAIL")
                for issue in issues:
                    print("-", issue)
                return 0 if not issues else 1
    except (OSError, ValueError, KeyError) as exc:
        parser.exit(2, f"ERROR: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
