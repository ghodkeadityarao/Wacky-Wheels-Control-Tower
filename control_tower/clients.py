"""Two clients with one interface: the live enterprise server, and a local digital twin for dry runs."""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

from .config import DATA_DIR, PKG_DIR


class HttpClient:
    """POST /act with bounded retries on *transport* errors only (a 4xx tool error is never retried)."""

    def __init__(self, base_url: str = "http://127.0.0.1:8000", retries: int = 3, timeout: float = 30):
        self.base, self.retries, self.timeout = base_url.rstrip("/"), retries, timeout

    def _req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        last = None
        for attempt in range(self.retries):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return r.status, json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:            # the server answered: a tool error
                return e.code, json.loads(e.read() or b"{}")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last = e
                time.sleep(0.5 * 2 ** attempt)
        raise ConnectionError(f"enterprise server unreachable at {self.base}: {last}")

    def events(self):
        return self._req("GET", "/events")[1]

    def tools(self):
        return self._req("GET", "/tools")[1]

    def act(self, event_id, tool, args):
        code, res = self._req("POST", "/act", {"event_id": event_id, "tool": tool, "args": args})
        return code == 200, res


class TwinClient:
    """In-process copy of the provided enterprise systems, used to prove a plan before sending it."""

    _ent = None
    _seq = 0          # process-wide, so every world id is unique even across client instances

    def __init__(self, namespace: str = "dry"):
        if TwinClient._ent is None:
            if str(PKG_DIR) not in sys.path:
                sys.path.insert(0, str(PKG_DIR))
            from enterprise import Enterprise              # the provided mock, unmodified
            TwinClient._ent = Enterprise(str(DATA_DIR))
        self.ent, self.ns = TwinClient._ent, namespace
        self.calls = []

    def fresh(self, event_id: str) -> str:
        TwinClient._seq += 1
        return f"{event_id}#{self.ns}{TwinClient._seq}"

    def act(self, event_id, tool, args):
        from enterprise import ToolError
        try:
            res, ok = self.ent.call(event_id, tool, args), True
        except ToolError as e:
            res, ok = {"error": str(e)}, False
        self.calls.append({"event_id": event_id, "tool": tool, "args": args, "ok": ok})
        return ok, res

    def state(self, event_id):
        return self.ent.state(event_id)

    def tools(self):
        from enterprise import TOOLS      # the same list the server publishes at GET /tools
        return [{"tool": k, "desc": v["desc"], "args": v["args"]} for k, v in TOOLS.items()]
