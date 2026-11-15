"""Read-only adapter to the existing Studio validator, with a private-path guard."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from control_center.status_reader import CONTRACTS, FORMAL_GATES, checked_path


def validate_existing(ref: str) -> dict:
    from scripts import studio

    ep = studio.episode_dir(ref)
    data = studio.read_status(ep)
    if set(studio.PATHS) != set(CONTRACTS) or not isinstance(data.get("artifacts"), dict):
        return {"status": "ERROR", "issues": ["契约结构不完整，请在终端核对现有 Studio 配置。"]}
    if set(data["artifacts"]) != set(studio.PATHS) or set(data.get("gates", {})) != set(FORMAL_GATES):
        return {"status": "ERROR", "issues": ["产物或 Gate 结构与现有 Studio 契约不一致。"]}
    for name, rel in studio.PATHS.items():
        if checked_path(ep, rel) is None:
            return {"status": "UNSAFE", "issues": [f"{name} 存在受限路径，未执行读取。"]}

    original_hash = studio.sha256
    original_inside = studio.inside_episode

    class PrivatePathError(ValueError):
        pass

    def guarded_hash(path):
        try:
            relative = path.relative_to(ep).as_posix()
        except ValueError:
            raise PrivatePathError("Restricted path") from None
        if checked_path(ep, relative) is None:
            raise PrivatePathError("Restricted path")
        return original_hash(path)

    def guarded_inside(base, rel):
        if checked_path(base, rel) is None:
            raise PrivatePathError("Restricted path")
        return original_inside(base, rel)

    studio.sha256 = guarded_hash
    studio.inside_episode = guarded_inside
    try:
        issues = studio.validate(ep)
        return {"status": "FAIL" if issues else "PASS", "issues": issues[:100],
                "engine": "scripts/studio.py validate (general)", "read_only": True}
    except PrivatePathError:
        return {"status": "UNSAFE", "issues": ["引用中包含受限或越界路径，已阻止读取。"]}
    finally:
        studio.sha256 = original_hash
        studio.inside_episode = original_inside


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("One Episode reference required")
        result = validate_existing(sys.argv[1])
    except Exception:
        # Do not echo malformed document content, arbitrary paths or secrets.
        result = {"status": "ERROR", "issues": ["Validate 无法完成；请在终端核对该期契约格式与文件。"]}
    print(json.dumps(result, ensure_ascii=False))
