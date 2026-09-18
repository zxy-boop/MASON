#!/usr/bin/env python3
"""MASON workbench: one browser page with the conversation on the left and the structure viewer on the right.

It reverse-proxies the opencode web interface and the MASON viewer under a single origin, so the page can embed
both in iframes and route viewer links from the conversation into the right-hand pane.

    python workbench.py --port 4096 --opencode 4106 --viewer 8931 --workspace /path/to/workspace
"""
import argparse, base64, http.client, json, socket, sys, threading, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SHELL = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>MASON</title>
<style>
  :root { --bg:#1b2140; --bar:#151a33; --fg:#c9d6f2; --accent:#2e86ab; --divider:#2a325c; }
  * { margin:0; padding:0; box-sizing:border-box; }
  html, body { height:100%; background:var(--bg); font:13px/1.4 -apple-system, "Helvetica Neue", Arial, sans-serif; }
  #wrap { display:flex; height:100%; }
  .pane { height:100%; min-width:240px; position:relative; display:flex; flex-direction:column; }
  #chatPane { flex:1 1 56%; }
  #viewPane { flex:1 1 44%; }
  iframe { width:100%; height:100%; border:0; background:#fff; flex:1 1 auto; min-height:0; }
  #divider { width:5px; cursor:col-resize; background:var(--divider); flex:0 0 auto; }
  .bar { height:30px; background:var(--bar); color:var(--fg); display:flex; align-items:center; gap:8px; padding:0 10px; flex:0 0 auto; }
  .bar .title { color:#fff; font-weight:600; letter-spacing:.3px; }
  .bar .addr { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; opacity:.75; }
  .bar button { background:none; border:1px solid var(--divider); color:var(--fg); border-radius:4px; padding:1px 8px; cursor:pointer; font-size:12px; }
  .bar button:hover { border-color:var(--accent); color:#fff; }
  #placeholder { position:absolute; inset:30px 0 0 0; display:flex; align-items:center; justify-content:center; text-align:center;
                 color:#5a6591; font-size:13px; background:#fff; padding:20px; line-height:1.7; }
  #placeholder.hidden { display:none; }
  #shield { position:fixed; inset:0; display:none; z-index:99; cursor:col-resize; }
</style>
<script>
// Seed opencode's client-side state before its iframe boots: register the workspace so the session history is
// visible, open the sidebar once, and default the interface language to English.
(function () {
  try {
    var DIR = "__WORKSPACE__";
    var KEY = "opencode.global.dat:server";
    var cur; try { cur = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) { cur = null; }
    if (!cur || typeof cur !== "object" || Array.isArray(cur)) cur = {};
    if (!Array.isArray(cur.list)) cur.list = [];
    if (!cur.projects || typeof cur.projects !== "object") cur.projects = {};
    var loc = Array.isArray(cur.projects.local) ? cur.projects.local : [];
    if (!loc.some(function (p) { return p && p.worktree === DIR; })) loc.push({ worktree: DIR, expanded: true });
    cur.projects.local = loc;
    if (!cur.lastProject || typeof cur.lastProject !== "object") cur.lastProject = {};
    if (typeof cur.lastProject.local !== "string") cur.lastProject.local = DIR;
    localStorage.setItem(KEY, JSON.stringify(cur));
    var LKEY = "opencode.global.dat:layout";
    if (localStorage.getItem(LKEY) === null)
      localStorage.setItem(LKEY, JSON.stringify({ sidebar: { opened: true, width: 300, workspaces: {}, workspacesDefault: false } }));
    var LANG = "opencode.global.dat:language";
    if (localStorage.getItem(LANG) === null) localStorage.setItem(LANG, JSON.stringify({ locale: "en" }));
  } catch (e) {}
})();
</script></head>
<body>
<div id="wrap">
  <div class="pane" id="chatPane">
    <div class="bar"><span class="title">MASON</span><span class="addr">conversation</span>
      <button id="newtab" title="Open the conversation in a new tab">&#8599;</button></div>
    <iframe id="chat" src="/__WORKSPACE_B64__" title="Conversation"></iframe>
  </div>
  <div id="divider" title="Drag to resize"></div>
  <div class="pane" id="viewPane">
    <div class="bar"><span class="title">Structure viewer</span><span class="addr" id="addr"></span>
      <button id="files" title="Browse every file in the workspace">Files</button>
      <button id="popout" title="Open the viewer in a new tab">&#8599;</button></div>
    <iframe id="view" title="Structure viewer"></iframe>
    <div id="placeholder">Structures built by the agent appear here.<br>Click a link in the conversation, or ask for a structure to be rendered.</div>
  </div>
</div>
<div id="shield"></div>
<script>
(function () {
  var chat = document.getElementById("chat"), view = document.getElementById("view");
  var addr = document.getElementById("addr"), placeholder = document.getElementById("placeholder");
  function openRight(url) {
    view.src = url; placeholder.classList.add("hidden");
    addr.textContent = decodeURIComponent(url.replace(location.origin, "").replace(/^\/viewer\//, "").replace(/\?t=\d+$/, ""));
    try { shownMtime = Math.max(shownMtime, Date.now() / 1000); } catch (_) {}
  }
  document.getElementById("popout").onclick = function () { if (view.src) window.open(view.src, "_blank"); };
  document.getElementById("newtab").onclick = function () { window.open(chat.src, "_blank"); };
  document.getElementById("files").onclick = function () { openRight(location.origin + "/viewer/"); };
  // Viewer links inside the conversation open in the right pane instead of a new tab.
  function routeTarget(raw, base) {
    var url; try { url = new URL(raw, base || location.href); } catch (_) { return null; }
    if (url.origin === location.origin && url.pathname.indexOf("/viewer/") === 0) return url.href;
    if (/:__VIEWER_PORT__$/.test(url.host)) return location.origin + "/viewer" + url.pathname + url.search;
    return null;
  }
  function hook() {
    try {
      var doc = chat.contentDocument;
      if (!doc || doc.__masonHooked) return;
      doc.__masonHooked = true;
      var win = chat.contentWindow, origOpen = win.open.bind(win);
      win.open = function (u, name, feat) { var t = routeTarget(u, win.location.href); if (t) { openRight(t); return null; } return origOpen(u, name, feat); };
      doc.addEventListener("click", function (e) {
        var a = e.target && e.target.closest ? e.target.closest("a[href]") : null;
        if (!a) return;
        var t = routeTarget(a.getAttribute("href"));
        if (t) { e.preventDefault(); e.stopPropagation(); openRight(t); }
      }, true);
    } catch (_) {}
  }
  setInterval(hook, 800); hook();
  // Every structure or selection page the agent writes is shown automatically in the right pane (links still work).
  var shownMtime = 0, autoStart = Date.now() / 1000 - 5;
  function pollLatest() {
    fetch("/__mason/latest", { cache: "no-store" }).then(function (r) { return r.json(); }).then(function (d) {
      if (!d.path || d.mtime <= shownMtime || d.mtime < autoStart) return;
      shownMtime = d.mtime;
      openRight(location.origin + "/viewer/" + d.path.split("/").map(encodeURIComponent).join("/") + "?t=" + Math.round(d.mtime));
    }).catch(function () {});
  }
  setInterval(pollLatest, 2500); pollLatest();
  // Drag the divider to resize the panes.
  var divider = document.getElementById("divider"), shield = document.getElementById("shield"), chatPane = document.getElementById("chatPane");
  divider.addEventListener("pointerdown", function (e) {
    e.preventDefault(); shield.style.display = "block";
    function move(ev) { var w = Math.min(Math.max(ev.clientX, 300), window.innerWidth - 300); chatPane.style.flex = "0 0 " + w + "px"; }
    function up() { shield.style.display = "none"; window.removeEventListener("pointermove", move); window.removeEventListener("pointerup", up); }
    window.addEventListener("pointermove", move); window.addEventListener("pointerup", up);
  });
})();
</script>
</body></html>
"""

def latest_page(workspace):
    """Newest .html page written to the workspace (structure renders and selection pages), for the viewer pane."""
    import os
    best = None
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("examples", "node_modules")]
        for f in files:
            if f.endswith(".html"):
                full = os.path.join(root, f)
                try: m = os.path.getmtime(full)
                except OSError: continue
                if best is None or m > best[0]: best = (m, os.path.relpath(full, workspace))
    return {"path": best[1].replace(os.sep, "/"), "mtime": best[0]} if best else {"path": None, "mtime": 0}


HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade"}


def make_handler(opencode, viewer, workspace):
    b64 = base64.urlsafe_b64encode(workspace.encode()).decode().rstrip("=")
    shell = (SHELL.replace("__WORKSPACE_B64__", b64).replace("__WORKSPACE__", workspace.replace("\\", "\\\\").replace('"', '\\"'))
             .replace("__VIEWER_PORT__", str(viewer[1]))).encode()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # keep the terminal quiet
            pass

        def do_ANY(self):
            parsed = urllib.parse.urlsplit(self.path)
            path = parsed.path
            if path == "/workbench":
                return self.reply(200, shell, "text/html; charset=utf-8")
            if path == "/__mason/latest":
                return self.reply(200, json.dumps(latest_page(workspace)).encode(), "application/json")
            if path == "/" and not parsed.query and self.headers.get("sec-fetch-dest") == "document":
                self.send_response(302); self.send_header("Location", "/workbench"); self.send_header("Content-Length", "0"); self.end_headers(); return
            if path == "/viewer" or path.startswith("/viewer/"):
                rest = path[len("/viewer"):] or "/"
                return self.proxy(viewer, rest + ("?" + parsed.query if parsed.query else ""))
            return self.proxy(opencode, self.path)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = do_HEAD = do_ANY

        def reply(self, status, body, ctype):
            self.send_response(status); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(body)

        def proxy(self, target, path):
            if "upgrade" in self.headers.get("Connection", "").lower():
                return self.tunnel(target, path)
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP and k.lower() != "host"}
            headers["Host"] = f"{target[0]}:{target[1]}"
            try:
                conn = http.client.HTTPConnection(target[0], target[1], timeout=600)
                conn.request(self.command, path, body=body, headers=headers)
                resp = conn.getresponse()
            except Exception as e:
                return self.reply(502, f"MASON: upstream service on port {target[1]} is not available ({e}).\n".encode(), "text/plain; charset=utf-8")
            self.send_response(resp.status, resp.reason)
            chunked = resp.getheader("Transfer-Encoding", "").lower() == "chunked"
            has_length = resp.getheader("Content-Length") is not None
            for k, v in resp.getheaders():
                if k.lower() in HOP or k.lower() == "content-length":
                    continue
                self.send_header(k, v)
            if has_length:
                self.send_header("Content-Length", resp.getheader("Content-Length"))
            elif chunked or self.command != "HEAD":
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                if self.command == "HEAD":
                    pass
                elif has_length:
                    remaining = int(resp.getheader("Content-Length"))
                    while remaining > 0:
                        data = resp.read(min(65536, remaining))
                        if not data:
                            break
                        self.wfile.write(data); remaining -= len(data)
                    self.wfile.flush()
                else:
                    while True:
                        data = resp.read1(65536)
                        if not data:
                            break
                        self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n"); self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True
            finally:
                conn.close()

        def tunnel(self, target, path):
            """WebSocket / upgrade requests: relay the raw request and pump bytes in both directions."""
            try:
                up = socket.create_connection(target, timeout=600)
            except Exception as e:
                return self.reply(502, f"MASON: upstream service on port {target[1]} is not available ({e}).\n".encode(), "text/plain; charset=utf-8")
            lines = [f"{self.command} {path} HTTP/1.1"]
            for k, v in self.headers.items():
                lines.append(f"{k}: {target[0]}:{target[1]}" if k.lower() == "host" else f"{k}: {v}")
            up.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            client = self.connection
            client.settimeout(None); up.settimeout(None)

            def pump(src, dst):
                try:
                    while True:
                        data = src.recv(65536)
                        if not data:
                            break
                        dst.sendall(data)
                except OSError:
                    pass
                finally:
                    for s in (src, dst):
                        try: s.shutdown(socket.SHUT_RDWR)
                        except OSError: pass
            t = threading.Thread(target=pump, args=(up, client), daemon=True); t.start()
            pump(client, up); t.join(timeout=5)
            self.close_connection = True

    return Handler


def serve(port, opencode_port, viewer_port, workspace, host="127.0.0.1"):
    handler = make_handler(("127.0.0.1", opencode_port), ("127.0.0.1", viewer_port), workspace)
    srv = ThreadingHTTPServer((host, port), handler); srv.daemon_threads = True
    print(f"MASON workbench: http://{host}:{port}/workbench", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--port", type=int, default=4096); ap.add_argument("--opencode", type=int, default=4106)
    ap.add_argument("--viewer", type=int, default=8931); ap.add_argument("--workspace", required=True)
    a = ap.parse_args(); serve(a.port, a.opencode, a.viewer, a.workspace)
