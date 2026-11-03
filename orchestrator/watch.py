"""`orchestrator watch`: a live view of state.db on http://127.0.0.1:<port>.

Stdlib only. The view opens state.db with `mode=ro` (never migrates, so it cannot disturb a
running `run`/`resume`), binds loopback only, and passes every event/error text through
`envfile.redact` and a length cap before it leaves the process. The page polls `/api/snapshot`.

Its one write is the OK button (O14): when a run waits for the user to run the post steps, the
page shows the commands and `POST /api/ok` records the OK through `state.ack_user_wait`. That
endpoint takes only a same-origin JSON request (Host, Origin and Content-Type are checked), so
another web page open in the browser cannot press it.
"""

from __future__ import annotations

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from orchestrator.envfile import redact
from orchestrator.state import USER_OK, USER_WAIT, ack_user_wait

EVENT_LIMIT = 80
TEXT_LIMIT = 700


def _clip(text: str | None) -> str | None:
    text = redact(text)
    if text is not None and len(text) > TEXT_LIMIT:
        return text[:TEXT_LIMIT] + "…"
    return text


def snapshot(db_path: Path | str) -> dict:
    """Latest run, its steps, recent events and attempts, as plain JSON-able data."""
    path = Path(db_path)
    if not path.exists():
        return {"run": None, "note": "state.db does not exist yet"}
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        run = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        if run is None:
            return {"run": None, "note": "no runs recorded"}
        rid = run["id"]
        steps = con.execute(
            "SELECT session_id, phase, status, attempts, cost_usd, last_error, started_at, "
            "updated_at FROM steps WHERE run_id=? ORDER BY session_id, phase",
            (rid,),
        ).fetchall()
        events = con.execute(
            "SELECT id, session_id, kind, detail, created_at FROM events WHERE run_id=? "
            "ORDER BY id DESC LIMIT ?",
            (rid, EVENT_LIMIT),
        ).fetchall()
        attempts = con.execute(
            "SELECT session_id, phase, files_changed, error, created_at FROM attempts "
            "WHERE run_id=? ORDER BY id DESC LIMIT 30",
            (rid,),
        ).fetchall()
        decisions = con.execute("SELECT COUNT(*) FROM decisions WHERE run_id=?", (rid,)).fetchone()
        waits = con.execute(
            "SELECT w.id, w.session_id, w.detail, w.created_at FROM events w WHERE w.run_id=? "
            "AND w.kind=? AND NOT EXISTS (SELECT 1 FROM events k WHERE k.kind=? "
            "AND k.detail=CAST(w.id AS TEXT)) ORDER BY w.id",
            (rid, USER_WAIT, USER_OK),
        ).fetchall()
    finally:
        con.close()
    return {
        "run": {k: run[k] for k in run.keys()},
        "steps": [{**dict(s), "last_error": _clip(s["last_error"])} for s in steps],
        "events": [{**dict(e), "detail": _clip(e["detail"])} for e in events],
        "attempts": [{**dict(a), "error": _clip(a["error"])} for a in attempts],
        "decisions": decisions[0],
        # not clipped: the user copies these commands
        "waiting": [
            {
                "id": w["id"],
                "session_id": w["session_id"],
                "created_at": w["created_at"],
                "script": redact(json.loads(w["detail"] or "{}").get("script", "")),
            }
            for w in waits
        ],
    }


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Orchestrator watch</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#ddd;--ok:#157f3b;--bad:#b3261e;--warn:#9a6700}
@media (prefers-color-scheme:dark){:root{--bg:#161616;--fg:#e6e6e6;--mut:#999;--line:#333;
--ok:#4cc38a;--bad:#ff7b72;--warn:#e3b341}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 ui-monospace,Menlo,monospace;margin:0;
padding:16px}
h1{font-size:16px;margin:0 0 4px}h2{font-size:13px;margin:20px 0 6px;color:var(--mut)}
table{border-collapse:collapse;width:100%}td,th{border-bottom:1px solid var(--line);
padding:4px 8px;text-align:left;vertical-align:top}th{color:var(--mut);font-weight:normal}
.done{color:var(--ok)}.blocked,.failed,.permission_denied{color:var(--bad)}
.running,.quota_wait{color:var(--warn)}.mut{color:var(--mut)}
td.d{white-space:pre-wrap;word-break:break-word}
.wait{border:2px solid var(--warn);border-radius:6px;padding:12px;margin:16px 0}
.wait h2{color:var(--warn);margin:0 0 8px;font-size:15px}
.wait pre{background:var(--line);padding:10px;overflow-x:auto;white-space:pre;margin:8px 0}
button{font:inherit;padding:6px 14px;margin-right:8px;cursor:pointer}
</style></head><body>
<h1 id="h">…</h1><div id="sub" class="mut"></div>
<div id="waits"></div>
<h2>Steps</h2><table id="steps"></table>
<h2>Events (newest first)</h2><table id="events"></table>
<h2>Attempts (newest first)</h2><table id="attempts"></table>
<script>
const $=id=>document.getElementById(id);
function row(cells,cls){const tr=document.createElement('tr');cells.forEach((c,i)=>{
const td=document.createElement(i===cells.length-1&&cls==='ev'?'td':'td');
if(cls==='ev'&&i===cells.length-1)td.className='d';td.textContent=c==null?'-':c;
if(cls==='st'&&i===2)td.className=c;tr.appendChild(td)});return tr}
function fill(id,head,rows,cls){const t=$(id);t.replaceChildren();
const h=document.createElement('tr');head.forEach(x=>{const th=document.createElement('th');
th.textContent=x;h.appendChild(th)});t.appendChild(h);rows.forEach(r=>t.appendChild(row(r,cls)))}
async function tick(){try{const s=await (await fetch('/api/snapshot')).json();
if(!s.run){$('h').textContent=s.note;return}
waits(s.waiting);
const r=s.run;$('h').innerHTML='';const sp=document.createElement('span');sp.className=r.status;
sp.textContent=r.status;$('h').append('run '+r.id+' '+r.pipeline+' — ',sp);
$('sub').textContent='cost $'+r.total_cost_usd.toFixed(2)+' · started '+r.started_at+
' · decisions '+s.decisions+' · refreshed '+new Date().toLocaleTimeString();
fill('steps',['session','phase','status','tries','usd','updated','last error'],
s.steps.map(x=>[x.session_id,x.phase,x.status,x.attempts,x.cost_usd.toFixed(2),x.updated_at,
x.last_error]),'st');
fill('events',['#','time','session','kind','detail'],
s.events.map(e=>[e.id,e.created_at.slice(11,19),e.session_id,e.kind,e.detail]),'ev');
fill('attempts',['session','phase','files changed','time','error'],
s.attempts.map(a=>[a.session_id,a.phase,a.files_changed,a.created_at.slice(11,19),a.error]),'at');
}catch(e){$('sub').textContent='fetch failed: '+e}}
let shown='';
function waits(list){const key=list.map(w=>w.id).join(',');if(key===shown)return;shown=key;
const box=$('waits');box.replaceChildren();list.forEach(w=>{const d=document.createElement('div');
d.className='wait';const h=document.createElement('h2');
h.textContent=w.session_id+': танд хийх ажил байна (wait #'+w.id+', '+w.created_at.slice(11,19)+')';
const p=document.createElement('div');p.textContent='Доорх командуудыг дарааллаар нь ажиллуулж, '+
'тайлбар дахь үр дүн гарсныг шалгаад OK дарна уу. OK дармагц orchestrator үргэлжилнэ.';
const pre=document.createElement('pre');pre.textContent=w.script;
const cp=document.createElement('button');cp.textContent='Хуулах';
cp.onclick=()=>navigator.clipboard.writeText(w.script).then(()=>cp.textContent='Хуулсан');
const ok=document.createElement('button');ok.textContent='OK: ажиллуулж дууслаа';
const msg=document.createElement('span');msg.className='mut';
ok.onclick=async()=>{if(!confirm('Бүх командыг ажиллуулж, үр дүн зөв гарсан уу?'))return;
ok.disabled=true;try{const r=await fetch('/api/ok',{method:'POST',
headers:{'Content-Type':'application/json'},body:JSON.stringify({wait_id:w.id})});
const j=await r.json();msg.textContent=j.ok?'OK бүртгэгдлээ, үргэлжилж байна…':'Алдаа: '+j.error;
if(!j.ok)ok.disabled=false}catch(e){msg.textContent='Алдаа: '+e;ok.disabled=false}};
d.append(h,p,pre,cp,ok,msg);box.appendChild(d)})}
tick();setInterval(tick,2000);
</script></body></html>
"""


def make_handler(db_path: Path | str) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 (stdlib name)
            if self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", PAGE.encode())
            elif self.path == "/api/snapshot":
                try:
                    body = json.dumps(snapshot(db_path)).encode()
                except sqlite3.Error as exc:
                    body = json.dumps({"run": None, "note": f"state.db busy: {exc}"}).encode()
                self._send(200, "application/json", body)
            else:
                self._send(404, "text/plain", b"not found")

        def do_POST(self) -> None:  # noqa: N802 (stdlib name)
            if self.path != "/api/ok":
                self._send(404, "text/plain", b"not found")
                return
            # Same origin only: Host defeats DNS rebinding, Origin a cross-site fetch, and a JSON
            # Content-Type is something a plain HTML form cannot send.
            port = self.server.server_address[1]
            own = {f"127.0.0.1:{port}", f"localhost:{port}"}
            origin = self.headers.get("Origin")
            if (
                self.headers.get("Host") not in own
                or (origin is not None and origin.removeprefix("http://") not in own)
                or self.headers.get_content_type() != "application/json"
            ):
                self._send(403, "text/plain", b"forbidden")
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if not 0 < length <= 1024:
                    raise ValueError(length)
                wait_id = int(json.loads(self.rfile.read(length))["wait_id"])
            except (ValueError, KeyError, TypeError):
                self._send(400, "text/plain", b'expected {"wait_id": <int>}')
                return
            try:
                error = ack_user_wait(db_path, wait_id)
            except sqlite3.Error as exc:
                error = f"state.db busy: {exc}"
            body = json.dumps({"ok": error is None, "error": error}).encode()
            self._send(409 if error else 200, "application/json", body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    return Handler


def make_server(db_path: Path | str, port: int) -> ThreadingHTTPServer:
    """Loopback only: the view must never be reachable from the network."""
    return ThreadingHTTPServer(("127.0.0.1", port), make_handler(db_path))
