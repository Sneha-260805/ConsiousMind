"""A5: local review page for the blinded HUMAN annotation review (one pass at a time). Standard library only.

    python scripts/19_human_review_server.py --pass 1 --reviewer "<name>"            # then open http://127.0.0.1:8765/
    python scripts/19_human_review_server.py --pass 2 --reviewer "<name>"            # only after Pass 1 is frozen
    ... --work-dir outputs/test_runs/<x>/stage2_inputs                                 # TEST ONLY
Serves on 127.0.0.1 only. Reads ONLY the view file of the requested pass (hash-checked against its export lock) and
that pass's own log. Every Save appends one validated, timestamped, hash-chained line (fsynced) to
data/processed/human_review/pass<N>_log.jsonl; nothing is ever overwritten, so stopping (Ctrl+C) and restarting
resumes exactly where you were. Refuses to start (and to save) once the pass is frozen.
"""
import argparse
import html
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import _bootstrap  # noqa: F401
from src import common as C
from src import human_review as hr

CSS = """
:root{--bg:#fbfbf9;--fg:#1d1d1b;--mut:#6b6b66;--line:#dcdcd6;--card:#fff;--acc:#2a5db0;--ok:#2e7d4f;--warn:#a15c00;
--cand:#fff4d6}
@media (prefers-color-scheme: dark){:root{--bg:#161615;--fg:#e9e9e4;--mut:#a0a09a;--line:#3a3a37;--card:#1f1f1d;
--acc:#7fa6ea;--ok:#6cc08f;--warn:#e0a24e;--cand:#3a3220}}
body{background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,Segoe UI,sans-serif;margin:0}
main{max-width:980px;margin:0 auto;padding:16px}
header{display:flex;flex-wrap:wrap;gap:12px;align-items:baseline;justify-content:space-between;border-bottom:1px solid var(--line);padding-bottom:8px}
h1{font-size:18px;margin:0} .mut{color:var(--mut);font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:14px 0}
.txt{white-space:pre-wrap;overflow-wrap:anywhere;font-family:ui-monospace,Consolas,monospace;font-size:14.5px}
.step{display:grid;grid-template-columns:4.5em 1fr;gap:8px;padding:6px 0;border-top:1px solid var(--line)}
.step:first-child{border-top:0} .sn{font-weight:600;color:var(--acc)} .cand{background:var(--cand);border-radius:6px;padding:6px}
fieldset{border:1px solid var(--line);border-radius:8px;margin:10px 0;padding:8px 12px}
label.opt{display:inline-block;margin:4px 10px 4px 0;padding:4px 8px;border:1px solid var(--line);border-radius:6px;cursor:pointer}
textarea{width:100%;box-sizing:border-box;font:inherit;background:var(--bg);color:var(--fg);border:1px solid var(--line);border-radius:6px}
button{font:inherit;padding:8px 16px;border-radius:6px;border:1px solid var(--acc);background:var(--acc);color:#fff;cursor:pointer}
a{color:var(--acc)} .grid{display:flex;flex-wrap:wrap;gap:4px}
.grid a{display:inline-block;min-width:2.2em;text-align:center;padding:2px 4px;border:1px solid var(--line);border-radius:4px;text-decoration:none;font-size:13px}
.grid a.done{background:var(--ok);color:#fff;border-color:var(--ok)} .grid a.here{outline:2px solid var(--acc)}
.err{color:var(--warn);font-weight:600} .q{font-weight:600}
"""


class State:
    def __init__(self, P, pass_no, reviewer, view, view_sha, max_reason):
        self.P, self.p, self.reviewer, self.view, self.view_sha, self.max_reason = (P, pass_no, reviewer, view, view_sha,
                                                                                    max_reason)
        self.by_id = {x["item_id"]: x for x in view["items"]}


def load(P, pass_no):
    el_path = P["export_lock"][pass_no]
    if not el_path.exists():
        raise SystemExit(f"STOP: Pass {pass_no} has not been exported" +
                         (" (scripts/18_export_human_review.py)" if pass_no == 1 else
                          " (it becomes available when Pass 1 is frozen: scripts/20_freeze_human_review.py --pass 1)"))
    if P["pass_lock"][pass_no].exists():
        raise SystemExit(f"STOP: Pass {pass_no} is frozen; judgments can no longer be changed")
    el = C.read_json(el_path)
    sha = C.sha256_file(P["view"][pass_no])
    if sha != el[f"pass{pass_no}_view_sha256"]:
        raise SystemExit(f"STOP: {P['view'][pass_no].name} changed after export")
    if pass_no == 2 and C.sha256_file(P["pass_lock"][1]) != el["pass1_lock_sha256"]:
        raise SystemExit("STOP: the Pass-2 view is not bound to the frozen Pass 1")
    return C.read_json(P["view"][pass_no]), sha


def esc(s):
    return html.escape(str(s), quote=True)


def page(st, idx, msg=""):
    view = st.view
    x = view["items"][idx]
    log = hr.read_log(st.P["log"][st.p])
    cur, nsav = hr.current(log)
    done = sum(i in cur for i in view["item_ids"])
    c = cur.get(x["item_id"])
    grid = "".join(f'<a href="/item/{n}" class="{"done" if i in cur else ""}{" here" if n == idx else ""}" '
                   f'title="{esc(i)}">{n + 1}</a>' for n, i in enumerate(view["item_ids"]))
    steps = x["steps"]
    rows = []
    for n, s in enumerate(steps, 1):
        cand = st.p == 2 and n == x["candidate_step"]
        rows.append(f'<div class="step{" cand" if cand else ""}"><div class="sn">Step {n}</div>'
                    f'<div class="txt">{esc(s.strip())}</div></div>')
    head = [f'<div class="card"><div class="mut">Problem</div><div class="txt">{esc(x["question"].strip())}</div></div>']
    if st.p == 1:
        sol = (f'<div class="card"><div class="mut">Original proposed solution ({len(steps)} steps)</div>'
               f'{"".join(rows)}</div>'
               f'<div class="card">Reference (correct) final answer: <b class="txt">{esc(x["gold_answer"])}</b><br>'
               f'Final answer reached by the proposed solution: <b class="txt">{esc(x["original_answer"])}</b></div>')
        val = c["judgment"] if c else None
        opts = "".join(f'<label class="opt"><input type="radio" name="judgment" value="{v}" required'
                       f'{" checked" if val == v else ""}> {v}</label>'
                       for v in list(range(1, len(steps) + 1)) + list(hr.PASS1_SPECIAL))
        jf = f'<fieldset><legend>earliest_error_step</legend>{opts}</fieldset>'
    else:
        f = x["candidate_step"]
        sol = (f'<div class="card"><div class="mut">Proposed solution, Steps 1 to {f} (any later steps are not '
               f'shown)</div>{"".join(rows)}</div>'
               f'<div class="card"><span class="mut">Candidate step:</span> <b>Step {f}</b>'
               f'<div class="txt cand">{esc(steps[-1].strip())}</div></div>')
        val = c["judgment"] if c else None
        opts = "".join(f'<label class="opt"><input type="radio" name="judgment" value="{v}" required'
                       f'{" checked" if val == v else ""}> {v}</label>' for v in hr.PASS2_VERDICTS)
        jf = f'<fieldset><legend>verdict on candidate Step {f}</legend>{opts}</fieldset>'
    conf = "".join(f'<label class="opt"><input type="radio" name="confidence" value="{v}" required'
                   f'{" checked" if c and c["confidence"] == v else ""}> {v}</label>' for v in hr.CONFIDENCE)
    saved = (f'<p class="mut">Saved {esc(c["saved_at"])} UTC &middot; {nsav[x["item_id"]]} save(s) recorded '
             f'(re-saving adds a revision; earlier saves are kept)</p>') if c else '<p class="mut">Not judged yet.</p>'
    form = (f'<form method="post" action="/save" class="card">'
            f'<p class="q">{esc(view["question"])}</p>{jf}'
            f'<fieldset><legend>confidence</legend>{conf}</fieldset>'
            f'<label>reason (optional, max {st.max_reason} characters)<br><textarea name="reason" rows="2" '
            f'maxlength="{st.max_reason}">{esc(c["reason"]) if c else ""}</textarea></label>'
            f'<input type="hidden" name="item_id" value="{esc(x["item_id"])}">'
            f'<input type="hidden" name="idx" value="{idx}">'
            f'<input type="hidden" name="opened_at" value="{esc(C.now())}">'
            f'<p><button type="submit">Save &amp; next</button></p>{saved}</form>')
    nav = (f'<p>{"<a href=/item/%d>&larr; previous</a>" % (idx - 1) if idx > 0 else ""} &nbsp; '
           f'{"<a href=/item/%d>next &rarr;</a>" % (idx + 1) if idx + 1 < len(view["items"]) else ""} &nbsp; '
           f'<a href="/next">next unjudged</a></p>')
    banner = '<p class="err">TEST ONLY</p>' if view.get("TEST_ONLY") else ""
    status = (f'<p class="err">{esc(msg)}</p>' if msg else "") + (
        '<p class="mut"><b>All items judged.</b> Stop the server (Ctrl+C) and run '
        f'<code>python scripts/20_freeze_human_review.py --pass {st.p} --reviewer "{esc(st.reviewer)}"</code></p>'
        if done == len(view["item_ids"]) else "")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width,initial-scale=1"><title>Human review pass {st.p}</title><style>{CSS}</style>'
            f'</head><body><main><header><h1>Stage B human review &middot; Pass {st.p}</h1>'
            f'<span class="mut">reviewer {esc(st.reviewer)} &middot; {done}/{len(view["item_ids"])} judged</span></header>'
            f'{banner}{status}<div class="grid" aria-label="items">{grid}</div>'
            f'<h2 style="font-size:17px">Item {idx + 1} of {len(view["items"])} &middot; {esc(x["item_id"])}</h2>'
            f'{"".join(head)}{sol}{form}{nav}</main></body></html>')


def next_pending(st, after=-1):
    cur, _ = hr.current(hr.read_log(st.P["log"][st.p]))
    ids = st.view["item_ids"]
    order = list(range(after + 1, len(ids))) + list(range(0, after + 1))
    return next((n for n in order if ids[n] not in cur), max(0, min(after + 1, len(ids) - 1)))


def make_handler(st):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body="", loc=None):
            b = body.encode("utf-8")
            self.send_response(code)
            if loc:
                self.send_header("Location", loc)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/next"):
                return self.send(303, loc=f"/item/{next_pending(st)}")
            if path.startswith("/item/") and path[6:].isdigit() and int(path[6:]) < len(st.view["items"]):
                return self.send(200, page(st, int(path[6:])))
            self.send(404, "not found")

        def do_POST(self):
            if urlparse(self.path).path != "/save":
                return self.send(404, "not found")
            n = int(self.headers.get("Content-Length") or 0)
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(n).decode("utf-8"), keep_blank_values=True).items()}
            idx = int(form.get("idx", "0")) if form.get("idx", "0").isdigit() else 0
            if st.P["pass_lock"][st.p].exists():
                return self.send(409, page(st, idx, "This pass is frozen; nothing was saved."))
            try:
                hr.append(st.P["log"][st.p], st.view, st.view_sha, st.p, st.reviewer, form.get("item_id"),
                          form.get("judgment"), form.get("confidence"), form.get("reason", ""),
                          form.get("opened_at"), st.max_reason)
            except ValueError as e:
                return self.send(400, page(st, idx, f"Not saved: {e}"))
            self.send(303, loc=f"/item/{next_pending(st, idx)}")
    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pass", dest="pass_no", type=int, choices=[1, 2], required=True)
    ap.add_argument("--reviewer", required=True)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--work-dir", default=None)
    a = ap.parse_args()
    if not a.reviewer.strip():
        raise SystemExit("STOP: --reviewer must be a non-empty name")
    cfg = C.load_config()
    P = hr.paths(a.work_dir)
    if a.pass_no == 2 and P["real"] and cfg["human_review"].get("pass2_waived_by"):
        raise SystemExit(f"STOP: the separate Pass-2 review was waived by amendment {cfg['human_review']['pass2_waived_by']}"
                         "; decisions come from scripts/21_freeze_pass1_only_review.py")
    view, sha = load(P, a.pass_no)
    log = hr.read_log(P["log"][a.pass_no])                      # verifies the chain before anything is served
    others = sorted({e["reviewer"] for e in log} - {a.reviewer.strip()})
    if others:
        raise SystemExit(f"STOP: this log was written by reviewer(s) {others}; use the same --reviewer name")
    st = State(P, a.pass_no, a.reviewer.strip(), view, sha, cfg["human_review"]["reason_max_chars"])
    srv = HTTPServer(("127.0.0.1", a.port), make_handler(st))
    url = f"http://127.0.0.1:{a.port}/"
    cur, _ = hr.current(log)
    print(f"Pass {a.pass_no}: {sum(i in cur for i in view['item_ids'])}/{view['n_items']} judged "
          f"({len(log)} saves logged). Review page: {url}   (Ctrl+C to stop; progress is saved on every Save)")
    if not a.no_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    log = hr.read_log(P["log"][a.pass_no])
    cur, _ = hr.current(log)
    print(f"stopped. Pass {a.pass_no}: {sum(i in cur for i in view['item_ids'])}/{view['n_items']} judged; "
          f"log sha256 {C.sha256_file(P['log'][a.pass_no]) if P['log'][a.pass_no].exists() else '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
