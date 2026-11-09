"""A tiny OpenAI-compatible server for testing the plumbing without Strata.

It is deliberately dumb: it proves the harness runs end to end and handles
tool calls, streaming and images, not that any eval passes.

    python tests/mock_server.py --port 8099
"""

from __future__ import annotations

import argparse
import json
import time
import uuid

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
MODEL = "mock-strata"


def _fake_args(schema: dict) -> dict:
    out = {}
    for key in schema.get("required", []):
        spec = schema.get("properties", {}).get(key, {})
        if "enum" in spec:
            out[key] = spec["enum"][0]
        elif spec.get("type") == "integer":
            out[key] = 1
        else:
            out[key] = "test"
    return out


def _reply(body: dict) -> dict:
    msgs = body.get("messages", [])
    tools = body.get("tools") or []
    last = msgs[-1] if msgs else {}
    content = last.get("content")
    has_image = isinstance(content, list) and any(p.get("type") == "image_url" for p in content)
    tool_turns = sum(1 for m in msgs if m.get("role") == "tool")

    if tools and not has_image:
        names = [t["function"]["name"] for t in tools]
        if "finish" in names and tool_turns >= 3:
            fn, args = "finish", {"summary": "done"}
        elif last.get("role") == "tool" and "finish" not in names:
            return {"role": "assistant", "content": "Summary: gas demand rose; storage is above average."}
        else:
            t = tools[0]["function"]
            fn, args = t["name"], _fake_args(t.get("parameters", {}))
        return {"role": "assistant", "content": None, "tool_calls": [{
            "id": f"call_{uuid.uuid4().hex[:8]}", "type": "function",
            "function": {"name": fn, "arguments": json.dumps(args)}}]}
    if has_image:
        text = '{"x": 640, "y": 400}' if "coordinates" in json.dumps(content) else "Submit application"
        return {"role": "assistant", "content": text}
    if "JSON" in json.dumps(msgs):
        return {"role": "assistant", "content": '```json\n{"title": "test", "year": 2025}\n```'}
    return {"role": "assistant", "content": "ready " + "word " * min(int(body.get("max_tokens", 20)), 50)}


@app.get("/")
def root():
    return {"ok": True}


@app.get("/v1/models")
def models():
    return {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "mock"}]}


@app.post("/v1/chat/completions")
async def chat(req: Request):
    body = await req.json()
    msg = _reply(body)
    usage = {"prompt_tokens": len(json.dumps(body.get("messages"))) // 4,
             "completion_tokens": len(msg.get("content") or "") // 4 + 5}
    usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
    cid, now = f"chatcmpl-{uuid.uuid4().hex[:10]}", int(time.time())

    if body.get("stream"):
        def gen():
            words = (msg.get("content") or "").split(" ")
            for i, w in enumerate(words):
                chunk = {"id": cid, "object": "chat.completion.chunk", "created": now, "model": MODEL,
                         "choices": [{"index": 0, "delta": {"content": (w if i == 0 else " " + w)},
                                      "finish_reason": None}]}
                yield f"data: {json.dumps(chunk)}\n\n"
            end = {"id": cid, "object": "chat.completion.chunk", "created": now, "model": MODEL,
                   "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
            yield f"data: {json.dumps(end)}\n\n"
            if (body.get("stream_options") or {}).get("include_usage"):
                yield f"data: {json.dumps({'id': cid, 'object': 'chat.completion.chunk', 'created': now, 'model': MODEL, 'choices': [], 'usage': usage})}\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    finish = "tool_calls" if msg.get("tool_calls") else "stop"
    return JSONResponse({"id": cid, "object": "chat.completion", "created": now, "model": MODEL,
                         "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                         "usage": usage})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    uvicorn.run(app, host="127.0.0.1", port=ap.parse_args().port, log_level="warning")
