"""
Local web server that gives bpsm-navigator.html live search + PDF download.

WHY THIS EXISTS
---------------
A static HTML page cannot call bpsm.bihar.gov.in directly from JavaScript
in the browser — that request is blocked by CORS, since the site doesn't
send Access-Control-Allow-Origin headers for cross-origin fetches. Browsers
enforce this regardless of what the target server itself allows; there is
no client-side-only workaround for a plain HTML file.

The fix: run this small local server. IT does the actual HTTP requests to
bpsm.bihar.gov.in (server-to-server — CORS only applies to browsers, not
this), and the HTML page in your browser only ever talks to *this* server
on localhost, which is same-origin, so normal fetch() works fine.

SETUP:
    pip install flask requests beautifulsoup4

RUN:
    python bpsm_server.py

Then open http://127.0.0.1:5000/ in your browser (NOT the html file
directly via file:// — it must be loaded from this server to be same-origin
with the /api/* routes).

This file reuses the search/parsing logic from bpsm_search.py — keep both
files in the same folder.
"""

import re
import uuid
from urllib.parse import urljoin

import requests
from flask import Flask, request, jsonify, Response, send_from_directory

from bpsm_search import BASE_URL, MODE_TO_OPT, HEADERS, do_search, collect_form_fields

app = Flask(__name__, static_folder=".", static_url_path="")

# In-memory session store, local single-user tool — no persistence needed.
# {session_id: {"session": requests.Session, "soup": soup_after_search, "url": url, "rows": [...]}}
SESSIONS = {}

PDF_URL_RE = re.compile(r"""window\.open\(\s*['"]([^'"]+?\.pdf[^'"]*)['"]""", re.IGNORECASE)


@app.route("/")
def home():
    return send_from_directory(".", "bpsm-navigator.html")


@app.route("/api/search")
def api_search():
    year, p1, p2, p3, p4 = (request.args.get(k) for k in ("year", "p1", "p2", "p3", "p4"))
    mode = request.args.get("mode")
    query = request.args.get("query", "").strip()

    if not all([year, p1, p2, p3, p4, mode, query]):
        return jsonify({"error": "missing required parameter"}), 400
    if mode not in MODE_TO_OPT:
        return jsonify({"error": "mode must be 'name' or 'designation'"}), 400

    url = BASE_URL.format(year=year, p1=p1, p2=p2, p3=p3, p4=p4)
    session = requests.Session()
    rows, soup2 = do_search(session, url, mode, query)

    if soup2 is None:
        return jsonify({"error": "search box not found on that page — check the department/district/year"}), 400

    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = {"session": session, "soup": soup2, "url": url, "rows": rows}

    # Strip internal postback fields before sending to the browser.
    public_rows = [
        {
            "index": i,
            "office": row.get("Office", ""),
            "cadre": row.get("Cadre", ""),
            "designation": row.get("Designation", ""),
            "name": row.get("Name", ""),
            "has_pdf": bool(row.get("_postback_target")),
        }
        for i, row in enumerate(rows)
    ]

    return jsonify({"session_id": session_id, "rows": public_rows})


@app.route("/api/download")
def api_download():
    session_id = request.args.get("session_id")
    index = request.args.get("index", type=int)

    entry = SESSIONS.get(session_id)
    if entry is None:
        return jsonify({"error": "unknown or expired session — search again"}), 404
    if index is None or not (0 <= index < len(entry["rows"])):
        return jsonify({"error": "invalid row index"}), 400

    row = entry["rows"][index]
    session, url, soup2 = entry["session"], entry["url"], entry["soup"]

    target = row.get("_postback_target")
    argument = row.get("_postback_argument")
    if not target or not argument:
        return jsonify({"error": "this row has no downloadable PDF"}), 404

    data = collect_form_fields(soup2)
    data["__EVENTTARGET"] = target
    data["__EVENTARGUMENT"] = argument
    data.pop("ctl00$ContentPlaceHolder2$cmdSearch", None)

    r = session.post(url, headers=HEADERS, data=data, timeout=30)
    r.raise_for_status()

    if "pdf" in r.headers.get("Content-Type", "").lower():
        pdf_bytes = r.content
    else:
        m = PDF_URL_RE.search(r.text)
        if not m:
            return jsonify({"error": "could not locate the PDF URL in the response"}), 502
        pdf_url = urljoin(r.url, m.group(1))
        r2 = session.get(pdf_url, headers=HEADERS, timeout=30)
        r2.raise_for_status()
        pdf_bytes = r2.content

    filename = re.sub(r"[^\w\-]+", "_", row.get("Name") or "asset").strip("_") + ".pdf"
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
