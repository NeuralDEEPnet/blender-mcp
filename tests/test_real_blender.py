"""Drive every non-CSM tool against a real, headless Blender.

Needs Blender as a Python module in the current interpreter:

    pip install bpy -e .
    python tests/test_real_blender.py            # add --offline to skip the URL downloads

It starts headless.py on a spare port, connects a real MCP stdio client to
blender_mcp.server, and checks the results inside Blender.
"""
import asyncio
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OFFLINE = "--offline" in sys.argv
MODEL_URL = "https://raw.githubusercontent.com/KhronosGroup/glTF-Sample-Assets/main/Models/Duck/glTF-Binary/Duck.glb"
IMAGE_URL = "https://raw.githubusercontent.com/KhronosGroup/glTF-Sample-Assets/main/Models/Duck/glTF/DuckCM.png"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def iserr(res):
    return getattr(res, "is_error", getattr(res, "isError", False))


def text(res):
    return res.content[0].text


async def run(port):
    env = dict(os.environ, BLENDER_MCP_PORT=str(port))
    params = StdioServerParameters(command=sys.executable, args=["-m", "blender_mcp.server"], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()

            async def call(tool, **args):
                res = await s.call_tool(tool, args)
                assert not iserr(res), f"{tool} failed: {res}"
                return res

            async def py(expr):
                return text(await call("execute_blender_code", code=expr)).split(": ", 1)[1]

            tools = {t.name for t in (await s.list_tools()).tools}
            for name in ("import_model_from_url", "apply_image_texture", "render_scene", "export_model"):
                assert name in tools, f"missing tool {name}"
            print(f"OK: {len(tools)} tools, Blender {await py('bpy.app.version_string')}")

            scene = json.loads(text(await call("get_scene_info")))
            assert scene["object_count"] >= 1, scene
            print("OK: get_scene_info ->", scene["object_count"], "objects")

            for kind in ("CUBE", "SPHERE", "CYLINDER", "PLANE", "CONE", "TORUS", "EMPTY", "CAMERA", "LIGHT"):
                out = text(await call("create_object", type=kind, name=f"T_{kind}", location=[0, 0, 5]))
                assert f"T_{kind}" in out, out
            print("OK: create_object for all 9 types")

            await call("modify_object", name="T_CUBE", location=[1, 2, 3], scale=[2, 2, 2])
            info = json.loads(text(await call("get_object_info", object_name="T_CUBE")))
            assert info["location"] == [1.0, 2.0, 3.0] and info["scale"] == [2.0, 2.0, 2.0], info
            print("OK: modify_object + get_object_info")

            out = text(await call("set_material", object_name="T_CUBE", material_name="Violet", color=[0.63, 0.34, 0.93]))
            assert "Violet" in out, out
            assert await py("bpy.data.objects['T_CUBE'].data.materials[0].name") == "Violet"
            print("OK: set_material ->", out)

            await call("delete_object", name="T_CONE")
            assert await py("'T_CONE' in bpy.data.objects") == "False"
            print("OK: delete_object")

            # Blender -> other apps: export, with base64 for 3DJ cad_import_model
            for fmt in ("glb", "stl", "obj", "fbx"):
                out = json.loads(text(await call("export_model", format=fmt, object_names=["T_CUBE"], include_base64=(fmt == "glb"))))
                assert out.get("size_bytes", 0) > 0, out
                if fmt == "glb":
                    assert base64.b64decode(out["base64"])[:4] == b"glTF", "export is not a GLB"
                os.unlink(out["filepath"])
                mtl = out["filepath"][:-4] + ".mtl"  # OBJ export writes a material file next to it
                if os.path.exists(mtl):
                    os.unlink(mtl)
            print("OK: export_model glb/stl/obj/fbx, base64 GLB is valid")

            # Blender -> SketchMonkey: a render returned as an image
            res = await call("render_scene", resolution_x=320, resolution_y=240, engine="CYCLES", samples=4)
            image = res.content[0]
            data = base64.b64decode(image.data)
            assert image.type == "image" and data[:8] == b"\x89PNG\r\n\x1a\n", "render is not a PNG"
            print(f"OK: render_scene -> PNG, {len(data)} bytes")

            res = await s.call_tool("import_model_from_url", {"url": "http://example.com/a.glb"})
            assert "Only https" in text(res)
            print("OK: import_model_from_url rejects non-https")

            if OFFLINE:
                print("SKIPPED: URL import and image texture (--offline)")
            else:
                out = json.loads(text(await call("import_model_from_url", url=MODEL_URL, name="Duck", scale=0.01)))
                assert out.get("succeed"), out
                meshes = [n for n in out["imported_objects"] if await py(f"bpy.data.objects[{n!r}].type") == "MESH"]
                assert meshes, out
                # the asset's root node ships with scale 0.01, so 0.01 * 0.01
                assert abs(float(await py("bpy.data.objects['Duck'].scale[0]")) - 1e-4) < 1e-7
                assert await py("bpy.data.objects['Duck'].parent") == "None", "name landed on a child"
                print("OK: import_model_from_url ->", out["imported_objects"])

                # SketchMonkey -> Blender: image as texture, on a UV mesh and on a UV-less one
                out = json.loads(text(await call("apply_image_texture", object_name=meshes[0], image_url=IMAGE_URL)))
                assert out.get("mapping") == "uv" and out["image_size"][0] > 0, out
                await py("bpy.data.objects['T_SPHERE'].data.uv_layers.remove(bpy.data.objects['T_SPHERE'].data.uv_layers[0])")
                out2 = json.loads(text(await call("apply_image_texture", object_name="T_SPHERE", image_url=IMAGE_URL)))
                assert out2.get("mapping") == "box_projection", out2
                linked = await py("bpy.data.objects['T_SPHERE'].data.materials[0].node_tree.nodes['Principled BSDF'].inputs['Base Color'].links[0].from_node.image.size[0]")
                assert int(linked) > 0
                print("OK: apply_image_texture uv + box projection,", out["image_size"])

            leftovers = [f for f in os.listdir(tempfile.gettempdir()) if f.startswith("blender_mcp_") and os.path.getmtime(os.path.join(tempfile.gettempdir(), f)) > START]
            assert not leftovers, f"temp files left behind: {leftovers}"
            print("OK: no temp files left behind")


START = time.time()


def main():
    try:
        import bpy  # noqa: F401
    except ImportError:
        sys.exit("SKIPPED: bpy is not installed in this interpreter (pip install bpy)")
    port = free_port()
    blender = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "headless.py"), "--port", str(port)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        for line in blender.stdout:
            if "headless ready" in line:
                break
        else:
            sys.exit("headless.py exited before it was ready")
        asyncio.run(run(port))
        print("ALL PASSED")
    finally:
        blender.terminate()


if __name__ == "__main__":
    main()
