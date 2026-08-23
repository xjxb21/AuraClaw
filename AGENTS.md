# AGENTS.md

## Project

AuraClaw is a pure Python Managed Agent backend. The architecture source of truth is under
`docs/Managed Agent 系统架构/`.

## Commands

```bash
uv sync --extra dev
uv run uvicorn auraclaw.main:app --reload
uv run pytest
uv run ruff check .
uv run mypy src/auraclaw
```

## Architecture rules

- `domain` and `contracts` contain no FastAPI or infrastructure imports.
- Canonical Session Events are the only task fact source.
- Projections are disposable and rebuildable.
- Runtime state never changes business Session state directly.
- Orchestrator schedules resources; Coordinator owns semantic decomposition.
- Runtime Event streams are not a result-delivery guarantee.
- Tools and model providers are accessed through gateways; Agent Runtime never reads secrets.
- All writes carry tenant, command id, expected version, actor, correlation and causation context.
- Relative imports are not used across top-level modules; import through `auraclaw.*`.
- Entrypoints call `composition`; `api`, gateways, and business packages never import `composition`.
- `api` and gateways do not select concrete infrastructure adapters. Infrastructure may implement
  and import stable ports, but does not depend on `api`, gateways, or `composition`.
- Package-to-deployment alignment is maintained by the component/package/entrypoint map in
  `docs/Managed Agent 模块重构方案.md`, not by forcing a 1:1 directory topology.

## Stage completion gate

Every development stage must have a checklist in `docs/开发阶段校验清单.md`. A stage is complete
only after all applicable functional, architecture, test, security, documentation and migration
items are checked. Then commit the complete stage as one intentional Git commit and push the
current branch to `origin`. Never stage `.env`, `.history`, virtual environments, caches or secrets.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **AuraClaw** (7788 symbols, 13374 relationships, 298 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/AuraClaw/context` | Codebase overview, check index freshness |
| `gitnexus://repo/AuraClaw/clusters` | All functional areas |
| `gitnexus://repo/AuraClaw/processes` | All execution flows |
| `gitnexus://repo/AuraClaw/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
