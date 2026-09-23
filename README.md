# traceroot-break-it

I installed the [TraceRoot](https://github.com/traceroot-ai/traceroot) Python SDK, wrote the smallest support agent I could, broke it three different ways on purpose, and kept the traces.

This is not a benchmark or a tutorial. It's a record of what the SDK actually sends when an agent fails, what you can and can't see in it, and two small things I found in the SDK along the way.

```
pip install -r requirements.txt
python trace_sink.py                 # terminal 1: catches what the SDK sends
python agent.py --bug tool-crash     # terminal 2: pick a bug
```

No API keys needed. The "model" is scripted so every run is deterministic. Set `OPENAI_API_KEY` and the same agent uses `gpt-4o-mini` through OpenAI tool calling, and TraceRoot's OpenAI integration adds LLM spans automatically.

## What's in here

| File | What it is |
|---|---|
| `agent.py` | A support agent for a watch shop with two tools, `lookup_order` and `issue_refund`, wrapped in `@observe`. A `--bug` flag switches one of three failures on. |
| `trace_sink.py` | ~100 lines that impersonate the TraceRoot backend: accept the OTLP protobuf POST, decode it, print a span tree, save JSON. |
| `traces/*.json` | The real spans from each run, unedited. |

Why a sink instead of the real backend? I wanted to read the raw payload before trusting any UI's interpretation of it. The SDK exports standard OTLP over HTTP to `/api/v1/public/traces`, and disables itself entirely if `TRACEROOT_API_KEY` is unset, so the sink just needs a dummy key and `TRACEROOT_HOST_URL=http://localhost:4318`. Swap those two env vars for real ones and everything here goes to app.traceroot.ai or a self-hosted instance instead.

## The three bugs

The user message is the same every time:

> Hi, where's my order ORD-1042? I'd like a refund if it's lost.

### Happy path (`--bug none`)

```
agent support_agent             12.9ms  [agent.py:223]
  llm   scripted_model             0.2ms  [agent.py:183]
  tool  lookup_order               0.1ms  [agent.py:188]
  llm   scripted_model             0.1ms  [agent.py:183]
```

Four spans: agent, model decides to call a tool, tool runs, model writes the answer. The `[file:line]` on each span is TraceRoot's `traceroot.git.source_file` / `source_line` attribute, which is what its root-cause agent later maps back onto your repo and commit history. Note that it records where the span was *called from*, not where the function is defined. `agent.py:188` is the `TOOLS[action.name](**action.args)` line inside the loop, for every tool.

### Bug 1: a tool crashes (`--bug tool-crash`)

`lookup_order` reads `order["customer_email"]`. The field is called `email`.

```
agent support_agent             15.4ms  [agent.py:223]  <-- ERROR
  !! KeyError: 'customer_email'
  !! KeyError: 'customer_email'
  llm   scripted_model             0.2ms  [agent.py:183]
  tool  lookup_order               1.4ms  [agent.py:188]  <-- ERROR
    !! KeyError: 'customer_email'
    !! KeyError: 'customer_email'
```

This is the easy one and the trace nails it: the tool span has `status.code = STATUS_CODE_ERROR`, `status.message = "KeyError: 'customer_email'"`, an `exception` event with the full Python traceback, and the error propagates up to the agent span. Logs would have given you the same traceback. What logs wouldn't give you is the `traceroot.span.input` on the tool span (`{"order_id": "ORD-1042"}`) sitting next to the model span that decided to make that call, in one object.

The one oddity: every exception appears **twice**. See "Two things I found in the SDK" below.

### Bug 2: the agent loops (`--bug loop`)

The agent calls the tool but never appends the result to the message history. The model, having seen no tool result, asks for the same lookup again. Six times. Then `MAX_STEPS` runs out and the user gets "Sorry, I wasn't able to look that up."

```
agent support_agent             13.9ms  [agent.py:223]
  llm   scripted_model             0.2ms  [agent.py:183]
  tool  lookup_order               0.1ms  [agent.py:188]
  llm   scripted_model             0.1ms  [agent.py:183]
  tool  lookup_order               0.1ms  [agent.py:188]
  llm   scripted_model             0.1ms  [agent.py:183]
  tool  lookup_order               0.0ms  [agent.py:188]
  llm   scripted_model             0.1ms  [agent.py:183]
  tool  lookup_order               0.0ms  [agent.py:188]
  llm   scripted_model             0.1ms  [agent.py:183]
  tool  lookup_order               0.0ms  [agent.py:188]
  llm   scripted_model             0.1ms  [agent.py:183]
  tool  lookup_order               0.0ms  [agent.py:188]
```

No exception. No error status. Every individual span is green and every `lookup_order` call returned the correct order. The failure only exists in the *shape* of the trace: 13 spans where the happy path has 4, and the same tool called with identical input six times in a row. That's a pattern a detector can flag and a grep through logs will not, because nothing in any single line is wrong.

In real life this bug costs six model calls per user turn and looks, in your OpenAI bill, exactly like "usage went up".

### Bug 3: the ghost refund (`--bug ghost-refund`)

The customer asked about **ORD-1042**. The model calls `issue_refund` for **ORD-1024** (two digits transposed, the kind of thing real models do with IDs). `issue_refund` never checks the order exists, returns `{"status": "ok"}`, and the agent cheerfully tells the customer:

> Done - refunded £19.99 to order ORD-1024.

```
agent support_agent             12.9ms  [agent.py:223]
  llm   scripted_model             0.2ms  [agent.py:183]
  tool  issue_refund               0.1ms  [agent.py:188]
  llm   scripted_model             0.1ms  [agent.py:183]
```

Four spans, all green, same shape as the happy path. This is the worst kind of agent failure: it looks like success. The only evidence is in the span attributes:

- `support_agent` input: `"...ORD-1042..."`
- `scripted_model` output: `{"name": "issue_refund", "args": {"order_id": "ORD-1024", ...}}`
- `issue_refund` input: `{"order_id": "ORD-1024", ...}`

An ID appears in a tool call that never appeared in any earlier span's output or in the user's message. That's the hallucination signature, and it's only detectable because TraceRoot captures full input/output on every span by default (`capture_input=True`, `capture_output=True` on `@observe`). Turn those off to save storage and this bug becomes invisible.

There are two bugs here really: the model hallucinated, and the tool trusted it. The trace shows both.

## Two things I found in the SDK (v0.1.2)

**1. Exceptions are recorded twice per span.** `@observe` wraps the function in `tracer.start_as_current_span(...)` and then, in its own `except`, calls `span.record_exception(e)` before re-raising. But OpenTelemetry's `start_as_current_span` defaults to `record_exception=True` and records the escaping exception itself on exit. Result: two identical `exception` events on every failed span (see `traces/tool-crash.json`). Harmless, but it doubles the exception payload and would confuse anyone counting events. Fix is one keyword argument: `start_as_current_span(span_name, record_exception=False)`, or drop the manual `record_exception`.

**2. Source location is the call site, not the definition.** `traceroot.git.source_line` for the agent span is `223`, which is the line in `main()` that *calls* `support_agent`, and `source_function` is `main`. For a decorated tool it's the dispatch line inside the agent loop. That's a reasonable design choice (it tells you which code path invoked the failing thing), but the attribute names suggest the opposite, and I initially read them wrong. Worth one sentence in the docs.

## What I'd want next

- Point this at a real TraceRoot instance and see whether the detectors catch bug 2 (repeated identical tool calls) and bug 3 (ID in a tool call with no provenance) without being told. Those two are the interesting ones. Bug 1 is table stakes.
- Run it with `OPENAI_API_KEY` set and a real model to see how often the ghost-refund class of bug happens *unprompted* over, say, 200 runs with slightly varied order IDs.

## Honest notes

- The model is scripted by default so the bugs reproduce every time. Bug 3 in particular is a simulated hallucination; the point is what the trace looks like when it happens, not whether it happens.
- Written and tested against `traceroot==0.1.2` and `opentelemetry-sdk==1.44.0` on Python 3.14. I used an LLM as a pair while writing this; every command and trace in the README was run for real and the output pasted unedited.
