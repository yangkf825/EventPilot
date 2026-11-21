"""No API calls: registration, pausing and resume isolation of the suite."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import run_methods as suite
from eventpilot.report import _efficiency


class SuiteTests(unittest.TestCase):
    def test_plan_five_methods_main_only_does_not_write_or_require_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'run'
            with patch.object(suite.subprocess,'run') as command,contextlib.redirect_stdout(io.StringIO()):
                command.return_value.returncode=0
                suite.main(['--run-root',str(root),'--dry-run'])
            self.assertFalse(root.exists())
            self.assertEqual(command.call_count,5)
            for call in command.call_args_list:
                argv=call.args[0]
                self.assertEqual(argv[argv.index('--experiments')+1],'main')
                self.assertIn('--dry-run',argv)

    def test_quota_stops_suite_and_resume_requires_same_identity(self):
        with tempfile.TemporaryDirectory() as tmp,patch.dict(os.environ,{'GATEWAY_DS_API_KEY':'LOCAL_TEST_NOT_A_KEY'}):
            root=Path(tmp)/'run';args=['--run-root',str(root)]
            with patch.object(suite,'stream_child',return_value=2) as child,contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as error:suite.main(args)
                self.assertEqual(error.exception.code,2)
                self.assertEqual(child.call_count,1)
            summary=json.loads((root/'tables/suite_summary.json').read_text())
            self.assertEqual(summary['registered_canonical'],1000)
            self.assertEqual(summary['recorded'],0)
            self.assertFalse(summary['complete_run'])
            with patch.object(suite,'stream_child',return_value=0) as child,contextlib.redirect_stdout(io.StringIO()):
                suite.main(args+['--resume','--retry-infra','--workers','16'])
                self.assertEqual(child.call_count,5)
            with contextlib.redirect_stderr(io.StringIO()),self.assertRaises(SystemExit):
                suite.main(args+['--resume','--output-tokens','9000'])
            with patch.object(suite,'stream_child') as child,contextlib.redirect_stdout(io.StringIO()):
                suite.main(args+['--export-only'])
                child.assert_not_called()

    def test_token_counts_cache_and_unknown(self):
        cached={'api_called':False,'method_stage':'cot_reason','response_origin':'identical_prompt_cached_model_prediction'}
        missing={'api_called':True,'method_stage':'cot_final'}
        stats=_efficiency({'calls':[cached,missing],'usage':[]})
        self.assertIsNone(stats['input_tokens'])
        self.assertEqual(stats['cache_hits'],1)
        stats=_efficiency({'calls':[cached,{'api_called':True,'usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}}],
                           'usage':[{'usage':{'prompt_tokens':10,'completion_tokens':2,'total_tokens':12}}]})
        self.assertEqual(stats['total_tokens'],12)
        self.assertEqual(stats['api_calls'],1)
        self.assertEqual(stats['stage_efficiency'][0]['model_requests'],1)
        malformed=_efficiency({'calls':[{'api_called':True,'usage':['invalid']}],
                               'usage':[{'usage':['invalid']}]})
        self.assertIsNone(malformed['stage_efficiency'][0]['total_tokens'])
        self.assertIsNone(malformed['total_tokens'])


if __name__=='__main__':unittest.main()
