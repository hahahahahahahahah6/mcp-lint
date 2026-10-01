#!/usr/bin/env python3
"""mcp-lint: a design linter for MCP servers.

Statically audits a server's tool definitions and reports design mistakes
that waste context or confuse agents: missing/rambling descriptions, vague
names, unpaginated list tools, destructive tools with no confirmation hint,
empty schemas, and schema bloat.

Usage:
    mcp-lint audit tools.json [--json] [--fail-under N]

tools.json is either a tools/list JSON-RPC result object or a bare JSON
array of tool definitions ({name, description, inputSchema}).

Stdlib only, Python 3.9+.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Dict, List

VERSION = "0.1.0"

# ---------------------------------------------------------------------------
# Rule catalogue: rule id -> (weight deducted from tool score, explanation)
# ---------------------------------------------------------------------------

RULES: Dict[str, tuple] = {
    "missing-description": (20, "tool has no description"),
    "short-description": (5, "description under 20 chars"),
    "long-description": (5, "description over 600 chars (context tax)"),
    "vague-name": (6, "name contains a vague filler word"),
    "no-pagination": (10, "list-style tool has no limit/offset/cursor/page param"),
    "destructive-no-confirm": (
        15,
        "destructive tool description has no confirm/approve/dry-run hint",
    ),
    "empty-schema": (10, "inputSchema has no properties at all"),
    "schema-bloat": (5, "more than 8 required params"),
}

VAGUE_TOKENS = {"do", "make", "handle", "process", "manage", "util", "data"}
LISTING_TOKENS = {"list", "search", "find", "get_all", "query", "all"}
DESTRUCTIVE_TOKENS = {"delete", "remove", "destroy", "drop", "purge"}
PAGINATION_PARAMS = {"limit", "offset", "cursor", "page", "pagesize", "page_size"}
CONFIRM_HINTS = ("confirm", "approve", "dry-run", "dry run")


def _tokens(name: str) -> List[str]:
    """Split a tool name into lowercase tokens on separators and case edges."""
    name = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name or "")
    return re.split(r"[^a-z0-9]+", name.lower())


def _properties(tool: Dict[str, Any]) -> Dict[str, Any]:
    schema = tool.get("inputSchema") or {}
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties") or {}
    return props if isinstance(props, dict) else {}


def _required(schema_tool: Dict[str, Any]) -> List[str]:
    schema = schema_tool.get("inputSchema") or {}
    if not isinstance(schema, dict):
        return []
    req = schema.get("required") or []
    return list(req) if isinstance(req, list) else []


def lint_tool(tool: Dict[str, Any]) -> Dict[str, Any]:
    """Lint one tool definition -> {name, findings, score}."""
    name = tool.get("name") or "<unnamed>"
    description = tool.get("description") or ""
    findings: List[Dict[str, str]] = []

    def add(rule_id: str) -> None:
        weight, hint = RULES[rule_id]
        findings.append({"rule": rule_id, "hint": hint, "weight": weight})

    tokens = set(_tokens(name))
    props = _properties(tool)

    # 1. descriptions
    if not description.strip():
        add("missing-description")
    else:
        if len(description) < 20:
            add("short-description")
        if len(description) > 600:
            add("long-description")

    # 2. vague name
    if tokens & VAGUE_TOKENS:
        add("vague-name")

    # 3. listing tool without pagination
    if tokens & LISTING_TOKENS:
        param_names = {p.lower() for p in props}
        if not (param_names & PAGINATION_PARAMS):
            add("no-pagination")

    # 4. destructive tool without confirmation hint
    if tokens & DESTRUCTIVE_TOKENS:
        lowered = description.lower()
        if not any(h in lowered for h in CONFIRM_HINTS):
            add("destructive-no-confirm")

    # 5. empty schema
    if not props:
        add("empty-schema")

    # 6. schema bloat
    if len(_required(tool)) > 8:
        add("schema-bloat")

    score = max(0, 100 - sum(f["weight"] for f in findings))
    return {"name": name, "findings": findings, "score": score}


def load_tools(path: str) -> List[Dict[str, Any]]:
    """Load tool definitions from a bare array or a tools/list result object."""
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)

    if isinstance(data, list):
        tools = data
    elif isinstance(data, dict):
        result = data.get("result", data)
        tools = result.get("tools") if isinstance(result, dict) else None
        if tools is None:
            raise ValueError(
                "JSON object has neither a 'tools' array nor a "
                "'result.tools' array"
            )
    else:
        raise ValueError("expected a JSON array or an object with 'tools'")

    if not isinstance(tools, list):
        raise ValueError("'tools' is not a JSON array")
    return tools


def audit(tools: List[Dict[str, Any]]) -> Dict[str, Any]:
    tool_reports = [lint_tool(t) for t in tools]
    score = (
        round(sum(r["score"] for r in tool_reports) / len(tool_reports))
        if tool_reports
        else 100
    )
    return {"score": score, "tool_count": len(tool_reports), "tools": tool_reports}


def render_table(report: Dict[str, Any]) -> str:
    lines = []
    lines.append(f"mcp-lint audit — {report['tool_count']} tool(s), "
                 f"design score: {report['score']}/100")
    lines.append("")
    for t in report["tools"]:
        header = f"  {t['name']}  (score {t['score']}/100)"
        lines.append(header)
        if t["findings"]:
            for f in t["findings"]:
                lines.append(f"    - [{f['rule']}] {f['hint']} (-{f['weight']})")
        else:
            lines.append("    OK — no findings")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mcp-lint",
        description="Lint MCP server tool definitions for design mistakes.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True)
    audit_p = sub.add_parser("audit", help="audit a tool definition file")
    audit_p.add_argument("file", help="tools.json: bare array or tools/list result")
    audit_p.add_argument("--json", action="store_true",
                         help="emit machine-readable JSON")
    audit_p.add_argument("--fail-under", type=int, metavar="N",
                         help="exit 1 if design score is below N (CI gate)")

    args = parser.parse_args(argv)

    if args.command == "audit":
        try:
            tools = load_tools(args.file)
        except FileNotFoundError:
            print(f"mcp-lint: error: file not found: {args.file}",
                  file=sys.stderr)
            return 2
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            print(f"mcp-lint: error: {exc}", file=sys.stderr)
            return 2

        report = audit(tools)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            sys.stdout.write(render_table(report))

        if args.fail_under is not None and report["score"] < args.fail_under:
            return 1
        return 0

    return 0  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
