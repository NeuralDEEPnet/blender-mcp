"""Run the BlenderMCP addon without the Blender GUI.

Two ways to start it:

    blender --background --python headless.py            # a normal Blender install
    pip install bpy && python headless.py                # Blender as a Python module

Options (after `--` when launched through the blender binary):
    --port 9876          socket port the MCP server connects to
    --host localhost     interface to bind
    --blend scene.blend  open this file first

Background Blender does not run `bpy.app.timers`, which the addon uses to hop
onto the main thread, so this runner swaps in a queue and drains it here.
"""
import argparse
import importlib.util
import os
import queue
import sys
import time

import bpy


def parse_args():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else sys.argv[1:]
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default=os.environ.get("BLENDER_MCP_HOST", "localhost"))
    p.add_argument("--port", type=int, default=int(os.environ.get("BLENDER_MCP_PORT", "9876")))
    p.add_argument("--blend", default=None)
    args, _ = p.parse_known_args(argv)
    return args


def load_addon():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "addon.py")
    spec = importlib.util.spec_from_file_location("blender_mcp_addon", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    args = parse_args()
    if args.blend:
        bpy.ops.wm.open_mainfile(filepath=args.blend)

    addon = load_addon()
    addon.register()

    jobs = queue.Queue()
    bpy.app.timers.register = lambda fn, first_interval=0.0, persistent=False: jobs.put(fn)

    server = addon.BlenderMCPServer(host=args.host, port=args.port)
    server.start()
    if not server.running:
        sys.exit(f"Could not start BlenderMCP on {args.host}:{args.port}")
    print(f"BlenderMCP headless ready on {args.host}:{args.port} (Blender {bpy.app.version_string})", flush=True)

    try:
        while server.running:
            try:
                fn = jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            fn()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
