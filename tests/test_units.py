import json
import os
import shutil
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from xichu_agent import commands, youtube
from xichu_agent.executors import CodexExecutor, Job, codex_account_id

FAKE_CODEX = r'''
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    print(os.environ.get("FAKE_CODEX_LOGIN", "Logged in using ChatGPT")); sys.exit(0)
cwd = Path(args[args.index("-C") + 1]); out = Path(args[args.index("-o") + 1])
prompt = sys.stdin.read()
(cwd / "_fake.json").write_text(json.dumps({"args": args, "has_key": "OPENAI_API_KEY" in os.environ,
    "has_anthropic_key": "ANTHROPIC_API_KEY" in os.environ,
    "codex_home": os.environ.get("CODEX_HOME"), "prompt": prompt}), encoding="utf-8")
(cwd / "result.json").write_text(json.dumps({"status": "ok", "summary": "fake", "artifacts": []}), encoding="utf-8")
out.write_text("done", encoding="utf-8")
print(json.dumps({"type": "thread.started"})); print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1}}))
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
    def test_parse(self):
        self.assertEqual(commands.parse("/video 做一期").kind, "video")
        self.assertEqual(commands.parse("/video@xichu_bot 做一期", "xichu_bot").arg, "做一期")
        self.assertIsNone(commands.parse("/video@other_bot 做一期", "xichu_bot"))
        self.assertEqual(commands.parse("审核：看 009").kind, "review")
        self.assertEqual(commands.parse("/retry t-0012").ticket_id, "T-0012")
        self.assertIsNone(commands.parse("随便聊聊"))

    def test_auth(self):
        msg = {"chat": {"id": -1, "type": "supergroup"}, "from": {"id": 5}}
        self.assertTrue(commands.authorized(msg, 5, -1)[0])
        self.assertEqual(commands.authorized({**msg, "from": {"id": 6}}, 5, -1)[1], "not_owner")
        self.assertEqual(commands.authorized({**msg, "sender_chat": {"id": -1}}, 5, -1)[1], "anonymous_sender")
        self.assertEqual(commands.authorized({"chat": {"id": 5, "type": "private"}, "from": {"id": 5}}, 5, -1)[1],
                         "not_group")

    def test_text_hash_normalizes(self):
        self.assertEqual(commands.text_hash("video", "做 一期"), commands.text_hash("video", " 做  一期 "))
        self.assertNotEqual(commands.text_hash("video", "a"), commands.text_hash("site", "a"))

    def test_youtube(self):
        for url in ("https://youtu.be/5dtMo0QiJv0", "https://www.youtube.com/watch?v=5dtMo0QiJv0&t=3",
                    "https://youtube.com/shorts/5dtMo0QiJv0"):
            self.assertEqual(youtube.extract_video_id(url), "5dtMo0QiJv0")
        for url in ("http://youtu.be/5dtMo0QiJv0", "https://evil.com/watch?v=5dtMo0QiJv0",
                    "https://www.youtube.com/playlist?list=PL123", "https://youtu.be/short"):
            self.assertIsNone(youtube.extract_video_id(url))
        self.assertEqual(youtube.find_url("链接 https://youtu.be/5dtMo0QiJv0。"), "https://youtu.be/5dtMo0QiJv0")


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xa-ex-"))
        self.work = self.tmp / "w"
        self.work.mkdir()
        self.codex = make_cli(self.tmp, "codex", FAKE_CODEX)
        os.environ["OPENAI_API_KEY"] = "sk-should-not-leak"
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-should-not-leak"

    def tearDown(self):
        for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "FAKE_CODEX_LOGIN"):
            os.environ.pop(k, None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def job(self, profile=None):
        return Job("T-0001", "produce", 1, self.work, "提示词：合成", 60, profile or {})

    def test_codex_flags_env_and_account_home(self):
        home = self.tmp / "codex_b_home"
        home.mkdir()
        ex = CodexExecutor("codex_b", {"cli": self.codex, "codex_home": str(home), "label": "B",
                           "windows_sandbox": "elevated", "model": "configured-model", "model_reasoning_effort": "xhigh"})
        out = ex.run(self.job({"no_project_docs": True}))
        self.assertTrue(out.ok, out.error)
        fake = json.loads((self.work / "_fake.json").read_text(encoding="utf-8"))
        self.assertFalse(fake["has_key"])
        self.assertFalse(fake["has_anthropic_key"])
        self.assertEqual(fake["codex_home"], str(home))
        a = fake["args"]
        for flag in ("--ask-for-approval", "exec", "--sandbox", "workspace-write", "--json", "--ignore-user-config",
                     'forced_login_method="chatgpt"', "sandbox_workspace_write.network_access=false",
                     "project_doc_max_bytes=0", 'windows.sandbox="elevated"', "-m", "configured-model",
                     'model_reasoning_effort="xhigh"'):
            self.assertIn(flag, a)
        self.assertEqual(a[-1], "-")
        self.assertIn("提示词：合成", fake["prompt"])

    def test_codex_refuses_non_chatgpt_login(self):
        os.environ["FAKE_CODEX_LOGIN"] = "Logged in using an API key"
        ex = CodexExecutor("codex_a", {"cli": self.codex})
        out = ex.run(self.job())
        self.assertFalse(out.ok)
        self.assertIn("未用 ChatGPT 订阅登录", out.error)

    def test_stage_reasoning_override_keeps_executor_default(self):
        conf={"cli":self.codex,"model":"configured-model","model_reasoning_effort":"xhigh"}
        ex=CodexExecutor('codex_c',conf)
        out=ex.run(self.job({'reasoning_effort':'medium'}))
        self.assertTrue(out.ok,out.error)
        fake=json.loads((self.work/'_fake.json').read_text(encoding='utf-8'))
        self.assertIn('model_reasoning_effort="medium"',fake['args'])
        self.assertNotIn('model_reasoning_effort="xhigh"',fake['args'])
        self.assertEqual(conf['model_reasoning_effort'],'xhigh')
        self.assertEqual(out.detail['model_reasoning_effort'],'medium')

    def test_schema_result_is_saved_without_model_file_writes(self):
        payload={"status":"ok","route":"review","brief":"只读验收","question":"","summary":"派给审核"}
        body='\n'.join(line for line in FAKE_CODEX.split('\n') if not line.startswith('(cwd / "result.json")'))
        body=body.replace('out.write_text("done", encoding="utf-8")',
                          'out.write_text('+repr(json.dumps(payload,ensure_ascii=False))+', encoding="utf-8")')
        cli=make_cli(self.tmp,'schema_codex',body)
        schema=self.work/'schema.json';schema.write_text('{}',encoding='utf-8')
        ex=CodexExecutor('codex_c',{'cli':cli})
        out=ex.run(self.job({'output_schema':str(schema),'structured_result':True}))
        self.assertTrue(out.ok,out.error)
        self.assertTrue(out.detail['structured_delivery'])
        self.assertEqual(json.loads((self.work/'result.json').read_text(encoding='utf-8')),payload)
        args=json.loads((self.work/'_fake.json').read_text(encoding='utf-8'))['args']
        self.assertIn('--output-schema',args)

    def test_codex_account_id_compare(self):
        import base64
        def home(name, acct):
            h = self.tmp / name
            h.mkdir()
            claims = base64.urlsafe_b64encode(json.dumps(
                {"https://api.openai.com/auth": {"chatgpt_account_id": acct}}).encode()).decode().rstrip("=")
            (h / "auth.json").write_text(json.dumps({"tokens": {"id_token": f"x.{claims}.y"}}), encoding="utf-8")
            return str(h)
        a, b, c = home("a", "acct-1"), home("b", "acct-1"), home("c", "acct-2")
        self.assertEqual(codex_account_id(a), codex_account_id(b))
        self.assertNotEqual(codex_account_id(a), codex_account_id(c))
        self.assertIsNone(codex_account_id(str(self.tmp / "missing")))

    def test_codex_missing_home(self):
        ex = CodexExecutor("codex_b", {"cli": self.codex, "codex_home": str(self.tmp / "nope")})
        self.assertIn("CODEX_HOME 不存在", ex.preflight()["error"])



if __name__ == "__main__":
    unittest.main()


class MigrationTests(unittest.TestCase):
    def test_old_database_is_upgraded(self):
        import sqlite3
        from xichu_agent.store import Store
        tmp = Path(tempfile.mkdtemp(prefix="xa-mig-"))
        db = tmp / "agent.db"
        c = sqlite3.connect(db)
        c.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('tg_offset', '688999461');
        CREATE TABLE tickets (id TEXT PRIMARY KEY, seq INTEGER NOT NULL UNIQUE, chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL, user_id INTEGER NOT NULL, robot TEXT NOT NULL, text TEXT NOT NULL,
            text_hash TEXT NOT NULL, state TEXT NOT NULL, stage TEXT, round INTEGER NOT NULL DEFAULT 1,
            youtube_url TEXT, youtube_info TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE (chat_id, message_id));
        CREATE TABLE stages (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT NOT NULL, name TEXT NOT NULL,
            executor TEXT NOT NULL, round INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1, workdir TEXT, result TEXT, error TEXT,
            simulated INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
            last_heartbeat TEXT);
        INSERT INTO stages(ticket_id,name,executor,status,created_at) VALUES ('T-0001','produce','codex_a','ok','x');
        CREATE TABLE outbox (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id TEXT, dedupe_key TEXT NOT NULL UNIQUE,
            chat_id INTEGER NOT NULL, reply_to INTEGER, text TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0,
            sent_message_id INTEGER, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at TEXT NOT NULL);
        """)
        c.commit()
        c.close()
        s = Store(db)
        cols = lambda t: {r["name"] for r in s.conn().execute(f"PRAGMA table_info({t})")}
        self.assertIn("bot", cols("outbox"))
        self.assertIn("role", cols("stages"))
        self.assertIn("brief", cols("tickets"))
        self.assertEqual(s.conn().execute("SELECT role FROM stages").fetchone()[0], "video")
        self.assertEqual(s.get_meta("tg_offset:pm"), "688999461")
        s.close()
        Store(db).close()  # 再次打开不报错
        shutil.rmtree(tmp, ignore_errors=True)
