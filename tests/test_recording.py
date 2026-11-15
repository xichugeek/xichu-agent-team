import unittest
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch
from xichu_agent.production_tools import wait_for_stopped, confirm_stop_before_restoring


class RecordingTests(unittest.TestCase):
    def test_failed_stop_never_restores_a_private_scene(self):
        class Obs:
            previous_scene = 'private_window'
            previous_mutes = {'desktop':False}
        obs=Obs()
        with patch('xichu_agent.production_tools.wait_for_stopped',return_value=(False,40)):
            with self.assertRaises(RuntimeError):confirm_stop_before_restoring(obs)
        self.assertIsNone(obs.previous_scene)
        self.assertEqual(obs.previous_mutes,{})

    def test_confirmed_stop_preserves_restoration_data(self):
        class Obs:
            previous_scene = 'original'
            previous_mutes = {'desktop':True}
        obs=Obs()
        with patch('xichu_agent.production_tools.wait_for_stopped',return_value=(True,2)):
            self.assertEqual(confirm_stop_before_restoring(obs),2)
        self.assertEqual(obs.previous_scene,'original')
        self.assertEqual(obs.previous_mutes,{'desktop':True})

    def test_mcp_discovery_is_utf8_with_legacy_windows_codepage(self):
        with tempfile.TemporaryDirectory() as tmp:
            server=Path(__file__).resolve().parents[1]/'xichu_agent/production_tools.py'
            messages=[{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}, {"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}]
            result=subprocess.run([sys.executable,str(server),'--studio',tmp,'--workdir',tmp],
                input=('\n'.join(json.dumps(x) for x in messages)+'\n').encode(),capture_output=True,
                timeout=10,env={**os.environ,'PYTHONIOENCODING':'cp936'})
            rows=[json.loads(x) for x in result.stdout.decode('utf-8').splitlines()]
            self.assertEqual(rows[1]['result']['tools'][0]['name'],'record_studio_test')
            self.assertIn('真实录制',rows[1]['result']['tools'][0]['description'])

    def test_delayed_obs_stop_is_waited_for(self):
        class Obs:
            n = 0
            def request(self, method):
                if method == "GetRecordStatus": self.n += 1
                return {"outputActive": method == "GetRecordStatus" and self.n < 3}
        with patch("xichu_agent.production_tools.time.sleep") as sleep:
            self.assertEqual(wait_for_stopped(Obs()), (True, 2))
            self.assertEqual(sleep.call_count, 2)

    def test_live_stream_is_never_reported_stopped(self):
        class Obs:
            def request(self, method): return {"outputActive": method == "GetStreamStatus"}
        with patch("xichu_agent.production_tools.time.sleep"):
            self.assertEqual(wait_for_stopped(Obs(), attempts=3), (False, 3))


if __name__ == "__main__": unittest.main()
