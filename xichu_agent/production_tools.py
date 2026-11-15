"""Project-native recording MCP: authenticated OBS API, isolated source, verified restoration.

This reuses the original Studio recorder. It never uses the Codex desktop helper,
changes global settings, or accepts arbitrary desktop/source capture commands.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
import sys
import time
import uuid
from fractions import Fraction
from pathlib import Path


def wait_for_stopped(obs, attempts=40, interval=.5):
    """StopRecord may return while OBS is still finalizing the output."""
    for n in range(attempts):
        record = obs.request("GetRecordStatus")["outputActive"]
        stream = obs.request("GetStreamStatus")["outputActive"]
        if not record and not stream:
            return True, n
        if n + 1 < attempts:
            time.sleep(interval)
    return False, attempts


def confirm_stop_before_restoring(obs):
    try:
        inactive, polls = wait_for_stopped(obs)
    except Exception:
        inactive, polls = False, None
    if not inactive:
        # Keep the isolated test scene and muted inputs if stopping is unconfirmed.
        # Legacy adapter __exit__ otherwise restores a potentially private source.
        obs.previous_scene = None
        obs.previous_mutes = {}
        raise RuntimeError("未确认录制/推流已停止，保留隔离测试画面和静音，不切回可能私密的原场景")
    return polls


def record_test(studio: Path, workdir: Path):
    sys.path.insert(0, str(studio / "scripts"))
    from recorder import ObsWebSocketAdapter, inspect_clip
    from run_obs_readiness import wait_for_finalized
    from media import full_decode
    output = workdir.resolve() / "output" / ("recording-" + uuid.uuid4().hex[:12])
    output.mkdir(parents=True, exist_ok=False)
    scene = None
    before_scene = None
    before_mutes = {}
    returned = None
    try:
        with ObsWebSocketAdapter() as obs:
            settings = obs.inspect()
            if (settings.width, settings.height) != (1920, 1080) or abs(settings.fps - 60) > .1:
                raise RuntimeError("原录制配置不是1920×1080/60fps，本工具不更改设置")
            before_scene = obs.request("GetCurrentProgramScene")["currentProgramSceneName"]
            test = obs.prepare_test_scene(studio / "sandbox/OBS_TEST_PATTERN.html")
            scene, before_mutes = obs.test_scene, dict(obs.previous_mutes)
            try:
                returned = Path(obs.record_test(10))
            finally:
                confirm_stop_before_restoring(obs)
        # Adapter restores scene and mutes. Verify them instead of claiming restoration.
        with ObsWebSocketAdapter() as obs:
            restored_scene = obs.request("GetCurrentProgramScene")["currentProgramSceneName"] == before_scene
            restored_mutes = all(obs.request("GetInputMute", {"inputName": k})["inputMuted"] == v for k, v in before_mutes.items())
            inactive, stop_polls = wait_for_stopped(obs)
            if scene and scene.startswith("OBS_TEST_PATTERN_RC_") and restored_scene and inactive:
                obs.request("RemoveScene", {"sceneName": scene})
        src = wait_for_finalized(returned)
        target = output / ("OBS_TEST_PATTERN_10s" + src.suffix.lower())
        shutil.copy2(src, target)
        data = inspect_clip(target, output / "frames", (1920, 1080))
        data.update(full_decode=full_decode(target), fps=data["video"][0].get("avg_frame_rate") if data["video"] else None,
                    recording_file=str(target), sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                    api="original Studio ObsWebSocketAdapter / authenticated localhost OBS WebSocket v5",
                    scope="Studio-owned animated test page only; not a formal desktop recording",
                    restoration={"scene": restored_scene, "mutes": restored_mutes, "recording_streaming_inactive": inactive, "stop_status_polls": stop_polls},
                    source_scene=test)
        data["pass"] = bool(data["pass"] and data["full_decode"] and data["audio"] and
                            abs(float(Fraction(data["fps"])) - 60) < .1 and restored_scene and restored_mutes and inactive)
        (output / "recording_report.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data
    except Exception as exc:
        (output / "recording_failure.json").write_text(json.dumps({"status": "failed", "error": str(exc), "original_scene": before_scene,
            "started_recording_file": str(returned) if returned else None}, ensure_ascii=False, indent=2), encoding="utf-8")
        raise


TOOLS = [{"name": "record_studio_test", "description": "复用原Studio认证OBS API，真实录制自有动画测试页10秒；验证1920×1080/60fps、解码、首中尾帧，并验证场景/音频恢复。拒绝已有录制/直播，不录桌面或其他应用，不修改配置。输出只写当前工单的新目录。",
          "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}]


def main():
    # MCP is UTF-8 even when a Windows child process inherits a legacy code page.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser();p.add_argument("--studio", required=True);p.add_argument("--workdir", required=True);args=p.parse_args()
    studio, workdir = Path(args.studio).resolve(), Path(args.workdir).resolve()
    for line in sys.stdin:
        try:
            msg=json.loads(line);method=msg.get("method");id=msg.get("id")
            if id is None: continue
            if method == "initialize":
                result={"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"xichugeek-production-ops","version":"1.0"}}
            elif method == "tools/list": result={"tools":TOOLS}
            elif method == "ping": result={}
            elif method == "tools/call":
                params=msg.get("params",{})
                if params.get("name") != "record_studio_test" or params.get("arguments") not in (None, {}): raise ValueError("Unsupported tool or arguments")
                data=record_test(studio,workdir)
                result={"content":[{"type":"text","text":json.dumps(data,ensure_ascii=False)}],"isError":not data["pass"]}
            else: raise ValueError("Unsupported method")
            print(json.dumps({"jsonrpc":"2.0","id":id,"result":result},ensure_ascii=False),flush=True)
        except Exception as exc:
            if isinstance(locals().get("msg"),dict) and msg.get("id") is not None:
                print(json.dumps({"jsonrpc":"2.0","id":msg["id"],"result":{"content":[{"type":"text","text":f"{type(exc).__name__}: {exc}"}],"isError":True}},ensure_ascii=False),flush=True)


if __name__ == "__main__": main()
