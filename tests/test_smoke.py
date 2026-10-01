"""Smoke tests for mcp-lint. Run: python3 -m unittest tests.test_smoke -v"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

CLI = os.path.join(os.path.dirname(__file__), "..", "mcp_lint.py")
CLI = os.path.abspath(CLI)

BAD_TOOLS = [
    {"name": "do_handle_data", "description": "",
     "inputSchema": {"type": "object", "properties": {},
                     "required": ["p%d" % i for i in range(9)]}},
    {"name": "make_list", "description": "",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "delete_data", "description": "y",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "process_all_items", "description": "",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "drop_purge_stuff", "description": "x" * 650,
     "inputSchema": {"type": "object", "properties": {},
                     "required": ["p%d" % i for i in range(9)]}},
]

GOOD_TOOLS = [
    {"name": "search_repos",
     "description": "Search public repositories by keyword. Returns a paginated "
                    "list of repository names, descriptions, and star counts.",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"},
                                    "limit": {"type": "integer"},
                                    "page": {"type": "integer"}},
                     "required": ["query"]}},
    {"name": "delete_project",
     "description": "Permanently deletes a project and all its data. You must "
                    "ask the user to confirm before calling this tool.",
     "inputSchema": {"type": "object",
                     "properties": {"project_id": {"type": "string"}},
                     "required": ["project_id"]}},
]


class LintTests(unittest.TestCase):
    def _write(self, data, suffix=".json"):
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        self.addCleanup(os.unlink, path)
        return path

    def _run(self, *argv):
        return subprocess.run(
            [sys.executable, CLI, *argv],
            capture_output=True, text=True, timeout=30,
        )

    def test_bad_toolset_scores_low_and_fires_every_rule(self):
        path = self._write(BAD_TOOLS)
        proc = self._run("audit", path, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertLess(report["score"], 70)
        fired = {f["rule"] for t in report["tools"] for f in t["findings"]}
        for rule in ("missing-description", "short-description",
                     "long-description", "vague-name", "no-pagination",
                     "destructive-no-confirm", "empty-schema", "schema-bloat"):
            self.assertIn(rule, fired, f"rule never fired: {rule}")

    def test_good_toolset_scores_high(self):
        path = self._write(GOOD_TOOLS)
        proc = self._run("audit", path, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertGreaterEqual(report["score"], 90)
        total_findings = sum(len(t["findings"]) for t in report["tools"])
        self.assertEqual(total_findings, 0)

    def test_accepts_bare_array(self):
        path = self._write(GOOD_TOOLS)
        proc = self._run("audit", path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("design score: 100/100", proc.stdout)

    def test_accepts_jsonrpc_wrapped(self):
        wrapped = {"jsonrpc": "2.0", "id": 1,
                   "result": {"tools": GOOD_TOOLS}}
        path = self._write(wrapped)
        proc = self._run("audit", path, "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(report["tool_count"], 2)
        self.assertEqual(report["score"], 100)

    def test_json_output_is_valid(self):
        path = self._write(BAD_TOOLS)
        proc = self._run("audit", path, "--json")
        report = json.loads(proc.stdout)  # raises if invalid
        self.assertIn("tools", report)
        self.assertIn("score", report)
        self.assertTrue(all("rule" in f for t in report["tools"]
                            for f in t["findings"]))

    def test_fail_under_exit_codes(self):
        bad = self._write(BAD_TOOLS)
        good = self._write(GOOD_TOOLS)
        self.assertEqual(self._run("audit", bad, "--fail-under", "80").returncode, 1)
        self.assertEqual(self._run("audit", good, "--fail-under", "80").returncode, 0)
        self.assertEqual(self._run("audit", good, "--fail-under", "100").returncode, 0)

    def test_missing_file_exit_2(self):
        proc = self._run("audit", "/tmp/does-not-exist-mcp-lint.json")
        self.assertEqual(proc.returncode, 2)

    def test_human_table_shows_rule_ids(self):
        path = self._write(BAD_TOOLS)
        proc = self._run("audit", path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("[vague-name]", proc.stdout)
        self.assertIn("do_handle_data", proc.stdout)


class RegressionTests(unittest.TestCase):
    """Direct unit tests for the 2026-10-01 review fixes."""

    def setUp(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import mcp_lint
        self.lint_tool = mcp_lint.lint_tool

    def _rules(self, tool):
        return {f["rule"] for f in self.lint_tool(tool)["findings"]}

    def test_destructive_hint_true_respected(self):
        tool = {"name": "delete_project",
                "description": "Permanently deletes the project.",
                "inputSchema": {"type": "object",
                                "properties": {"id": {"type": "string"}}},
                "annotations": {"destructiveHint": True}}
        self.assertNotIn("destructive-no-confirm", self._rules(tool))

    def test_destructive_hint_false_respected(self):
        tool = {"name": "remove_cache",
                "description": "Clears the local cache directory.",
                "inputSchema": {"type": "object",
                                "properties": {"p": {"type": "string"}}},
                "annotations": {"destructiveHint": False}}
        self.assertNotIn("destructive-no-confirm", self._rules(tool))

    def test_destructive_heuristic_still_fires_without_annotations(self):
        tool = {"name": "delete_project",
                "description": "Permanently deletes the project.",
                "inputSchema": {"type": "object",
                                "properties": {"id": {"type": "string"}}}}
        self.assertIn("destructive-no-confirm", self._rules(tool))

    def test_zero_param_tool_not_penalized(self):
        tool = {"name": "get_time",
                "description": "Returns the current server time as ISO 8601.",
                "inputSchema": {"type": "object"}}
        self.assertNotIn("empty-schema", self._rules(tool))
        self.assertEqual(self.lint_tool(tool)["score"], 100)

    def test_required_without_properties_still_flagged(self):
        tool = {"name": "run_query",
                "description": "Runs the saved query and returns its rows.",
                "inputSchema": {"type": "object", "required": ["query_id"]}}
        self.assertIn("empty-schema", self._rules(tool))

    def test_pagination_param_variants(self):
        for param in ("max_results", "per_page", "top_k", "pageSize"):
            tool = {"name": "list_items",
                    "description": "Lists items in the store with pagination.",
                    "inputSchema": {"type": "object",
                                    "properties": {param: {"type": "integer"}}}}
            self.assertNotIn("no-pagination", self._rules(tool),
                             f"pagination param not recognized: {param}")


if __name__ == "__main__":
    unittest.main()
