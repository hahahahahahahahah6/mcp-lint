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


class NearDuplicateTests(unittest.TestCase):
    """Tests for the v0.2.0 near-duplicate-names rule (pairwise check)."""

    def setUp(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        import mcp_lint
        self.mcp_lint = mcp_lint

    def _rules_for(self, tools):
        report = self.mcp_lint.audit(tools)
        return {t["name"]: {f["rule"] for f in t["findings"]}
                for t in report["tools"]}

    def _hint_for(self, tools, name):
        report = self.mcp_lint.audit(tools)
        for t in report["tools"]:
            if t["name"] == name:
                for f in t["findings"]:
                    if f["rule"] == "near-duplicate-names":
                        return f["hint"]
        return ""

    def test_word_swap_pair_flagged(self):
        rules = self._rules_for(NEAR_DUP_TOOLS)
        self.assertIn("near-duplicate-names", rules["search_items"])
        self.assertIn("near-duplicate-names", rules["item_search"])
        self.assertIn("item_search", self._hint_for(NEAR_DUP_TOOLS, "search_items"))

    def test_levenshtein_close_with_overlapping_shape_flagged(self):
        rules = self._rules_for(NEAR_DUP_TOOLS)
        self.assertIn("near-duplicate-names", rules["get_user"])
        self.assertIn("near-duplicate-names", rules["get_users"])

    def test_levenshtein_close_with_similar_description_flagged(self):
        # user_lookup vs user_lookups: 1 edit apart, disjoint param names,
        # but near-identical descriptions -> still flagged.
        rules = self._rules_for(NEAR_DUP_TOOLS)
        self.assertIn("near-duplicate-names", rules["user_lookup"])
        self.assertIn("near-duplicate-names", rules["user_lookups"])

    def test_levenshtein_close_without_corroboration_not_flagged(self):
        # search_users vs search_usres: 2 edits apart, but disjoint input
        # shapes AND unrelated descriptions -> clean.
        tools = [
            {"name": "search_users",
             "description": "Search for user accounts by name fragment.",
             "inputSchema": {"type": "object",
                             "properties": {"q": {"type": "string"},
                                            "limit": {"type": "integer"}},
                             "required": ["q"]}},
            {"name": "search_usres",
             "description": "Export the monthly billing report as CSV.",
             "inputSchema": {"type": "object",
                             "properties": {"month": {"type": "string"},
                                            "page": {"type": "integer"}},
                             "required": ["month"]}},
        ]
        rules = self._rules_for(tools)
        self.assertNotIn("near-duplicate-names", rules["search_users"])
        self.assertNotIn("near-duplicate-names", rules["search_usres"])

    def test_short_names_exempt(self):
        # add vs and: 1 edit apart and identical shapes, but too short to
        # be a meaningful signal -> clean.
        tools = [
            {"name": "add",
             "description": "Adds two integers and returns their sum.",
             "inputSchema": {"type": "object",
                             "properties": {"a": {"type": "integer"},
                                            "b": {"type": "integer"}},
                             "required": ["a", "b"]}},
            {"name": "and",
             "description": "Bitwise AND of two integers, returns the result.",
             "inputSchema": {"type": "object",
                             "properties": {"a": {"type": "integer"},
                                            "b": {"type": "integer"}},
                             "required": ["a", "b"]}},
        ]
        rules = self._rules_for(tools)
        self.assertNotIn("near-duplicate-names", rules["add"])
        self.assertNotIn("near-duplicate-names", rules["and"])

    def test_camelcase_word_swap_flagged(self):
        tools = [
            {"name": "searchItems",
             "description": "Search the product catalog by keyword, paginated.",
             "inputSchema": {"type": "object",
                             "properties": {"query": {"type": "string"},
                                            "limit": {"type": "integer"}},
                             "required": ["query"]}},
            {"name": "itemSearch",
             "description": "Look up products in the catalog with pagination.",
             "inputSchema": {"type": "object",
                             "properties": {"query": {"type": "string"},
                                            "limit": {"type": "integer"}},
                             "required": ["query"]}},
        ]
        rules = self._rules_for(tools)
        self.assertIn("near-duplicate-names", rules["searchItems"])
        self.assertIn("near-duplicate-names", rules["itemSearch"])

    def test_exact_duplicate_names_flagged(self):
        tools = [
            {"name": "backup_db",
             "description": "Creates a full backup of the primary database.",
             "inputSchema": {"type": "object",
                             "properties": {"dest": {"type": "string"}}}},
            {"name": "backup_db",
             "description": "Creates a full backup of the primary database.",
             "inputSchema": {"type": "object",
                             "properties": {"dest": {"type": "string"}}}},
        ]
        rules = self._rules_for(tools)
        self.assertIn("near-duplicate-names", rules["backup_db"])

    def test_clean_toolset_has_no_near_duplicate_findings(self):
        report = self.mcp_lint.audit(CLEAN_TOOLS)
        self.assertEqual(report["score"], 100)
        fired = {f["rule"] for t in report["tools"] for f in t["findings"]}
        self.assertNotIn("near-duplicate-names", fired)

    def test_unpaired_tool_not_flagged_in_mixed_set(self):
        # search_items is paired with item_search only; it must not also
        # be flagged as a duplicate of get_user.
        hint = self._hint_for(NEAR_DUP_TOOLS, "search_items")
        self.assertNotIn("get_user", hint)


NEAR_DUP_TOOLS = [
    # word-order swap, overlapping input shape
    {"name": "search_items",
     "description": "Search the product catalog by keyword. Returns matching items.",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"},
                                    "limit": {"type": "integer"}},
                     "required": ["query"]}},
    {"name": "item_search",
     "description": "Look up products in the catalog. Returns items that match the query.",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"},
                                    "limit": {"type": "integer"}},
                     "required": ["query"]}},
    # Levenshtein-close (1 edit) + overlapping input shape
    {"name": "get_user",
     "description": "Fetch a user record by ID. Returns the full user profile.",
     "inputSchema": {"type": "object",
                     "properties": {"user_id": {"type": "string"}},
                     "required": ["user_id"]}},
    {"name": "get_users",
     "description": "Fetch user records by ID. Returns full user profiles.",
     "inputSchema": {"type": "object",
                     "properties": {"user_id": {"type": "string"}},
                     "required": ["user_id"]}},
    # Levenshtein-close + near-identical descriptions, disjoint param names
    {"name": "user_lookup",
     "description": "Looks up a user by identifier and returns the full profile.",
     "inputSchema": {"type": "object",
                     "properties": {"user_id": {"type": "string"}},
                     "required": ["user_id"]}},
    {"name": "user_lookups",
     "description": "Looks up a user by identifier and returns the full profile.",
     "inputSchema": {"type": "object",
                     "properties": {"id": {"type": "string"}},
                     "required": ["id"]}},
]

CLEAN_TOOLS = [
    {"name": "search_repos",
     "description": "Search public repositories by keyword. Returns a paginated "
                    "list of repository names, descriptions, and star counts.",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"},
                                    "limit": {"type": "integer"},
                                    "page": {"type": "integer"}},
                     "required": ["query"]}},
    {"name": "create_issue",
     "description": "Opens a new issue in the tracker with the given title and body.",
     "inputSchema": {"type": "object",
                     "properties": {"title": {"type": "string"},
                                    "body": {"type": "string"}},
                     "required": ["title"]}},
    {"name": "ping",
     "description": "Health check. Returns pong when the server is alive and ready.",
     "inputSchema": {"type": "object", "properties": {}}},
]


if __name__ == "__main__":
    unittest.main()
