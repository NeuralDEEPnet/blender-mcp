"""End-to-end smoke test: real MCP stdio client -> blender_mcp.server -> fake Blender addon socket.

Run with:  python tests/test_e2e.py
Works with MCP Python SDK 1.x and 2.x.
"""
import asyncio
import json
import os
import socket
import sys
import threading

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


PORT = _free_port()
EXPECTED_TOOLS = {
    "get_scene_info", "get_object_info", "create_object", "modify_object",
    "delete_object", "animate_object", "set_material", "execute_blender_code",
    "get_csm_status", "search_csm_models", "import_csm_model", "import_model_from_url",
    "apply_image_texture", "render_scene", "export_model",
}
received = []


def iserr(res):
    return getattr(res, "is_error", getattr(res, "isError", False))


def fake_blender(ready):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(5)
    ready.set()
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def handle(conn):
    while True:
        data = conn.recv(65536)
        if not data:
            return
        cmd = json.loads(data)
        received.append(cmd["type"])
        if cmd["type"] == "get_csm_status":
            result = {"enabled": False}
        elif cmd["type"] == "get_scene_info":
            result = {"name": "Scene", "object_count": 1, "objects": [{"name": "Cube"}]}
        else:
            result = {"name": cmd["params"].get("name", "Cube"), "echo": cmd["params"]}
        conn.sendall(json.dumps({"status": "success", "result": result}).encode())


async def main():
    ready = threading.Event()
    threading.Thread(target=fake_blender, args=(ready,), daemon=True).start()
    ready.wait(5)
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "blender_mcp.server"],
        env=dict(os.environ, BLENDER_MCP_PORT=str(PORT)),
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            missing = EXPECTED_TOOLS - tools
            assert not missing, f"missing tools: {missing}"
            print(f"OK: {len(tools)} tools listed")

            res = await s.call_tool("get_scene_info", {})
            assert not iserr(res) and "Cube" in res.content[0].text, res
            print("OK: get_scene_info ->", res.content[0].text[:60].replace("\n", " "))

            res = await s.call_tool("create_object", {"type": "CUBE", "name": "T"})
            assert not iserr(res), res
            print("OK: create_object")

            res = await s.call_tool("delete_object", {"name": "T"})
            assert not iserr(res), res
            print("OK: delete_object")

            res = await s.call_tool("import_model_from_url", {"url": "http://example.com/a.glb"})
            assert "Only https" in res.content[0].text, res
            print("OK: import_model_from_url rejects non-https")

            prompts = {p.name for p in (await s.list_prompts()).prompts}
            assert "asset_creation_strategy" in prompts
            print("OK: prompt listed")
    assert "get_scene_info" in received
    print("ALL PASSED")


asyncio.run(main())
