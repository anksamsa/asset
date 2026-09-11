"""
BPSM Bihar Asset Declaration — automated search + PDF download (requests-based).

SEARCH FORM (confirmed from live page HTML):
    <select name="ctl00$ContentPlaceHolder2$cmbSearchOpt" id="...cmbSearchOpt">
        <option value="1">Name</option>
        <option value="2">Designation</option>
    </select>
    <input name="ctl00$ContentPlaceHolder2$txtEmpName" id="...txtEmpName" type="text">
    <input name="ctl00$ContentPlaceHolder2$cmdSearch" id="...cmdSearch" type="submit" value="Search">

This is a plain HTML form POST — no JS required to search.

PDF DOWNLOAD (confirmed from live row HTML):
    <input type="image" src="./AppResources/pdf1.jpg"
           onclick="javascript:__doPostBack('ctl00$ContentPlaceHolder2$GridView1','ViewAssets$0')">

This is an ASP.NET GridView command button. Clicking it does NOT link to a
file directly — it fires a second form POST with two extra hidden fields:
    __EVENTTARGET   = ctl00$ContentPlaceHolder2$GridView1
    __EVENTARGUMENT = ViewAssets$<row index>
The server's response to that POST is another HTML page, but it embeds a
small script the browser uses to pop the actual file in a new tab:
    <script>window.open('AssetDetails/2026/Departments/.../<file>.pdf')</script>
So the script here does it in two hops: POST the postback, regex the PDF
path out of the returned HTML, then GET that path directly.

Because both steps are ordinary form POSTs, everything here uses `requests`
only — no browser/Playwright needed.

SETUP:
    pip install requests beautifulsoup4

USAGE:
    # Search by name and download every matching row's PDF
    python bpsm_search.py --year 2026 --p1 1 --p2 8 --p3 28 --p4 2 \\
        --mode name --query "ANKUR" --download --out results/

    # Search only (no download), write a CSV of matches
    python bpsm_search.py --year 2026 --p1 1 --p2 8 --p3 0 --p4 0 \\
        --mode designation --query "ASSISTANT DIRECTOR" --csv out.csv

    # Batch: one search term per line, downloading PDFs for all matches
    python bpsm_search.py --year 2026 --p1 1 --p2 8 --p3 28 --p4 2 \\
        --mode name --query-file names.txt --download --out results/

P1-P4 reference:
    P1=1, P2=<dept>, P3=<district>, P4=2   department -> district
    P1=2, P2=<district>, P3=<dept>, P4=1   district -> department
    P1=1, P2=<dept>, P3=0, P4=0            department only (all districts)
    P1=2, P2=<district>, P3=0, P4=0        district only (all departments)

NOTE: needs live internet access; will not run in an offline/sandboxed environment.
"""

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing deps.\n    pip install requests beautifulsoup4")

BASE_URL = "https://bpsm.bihar.gov.in/Assets{year}/AssetDetails.aspx?P1={p1}&P2={p2}&P3={p3}&P4={p4}"

SEARCH_OPT_FIELD = "ctl00$ContentPlaceHolder2$cmbSearchOpt"
QUERY_FIELD = "ctl00$ContentPlaceHolder2$txtEmpName"
SUBMIT_FIELD = "ctl00$ContentPlaceHolder2$cmdSearch"
SUBMIT_VALUE = "Search"
GRIDVIEW_TARGET = "ctl00$ContentPlaceHolder2$GridView1"

MODE_TO_OPT = {"name": "1", "designation": "2"}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}

POSTBACK_RE = re.compile(r"__doPostBack\('([^']+)','([^']*)'\)")
PDF_URL_RE = re.compile(r"""window\.open\(\s*['"]([^'"]+?\.pdf[^'"]*)['"]""", re.IGNORECASE)


def collect_form_fields(soup):
    """Collect every named form control's current value from the page —
    hidden ViewState fields plus any visible inputs/selects — so a follow-up
    POST looks like a real postback of the existing form state."""
    data = {}
    for inp in soup.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        itype = (inp.get("type") or "text").lower()
        if itype in ("submit", "image", "button"):
            continue  # only include the one we're "clicking" explicitly
        if itype in ("checkbox", "radio"):
            if inp.has_attr("checked"):
                data[name] = inp.get("value", "on")
        else:
            data[name] = inp.get("value", "")
    for sel in soup.find_all("select"):
        name = sel.get("name")
        if not name:
            continue
        chosen = sel.find("option", selected=True) or sel.find("option")
        if chosen:
            data[name] = chosen.get("value", chosen.get_text(strip=True))
    return data


def parse_results(soup):
    """Pull grid rows into dicts; capture each row's __doPostBack args for
    the 'ViewAssets' PDF button if present."""
    rows = []
    table = soup.find("table", id=re.compile("GridView"))
    if table is None:
        return rows

    trs = table.find_all("tr")
    if not trs:
        return rows

    headers = [th.get_text(strip=True) for th in trs[0].find_all(["th", "td"])]
    for tr in trs[1:]:
        cells = tr.find_all("td")
        if not cells:
            continue
        row = {}
        for i, cell in enumerate(cells):
            key = headers[i] if i < len(headers) else f"col{i}"
            row[key] = cell.get_text(strip=True)

            link = cell.find("a", href=True)
            if link:
                row[f"{key}_link"] = urljoin("https://bpsm.bihar.gov.in", link["href"])

            img_btn = cell.find("input", type="image")
            if img_btn and img_btn.get("onclick"):
                m = POSTBACK_RE.search(img_btn["onclick"])
                if m:
                    row["_postback_target"] = m.group(1)
                    row["_postback_argument"] = m.group(2)
        rows.append(row)
    return rows


def do_search(session, url, mode, query):
    r = session.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    if not soup.find(id="ctl00_ContentPlaceHolder2_txtEmpName"):
        print("  ! search box not found on this page — check P1-P4 / year")
        return [], None

    data = collect_form_fields(soup)
    data[SEARCH_OPT_FIELD] = MODE_TO_OPT[mode]
    data[QUERY_FIELD] = query
    data[SUBMIT_FIELD] = SUBMIT_VALUE

    r2 = session.post(url, headers=HEADERS, data=data, timeout=20)
    r2.raise_for_status()
    soup2 = BeautifulSoup(r2.text, "html.parser")

    rows = parse_results(soup2)
    return rows, soup2


def download_pdf(session, url, soup_after_search, row, dest_path):
    """Fire the ViewAssets postback for one row, then fetch the actual PDF.

    The postback response is HTML, not the PDF itself — it embeds a
    <script>window.open('AssetDetails/2026/Departments/.../file.pdf')</script>
    snippet that the browser uses to pop the file in a new tab. So: do the
    postback, regex the PDF path out of the response, then GET it directly.
    """
    target = row.get("_postback_target")
    argument = row.get("_postback_argument")
    if not target or not argument:
        print(f"    ! no download button found for row: {row.get('Name') or row}")
        return False

    data = collect_form_fields(soup_after_search)
    data["__EVENTTARGET"] = target
    data["__EVENTARGUMENT"] = argument
    # Drop the search-submit field — we're clicking the grid button, not Search
    data.pop(SUBMIT_FIELD, None)

    r = session.post(url, headers=HEADERS, data=data, timeout=30)
    r.raise_for_status()

    content_type = r.headers.get("Content-Type", "")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    if "pdf" in content_type.lower():
        # Some deployments may stream it directly from the postback itself.
        dest_path.write_bytes(r.content)
        print(f"    saved {dest_path}")
        return True

    m = PDF_URL_RE.search(r.text)
    if m:
        pdf_url = urljoin(r.url, m.group(1))
        r2 = session.get(pdf_url, headers=HEADERS, timeout=30)
        r2.raise_for_status()
        if "pdf" in r2.headers.get("Content-Type", "").lower() or r2.content[:4] == b"%PDF":
            dest_path.write_bytes(r2.content)
            print(f"    saved {dest_path}  (from {pdf_url})")
            return True
        else:
            print(f"    ! fetched {pdf_url} but it wasn't a PDF "
                  f"(Content-Type: {r2.headers.get('Content-Type', 'unknown')})")
            return False

    # Fallback: couldn't find the pattern — dump for inspection.
    debug_path = dest_path.with_suffix(".debug.html")
    debug_path.write_text(r.text, encoding="utf-8", errors="ignore")
    print(f"    ! could not locate a PDF URL in the postback response; "
          f"saved raw response to {debug_path} for inspection")
    return False


def sanitize_filename(s):
    return re.sub(r"[^\w\-]+", "_", s).strip("_") or "unnamed"


def main():
    ap = argparse.ArgumentParser(description="Search BPSM asset records and download PDFs")
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument("--p1", required=True, choices=["1", "2"])
    ap.add_argument("--p2", required=True)
    ap.add_argument("--p3", required=True)
    ap.add_argument("--p4", required=True, choices=["0", "1", "2"])
    ap.add_argument("--mode", required=True, choices=["name", "designation"])
    ap.add_argument("--query", help="Single search term")
    ap.add_argument("--query-file", help="Text file, one search term per line")
    ap.add_argument("--download", action="store_true", help="Download PDFs for each result row")
    ap.add_argument("--out", default="results", help="Directory to save PDFs into (with --download)")
    ap.add_argument("--csv", default=None, help="Optional path to write results as CSV")
    args = ap.parse_args()

    if not args.query and not args.query_file:
        sys.exit("Provide --query or --query-file.")

    queries = [args.query] if args.query else [
        ln.strip() for ln in Path(args.query_file).read_text(encoding="utf-8").splitlines() if ln.strip()
    ]

    url = BASE_URL.format(year=args.year, p1=args.p1, p2=args.p2, p3=args.p3, p4=args.p4)
    out_dir = Path(args.out)

    all_rows = []
    for q in queries:
        print(f"Searching {args.mode}: {q}")
        # fresh session per query keeps ViewState/grid state unambiguous
        session = requests.Session()
        rows, soup2 = do_search(session, url, args.mode, q)

        if not rows:
            print("  (no results)")
            continue

        for i, row in enumerate(rows):
            row["_query"] = q
            print(f"  - {row.get('Name', row)} | {row.get('Designation', '')}")
            all_rows.append(row)

            if args.download:
                name_part = sanitize_filename(row.get("Name") or f"row{i}")
                dest = out_dir / f"{name_part}_{i}.pdf"
                download_pdf(session, url, soup2, row, dest)

    if args.csv and all_rows:
        import csv
        keys = sorted({k for row in all_rows for k in row.keys()})
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"\nWrote {len(all_rows)} rows to {args.csv}")


if __name__ == "__main__":
    main()
