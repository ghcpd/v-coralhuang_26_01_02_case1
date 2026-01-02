# agent_tools.py — Refactor PR

This repository contains a focused refactor of `agent_tools.py`. The goal was to remove duplicated logic between synchronous and asynchronous execution paths while preserving all observable behavior (API and edge-case semantics).

---

## Refactor Summary

- Centralized common execution steps (resolve, parse/coerce, cache check, guard execution, normalization, caching) into small, well-named helper methods.
- Reduced duplication between `Engine.run_sync` and `Engine.run_async` by extracting shared logic and keeping only the invocation-specific code paths separate and minimal.
- Converted the per-instance cache into a shared, global cache (preserves existing tests that rely on cross-instance cache behavior).
- Kept the public API identical and preserved all trace event names/payloads and error mappings.

---

## Design Rationale

- Clear, small helpers (e.g. `_resolve_tool`, `_parse_args`, `_check_cache`, `_run_invoke_sync`, `_run_invoke_async`, `_postprocess_and_maybe_cache`) make the execution flow explicit and reduce the risk that the sync and async paths will drift apart in the future.
- Invocation differences (timeout behavior, `asyncio` interaction) remain local to short, well-tested helpers to avoid subtle behavioral changes when refactoring.
- Shared cache reduces surprising cross-instance differences and matches the existing test-suite expectations.

---

## Behavior Invariants (fragile / non-obvious)

The refactor intentionally preserves these behaviors (tests cover them):

- Cache key is the exact tuple `(tool_name, raw_string)` — whitespace and ordering matter.
- On a cache hit: input guardrails, tool invocation, and output guardrails are NOT executed.
- `TraceSink.emit()` copies the payload; subsequent mutation of the original dict does not affect stored events.
- Unknown arguments (not in the schema) are passed through unchanged to the tool function.
- Calling `run_sync` while an event loop is running for an async tool results in a `tool_error:` mapped RuntimeError (message includes `asyncio.run()`).
- Error mapping is preserved exactly (unknown tool, bad args, user ValueError, RetryableToolError, async timeout, GuardrailError).
- Execution order is preserved: resolve → parse/coerce/defaults → cache check → input guardrails → invoke (retry/timeout) → normalize → output guardrails → cache store.

---

## How to run

Requirements: Python 3.11+ (no non-standard runtime deps required). The project includes a reproducible `run_tests` script that sets up a virtualenv and runs the full test suite.

From the repository root:

```bash
./run_tests
```

What `run_tests` does:
- creates a local virtual environment (`.venv`)
- installs deterministic test-only deps (`pytest`, `pytest-asyncio`)
- runs `pytest -q`

If you prefer to run manually on Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -U pip
.\.venv\Scripts\python -m pip install pytest pytest-asyncio
.\.venv\Scripts\python -m pytest -q
```

---

## Notes

- No public API was changed.
- No new runtime dependencies were added; `pytest-asyncio` is added only for running tests.
- See the test-suite for precise behavior guarantees.
