"""Minimal MCP Streamable-HTTP client for a Home Assistant `ha-mcp` endpoint.

Usage:
  hamcp.py list                         # tool names + descriptions
  hamcp.py schema <tool> [<tool> ...]   # input schemas
  hamcp.py call <tool> '<json args>'    # call a tool, print its text content

Requires HA_MCP_URL (the ha-mcp webhook URL, including its secret key) in
the environment. Never write that URL into a file that is committed or
posted anywhere; pass it via the environment only. Output size is capped
by HAMCP_MAX (chars, default 6000) -- raise it for large payloads such as
diagnostics dumps.
"""
import itertools, json, os, sys, tempfile, urllib.request

URL = os.environ.get("HA_MCP_URL")
if not URL:
    sys.exit("HA_MCP_URL is not set (export the ha-mcp webhook URL first)")
SID_FILE = os.path.join(tempfile.gettempdir(), ".hamcp_session")
_ids = itertools.count(1)

def _post(payload, sid=None):
    data = json.dumps(payload).encode()
    hdr = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if sid: hdr["Mcp-Session-Id"] = sid
    req = urllib.request.Request(URL, data=data, headers=hdr, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            body = r.read().decode("utf-8", "replace"); ctype = r.headers.get("Content-Type", "")
            new_sid = r.headers.get("Mcp-Session-Id"); status = r.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace"); ctype = e.headers.get("Content-Type", ""); new_sid = None; status = e.code
    msgs = []
    if "text/event-stream" in ctype:
        for line in body.splitlines():
            if line.startswith("data:"):
                try: msgs.append(json.loads(line[5:].strip()))
                except json.JSONDecodeError: pass
    elif body.strip():
        try: msgs.append(json.loads(body))
        except json.JSONDecodeError: msgs.append({"raw": body[:2000]})
    return status, new_sid, msgs

def session():
    if os.path.exists(SID_FILE):
        return open(SID_FILE).read().strip() or None
    status, sid, msgs = _post({"jsonrpc": "2.0", "id": next(_ids), "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "nimbus-review", "version": "0"}}})
    if status >= 300 or not any("result" in m for m in msgs):
        print("initialize failed", status, msgs, file=sys.stderr); sys.exit(1)
    _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, sid)
    open(SID_FILE, "w").write(sid or ""); return sid or None

def rpc(method, params, sid):
    status, _, msgs = _post({"jsonrpc": "2.0", "id": next(_ids), "method": method, "params": params}, sid)
    if status == 404:  # stale session id
        os.remove(SID_FILE); return rpc(method, params, session())
    for m in msgs:
        if "result" in m or "error" in m: return m
    return {"status": status, "msgs": msgs}

def call_tool(name, arguments=None):
    """Call one tool and return the parsed JSON of its first text block (or the raw text)."""
    r = rpc("tools/call", {"name": name, "arguments": arguments or {}}, session())
    if "error" in r:
        raise RuntimeError(json.dumps(r["error"])[:2000])
    res = r["result"]
    texts = [c.get("text", "") for c in res.get("content", []) if c.get("type", "text") == "text"]
    if res.get("isError"):
        raise RuntimeError((texts[0] if texts else "tool error")[:2000])
    body = texts[0] if texts else ""
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return body


if __name__ == "__main__":
    sid = session()
    if sys.argv[1] == "list":
        r = rpc("tools/list", {}, sid)
        for t in r.get("result", {}).get("tools", []):
            print(f"{t['name']:36s} {t.get('description','')[:110].replace(chr(10),' ')}")
        if "error" in r: print(r)
    elif sys.argv[1] == "call":
        r = rpc("tools/call", {"name": sys.argv[2], "arguments": json.loads(sys.argv[3] if len(sys.argv) > 3 else "{}")}, sid)
        if "error" in r: print(json.dumps(r["error"])[:3000]); sys.exit(2)
        res = r["result"]
        for c in res.get("content", []):
            print(c.get("text", json.dumps(c))[:int(os.environ.get("HAMCP_MAX", "6000"))])
        if res.get("isError"): sys.exit(3)
    elif sys.argv[1] == "schema":
        r = rpc("tools/list", {}, sid)
        want = set(sys.argv[2:])
        for t in r.get("result", {}).get("tools", []):
            if t["name"] in want:
                print("###", t["name"]); print(t.get("description","")[:1800]); print(json.dumps(t.get("inputSchema", {}), indent=1)[:2500]); print()
