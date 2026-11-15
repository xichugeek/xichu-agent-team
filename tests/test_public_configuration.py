import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xichu_agent.config import load
from xichu_agent.capabilities import tool_documents
from control_center.agent_reader import agent_config


class LocalConfigurationTests(unittest.TestCase):
    def test_local_configuration_overrides_public_template_without_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            public={'telegram':{'owner_user_id':0,'group_chat_id':0},'executors':{'codex_a':{'enabled':True}}}
            local={'telegram':{'owner_user_id':42,'group_chat_id':-100},'executors':{'codex_a':{'codex_home':'synthetic-home'}}}
            public_bytes=json.dumps(public).encode()
            (root/'config.json').write_bytes(public_bytes)
            (root/'config.local.json').write_text(json.dumps(local),encoding='utf-8')
            (root/'secrets.local.json').write_text('{"telegram_bot_tokens":{"pm":"SYNTHETIC_TEST_ONLY"}}',encoding='utf-8')
            cfg=load(root/'config.json')
            self.assertEqual(cfg.tg['owner_user_id'],42)
            self.assertEqual(cfg.executor('codex_a')['codex_home'],'synthetic-home')
            self.assertEqual((root/'config.json').read_bytes(),public_bytes)
            original=Path.read_text
            def guarded(path,*args,**kwargs):
                self.assertNotEqual(path.name,'secrets.local.json')
                return original(path,*args,**kwargs)
            with patch.object(Path,'read_text',guarded):
                metadata=agent_config(root)
            self.assertEqual(metadata['telegram']['owner_user_id'],42)
            self.assertNotIn('telegram_bot_tokens',metadata)

    def test_tool_document_home_can_be_unspecified(self):
        with tempfile.TemporaryDirectory() as folder:
            home=Path(folder)
            skill=home/'skills/playwright/SKILL.md';skill.parent.mkdir(parents=True);skill.write_text('synthetic',encoding='utf-8')
            cfg=load(home/'absent.json',overrides={'tools_source_home':None})
            with patch.dict('os.environ',{'CODEX_HOME':str(home)}):
                self.assertEqual(tool_documents(cfg)['browser'],str(skill))


if __name__=='__main__':unittest.main()
