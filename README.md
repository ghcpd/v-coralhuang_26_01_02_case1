# Engine refactor — agent_tools.py

This repository contains a targeted refactor of `agent_tools.py` that removes duplication between synchronous and asynchronous execution paths while preserving all public behavior and invariants.

## Refactor Summary
- Centralized shared logic (argument parsing/coercion, cache handling, guardrails, normalization, trace emission).
- Extracted invocation paths into two small helpers: `_invoke_sync` and `_invoke_async`.
- Reduced duplicated code in `run_sync` and `run_async` by moving common sequences into helper methods.

## Design Rationale
- Shared responsibilities (parse → cache → guards → invoke → normalize → guards → cache) are kept in a single place so that sync/async behavior cannot drift independently.
- Invocation-specific concerns (blocking vs. awaiting, timeout handling) remain isolated in small helpers so behavior is explicit and easy to audit.

## Behavior Invariants (important / fragile)
- Cache key is exactly `(tool_name, raw)` using the raw string (no JSON canonicalization).
- Cache-hit short-circuits: no guardrails or tool invocation run after a cache hit.
- `TraceSink.emit()` copies payloads so later mutation of the original dict does not affect stored events.
- Unknown args are passed through to tool functions unchanged.
- If `run_sync` calls an async tool while an event loop is running, the `RuntimeError` from `asyncio.run()` is mapped to `tool_error:<message>`.
- Error mapping rules (unknown tool, bad args, ValueError → user_error, RetryableToolError → retries, async timeout → `tool_error:timeout`, GuardrailError → `guardrail:<...>`) are preserved.

## How to run
1. Ensure you have Python 3.8+ installed.
2. Run the test suite using the bundled script:

```bash
./run_tests
```

The script will create a reproducible virtual environment in `.venv`, install `pytest`, and run the tests.

---

No behavioral changes were introduced — the refactor only reorganizes internal structure and consolidates duplicated logic.
