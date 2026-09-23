"""A ~90-line stand-in for the TraceRoot backend.

The TraceRoot Python SDK exports OTLP protobuf over HTTP to
``{TRACEROOT_HOST_URL}/api/v1/public/traces``. This server accepts that
request, decodes it, prints the spans as a tree, and saves the raw
spans as JSON in ``traces/``.

It exists so you can see exactly what the SDK sends before you point it
at app.traceroot.ai or a self-hosted instance. It is not a replacement
for TraceRoot - there is no UI, no detectors, no root-cause agent.

Usage:
    python trace_sink.py            # listens on http://localhost:4318
"""

import gzip
import json
import sys
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from google.protobuf.json_format import MessageToDict
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import (
    ExportTraceServiceRequest,
    ExportTraceServiceResponse,
)

PORT = 4318
OUT_DIR = Path(__file__).parent / "traces"


def _attrs(span: dict) -> dict:
    """Flatten OTLP key/value attributes into a plain dict."""
    out = {}
    for kv in span.get("attributes", []):
        v = kv.get("value", {})
        out[kv["key"]] = next(iter(v.values()), None)
    return out


def _print_tree(spans: list[dict]) -> None:
    by_parent = defaultdict(list)
    for s in spans:
        by_parent[s.get("parentSpanId", "")].append(s)

    def walk(parent_id: str, depth: int) -> None:
        for s in sorted(by_parent[parent_id], key=lambda x: int(x["startTimeUnixNano"])):
            a = _attrs(s)
            dur_ms = (int(s["endTimeUnixNano"]) - int(s["startTimeUnixNano"])) / 1e6
            status = s.get("status", {}).get("code", "STATUS_CODE_UNSET")
            flag = "  <-- ERROR" if status == "STATUS_CODE_ERROR" else ""
            src = a.get("traceroot.git.source_file")
            line = a.get("traceroot.git.source_line")
            where = f"  [{src}:{line}]" if src else ""
            print(f"{'  ' * depth}{a.get('traceroot.span.type', 'span'):5} {s['name']:<22} {dur_ms:7.1f}ms{where}{flag}")
            for ev in s.get("events", []):
                if ev.get("name") == "exception":
                    ea = _attrs(ev)
                    print(f"{'  ' * (depth + 1)}!! {ea.get('exception.type')}: {ea.get('exception.message')}")
            walk(s["spanId"], depth + 1)

    walk("", 0)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        req = ExportTraceServiceRequest()
        req.ParseFromString(body)
        spans = []
        for rs in req.resource_spans:
            resource = _attrs(MessageToDict(rs)["resource"])
            for ss in rs.scope_spans:
                for span in ss.spans:
                    d = MessageToDict(span)
                    # MessageToDict base64-encodes bytes; ids can contain "/",
                    # which broke the file write the first time I ran this.
                    d["traceId"] = span.trace_id.hex()
                    d["spanId"] = span.span_id.hex()
                    d["parentSpanId"] = span.parent_span_id.hex()
                    d["resource"] = resource
                    spans.append(d)
        traces = defaultdict(list)
        for s in spans:
            traces[s["traceId"]].append(s)
        for trace_id, tspans in traces.items():
            print(f"\n=== trace {trace_id[:16]}...  ({len(tspans)} spans)  path={self.path}")
            _print_tree(tspans)
            OUT_DIR.mkdir(exist_ok=True)
            out = OUT_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{trace_id[:8]}.json"
            out.write_text(json.dumps(tspans, indent=2))
            print(f"saved -> traces/{out.name}")
        sys.stdout.flush()
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()
        self.wfile.write(ExportTraceServiceResponse().SerializeToString())

    def log_message(self, *_):  # silence default access log
        pass


if __name__ == "__main__":
    print(f"trace sink listening on http://localhost:{PORT}  (Ctrl+C to stop)")
    HTTPServer(("localhost", PORT), Handler).serve_forever()
