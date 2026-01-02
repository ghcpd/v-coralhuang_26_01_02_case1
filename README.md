# Agent Tools Refactor

## Refactor Summary ✅

- Centralized the common flow for `Engine.run_sync` and `Engine.run_async` into shared helpers:
  - `_resolve_tool`, `_parse_args`, `_cache_lookup`, `_run_input_guards`, `_run_output_guards`
  - `_invoke_sync` and `_invoke_async` handle the only differing parts (invocation semantics and async timeout handling)
- Removed duplicated control flow while preserving trace events, ordering, and error mappings.
- Ensured `TraceSink.emit()` copies payloads to preserve immutability.

## Design Rationale 🔧

- The original implementation duplicated the entire request lifecycle across `run_sync` and `run_async` (resolve → parse → cache → input guard → invoke → normalize → output guard → cache store). By extracting common stages into focused helper methods, the refactor:
  - Reduces the risk of behavior drift between sync and async (shared helpers keep semantics aligned).
  - Makes differences explicit and small (only `_invoke_sync` vs `_invoke_async`).
  - Makes the code easier to audit and reason about for future changes.

## Behavior Invariants (Critical) ⚠️

The refactor preserves all critical behaviors and tests:

- **Public API**: `Engine.run_sync(ctx, name, raw)` and `Engine.run_async(ctx, name, raw)` unchanged.
- **ToolResult semantics**: fields `ok`, `output`, `error_message`, `attempts`, `cached` remain the same.
- **Cache key**: exact tuple `(tool_name, raw_args_string)` — raw string is used as-is (spaces/order preserved).
- **Cache hit short-circuit**: on cache hit, no guards or invocation are executed and a `cache.hit` event is emitted.
- **Trace payload immutability**: `TraceSink.emit()` copies the provided payload dict.
- **Unknown argument passthrough**: extra kwargs not in schema are forwarded to the tool function.
- **Nested event loop behavior**: calling `run_sync` for an async tool while an event loop is active maps the resulting `RuntimeError` from `asyncio.run()` to `tool_error:<message>`.
- **Error mapping rules** (unaltered):
  - Unknown tool → `unknown_tool`
  - Bad JSON / args parsing / coercion → `bad_args:<...>`
  - `ValueError` from tool invocation → `user_error:<...>` (no retry)
  - `RetryableToolError` → retryable; after retries exhausted → `tool_error:<...>`
  - Async timeout → `tool_error:timeout` (no retry)
  - `GuardrailError` → `guardrail:<...>`
- **Execution order preserved**:
  resolve → parse/coerce/defaults → cache check → input guardrails → invoke (retry/timeout) → normalize → output guardrails → cache store

## How to Run ▶️

Requirements: Python 3.8+ (no other system dependencies).

Run the provided helper script which sets up a virtual environment, installs `pytest`, and runs the tests:

- POSIX/macOS/Linux:

```bash
./run_tests
```

- Windows (PowerShell / cmd):

```powershell
./run_tests.bat
```

Or, manually:

```bash
python -m venv .venv
# Activate .venv appropriately for your shell
python -m pip install --upgrade pip
python -m pip install pytest
python -m pytest -q
```

All tests should pass (`7 passed`).
