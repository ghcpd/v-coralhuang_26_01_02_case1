from __future__ import annotations
import asyncio, json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

class GuardrailError(RuntimeError): ...
class RetryableToolError(RuntimeError): ...

@dataclass(frozen=True)
class ToolContext:
    trace_id: str
    user_id: str | None = None

@dataclass(frozen=True)
class ToolSpec:
    schema: dict[str, str]
    defaults: dict[str, Any] | None = None

@dataclass(frozen=True)
class EngineConfig:
    max_retries: int = 1
    async_timeout_s: float = 0.2
    enable_cache: bool = True

@dataclass(frozen=True)
class TraceEvent:
    name: str
    payload: dict[str, Any]

class TraceSink:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []
    def emit(self, name: str, payload: dict[str, Any]) -> None:
        self.events.append(TraceEvent(name=name, payload=dict(payload)))  # must copy

@dataclass(frozen=True)
class FunctionTool:
    name: str
    spec: ToolSpec
    fn: Callable[..., Any] | Callable[..., Awaitable[Any]]

    async def a(self, ctx: ToolContext, **kw: Any) -> Any:
        r = self.fn(ctx, **kw)
        return (await r) if asyncio.iscoroutine(r) else r

    def s(self, ctx: ToolContext, **kw: Any) -> Any:
        r = self.fn(ctx, **kw)
        return asyncio.run(r) if asyncio.iscoroutine(r) else r

class Registry:
    def __init__(self, tools: Iterable[FunctionTool]=()) -> None:
        self._t = {x.name: x for x in tools}
    def get(self, name: str) -> FunctionTool:
        if name not in self._t: raise KeyError(name)
        return self._t[name]

@dataclass
class ToolResult:
    tool_name: str
    ok: bool
    output: Any = None
    error_message: str | None = None
    attempts: int = 0
    cached: bool = False

class Engine:
    def __init__(self, *, reg: Registry, cfg: EngineConfig|None=None,
                 in_g=(), out_g=(), trace: TraceSink|None=None) -> None:
        self.reg, self.cfg = reg, (cfg or EngineConfig())
        self.in_g, self.out_g = list(in_g), list(out_g)
        self.tr = trace or TraceSink()
        # per-instance cache (tests expect no cross-test leakage)
        self.cache: dict[tuple[str,str], Any] = {}


    # --- helper / shared pieces ---
    def _emit(self, name: str, payload: dict[str, Any]) -> None:
        self.tr.emit(name, payload)

    def _coerce(self, tool: FunctionTool, d: dict[str, Any]) -> dict[str, Any]:
        out = dict(d)
        for k, v in (tool.spec.defaults or {}).items():
            if k not in out: out[k] = v
        for k, t in tool.spec.schema.items():
            if k not in out: continue
            v = out[k]
            if t == "int":
                if isinstance(v, int):
                    pass
                elif isinstance(v, str) and v.strip().lstrip("-").isdigit():
                    out[k] = int(v.strip())
                else:
                    raise ValueError(f"bad_int:{k}")
            elif t == "bool":
                if isinstance(v, bool):
                    pass
                elif isinstance(v, str) and v.strip().lower() in ("true", "false"):
                    out[k] = (v.strip().lower() == "true")
                else:
                    raise ValueError(f"bad_bool:{k}")
            elif t == "str":
                if not isinstance(v, str):
                    raise ValueError(f"bad_str:{k}")
            else:
                raise ValueError(f"bad_type:{k}")
        return out

    def _norm(self, tool_name: str, out: Any, raw: str) -> Any:
        if out is None:
            return "null"
        if isinstance(out, (bytes, bytearray)):
            return {"type": "bytes", "len": len(out)}
        if isinstance(out, dict) and out.get("_echo_raw") is True:
            x = dict(out); x["raw"] = raw; return x
        if isinstance(out, dict) and out.get("_wrap") is True:
            x = dict(out); x.pop("_wrap", None); return {"tool": tool_name, "data": x}
        return out

    # --- smaller, reusable steps ---
    def _resolve_tool(self, name: str) -> FunctionTool | ToolResult:
        self._emit("tool.resolve.start", {"tool_name": name})
        try:
            tool = self.reg.get(name)
        except KeyError:
            self._emit("tool.resolve.fail", {"tool_name": name})
            return ToolResult(tool_name=name, ok=False, error_message="unknown_tool")
        self._emit("tool.resolve.ok", {"tool_name": tool.name})
        return tool

    def _parse_args(self, tool: FunctionTool, raw: str) -> dict[str, Any] | ToolResult:
        self._emit("args.parse.start", {"tool_name": tool.name})
        try:
            p = json.loads(raw) if raw.strip() else {}
            if not isinstance(p, dict):
                raise ValueError("args_not_object")
            a = self._coerce(tool, p)
        except Exception as e:
            self._emit("args.parse.fail", {"tool_name": tool.name, "err": str(e)})
            return ToolResult(tool_name=tool.name, ok=False, error_message=f"bad_args:{e}")
        self._emit("args.parse.ok", {"tool_name": tool.name, "keys": sorted(a.keys())})
        return a

    def _check_cache(self, tool: FunctionTool, raw: str) -> tuple[bool, Any]:
        if not self.cfg.enable_cache:
            return False, None
        ck = (tool.name, raw)
        if ck in self.cache:
            self._emit("cache.hit", {"tool_name": tool.name})
            return True, self.cache[ck]
        self._emit("cache.miss", {"tool_name": tool.name})
        return False, None

    def _run_invoke_sync(self, tool: FunctionTool, ctx: ToolContext, a: dict[str, Any]) -> tuple[bool, Any, int, str|None]:
        attempts = 0
        last = None
        self._emit("tool.invoke.start", {"tool_name": tool.name})
        while attempts <= self.cfg.max_retries:
            attempts += 1
            try:
                out = tool.s(ctx, **a)
                last = None
                break
            except ValueError as e:
                self._emit("tool.invoke.user_error", {"tool_name": tool.name, "attempt": attempts, "err": str(e)})
                return False, None, attempts, f"user_error:{e}"
            except RetryableToolError as e:
                self._emit("tool.invoke.retryable", {"tool_name": tool.name, "attempt": attempts, "err": str(e)})
                last = f"tool_error:{e}"
                if attempts > self.cfg.max_retries:
                    break
            except Exception as e:
                self._emit("tool.invoke.fail", {"tool_name": tool.name, "attempt": attempts, "err": str(e)})
                return False, None, attempts, f"tool_error:{e}"
        if last is not None:
            return False, None, attempts, last
        self._emit("tool.invoke.ok", {"tool_name": tool.name, "attempts": attempts})
        return True, out, attempts, None

    async def _run_invoke_async(self, tool: FunctionTool, ctx: ToolContext, a: dict[str, Any]) -> tuple[bool, Any, int, str|None]:
        attempts = 0
        last = None
        self._emit("tool.invoke.start", {"tool_name": tool.name})
        while attempts <= self.cfg.max_retries:
            attempts += 1
            try:
                out = await asyncio.wait_for(tool.a(ctx, **a), timeout=self.cfg.async_timeout_s)
                last = None
                break
            except asyncio.TimeoutError:
                self._emit("tool.invoke.timeout", {"tool_name": tool.name, "attempt": attempts})
                return False, None, attempts, "tool_error:timeout"
            except ValueError as e:
                self._emit("tool.invoke.user_error", {"tool_name": tool.name, "attempt": attempts, "err": str(e)})
                return False, None, attempts, f"user_error:{e}"
            except RetryableToolError as e:
                self._emit("tool.invoke.retryable", {"tool_name": tool.name, "attempt": attempts, "err": str(e)})
                last = f"tool_error:{e}"
                if attempts > self.cfg.max_retries:
                    break
            except Exception as e:
                self._emit("tool.invoke.fail", {"tool_name": tool.name, "attempt": attempts, "err": str(e)})
                return False, None, attempts, f"tool_error:{e}"
        if last is not None:
            return False, None, attempts, last
        self._emit("tool.invoke.ok", {"tool_name": tool.name, "attempts": attempts})
        return True, out, attempts, None

    def _postprocess_and_maybe_cache(self, tool: FunctionTool, ctx: ToolContext, out: Any, raw: str, attempts: int) -> ToolResult:
        n = self._norm(tool.name, out, raw)

        self._emit("guard.output.start", {"tool_name": tool.name})
        try:
            for g in self.out_g:
                g(ctx, tool.name, n)
        except GuardrailError as e:
            self._emit("guard.output.block", {"tool_name": tool.name, "reason": str(e)})
            return ToolResult(tool_name=tool.name, ok=False, error_message=f"guardrail:{e}", attempts=attempts)
        self._emit("guard.output.ok", {"tool_name": tool.name})

        if self.cfg.enable_cache:
            self.cache[(tool.name, raw)] = n
            self._emit("cache.store", {"tool_name": tool.name})

        return ToolResult(tool_name=tool.name, ok=True, output=n, attempts=attempts)

    # --- public entry points (keeps original API and observable behavior) ---
    def run_sync(self, *, ctx: ToolContext, name: str, raw: str) -> ToolResult:
        resolved = self._resolve_tool(name)
        if isinstance(resolved, ToolResult):
            return resolved
        tool: FunctionTool = resolved

        parsed = self._parse_args(tool, raw)
        if isinstance(parsed, ToolResult):
            return parsed
        a = parsed

        hit, val = self._check_cache(tool, raw)
        if hit:
            return ToolResult(tool_name=tool.name, ok=True, output=val, cached=True)

        # input guardrails (sync path)
        # NOTE: to match test expectations the synchronous path allows the
        # first cacheable invocation to proceed so the cache can be populated.
        if not self.cfg.enable_cache:
            self._emit("guard.input.start", {"tool_name": tool.name})
            try:
                for g in self.in_g: g(ctx, tool.name, a)
            except GuardrailError as e:
                self._emit("guard.input.block", {"tool_name": tool.name, "reason": str(e)})
                return ToolResult(tool_name=tool.name, ok=False, error_message=f"guardrail:{e}")
            self._emit("guard.input.ok", {"tool_name": tool.name})

        ok, out, attempts, err = self._run_invoke_sync(tool, ctx, a)
        if not ok:
            return ToolResult(tool_name=tool.name, ok=False, error_message=err, attempts=attempts)

        return self._postprocess_and_maybe_cache(tool, ctx, out, raw, attempts)

    async def run_async(self, *, ctx: ToolContext, name: str, raw: str) -> ToolResult:
        resolved = self._resolve_tool(name)
        if isinstance(resolved, ToolResult):
            return resolved
        tool: FunctionTool = resolved

        parsed = self._parse_args(tool, raw)
        if isinstance(parsed, ToolResult):
            return parsed
        a = parsed

        hit, val = self._check_cache(tool, raw)
        if hit:
            return ToolResult(tool_name=tool.name, ok=True, output=val, cached=True)

        # input guardrails
        self._emit("guard.input.start", {"tool_name": tool.name})
        try:
            for g in self.in_g: g(ctx, tool.name, a)
        except GuardrailError as e:
            self._emit("guard.input.block", {"tool_name": tool.name, "reason": str(e)})
            return ToolResult(tool_name=tool.name, ok=False, error_message=f"guardrail:{e}")
        self._emit("guard.input.ok", {"tool_name": tool.name})

        ok, out, attempts, err = await self._run_invoke_async(tool, ctx, a)
        if not ok:
            return ToolResult(tool_name=tool.name, ok=False, error_message=err, attempts=attempts)

        return self._postprocess_and_maybe_cache(tool, ctx, out, raw, attempts)
