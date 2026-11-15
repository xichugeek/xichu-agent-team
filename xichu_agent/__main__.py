"""命令行入口：python -m xichu_agent {run|doctor|discover|status}"""

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
        report[f"pool:{role}"] = " → ".join(
            name + ("（待登录，未启用）" if not cfg.executor(name).get("enabled", True) else "")
            for name in rconf["pool"])
    report["owner_user_id"] = "OK" if int(cfg.tg.get("owner_user_id") or 0) > 0 else "FAIL 未配置"
    bad += report["owner_user_id"] != "OK"
    for key in ("ffmpeg_path", "ffprobe_path"):
        ok = Path(cfg.get(key) or "").is_file()
        report[key] = "OK" if ok else f"FAIL 不存在 {cfg.get(key)}"
        bad += not ok
    site = Path(cfg.get("site_repo") or "") / "content" / "episodes" / cfg.get("site_template_episode", "")
    report["site_template(只读)"] = "OK" if (site / "episode.json").is_file() else f"WARN 读不到 {site}，将用合成模板"
    wf = Workflows(cfg)
    for name in cfg.executor_names:
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
    from .executors import codex_account_id
    identities = {}
    for name in cfg.executor_names:
        conf = cfg.executor(name)
        if conf["type"] != "codex":
            continue
        account = codex_account_id(conf.get("codex_home"))
        if account and account in identities:
            report[f"账号唯一性:{name}"] = f"FAIL {name} 与 {identities[account]} 登录的是同一个 ChatGPT 账号，需重新登录独立账号"
            bad += 1
        elif account:
            identities[account] = name
    if identities:
        report["Codex 独立账号数"] = f"{len(identities)}（已核验登录身份）"
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
        print("没有待取的消息：把 bot 拉进私群后，在群里发一条 /help@机器人用户名 再运行。")
    return 0


def cmd_login_codex(cfg, name: str) -> int:
    """用指定账号目录交互式登录 Codex（ChatGPT 订阅），例如账号 B。"""
    import os
    import subprocess
    from . import procutil
    from .executors import CODEX_DROP
    conf = cfg.executor(name)
    cli = procutil.resolve_cli(conf.get("cli", "codex"))
    if not cli:
        print("找不到 Codex CLI")
        return 1
    home = conf.get("codex_home")
    if home:
        Path(home).mkdir(parents=True, exist_ok=True)
    print(f"为 {conf.get('label')} 登录，账号目录 CODEX_HOME={home or '(默认)'}；浏览器打开后请用该账号登录 ChatGPT。")
    env = procutil.clean_env(CODEX_DROP, {"CODEX_HOME": home} if home else None)
    print(f"提示：浏览器里如果已登录另一个 ChatGPT 账号，会被直接沿用。请先切换到 {conf.get('label')} 对应的独立账号；"
          "不要沿用其他槽位的登录。")
    rc = subprocess.call([cli, "login"], env=env)
    subprocess.call([cli, "login", "status"], env=env)
    from .executors import codex_account_id
    mine = codex_account_id(home)
    for other, other_conf in cfg.raw["executors"].items():
        if other == name or other_conf["type"] != "codex":
            continue
        theirs = codex_account_id(other_conf.get("codex_home"))
        if mine and theirs and mine == theirs:
            print(f"\n⚠️ 注意：{conf.get('label')} 与 {other_conf.get('label')} 是同一个 ChatGPT 账号！"
                  "请在浏览器里退出 ChatGPT（或切到另一个账号）后重新运行本脚本。")
            return 2
    if mine and rc == 0:
        print(f"\n✅ {conf.get('label')} 已登录，与其他已配置账号身份不同。")
    return rc


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="xichu_agent")
    p.add_argument("--config", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    sub.add_parser("doctor")
    sub.add_parser("discover")
    s = sub.add_parser("status")
    s.add_argument("ticket", nargs="?")
    lg = sub.add_parser("login-codex")
    lg.add_argument("executor", choices=["codex_a", "codex_b", "codex_c"])
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
    if args.cmd == "login-codex":
        return cmd_login_codex(cfg, args.executor)
    from .scheduler import Scheduler
    if args.cmd == "status":
        sched = Scheduler(cfg, telegram=None)
        print(sched.status_text(args.ticket))
        return 0
    Scheduler(cfg, _clients(cfg)).run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
