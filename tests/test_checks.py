"""Unit tests for the eval-harness engine: checks, fence handling, suite loading."""
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import eval as ev


CTX = {"judge_model": "claude"}


class TestStripFences(unittest.TestCase):
    def test_strips_json_fences(self):
        self.assertEqual(ev.strip_fences('```json\n{"a": 1}\n```'), '{"a": 1}')

    def test_plain_text_untouched(self):
        self.assertEqual(ev.strip_fences('{"a": 1}'), '{"a": 1}')


class TestRuleChecks(unittest.TestCase):
    def test_contains_case_insensitive(self):
        ok, _ = ev.check_contains("Take a REST day.", ["rest"], CTX)
        self.assertTrue(ok)
        ok, detail = ev.check_contains("all hard days", ["rest"], CTX)
        self.assertFalse(ok)
        self.assertIn("rest", detail)

    def test_not_contains(self):
        ok, _ = ev.check_not_contains("ease off today", ["push through"], CTX)
        self.assertTrue(ok)
        ok, _ = ev.check_not_contains("just push through it", ["push through"], CTX)
        self.assertFalse(ok)

    def test_regex(self):
        ok, _ = ev.check_regex("Sunday is your Long Run", r"long run", CTX)
        self.assertTrue(ok)

    def test_is_json_handles_fences(self):
        ok, _ = ev.check_is_json('```json\n{"days": 7}\n```', True, CTX)
        self.assertTrue(ok)
        ok, _ = ev.check_is_json("not json at all", True, CTX)
        self.assertFalse(ok)

    def test_json_schema(self):
        schema = {"type": "object", "required": ["days"]}
        ok, _ = ev.check_json_schema('{"days": []}', schema, CTX)
        self.assertTrue(ok)
        ok, detail = ev.check_json_schema('{"weeks": []}', schema, CTX)
        self.assertFalse(ok)
        self.assertIn("days", detail)

    def test_numeric_bounds(self):
        spec = {"pattern": r"(\d+)\s*miles", "min": 0, "max": 26}
        ok, _ = ev.check_numeric_bounds("run 22 miles next week", spec, CTX)
        self.assertTrue(ok)
        ok, _ = ev.check_numeric_bounds("run 40 miles next week", spec, CTX)
        self.assertFalse(ok)
        ok, _ = ev.check_numeric_bounds("no numbers here", spec, CTX)
        self.assertFalse(ok)          # nothing matched = fail loudly

    def test_word_count(self):
        ok, _ = ev.check_word_count("one two three", {"min": 2, "max": 5}, CTX)
        self.assertTrue(ok)
        ok, _ = ev.check_word_count("one", {"min": 2}, CTX)
        self.assertFalse(ok)


class TestLLMJudge(unittest.TestCase):
    def test_passes_at_threshold(self):
        with patch.object(ev, "run_model", return_value='{"score": 5, "reason": "safe"}'):
            ok, detail = ev.check_llm_judge("output", {"rubric": "r"}, CTX)
        self.assertTrue(ok)
        self.assertIn("5/5", detail)

    def test_fails_below_threshold(self):
        with patch.object(ev, "run_model", return_value='{"score": 2, "reason": "unsafe"}'):
            ok, _ = ev.check_llm_judge("output", {"rubric": "r", "threshold": 4}, CTX)
        self.assertFalse(ok)

    def test_judge_error_fails_closed(self):
        with patch.object(ev, "run_model", side_effect=RuntimeError("no key")):
            ok, detail = ev.check_llm_judge("output", {"rubric": "r"}, CTX)
        self.assertFalse(ok)
        self.assertIn("judge error", detail)


class TestSuiteLoading(unittest.TestCase):
    def test_bare_list_format(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "s.yaml"
            p.write_text('- name: t1\n  prompt: hi\n  checks:\n    contains: ["hi"]\n')
            config, tests = ev.load_suite(str(p))
            self.assertEqual(config, {})
            self.assertEqual(tests[0]["name"], "t1")

    def test_system_file_loads_relative_prompt(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            (d / "persona.md").write_text("You are a support bot.")
            (d / "s.yaml").write_text(
                "config:\n  system_file: persona.md\ntests:\n"
                '- name: t1\n  prompt: hi\n  checks:\n    contains: ["hi"]\n')
            config, _ = ev.load_suite(str(d / "s.yaml"))
            self.assertEqual(config["system"], "You are a support bot.")


class TestScoring(unittest.TestCase):
    def test_mock_end_to_end(self):
        test = {"name": "echo", "prompt": "include rest day",
                "checks": {"contains": ["rest"], "is_json": True}}
        ok, results, output = ev.score_test(test, "mock", {})
        self.assertFalse(ok)                       # echo passes contains, fails is_json
        self.assertEqual(output, "include rest day")
        self.assertEqual(len(results), 2)


if __name__ == "__main__":
    unittest.main()
