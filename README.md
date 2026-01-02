# Engine refactor — README

## Refactor Summary

This change refactors `agent_tools.py` to centralize shared orchestration logic while keeping the public API and observable behavior identical.

- Extracted shared pre/post flow into small helpers: resolution, arg parsing/coercion, cache check, input/output guard execution, and cache store. ✅
- Centralized retry/error-mapping logic into dedicated invocation helpers (`_invoke_with_retries_sync` and `_invoke_with_retries_async`). ✅
- Kept `run_sync` and `run_async` as thin entrypoints that call the shared helpers in the same sequence as before. ✅
- Preserved `FunctionTool`, `TraceSink` semantics and all trace event names/payloads.

Duplication removed:
- All duplicated orchestration around resolve → parse → cache → guards → invoke → normalize → output guards → cache store.
- Consolidated error-to-trace mapping and retry handling into single helpers (one sync, one async).

## Design Rationale

- Small, focused helpers reduce the surface for drift between the sync and async code paths while keeping behaviour explicit and easy to reason about.
- Keeping two thin invocation helpers (sync vs async) preserves subtle runtime semantics (e.g. `asyncio.run()` behavior and async timeouts) while sharing the rest of the logic.
- Trace emission locations and payloads are unchanged and colocated with the behaviour they describe to make future maintenance less error-prone.

## Behaviour Invariants (fragile / non-obvious)

These behaviors are intentionally preserved exactly and are covered by tests:

- Cache key: exact tuple `(tool_name, raw_string)` — no JSON canonicalization; whitespace and key order matter.
- Cache hit short-circuit: on hit, input/output guardrails and tool invocation are not executed and there are no subsequent `guard.*` or `tool.invoke.*` events after `cache.hit`.
- Trace payload immutability: `TraceSink.emit()` copies the payload (mutating the original dict after emit does not affect stored events).
- Unknown args passthrough: arguments not declared in the schema are forwarded unchanged to the tool function.
- Nested event loop: calling `run_sync` while an event loop is running against an async tool still results in the same `tool_error:<...>` produced by `asyncio.run()`.
- Error mapping: unknown tool, bad args, user errors, retryable errors, guardrail errors and async timeout map to the exact error categories the existing tests expect.
- Execution order: resolve → parse/coerce/defaults → cache check → input guardrails → invoke (retry/timeout) → normalize → output guardrails → cache store.

## How to run

Requirements: Python 3.10+ (no other system deps).

1. Make the test runner executable (Unix/macOS):

   chmod +x ./run_tests

2. Run the full test suite (creates an isolated virtualenv at `.venv/`):

   ./run_tests

The script is cross-platform and will use the bundled Python to create and drive a reproducible `.venv/` and run `pytest` inside it.


## Files changed / added

- Modified: `agent_tools.py` (refactor, no public API changes)
- Added: `README.md`, `run_tests`, `.gitignore`


## Contact

For questions about the refactor or to request follow-ups, open an issue with a focused reproduction and test.