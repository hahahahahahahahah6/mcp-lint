#!/usr/bin/env python3
"""mcp-lint: a design linter for MCP servers.

Statically audits a server's tool definitions and reports design mistakes
that waste context or confuse agents: missing/rambling descriptions, vague
names, unpaginated list tools, destructive tools with no confirmation hint,
empty schemas, schema bloat, and near-duplicate tool names that make an
agent confidently call the wrong tool.

Every finding carries a severity — "error" for design mistakes that break
agent usage, "warning" for smells worth fixing. The numeric score is
unchanged: each tool starts at 100 and loses points per finding.

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

VERSION = "0.3.0"

# ---------------------------------------------------------------------------
# Rule catalogue: rule id -> (weight deducted from tool score, severity,
# explanation). Severity is "error" for design mistakes that break agent
# usage and "warning" for smells worth fixing; near-duplicate names are
# warning-level: they don't break a schema, they make an agent confidently
# call the wrong tool.
# ---------------------------------------------------------------------------

RULES: Dict[str, tuple] = {
    "missing-description": (20, "error", "tool has no description"),
    "short-description": (5, "warning", "description under 20 chars"),
    "long-description": (5, "warning", "description over 600 chars (context tax)"),
    "vague-name": (6, "warning", "name contains a vague filler word"),
    "no-pagination": (10, "error", "list-style tool has no limit/offset/cursor/page param"),
    "destructive-no-confirm": (
        15,
        "error",
        "destructive tool description has no confirm/approve/dry-run hint",
    ),
    "empty-schema": (10, "error", "inputSchema lists required params but defines no properties"),
    "schema-bloat": (5, "warning", "more than 8 required params"),
    "near-duplicate-names": (
        12,
        "warning",
        "near-duplicate of another tool's name (confusable pair)",
    ),
}

VAGUE_TOKENS = {"do", "make", "handle", "process", "manage", "util", "data"}
LISTING_TOKENS = {"list", "search", "find", "get_all", "query", "all"}
DESTRUCTIVE_TOKENS = {"delete", "remove", "destroy", "drop", "purge"}
PAGINATION_PARAMS = {"limit", "offset", "cursor", "page", "pagesize", "page_size",
                     "max_results", "per_page", "top_k"}
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


# ---------------------------------------------------------------------------
# near-duplicate-names (v0.2.0): a pairwise check across the whole tool list.
# A missing description makes the model hesitate; a near-duplicate name
# makes it confidently call the wrong tool — the nastier failure mode.
# ---------------------------------------------------------------------------

def _norm_name(name: str) -> str:
    """Lowercase name with every separator stripped: search_items -> searchitems."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _levenshtein(a: str, b: str) -> int:
    """Iterative edit distance (stdlib-only)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1,          # deletion
                           cur[j - 1] + 1,      # insertion
                           prev[j - 1] + (ca != cb)))  # substitution
        prev = cur
    return prev[-1]


def _stem(token: str) -> str:
    """Light plural stemmer, used only for name-token comparison so that
    search_items vs item_search still counts as the same word bag."""
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _desc_words(description: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (description or "").lower()))


def _desc_similarity(d1: str, d2: str) -> float:
    """Jaccard similarity of description word sets. Empty descriptions carry
    no evidence, so they score 0 rather than 'identical'."""
    w1, w2 = _desc_words(d1), _desc_words(d2)
    if not w1 or not w2:
        return 0.0
    return len(w1 & w2) / len(w1 | w2)


def _shape_overlap(t1: Dict[str, Any], t2: Dict[str, Any]) -> float:
    """Overlap of inputSchema property names, normalized by the smaller set.
    Two zero-parameter tools have identical (empty) shapes -> 1.0; a tool
    with params vs one without -> 0.0."""
    p1 = set(_properties(t1))
    p2 = set(_properties(t2))
    if not p1 and not p2:
        return 1.0
    if not p1 or not p2:
        return 0.0
    return len(p1 & p2) / min(len(p1), len(p2))


def find_near_duplicate_pairs(tools: List[Dict[str, Any]]) -> List[tuple]:
    """Return [(i, j, reason)] for confusable name pairs in a tool list."""
    pairs = []
    names = [t.get("name") or "" for t in tools]
    for i in range(len(tools)):
        for j in range(i + 1, len(tools)):
            n1, n2 = names[i], names[j]
            if not n1 or not n2:
                continue
            # (a) Same token bag in a different order — the classic
            # search_items vs item_search — or the same name modulo
            # separators/case. Inherently confusable; no further evidence
            # needed. Token comparison is plural-insensitive so that a
            # singular/plural flip doesn't hide the swap.
            tok1 = sorted(_tokens(n1))
            tok2 = sorted(_tokens(n2))
            stem1 = sorted(_stem(t) for t in tok1)
            stem2 = sorted(_stem(t) for t in tok2)
            if tok1 == tok2 or stem1 == stem2 or _norm_name(n1) == _norm_name(n2):
                if n1.lower() == n2.lower():
                    reason = "identical names"
                elif _norm_name(n1) == _norm_name(n2):
                    reason = "identical names (modulo separators/case)"
                elif tok1 == tok2:
                    reason = "word-order swap"
                else:
                    reason = "same words, different order/plural"
                pairs.append((i, j, reason))
                continue
            # (b) Levenshtein-close names only count when something else
            # corroborates: overlapping input shape, or near-identical
            # descriptions. The min-length guard keeps short distinct verbs
            # like add/and out of the blast radius.
            na, nb = _norm_name(n1), _norm_name(n2)
            if na and nb and min(len(na), len(nb)) >= 4:
                dist = _levenshtein(na, nb)
                if 0 < dist <= 2:
                    if _shape_overlap(tools[i], tools[j]) >= 0.5:
                        pairs.append((i, j, "names differ by %d edit(s); "
                                            "input shapes overlap" % dist))
                    elif _desc_similarity(tools[i].get("description") or "",
                                          tools[j].get("description") or "") >= 0.6:
                        pairs.append((i, j, "names differ by %d edit(s); "
                                            "descriptions near-identical" % dist))
    return pairs


def lint_tool(tool: Dict[str, Any]) -> Dict[str, Any]:
    """Lint one tool definition -> {name, findings, score}."""
    name = tool.get("name") or "<unnamed>"
    description = tool.get("description") or ""
    findings: List[Dict[str, str]] = []

    def add(rule_id: str) -> None:
        weight, severity, hint = RULES[rule_id]
        findings.append({"rule": rule_id, "severity": severity,
                         "hint": hint, "weight": weight})

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

    # 4. destructive tool without confirmation hint.
    # If the tool explicitly declares annotations.destructiveHint (true or
    # false, per the MCP spec), respect the author's declaration and skip the
    # name-based heuristic.
    annotations = tool.get("annotations")
    destructive_hint = (annotations.get("destructiveHint")
                        if isinstance(annotations, dict) else None)
    if tokens & DESTRUCTIVE_TOKENS and destructive_hint is None:
        lowered = description.lower()
        if not any(h in lowered for h in CONFIRM_HINTS):
            add("destructive-no-confirm")

    # 5. empty schema. A schema with no properties AND no required params is
    # an intentionally empty schema (a legitimate zero-parameter tool like
    # get_time), not a mistake — only flag it when required params exist but
    # properties are missing.
    if not props and _required(tool):
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

    # Cross-tool rule: near-duplicate names. Applied here (not in lint_tool)
    # because it needs the whole tool list. Warning-level: the schema isn't
    # broken, but an agent can confidently call the wrong one.
    nd_weight, nd_severity, _ = RULES["near-duplicate-names"]
    for i, j, reason in find_near_duplicate_pairs(tools):
        for idx, other in ((i, j), (j, i)):
            other_name = tools[other].get("name") or "<unnamed>"
            tool_reports[idx]["findings"].append({
                "rule": "near-duplicate-names",
                "severity": nd_severity,
                "hint": f"near-duplicate of '{other_name}' ({reason})",
                "weight": nd_weight,
            })
            tool_reports[idx]["score"] = max(
                0,
                100 - sum(f["weight"] for f in tool_reports[idx]["findings"]),
            )

    score = (
        round(sum(r["score"] for r in tool_reports) / len(tool_reports))
        if tool_reports
        else 100
    )
    return {"score": score, "tool_count": len(tool_reports), "tools": tool_reports}


def render_table(report: Dict[str, Any]) -> str:
    lines = []
    errors = sum(1 for t in report["tools"] for f in t["findings"]
                 if f.get("severity") == "error")
    warnings = sum(1 for t in report["tools"] for f in t["findings"]
                   if f.get("severity") == "warning")
    lines.append(f"mcp-lint audit — {report['tool_count']} tool(s), "
                 f"design score: {report['score']}/100 "
                 f"({errors} error(s), {warnings} warning(s))")
    lines.append("")
    for t in report["tools"]:
        header = f"  {t['name']}  (score {t['score']}/100)"
        lines.append(header)
        if t["findings"]:
            for f in t["findings"]:
                lines.append(f"    - [{f['rule']}] ({f.get('severity', 'warning')}) "
                             f"{f['hint']} (-{f['weight']})")
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
