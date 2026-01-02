# Agent Tools Engine Refactor

## Refactor Summary

This refactor eliminates structural duplication between `Engine.run_sync()` and `Engine.run_async()` by extracting shared execution phases into dedicated helper methods. The code maintains **100% behavioral equivalence** with the original implementation while significantly improving maintainability and reducing future drift risk.

### Structural Changes

The refactor introduces six new internal helper methods that encapsulate the execution pipeline:

1. **`_resolve(name)`** — Tool registry lookup with trace emissions
2. **`_parse_and_coerce(tool, raw)`** — JSON parsing and argument coercion with defaults
3. **`_check_cache(tool, raw)`** — Cache hit detection and short-circuit handling
4. **`_run_input_guardrails(tool, ctx, args)`** — Input validation guard execution
5. **`_run_output_guardrails(tool, ctx, output, attempts)`** — Output validation guard execution
6. **`_store_cache(tool, raw, output)`** — Cache storage with trace emission

### Duplication Removed

**Before:** ~180 lines of nearly identical code split across `run_sync` and `run_async`:
- Tool resolution logic
- JSON parsing and coercion
- Cache check logic
- Input/output guardrail execution
- Cache storage

**After:** Single implementation of each phase, reused by both methods.

The only difference between `run_sync` and `run_async` is now **the invocation phase itself** (lines that differ):
- **`run_sync`**: `tool.s(ctx, **args)` — synchronous execution
- **`run_async`**: `await asyncio.wait_for(tool.a(ctx, **args), ...)` — async execution with timeout

---

## Design Rationale

### Why This Approach?

1. **Correctness First**: Each helper is a self-contained unit that can be tested, reviewed, and reasoned about independently. The helper signatures make the control flow explicit.

2. **Minimal Abstraction**: Helpers are not over-generalized. They return tuples `(success_value, error_result)` which keep early-return semantics clear and preserve execution order guarantees.

3. **Zero Behavior Change**: 
   - Helper methods handle trace emission internally — same events, same order, same payloads
   - Error handling is identical
   - Argument passing is unchanged
   - Return values are unchanged

4. **Future-Proof**:
   - New behavior added to `_resolve` automatically propagates to both `run_sync` and `run_async`
   - Reduces probability of sync/async code divergence
   - Guards against accidental duplication creep

### How It Prevents Sync/Async Drift

Before the refactor, changes to input validation logic, cache checking, or guardrails required updates in two places. A developer might:
- Fix a bug in `run_sync` but forget `run_async`
- Add retry logic to `run_async` but not `run_sync`
- Emit a trace event in one path but not the other

With the refactor:
- All shared phases are centralized in single methods
- Changes are automatically visible in both code paths
- Tests exercise both paths through the same code

---

## Behavior Invariants

These critical edge cases are preserved exactly and tested:

### 1. **Cache Key Exactness**
- Cache key: `(tool_name, raw_args_string)` — uses raw JSON string as-is
- **NOT canonicalized**: `{"x":1}` ≠ `{ "x":1 }`
- **Test**: `test_cache_key_exact_raw_whitespace_matters`

### 2. **Cache Hit Short-Circuit**
- On cache hit: skip guards, skip tool invoke, return immediately
- **Invariant**: After `cache.hit` trace event, no `guard.*` or `tool.invoke.*` events appear
- **Test**: `test_cache_hit_short_circuit_no_guards_or_invoke_after_hit`

### 3. **Trace Payload Immutability**
- `TraceSink.emit()` copies the payload dict (`dict(payload)`)
- Mutating original dict after emit does not affect stored events
- **Test**: `test_trace_payload_copy`

### 4. **Unknown Argument Passthrough**
- Arguments not in schema are passed to tool via `**kwargs`
- **Test**: `test_unknown_args_passthrough`

### 5. **Nested Event Loop Behavior**
- `run_sync` + async tool = `asyncio.run()` raises `RuntimeError`
- Must be caught and mapped to `"tool_error:<RuntimeError message>"`
- Error category **must** remain `tool_error`, not upgraded or downgraded
- **Test**: `test_nested_loop_run_sync_async_tool_maps_to_tool_error`

### 6. **Async Timeout No-Retry**
- Async timeout (`asyncio.TimeoutError`) → `"tool_error:timeout"`
- **Never** retried (even if `max_retries > 0`)
- Attempts count: exactly 1
- **Test**: `test_async_timeout_no_retry`

### 7. **Execution Order**
```
tool.resolve
  → args.parse / coerce / defaults
  → cache.check
  → guard.input
  → tool.invoke (with retry loop)
  → normalize
  → guard.output
  → cache.store
```

This order is verified through trace event sequencing in all tests.

---

## Error Mapping Rules

The refactored code preserves all error classification:

| Condition | Error Message Format | Retryable |
|-----------|----------------------|-----------|
| Unknown tool | `"unknown_tool"` | No |
| Bad JSON / args parsing | `"bad_args:<exception>"` | No |
| ValueError during invoke | `"user_error:<exception>"` | No |
| RetryableToolError | `"tool_error:<exception>"` | Yes (up to max_retries) |
| Timeout (async only) | `"tool_error:timeout"` | No |
| RuntimeError (nested loop) | `"tool_error:<exception>"` | No |
| GuardrailError (input) | `"guardrail:<exception>"` | No |
| GuardrailError (output) | `"guardrail:<exception>"` | No |

---

## How to Run Tests

### Quick Start

```bash
# From the repository root:
./run_tests
```

This script:
1. Creates a Python virtual environment (`.venv/`)
2. Installs `pytest` and `pytest-asyncio`
3. Runs the full test suite (`pytest -q`)
4. Reports results

### Manual Setup

If you prefer to set up manually:

```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate        # On Windows: .venv\Scripts\activate

# Install dependencies
pip install pytest pytest-asyncio

# Run tests
pytest -q
```

### Test Output

All 7 tests should pass:

```
.......                                                   [100%]
==================================== 7 passed in 0.09s =====================================
```

A `RuntimeWarning` about an unawaited coroutine in `test_nested_loop_run_sync_async_tool_maps_to_tool_error` is expected — it's part of the test's verification of error handling.

---

## Project Structure

```
.
├── agent_tools.py              # Core engine implementation (refactored)
├── tests/
│   ├── __init__.py
│   ├── conftest.py             # Pytest configuration (sets up import paths)
│   └── test_engine.py          # Test suite (unchanged)
├── README.md                   # This file
├── run_tests                   # Test runner script
├── .gitignore                  # Git ignore rules
└── final_prompt.txt            # Original requirements document
```

---

## Implementation Notes

### Why `dict(payload)` in `TraceSink.emit()`?

The `TraceSink` copies payloads because tests verify that mutations to the original dict don't affect stored events. This prevents subtle bugs where a caller mutates a dict after passing it to a trace sink.

### Why Return Tuples `(value, error)`?

Helper methods return tuples instead of raising exceptions because:
- They make control flow explicit (no hidden exception handling)
- Early returns in `run_sync` and `run_async` remain flat and readable
- Trace emissions happen at the right places (before returns)

### Invocation Retry Loop

Both `run_sync` and `run_async` contain their own invocation retry loops (not extracted to a helper) because:
- The loop is fundamentally different (sync vs async with timeout)
- Extracting it would require passing a callable, reducing clarity
- The retry logic is stable and unlikely to change
- Tests thoroughly exercise both paths

---

## Verification

Run the tests to confirm all behavior is preserved:

```bash
./run_tests
```

Expected output: **7 passed**

If any test fails, the refactor has inadvertently changed behavior. Debug the failure by:
1. Running `pytest -vv` for detailed trace
2. Checking that helper methods emit the correct trace events
3. Verifying that error handling paths are identical

---

## License & Attribution

This refactoring maintains 100% behavioral equivalence with the original `agent_tools.py` while reorganizing code for long-term maintainability.
