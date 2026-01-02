from __future__ import annotations
import asyncio, pytest
from agent_tools import Engine, EngineConfig, FunctionTool, GuardrailError, RetryableToolError, Registry, ToolContext, ToolSpec, TraceSink

def E(tools, **kw): return Engine(reg=Registry(tools), **kw)

def test_trace_payload_copy():
    t=TraceSink(); p={"x":1}; t.emit("ev", p); p["x"]=9; assert t.events[0].payload["x"]==1

def test_cache_key_exact_raw_whitespace_matters():
    calls={"n":0}
    def f(ctx, x:int): calls["n"]+=1; return {"x":x}
    tool=FunctionTool("t", ToolSpec({"x":"int"}), f)
    e=E([tool], cfg=EngineConfig(enable_cache=True))
    r1=e.run_sync(ctx=ToolContext("t1"), name="t", raw='{"x":1}')
    r2=e.run_sync(ctx=ToolContext("t1"), name="t", raw='{ "x":1 }')
    assert r1.ok and r2.ok and calls["n"]==2

def test_cache_hit_short_circuit_no_guards_or_invoke_after_hit():
    calls={"n":0}; t=TraceSink()
    def f(ctx, x:int): calls["n"]+=1; return {"x":x}
    def bad_guard(ctx, tool, args): raise GuardrailError("nope")
    tool=FunctionTool("t", ToolSpec({"x":"int"}), f)
    e=E([tool], trace=t, cfg=EngineConfig(enable_cache=True), in_g=[bad_guard])
    e.run_sync(ctx=ToolContext("t2"), name="t", raw='{"x":1}')
    r2=e.run_sync(ctx=ToolContext("t2"), name="t", raw='{"x":1}')
    assert r2.cached and r2.attempts==0 and calls["n"]==1
    names=[ev.name for ev in t.events]
    hit=max(i for i,n in enumerate(names) if n=="cache.hit")
    assert all(n not in ("guard.input.start","tool.invoke.start","guard.output.start") for n in names[hit+1:])

def test_unknown_args_passthrough():
    def f(ctx, x:int, **kw): return {"x":x,"extra":kw.get("extra")}
    tool=FunctionTool("t", ToolSpec({"x":"int"}), f)
    e=E([tool])
    r=e.run_sync(ctx=ToolContext("t3"), name="t", raw='{"x":"2","extra":7}')
    assert r.ok and r.output=={"x":2,"extra":7}

def test_retryable_then_wrap_norm_and_attempts():
    calls={"n":0}
    def f(ctx, x:int):
        calls["n"]+=1
        if calls["n"]==1: raise RetryableToolError("again")
        return {"_wrap":True,"x":x}
    tool=FunctionTool("t", ToolSpec({"x":"int"}), f)
    e=E([tool], cfg=EngineConfig(max_retries=1, enable_cache=False))
    r=e.run_sync(ctx=ToolContext("t4"), name="t", raw='{"x":1}')
    assert r.ok and r.attempts==2 and r.output=={"tool":"t","data":{"x":1}}

@pytest.mark.asyncio
async def test_nested_loop_run_sync_async_tool_maps_to_tool_error():
    async def f(ctx, x:int): await asyncio.sleep(0); return {"x":x}
    tool=FunctionTool("t", ToolSpec({"x":"int"}), f)
    e=E([tool], cfg=EngineConfig(enable_cache=False))
    r=e.run_sync(ctx=ToolContext("t5"), name="t", raw='{"x":1}')
    assert (not r.ok) and r.error_message and r.error_message.startswith("tool_error:") and "asyncio.run()" in r.error_message

@pytest.mark.asyncio
async def test_async_timeout_no_retry():
    async def f(ctx): await asyncio.sleep(1); return "ok"
    tool=FunctionTool("t", ToolSpec({}), f)
    e=E([tool], cfg=EngineConfig(async_timeout_s=0.05, max_retries=9, enable_cache=False))
    r=await e.run_async(ctx=ToolContext("t6"), name="t", raw="{}")
    assert (not r.ok) and r.error_message=="tool_error:timeout" and r.attempts==1
