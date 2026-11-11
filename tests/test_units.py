import json
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from agent_team import commands, config
from agent_team.executors import ClaudeExecutor, CodexExecutor, Job

FAKE_CODEX = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    print(os.environ.get("FAKE_CODEX_LOGIN", "Logged in using ChatGPT")); sys.exit(0)
cwd = Path(args[args.index("-C") + 1]); out = Path(args[args.index("-o") + 1])
prompt = sys.stdin.read()
(cwd / "_fake.json").write_text(json.dumps({"args": args, "has_key": "OPENAI_API_KEY" in os.environ,
    "codex_home": os.environ.get("CODEX_HOME"), "prompt": prompt}), encoding="utf-8")
(cwd / "result.json").write_text(json.dumps({"status": "ok", "summary": "fake", "artifacts": []}), encoding="utf-8")
out.write_text("done", encoding="utf-8")
print(json.dumps({"type": "thread.started"})); print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}))
'''

FAKE_CLAUDE = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": os.environ.get("FAKE_CLAUDE_AUTH", "claude.ai"),
                      "apiProvider": "firstParty", "subscriptionType": "max"})); sys.exit(0)
prompt = sys.stdin.read()
Path("_fake.json").write_text(json.dumps({"args": args, "has_key": "ANTHROPIC_API_KEY" in os.environ,
    "prompt": prompt}), encoding="utf-8")
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "审核完成", "num_turns": 2}))
'''


def make_cli(directory: Path, name: str, body: str) -> str:
    script = directory / f"{name}.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    if os.name == "nt":
        cmd = directory / f"{name}.cmd"
        cmd.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return str(cmd)
    sh = directory / name
    sh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
    sh.chmod(sh.stat().st_mode | stat.S_IEXEC)
    return str(sh)


class CommandTests(unittest.TestCase):
    def setUp(self):
        commands.setup(config.DEFAULTS["roles"])

    def test_parse(self):
        self.assertEqual(commands.parse("/writer 写周报").kind, "writer")
        self.assertEqual(commands.parse("/coder@team_bot 修 bug", "team_bot").arg, "修 bug")
        self.assertIsNone(commands.parse("/coder@other_bot 修 bug", "team_bot"))
        self.assertEqual(commands.parse("写手：写一篇说明").kind, "writer")
        self.assertEqual(commands.parse("审核：看一下这份方案").kind, "review")
        self.assertEqual(commands.parse("/retry t-0012").ticket_id, "T-0012")
        self.assertIsNone(commands.parse("随便聊聊"))
        self.assertIsNone(commands.parse("/video 旧命令不存在"))

    def test_roles_from_config(self):
        commands.setup({"pm": {"label": "经理"}, "ops": {"label": "运维(Codex)", "aliases": ["运维"]}})
        self.assertEqual(commands.parse("运维：看看磁盘").kind, "ops")
        self.assertIsNone(commands.parse("/writer 已删除的岗位"))
        self.assertIn("运维(Codex)", commands.HELP)

    def test_auth(self):
        msg = {"chat": {"id": -1, "type": "supergroup"}, "from": {"id": 5}}
        self.assertTrue(commands.authorized(msg, 5, -1)[0])
        self.assertEqual(commands.authorized({**msg, "from": {"id": 6}}, 5, -1)[1], "not_owner")
        self.assertEqual(commands.authorized({**msg, "sender_chat": {"id": -1}}, 5, -1)[1], "anonymous_sender")
        self.assertEqual(commands.authorized({"chat": {"id": 5, "type": "private"}, "from": {"id": 5}}, 5, -1)[1],
                         "not_group")

    def test_text_hash_normalizes(self):
        self.assertEqual(commands.text_hash("writer", "写 周报"), commands.text_hash("writer", " 写  周报 "))
        self.assertNotEqual(commands.text_hash("writer", "a"), commands.text_hash("coder", "a"))


class QuotaTextTests(unittest.TestCase):
    def test_claude_five_hour_limit_is_quota(self):
        from agent_team.executors import _usage_limited
        self.assertTrue(_usage_limited("You've hit your 5-hour limit · resets 3:20am (Asia/Shanghai)"))
        self.assertTrue(_usage_limited("Codex usage limit reached"))
        self.assertFalse(_usage_limited("SyntaxError: invalid syntax"))


class ConfigTests(unittest.TestCase):
    def test_roles_replace_defaults_and_pm_required(self):
        tmp = Path(tempfile.mkdtemp(prefix="at-cfg-"))
        try:
            (tmp / "config.json").write_text(json.dumps({"roles": {"pm": {"label": "经理", "pool": ["claude"]},
                                                                    "ops": {"label": "运维", "pool": ["codex"]}}}),
                                             encoding="utf-8")
            cfg = config.load(tmp / "config.json")
            self.assertEqual(cfg.workers, ["ops"])
            (tmp / "config.json").write_text(json.dumps({"roles": {"ops": {"label": "运维", "pool": ["codex"]}}}),
                                             encoding="utf-8")
            with self.assertRaises(ValueError):
                config.load(tmp / "config.json")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xa-ex-"))
        self.work = self.tmp / "w"
        self.work.mkdir()
        self.codex = make_cli(self.tmp, "codex", FAKE_CODEX)
        self.claude = make_cli(self.tmp, "claude", FAKE_CLAUDE)
        os.environ["OPENAI_API_KEY"] = "sk-should-not-leak"
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-should-not-leak"

    def tearDown(self):
        for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "FAKE_CODEX_LOGIN", "FAKE_CLAUDE_AUTH"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def job(self, profile=None):
        return Job("T-0001", "work", 1, self.work, "提示词：合成", 60, profile or {})

    def test_codex_flags_env_and_account_home(self):
        home = self.tmp / "codex_home"
        home.mkdir()
        ex = CodexExecutor("codex", {"cli": self.codex, "codex_home": str(home), "label": "Codex", "windows_sandbox": "elevated"})
        out = ex.run(self.job({"no_project_docs": True}))
        self.assertTrue(out.ok, out.error)
        fake = json.loads((self.work / "_fake.json").read_text(encoding="utf-8"))
        self.assertFalse(fake["has_key"])
        self.assertEqual(fake["codex_home"], str(home))
        a = fake["args"]
        for flag in ("--ask-for-approval", "exec", "--sandbox", "workspace-write", "--json", "--ignore-user-config",
                     'forced_login_method="chatgpt"', "sandbox_workspace_write.network_access=false",
                     "project_doc_max_bytes=0", 'windows.sandbox="elevated"'):
            self.assertIn(flag, a)
        self.assertEqual(a[-1], "-")
        self.assertIn("提示词：合成", fake["prompt"])

    def test_codex_refuses_non_chatgpt_login(self):
        os.environ["FAKE_CODEX_LOGIN"] = "Logged in using an API key"
        ex = CodexExecutor("codex", {"cli": self.codex})
        out = ex.run(self.job())
        self.assertFalse(out.ok)
        self.assertIn("未用 ChatGPT 订阅登录", out.error)

    def test_codex_missing_home(self):
        ex = CodexExecutor("codex", {"cli": self.codex, "codex_home": str(self.tmp / "nope")})
        self.assertIn("CODEX_HOME 不存在", ex.preflight()["error"])

    def test_claude_subscription_and_readonly_tools(self):
        ex = ClaudeExecutor("claude", {"cli": self.claude})
        out = ex.run(self.job())
        self.assertTrue(out.ok, out.error)
        fake = json.loads((self.work / "_fake.json").read_text(encoding="utf-8"))
        self.assertFalse(fake["has_key"])
        a = fake["args"]
        self.assertIn("-p", a)
        self.assertIn("Bash,WebFetch,WebSearch", a)
        self.assertIn("--strict-mcp-config", a)

    def test_claude_refuses_api_key_auth(self):
        os.environ["FAKE_CLAUDE_AUTH"] = "api_key"
        ex = ClaudeExecutor("claude", {"cli": self.claude})
        self.assertFalse(ex.preflight()["ok"])


if __name__ == "__main__":
    unittest.main()
