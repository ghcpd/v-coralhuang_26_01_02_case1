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
        self.cache: dict[tuple[str,str], Any] = {}  # exact raw string key

    def _coerce(self, tool: FunctionTool, d: dict[str, Any]) -> dict[str, Any]:
        out = dict(d)
        for k,v in (tool.spec.defaults or {}).items():
            if k not in out: out[k]=v
        for k,t in tool.spec.schema.items():
            if k not in out: continue
            v = out[k]
            if t=="int":
                if isinstance(v,int): pass
                elif isinstance(v,str) and v.strip().lstrip("-").isdigit(): out[k]=int(v.strip())
                else: raise ValueError(f"bad_int:{k}")
            elif t=="bool":
                if isinstance(v,bool): pass
                elif isinstance(v,str) and v.strip().lower() in ("true","false"): out[k]= (v.strip().lower()=="true")
                else: raise ValueError(f"bad_bool:{k}")
            elif t=="str":
                if not isinstance(v,str): raise ValueError(f"bad_str:{k}")
            else:
                raise ValueError(f"bad_type:{k}")
        return out

    def _norm(self, tool_name: str, out: Any, raw: str) -> Any:
        if out is None: return "null"
        if isinstance(out,(bytes,bytearray)): return {"type":"bytes","len":len(out)}
        if isinstance(out,dict) and out.get("_echo_raw") is True:
            x=dict(out); x["raw"]=raw; return x
        if isinstance(out,dict) and out.get("_wrap") is True:
            x=dict(out); x.pop("_wrap",None); return {"tool":tool_name,"data":x}
        return out

    def _resolve(self, name: str) -> tuple[FunctionTool | None, ToolResult | None]:
        """
        Resolve tool by name.
        Returns (tool, error_result).
        If successful: (tool, None)
        If failed: (None, error_result)
        """
        self.tr.emit("tool.resolve.start", {"tool_name": name})
        try:
            tool = self.reg.get(name)
        except KeyError:
            self.tr.emit("tool.resolve.fail", {"tool_name": name})
            return None, ToolResult(tool_name=name, ok=False, error_message="unknown_tool")
        self.tr.emit("tool.resolve.ok", {"tool_name": tool.name})
        return tool, None

    def _parse_and_coerce(self, tool: FunctionTool, raw: str) -> tuple[dict[str, Any] | None, ToolResult | None]:
        """
        Parse JSON and coerce arguments.
        Returns (args, error_result).
        If successful: (args_dict, None)
        If failed: (None, error_result)
        """
        self.tr.emit("args.parse.start", {"tool_name": tool.name})
        try:
            p = json.loads(raw) if raw.strip() else {}
            if not isinstance(p, dict):
                raise ValueError("args_not_object")
            a = self._coerce(tool, p)
        except Exception as e:
            self.tr.emit("args.parse.fail", {"tool_name": tool.name, "err": str(e)})
            return None, ToolResult(tool_name=tool.name, ok=False, error_message=f"bad_args:{e}")
        self.tr.emit("args.parse.ok", {"tool_name": tool.name, "keys": sorted(a.keys())})
        return a, None

    def _check_cache(self, tool: FunctionTool, raw: str) -> tuple[Any | None, bool]:
        """
        Check cache for hit.
        Returns (cached_output, is_hit).
        If hit: (cached_value, True)
        If miss: (None, False)
        """
        if self.cfg.enable_cache:
            ck = (tool.name, raw)
            if ck in self.cache:
                self.tr.emit("cache.hit", {"tool_name": tool.name})
                return self.cache[ck], True
            self.tr.emit("cache.miss", {"tool_name": tool.name})
        return None, False

    def _run_input_guardrails(self, tool: FunctionTool, ctx: ToolContext, args: dict[str, Any]) -> ToolResult | None:
        """
        Run input guardrails.
        Returns error_result if blocked, None if passed.
        """
        self.tr.emit("guard.input.start", {"tool_name": tool.name})
        try:
            for g in self.in_g:
                g(ctx, tool.name, args)
        except GuardrailError as e:
            self.tr.emit("guard.input.block", {"tool_name": tool.name, "reason": str(e)})
            return ToolResult(tool_name=tool.name, ok=False, error_message=f"guardrail:{e}")
        self.tr.emit("guard.input.ok", {"tool_name": tool.name})
        return None

    def _run_output_guardrails(self, tool: FunctionTool, ctx: ToolContext, output: Any, attempts: int) -> ToolResult | None:
        """
        Run output guardrails.
        Returns error_result if blocked, None if passed.
        """
        self.tr.emit("guard.output.start", {"tool_name": tool.name})
        try:
            for g in self.out_g:
                g(ctx, tool.name, output)
        except GuardrailError as e:
            self.tr.emit("guard.output.block", {"tool_name": tool.name, "reason": str(e)})
            return ToolResult(tool_name=tool.name, ok=False, error_message=f"guardrail:{e}", attempts=attempts)
        self.tr.emit("guard.output.ok", {"tool_name": tool.name})
        return None

    def _store_cache(self, tool: FunctionTool, raw: str, output: Any) -> None:
        """Store output in cache if caching is enabled."""
        if self.cfg.enable_cache:
            self.cache[(tool.name, raw)] = output
            self.tr.emit("cache.store", {"tool_name": tool.name})

    def run_sync(self, *, ctx: ToolContext, name: str, raw: str) -> ToolResult:
        tool, error = self._resolve(name)
        if error:
            return error

        args, error = self._parse_and_coerce(tool, raw)
        if error:
            return error

        cached_output, is_hit = self._check_cache(tool, raw)
        if is_hit:
            return ToolResult(tool_name=tool.name, ok=True, output=cached_output, cached=True)

        error = self._run_input_guardrails(tool, ctx, args)
        if error:
            return error

        attempts = 0
        last = None
        self.tr.emit("tool.invoke.start", {"tool_name": tool.name})
        while attempts <= self.cfg.max_retries:
            attempts += 1
            try:
                out = tool.s(ctx, **args)
                last = None
                break
            except ValueError as e:
                self.tr.emit("tool.invoke.user_error", {"tool_name": tool.name, "attempt": attempts, "err": str(e)})
                return ToolResult(tool_name=tool.name, ok=False, error_message=f"user_error:{e}", attempts=attempts)
            except RetryableToolError as e:
                self.tr.emit("tool.invoke.retryable", {"tool_name": tool.name, "attempt": attempts, "err": str(e)})
                last = f"tool_error:{e}"
                if attempts > self.cfg.max_retries:
                    break
            except Exception as e:
                self.tr.emit("tool.invoke.fail", {"tool_name": tool.name, "attempt": attempts, "err": str(e)})
                return ToolResult(tool_name=tool.name, ok=False, error_message=f"tool_error:{e}", attempts=attempts)
        if last is not None:
            return ToolResult(tool_name=tool.name, ok=False, error_message=last, attempts=attempts)
        self.tr.emit("tool.invoke.ok", {"tool_name": tool.name, "attempts": attempts})

        n = self._norm(tool.name, out, raw)

        error = self._run_output_guardrails(tool, ctx, n, attempts)
        if error:
            return error

        self._store_cache(tool, raw, n)

        return ToolResult(tool_name=tool.name, ok=True, output=n, attempts=attempts)

    async def run_async(self, *, ctx: ToolContext, name: str, raw: str) -> ToolResult:
        tool, error = self._resolve(name)
        if error:
            return error

        args, error = self._parse_and_coerce(tool, raw)
        if error:
            return error

        cached_output, is_hit = self._check_cache(tool, raw)
        if is_hit:
            return ToolResult(tool_name=tool.name, ok=True, output=cached_output, cached=True)

        error = self._run_input_guardrails(tool, ctx, args)
        if error:
            return error

        attempts = 0
        last = None
        self.tr.emit("tool.invoke.start", {"tool_name": tool.name})
        while attempts <= self.cfg.max_retries:
            attempts += 1
            try:
                out = await asyncio.wait_for(tool.a(ctx, **args), timeout=self.cfg.async_timeout_s)
                last = None
                break
            except asyncio.TimeoutError:
                self.tr.emit("tool.invoke.timeout", {"tool_name": tool.name, "attempt": attempts})
                return ToolResult(tool_name=tool.name, ok=False, error_message="tool_error:timeout", attempts=attempts)
            except ValueError as e:
                self.tr.emit("tool.invoke.user_error", {"tool_name": tool.name, "attempt": attempts, "err": str(e)})
                return ToolResult(tool_name=tool.name, ok=False, error_message=f"user_error:{e}", attempts=attempts)
            except RetryableToolError as e:
                self.tr.emit("tool.invoke.retryable", {"tool_name": tool.name, "attempt": attempts, "err": str(e)})
                last = f"tool_error:{e}"
                if attempts > self.cfg.max_retries:
                    break
            except Exception as e:
                self.tr.emit("tool.invoke.fail", {"tool_name": tool.name, "attempt": attempts, "err": str(e)})
                return ToolResult(tool_name=tool.name, ok=False, error_message=f"tool_error:{e}", attempts=attempts)
        if last is not None:
            return ToolResult(tool_name=tool.name, ok=False, error_message=last, attempts=attempts)
        self.tr.emit("tool.invoke.ok", {"tool_name": tool.name, "attempts": attempts})

        n = self._norm(tool.name, out, raw)

        error = self._run_output_guardrails(tool, ctx, n, attempts)
        if error:
            return error

        self._store_cache(tool, raw, n)

        return ToolResult(tool_name=tool.name, ok=True, output=n, attempts=attempts)
