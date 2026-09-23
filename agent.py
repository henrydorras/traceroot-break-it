"""A deliberately fragile customer-support agent, instrumented with TraceRoot.

The agent answers one question - "where is my order / can I get a refund" -
using two tools. It has three bugs you can switch on one at a time:

    python agent.py --bug none          # happy path
    python agent.py --bug tool-crash    # a tool raises KeyError
    python agent.py --bug loop          # agent forgets tool results and loops
    python agent.py --bug ghost-refund  # refund issued for an order that doesn't exist

By default the "model" is a small scripted stand-in so every run is
deterministic and free. Set OPENAI_API_KEY to use a real model instead;
TraceRoot's OpenAI integration will then add LLM spans to the trace.

Traces go wherever TRACEROOT_HOST_URL points - the local sink in
trace_sink.py, or app.traceroot.ai with a real TRACEROOT_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass

import traceroot
from traceroot import Integration, observe

BUG = "none"
MAX_STEPS = 6

# --------------------------------------------------------------------------
# "Database"
# --------------------------------------------------------------------------

ORDERS = {
    "ORD-1042": {
        "email": "sam@example.com",
        "item": "Casio F-91W",
        "total": 19.99,
        "status": "delivered",
    },
}

# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------


@observe(type="tool")
def lookup_order(order_id: str) -> dict:
    """Return the order record for a given order id."""
    order = ORDERS.get(order_id)
    if order is None:
        return {"error": f"no order with id {order_id}"}
    if BUG == "tool-crash":
        # Bug: the field is called "email", not "customer_email".
        contact = order["customer_email"]
    else:
        contact = order["email"]
    return {"order_id": order_id, "contact": contact, **order}


@observe(type="tool")
def issue_refund(order_id: str, amount: float) -> dict:
    """Refund an order. Bug: never checks that the order exists."""
    return {"order_id": order_id, "refunded": amount, "status": "ok"}


TOOLS = {"lookup_order": lookup_order, "issue_refund": issue_refund}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "lookup_order",
            "description": "Look up an order by its id, e.g. ORD-1042.",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "issue_refund",
            "description": "Refund an order in full.",
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "amount": {"type": "number"},
                },
                "required": ["order_id", "amount"],
            },
        },
    },
]

# --------------------------------------------------------------------------
# Models: a scripted stand-in, or OpenAI if a key is present
# --------------------------------------------------------------------------


@dataclass
class ToolCall:
    name: str
    args: dict


@dataclass
class Final:
    text: str


class ScriptedModel:
    """Deterministic stand-in for an LLM.

    It only knows how to handle the one demo question. It looks at the
    conversation so far and decides what a reasonable (or, for the
    ghost-refund bug, an unreasonable) model would do next.
    """

    @observe(name="scripted_model", type="llm")
    def chat(self, messages: list[dict]) -> ToolCall | Final:
        seen_tool_results = [m for m in messages if m["role"] == "tool"]

        if BUG == "ghost-refund" and not seen_tool_results:
            # Simulated hallucination: the customer said ORD-1042, the model
            # transposes two digits and confidently refunds ORD-1024.
            return ToolCall("issue_refund", {"order_id": "ORD-1024", "amount": 19.99})

        if not seen_tool_results:
            return ToolCall("lookup_order", {"order_id": "ORD-1042"})

        last = json.loads(seen_tool_results[-1]["content"])
        if "refunded" in last:
            return Final(f"Done - refunded £{last['refunded']:.2f} to order {last['order_id']}.")
        return Final(
            f"Order {last['order_id']} ({last['item']}) shows as {last['status']}. "
            f"We'll email {last['contact']} with tracking."
        )


class OpenAIModel:
    """Real model via OpenAI tool calling. Requires OPENAI_API_KEY."""

    def __init__(self, model: str = "gpt-4o-mini"):
        from openai import OpenAI

        self.client = OpenAI()
        self.model = model

    def chat(self, messages: list[dict]) -> ToolCall | Final:
        resp = self.client.chat.completions.create(
            model=self.model, messages=messages, tools=TOOL_SCHEMAS
        )
        msg = resp.choices[0].message
        if msg.tool_calls:
            call = msg.tool_calls[0]
            return ToolCall(call.function.name, json.loads(call.function.arguments))
        return Final(msg.content or "")


# --------------------------------------------------------------------------
# Agent loop
# --------------------------------------------------------------------------


@observe(name="support_agent", type="agent")
def support_agent(question: str, model) -> str:
    messages = [
        {
            "role": "system",
            "content": "You are a support agent for a watch shop. Use tools to answer.",
        },
        {"role": "user", "content": question},
    ]

    for _ in range(MAX_STEPS):
        action = model.chat(messages)

        if isinstance(action, Final):
            return action.text

        result = TOOLS[action.name](**action.args)

        if BUG == "loop":
            # Bug: we never tell the model what the tool returned,
            # so it asks for the same lookup again. And again.
            continue

        messages.append({"role": "tool", "name": action.name, "content": json.dumps(result)})

    return "Sorry, I wasn't able to look that up. Please contact support."


def main() -> None:
    global BUG
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bug",
        choices=["none", "tool-crash", "loop", "ghost-refund"],
        default="none",
    )
    args = parser.parse_args()
    BUG = args.bug

    # The SDK reads TRACEROOT_API_KEY / TRACEROOT_HOST_URL from the environment.
    # With no key at all it disables itself, so default to the local sink.
    os.environ.setdefault("TRACEROOT_API_KEY", "local-sink")
    os.environ.setdefault("TRACEROOT_HOST_URL", "http://localhost:4318")
    os.environ.setdefault("OTEL_SERVICE_NAME", "support-agent-demo")
    traceroot.initialize(integrations=[Integration.OPENAI])

    model = OpenAIModel() if os.environ.get("OPENAI_API_KEY") else ScriptedModel()
    question = "Hi, where's my order ORD-1042? I'd like a refund if it's lost."

    print(f"[bug={BUG}] user: {question}")
    try:
        answer = support_agent(question, model)
        print(f"[bug={BUG}] agent: {answer}")
    finally:
        traceroot.flush()


if __name__ == "__main__":
    main()
