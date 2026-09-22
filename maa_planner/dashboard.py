"""Loopback-only, read-only view of the supervisor's immutable history."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import UTC, datetime
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .supervisor import ACCEPTED_RESULTS, RUN_MODES, SupervisorError, load_run_events, runtime_snapshot

RUN_ID = re.compile(r"[0-9]{8}T[0-9]{6}[.][0-9]{6}Z-[0-9a-f]{8}")


def read_run(root: Path, run_id: str) -> dict:
    result = {"run_id": run_id, "status": "invalid", "phases": []}
    try:
        events = load_run_events(root, run_id)
        start = events[0]["payload"]
        expected = RUN_MODES[start["mode"]]
        if start["expected_phases"] != list(expected):
            raise ValueError("unexpected phases")
        phases = [dict(e["payload"], recorded_at=e["recorded_at"])
                  for e in events if e["event_type"] == "phase-finished"]
        names = [p["phase"] for p in phases]
        if len(set(names)) != len(names) or any(p not in expected for p in names):
            raise ValueError("invalid phase terminals")
        finishes = [e for e in events if e["event_type"] == "run-finished"]
        if len(finishes) > 1:
            raise ValueError("duplicate run terminal")
        status = "unfinished"
        if finishes:
            terminal = finishes[0]["payload"]
            accepted = (terminal["process_status"] == 0 and set(names) == set(expected)
                        and all(p["result"] in ACCEPTED_RESULTS for p in phases))
            status = "success" if accepted and terminal["status"] == "success" else "failed"
            if terminal["status"] == "success" and not accepted:
                raise ValueError("success without complete phase evidence")
        result.update(status=status, mode=start["mode"], started_at=events[0]["recorded_at"],
                      updated_at=events[-1]["recorded_at"],
                      finished_at=finishes[0]["recorded_at"] if finishes else None,
                      expected_phases=list(expected), phases=phases,
                      repository=start.get("repository", {}),
                      recovery=[{"event_type": e["event_type"], "recorded_at": e["recorded_at"]}
                                for e in events if e["event_type"].startswith("recovery-")])
    except (SupervisorError, OSError, ValueError, KeyError, TypeError, AttributeError):
        result.update(status="invalid", error="运行证据无法读取或校验未通过；请检查本地审计记录。")
    return result


def service_status(unit: str = "zootd.service") -> dict:
    """Read systemd only; never start a device or infer liveness from old events."""
    try:
        response = subprocess.run(
            ["systemctl", "--user", "show", unit, "--no-pager",
             "--property=LoadState,ActiveState,SubState,MainPID,Result"],
            capture_output=True, text=True, timeout=3, check=False)
        values = dict(line.split("=", 1) for line in response.stdout.splitlines() if "=" in line)
        if response.returncode or values.get("LoadState") != "loaded":
            return {"available": False}
        return {"available": True, **values}
    except (OSError, subprocess.TimeoutExpired):
        return {"available": False}


def history(root: Path, offset: int = 0, limit: int = 30) -> dict:
    directory = root / "var/state/supervisor/runs"
    ids = sorted((p.name for p in directory.iterdir() if RUN_ID.fullmatch(p.name)), reverse=True) if directory.exists() else []
    runs = [read_run(root, run_id) for run_id in ids[offset:offset + limit]]
    return {"total": len(ids), "offset": offset, "limit": limit, "runs": runs}


class Handler(BaseHTTPRequestHandler):
    def __init__(self, *args, root: Path, **kwargs):
        self.root = root
        super().__init__(*args, **kwargs)

    def send_body(self, status: int, body: bytes, content_type: str):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        # Reject foreign Host headers (including DNS rebinding). No CORS is enabled.
        if self.headers.get("Host") not in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}:
            self.send_body(403, b"Forbidden", "text/plain")
            return
        url = urlsplit(self.path)
        static = {"/": ("index.html", "text/html; charset=utf-8"),
                  "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                  "/style.css": ("style.css", "text/css; charset=utf-8")}
        try:
            if url.path in static:
                filename, mime = static[url.path]
                self.send_body(200, (self.root / "web" / filename).read_bytes(), mime)
                return
            if url.path == "/api/status":
                data = {"observed_at": datetime.now(UTC).isoformat(), "services": {unit: service_status(unit) for unit in
                                     ("zootd.service", "zootd-prereset.service")},
                        "runtime": runtime_snapshot(self.root)}
            elif url.path == "/api/runs":
                query = parse_qs(url.query)
                offset, limit = int(query.get("offset", ["0"])[0]), int(query.get("limit", ["30"])[0])
                if offset < 0 or not 1 <= limit <= 100:
                    raise ValueError("invalid pagination")
                data = history(self.root, offset, limit)
            elif url.path.startswith("/api/runs/") and RUN_ID.fullmatch(url.path[10:]):
                if not (self.root / "var/state/supervisor/runs" / url.path[10:]).exists():
                    self.send_body(404, b"Not found", "text/plain")
                    return
                data = read_run(self.root, url.path[10:])
            else:
                self.send_body(404, b"Not found", "text/plain")
                return
            self.send_body(200, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")
        except ValueError:
            self.send_body(400, b"Invalid request", "text/plain")
        except OSError:
            self.send_body(503, b"Data temporarily unavailable", "text/plain")


def main():
    parser = argparse.ArgumentParser(description="ZOOTd read-only local dashboard")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), partial(Handler, root=args.project_root.resolve()))
    print(f"ZOOTd 面板：http://127.0.0.1:{args.port} （Ctrl+C 停止）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
