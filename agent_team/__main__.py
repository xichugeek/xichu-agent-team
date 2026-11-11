"""命令行入口：python -m agent_team {run|doctor|discover|status}"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import config as config_mod
from .executors import build
from .telegram import TelegramClient, TelegramError
from .workflows import Workflows


def _client(cfg, token=None):
    return TelegramClient(token or cfg.bot_token, cfg.tg.get("api_base"), cfg.tg.get("proxy"))


def _clients(cfg) -> dict:
    return {role: _client(cfg, tok) for role, tok in cfg.bot_tokens.items() if role in cfg.raw["roles"]}


def cmd_doctor(cfg) -> int:
    report, bad = {}, 0
    tokens = cfg.bot_tokens
    for role, rconf in cfg.raw["roles"].items():
        key = f"bot:{role}"
        if role not in tokens:
            report[key] = f"FAIL {rconf['label']} 未配置 token（secrets.local.json 的 telegram_bot_tokens.{role}）"
            bad += 1
            continue
        try:
            tg = _client(cfg, tokens[role])
            me = tg.get_me()
            chat = tg.get_chat(int(cfg.tg["group_chat_id"]))
            note = ("（隐私模式关，收群内全部消息）" if me.get("can_read_all_group_messages")
                    else "（隐私模式开：只收 /命令、@它 和对它的回复）")
            report[key] = f"OK {rconf['label']} @{me.get('username')} 在群「{chat.get('title')}」{note}"
        except (ValueError, TelegramError) as exc:
            report[key] = f"FAIL {rconf['label']}：{exc}"
            bad += 1
        report[f"pool:{role}"] = " → ".join(rconf["pool"])
    report["owner_user_id"] = "OK" if int(cfg.tg.get("owner_user_id") or 0) > 0 else "FAIL 未配置"
    bad += report["owner_user_id"] != "OK"
    report["group_chat_id"] = "OK" if int(cfg.tg.get("group_chat_id") or 0) != 0 else "FAIL 未配置"
    bad += report["group_chat_id"] != "OK"
    pm_note = report.get("bot:pm", "")
    if "隐私模式开" in pm_note:
        report["bot:pm"] = pm_note.replace("OK", "FAIL", 1) + "——项目经理必须关闭隐私模式（BotFather /setprivacy → Disable），再移出群重新拉回"
        bad += 1
    wf = Workflows(cfg)
    for name in cfg.raw["executors"]:
        conf = cfg.executor(name)
        ex = build(name, conf, cfg.is_simulated(name), wf.simulate)
        pre = ex.preflight()
        tag = "SIMULATED" if ex.simulated else ("OK" if pre.get("ok") else "FAIL")
        if ex.simulated:
            pre = build(name, conf, False, wf.simulate).preflight()
            pre = {"真实执行器": "可用" if pre.get("ok") else pre.get("error")}
        report[f"executor:{name}"] = f"{tag} {conf.get('label')} " + json.dumps(
            {k: v for k, v in pre.items() if k != "ok"}, ensure_ascii=False)
        bad += tag == "FAIL"
    for k, v in report.items():
        print(f"{k:24s} {v}")
    sims = [k for k, v in report.items() if k.startswith("executor:") and v.startswith("SIMULATED")]
    verdict = "可运行" if not bad else f"{bad} 项未就绪"
    if sims:
        verdict += f"；{len(sims)} 个执行器处于模拟模式（{', '.join(s.split(':')[1] for s in sims)}），其结果标【模拟】，不算真实执行"
    print("结论：", verdict)
    return 1 if bad else 0


def cmd_discover(cfg) -> int:
    seen = set()
    for role, tg in _clients(cfg).items():
        me = tg.get_me()
        print(f"[{role}] bot: @{me.get('username')} id={me.get('id')}")
        for upd in tg.get_updates(None, 0):
            msg = upd.get("message") or upd.get("my_chat_member") or {}
            chat, frm = msg.get("chat") or {}, msg.get("from") or {}
            key = (chat.get("id"), frm.get("id"))
            if key in seen:
                continue
            seen.add(key)
            print(f"chat_id={chat.get('id')} type={chat.get('type')} title={chat.get('title')!r} | "
                  f"from_id={frm.get('id')} username={frm.get('username')!r}")
    if not seen:
        print("没有待取的消息：把机器人拉进私群后，在群里随便发一句话（或 /help），再运行一次。")
    else:
        print("\n把你自己的 from_id 填到 config.json 的 owner_user_id，把群的 chat_id（负数）填到 group_chat_id。")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agent_team")
    p.add_argument("--config", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("doctor")
    sub.add_parser("discover")
    s = sub.add_parser("status")
    s.add_argument("ticket", nargs="?")
    args = p.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    cfg = config_mod.load(args.config)
    if args.cmd == "doctor":
        return cmd_doctor(cfg)
    if args.cmd == "discover":
        return cmd_discover(cfg)
    from .scheduler import Scheduler
    if args.cmd == "status":
        sched = Scheduler(cfg, telegram=None)
        print(sched.status_text(args.ticket))
        return 0
    Scheduler(cfg, _clients(cfg)).run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
