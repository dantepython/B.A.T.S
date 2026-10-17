"""
B.A.T.S plugin: control Roblox Studio through a local bridge.
Requires the BatsBridge.lua Studio plugin to be installed (see tutorial).
"""

import json
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_PORT = 38741
_commands = queue.Queue()
_results = {}
_events = {}
_lock = threading.Lock()
_last_poll = 0.0
_server_started = False


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # keep the console quiet

    def _send(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        global _last_poll
        if self.path == "/poll":
            _last_poll = time.time()
            try:
                item = _commands.get_nowait()
            except queue.Empty:
                item = {}
            self._send(item)
        else:
            self._send({"error": "not found"})

    def do_POST(self):
        if self.path == "/result":
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length) or b"{}")
                cid = data.get("id")
                with _lock:
                    _results[cid] = data
                    ev = _events.get(cid)
                if ev:
                    ev.set()
            except Exception:
                pass
        self._send({"ok": True})


def _ensure_server():
    global _server_started
    if _server_started:
        return True
    try:
        server = ThreadingHTTPServer(("127.0.0.1", _PORT), _Handler)
    except OSError:
        return False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _server_started = True
    return True


# Start listening as soon as B.A.T.S loads the plugin, so Studio can connect early.
_ensure_server()


def _maybe_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


PLUGIN = {
    "name": "roblox_studio",
    "description": (
        "Controls Roblox Studio while it is open: inspect the Explorer tree, create "
        "instances (Parts, Models, GUIs, etc.), change properties, delete or clone "
        "objects, select objects, and create, read, edit or run Lua scripts. Use when "
        "the user says things like 'in Roblox Studio, create...', 'add a script that...', "
        "'make the part red', 'show me what's in Workspace'. Do NOT use game_updater or "
        "open_app for this; those only launch things."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "One of: ping, get_tree, create, set_property, get_property, delete, "
                    "clone, select, get_selection, create_script, get_source, set_source, run_code"
                ),
            },
            "path": {
                "type": "STRING",
                "description": "Dot path of the target, e.g. 'Workspace.Baseplate' or 'ServerScriptService.Main'",
            },
            "parent": {
                "type": "STRING",
                "description": "Dot path of the parent for create/create_script/clone, e.g. 'Workspace'",
            },
            "class_name": {
                "type": "STRING",
                "description": "Roblox class, e.g. Part, Model, Script, LocalScript, ModuleScript, ScreenGui",
            },
            "name": {"type": "STRING", "description": "Name for the new instance"},
            "property": {"type": "STRING", "description": "Property name for set_property/get_property"},
            "value": {
                "type": "STRING",
                "description": (
                    "Property value as JSON. Plain: 5, true, \"text\". Typed: "
                    "{\"t\":\"Vector3\",\"v\":[4,1,2]}, {\"t\":\"Color3\",\"v\":[255,0,0]}, "
                    "{\"t\":\"Enum\",\"v\":\"Material.Neon\"}, {\"t\":\"CFrame\",\"v\":[0,10,0]}, "
                    "{\"t\":\"UDim2\",\"v\":[0.5,0,0.5,0]}, {\"t\":\"BrickColor\",\"v\":\"Bright red\"}"
                ),
            },
            "properties": {
                "type": "STRING",
                "description": "JSON object of properties to apply on create, same value format as 'value'",
            },
            "source": {"type": "STRING", "description": "Lua source code for create_script/set_source/run_code"},
            "depth": {"type": "STRING", "description": "Tree depth for get_tree (default 2)"},
        },
        "required": ["action"],
    },
}


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        if not _ensure_server():
            return "Sir, the Roblox bridge port is already in use by another program."

        action = (parameters.get("action") or "").strip().lower()
        if not action:
            return "Sir, I need to know what to do in Roblox Studio."

        if time.time() - _last_poll > 5 and action != "ping":
            return (
                "Sir, Roblox Studio isn't connected. Make sure Studio is open "
                "and the BATS Bridge plugin is enabled."
            )

        cmd = {k: v for k, v in parameters.items() if v not in (None, "")}
        for key in ("value", "properties"):
            if key in cmd:
                cmd[key] = _maybe_json(cmd[key])
        if "depth" in cmd:
            try:
                cmd["depth"] = int(cmd["depth"])
            except Exception:
                cmd["depth"] = 2

        cid = uuid.uuid4().hex
        ev = threading.Event()
        with _lock:
            _events[cid] = ev
        _commands.put({"id": cid, "cmd": cmd})

        if not ev.wait(timeout=15):
            with _lock:
                _events.pop(cid, None)
            return "Sir, Roblox Studio didn't answer in time."

        with _lock:
            data = _results.pop(cid, {})
            _events.pop(cid, None)

        message = str(data.get("message", "Done."))
        if not data.get("ok", False):
            message = f"Sir, Studio reported a problem: {message}"

        if player:
            try:
                player.write_log(f"JARVIS: {message}")
            except Exception:
                pass
        return message
    except Exception as e:
        return f"Sir, roblox_studio failed: {e}"