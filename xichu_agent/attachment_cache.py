"""Private, verified Telegram attachment cache; each job gets its own copy."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path


# A bounded set of locks also covers concurrent workers requesting the same key.
_LOCKS = tuple(threading.Lock() for _ in range(64))


class _IntegrityError(RuntimeError):
    pass


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()


def _entry(blob: Path, metadata: Path, key: str):
    try:
        if blob.is_symlink() or metadata.is_symlink() or metadata.stat().st_size > 4096:
            return None
        info = json.loads(metadata.read_text(encoding="utf-8"))
        if not isinstance(info, dict) or info.get("version") != 1 or info.get("key") != key:
            return None
        size, digest = info.get("bytes"), info.get("sha256")
        if type(size) is not int or size < 0 or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            return None
        if not blob.is_file() or blob.stat().st_size != size or _digest(blob) != digest:
            return None
        return {"bytes": size, "sha256": digest}
    except (OSError, ValueError, TypeError):
        return None


def _temporary(folder: Path, prefix: str) -> Path:
    with tempfile.NamedTemporaryFile(dir=folder, prefix=prefix, delete=False) as stream:
        return Path(stream.name)


def _materialize(blob: Path, dest: Path, info: dict) -> None:
    """Atomic copy, never follow an existing job-file symlink or share an inode."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    temporary = _temporary(dest.parent, ".attachment-")
    try:
        shutil.copy2(blob, temporary)
        if temporary.stat().st_size != info["bytes"] or _digest(temporary) != info["sha256"]:
            raise _IntegrityError("附件缓存复制校验失败")
        os.replace(temporary, dest)
    finally:
        temporary.unlink(missing_ok=True)


def fetch_attachment(cfg, item: dict, dest: Path, client) -> dict:
    """Reuse only intact bytes from this receiving bot; preserve the source records.

    Unknown item fields cannot choose a cache path or provide a trusted digest.
    Failed/partial downloads are never installed as successful entries.
    """
    dest = Path(dest)
    bot, file_id = item.get("bot"), item.get("file_id")
    if not isinstance(bot, str) or not bot or not isinstance(file_id, str) or not file_id:
        raise ValueError("附件缺少接收机器人或 file_id")
    unique = item.get("file_unique_id")
    identity = unique if isinstance(unique, str) and unique else file_id
    key = hashlib.sha256((bot + "\0" + identity).encode("utf-8")).hexdigest()
    folder = Path(cfg.data_dir) / "attachment_cache"
    folder.mkdir(parents=True, exist_ok=True)
    if dest.resolve().is_relative_to(folder.resolve()):
        raise ValueError("任务附件路径不能指向私密缓存目录")
    blob, metadata = folder / (key + ".blob"), folder / (key + ".json")
    lock = _LOCKS[int(key[:8], 16) % len(_LOCKS)]
    with lock:
        info = _entry(blob, metadata, key)
        expected = item.get("bytes")
        if info and type(expected) is int and expected >= 0 and info["bytes"] != expected:
            info = None
        if info:
            try:
                _materialize(blob, dest, info)
                return {"path": str(dest), "kind": item["kind"], "sha256": info["sha256"], "cache_hit": True}
            except _IntegrityError:
                # If the cached bytes changed while being copied, fetch again.
                info = None
        download = _temporary(folder, key + ".download-")
        meta_temp = None
        try:
            client.download_file(file_id, download)
            size = download.stat().st_size
            if type(expected) is int and expected >= 0 and size != expected:
                raise _IntegrityError("下载的附件长度与原消息不符，未缓存")
            info = {"bytes": size, "sha256": _digest(download)}
            meta_temp = _temporary(folder, key + ".metadata-")
            meta_temp.write_text(json.dumps({"version": 1, "key": key, **info}), encoding="utf-8")
            os.replace(download, blob)
            os.replace(meta_temp, metadata)
            _materialize(blob, dest, info)
            return {"path": str(dest), "kind": item["kind"], "sha256": info["sha256"], "cache_hit": False}
        finally:
            download.unlink(missing_ok=True)
            if meta_temp is not None:
                meta_temp.unlink(missing_ok=True)
