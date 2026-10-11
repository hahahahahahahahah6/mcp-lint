# mcp-lint

A design linter for MCP servers. Bad MCP tool design — missing or rambling
descriptions, vague names, unpaginated list tools, destructive tools with no
confirmation hint, near-duplicate tool names — wastes context and confuses
agents.

## The pain

Over on r/mcp, a recurring complaint from people wiring up their first
servers goes something like this:

> "I wired up 4–5 MCP servers and I *still* can't design one from scratch.
> When is something a tool vs a resource? Why does my agent keep calling the
> wrong thing?"

That confusion is almost never a transport problem — it's a **design**
problem. Tools with no description, names like `handle_data`, list endpoints
that dump everything with no pagination, and `delete_*` tools that never
hint at confirmation. mcp-lint catches those mistakes statically, before
your server ships.

## Install

No dependencies — stdlib only, Python 3.9+.

```bash
# clone and run directly
git clone https://github.com/hahahahahahahahah6/mcp-lint
cd mcp-lint
python3 mcp_lint.py audit tools.json
```

## Usage

`tools.json` is either a `tools/list` JSON-RPC result object or a bare JSON
array of tool definitions (`{name, description, inputSchema}`).

```bash
mcp-lint audit tools.json              # human-readable table
mcp-lint audit tools.json --json       # machine-readable JSON
mcp-lint audit tools.json --fail-under 80   # exit 1 if score < 80 (CI gate)
```

Example output:

```
mcp-lint audit — 4 tool(s), design score: 85/100 (2 error(s), 2 warning(s))

  search_repos  (score 100/100)
    OK — no findings

  handle_data  (score 64/100)
    - [missing-description] (error) tool has no description (-20)
    - [vague-name] (warning) name contains a vague filler word (-6)
    - [empty-schema] (error) inputSchema has no properties at all (-10)

  list_issues  (score 90/100)
    - [no-pagination] (error) list-style tool has no limit/offset/cursor/page param (-10)

  delete_repo  (score 85/100)
    - [destructive-no-confirm] (error) destructive tool description has no confirm/approve/dry-run hint (-15)
```

### Rules

| Rule id | Severity | Trigger |
|---|---|---|
| `missing-description` | error | no description at all (−20) |
| `short-description` | warning | description under 20 chars (−5) |
| `long-description` | warning | description over 600 chars — context tax (−5) |
| `vague-name` | warning | name contains do/make/handle/process/manage/util/data (−6) |
| `no-pagination` | error | list/search/find/get_all/query/all tool with no limit/offset/cursor/page param (−10) |
| `destructive-no-confirm` | error | delete/remove/destroy/drop/purge tool with no confirm/approve/dry-run hint in description (−15) |
| `empty-schema` | error | inputSchema has no properties at all (−10) |
| `schema-bloat` | warning | more than 8 required params (−5) |
| `near-duplicate-names` | warning | pairwise: same token bag in different order, identical names, or Levenshtein-close names with overlapping input shape / near-identical descriptions — a confusable pair (−12) |

Each tool starts at 100 and loses points per finding; the server score is
the mean of its tool scores. 100 = clean. Severity is advisory — the score
math is unchanged — but CI can now distinguish "will confuse the agent"
(errors) from "worth a cleanup pass" (warnings).

### v0.3: rule severity levels

Every finding now carries a severity. `near-duplicate-names` is
**warning-level**: the schema isn't broken, but an agent can confidently
call the wrong tool — e.g. `read_file` vs `readfile` vs `read-file` in the
same server. Errors are the design mistakes that break agent usage
(missing descriptions, unpaginated list tools, destructive tools with no
confirmation hint, empty schemas).

### v0.2: near-duplicate names

A missing description makes the model hesitate; a near-duplicate name
makes it **confidently call the wrong tool** — the nastier failure mode.
mcp-lint now checks every pair of tools in `tools/list`:

- **word-order swap**: `search_items` vs `item_search` (plural-insensitive)
- **identical names**, modulo separators/case: `backup_db` vs `backup-db`
- **Levenshtein-close names** (≤ 2 edits, 4+ chars) **plus** corroborating
  evidence: overlapping input shape, or near-identical descriptions.
  `get_user` vs `get_users` with the same params is flagged; `add` vs `and`
  is not.

```
mcp-lint audit — 2 tool(s), design score: 88/100 (0 error(s), 2 warning(s))

  search_items  (score 88/100)
    - [near-duplicate-names] (warning) near-duplicate of 'item_search' (same words, different order/plural) (-12)

  item_search  (score 88/100)
    - [near-duplicate-names] (warning) near-duplicate of 'search_items' (same words, different order/plural) (-12)
```

Thanks to [arobakid](https://dev.to/arobakid) (Elva) for requesting this
rule — and for volunteering to test it. He works on OpenAPI→MCP naming
scoring by day, so this one comes straight from production pain.

## mcp-lint vs mcp-tax

Sibling tools, different axis. [mcp-tax](https://github.com/hahahahahahahahah6/mcp-tax)
audits **token cost** — how much context your MCP server burns. mcp-lint
audits **design quality** — whether the tools are shaped well enough for an
agent to use correctly. A server can be cheap and still unusable, or
well-designed and still expensive. Run both.

## Tests

```bash
python3 -m unittest tests.test_smoke -v
```

## License

MIT — see [LICENSE](LICENSE).
