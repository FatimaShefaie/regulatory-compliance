"""Write scope and live evidence snapshots for an Article 50 review."""
import argparse
import csv
import hashlib
import html
import io
import json
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

CSV_FIELDS = {
    "SYSTEMS": ("system_id", "record_version"),
    "EVIDENCE": ("record_id", "system_id", "reported_at"),
    "CALENDAR": ("action_id", "system_id", "due_date", "source_version"),
}
CELEX = {"OJ": "32024R1689", "AMEND": "32026R1744",
         "CONSOLIDATED": "02024R1689-20260727"}
# HTTPS equivalents of the manifestations advertised by Cellar for these CELEX IDs.
CELLAR = {
    "OJ": "dc8116a1-3fe6-11ef-865a-01aa75ed71a1.0006.03",
    "AMEND": "b459c07f-86fb-11f1-bf5e-01aa75ed71a1.0006.03",
    "CONSOLIDATED": "b1730fb2-8f1c-11f1-9262-01aa75ed71a1.0001.03",
}
HTML_MARKERS = {
    "LAW": r"<title[^>]*>\s*Article 50:",
    "TIME": r"<title[^>]*>\s*Timeline for the Implementation of the EU AI Act",
    "FAQ": r"<title[^>]*>\s*Transparency obligations under Article 50",
    "OJ": r"32024R1689|L_202401689|2024/1689",
    "AMEND": r"32026R1744|L_202601744|2026/1744",
    "CONSOLIDATED": r"02024R1689-20260727|CL2024R1689EN001|27[./]07[./]2026",
}
NAMES = ("LAW", "OJ", "AMEND", "CONSOLIDATED", "TIME", "FAQ",
         "POLICY", "SYSTEMS", "EVIDENCE", "CALENDAR")


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def routes(name, url):
    if name in CSV_FIELDS:
        match = re.match(r"^/spreadsheets/d/([\w-]+)(?:/|$)", urlsplit(url).path)
        if not match or urlsplit(url).hostname != "docs.google.com":
            raise ValueError("Expected a Google Sheets document URL")
        yield f"https://docs.google.com/spreadsheets/d/{match[1]}/export?format=csv", [], None
    elif name == "POLICY":
        match = re.search(r"([a-f0-9]{32})/?$", urlsplit(url).path)
        if not match or not (urlsplit(url).hostname or "").endswith(".notion.site"):
            raise ValueError("Expected a published Notion page URL")
        page_id = str(UUID(match[1]))
        yield url, [], None
        yield "https://www.notion.so/api/v3/loadPageChunk", [
            "-H", "Content-Type: application/json", "--data",
            json.dumps({"pageId": page_id, "limit": 100, "cursor": {"stack": []},
                        "chunkNumber": 0, "verticalColumns": False}),
        ], page_id
    else:
        yield url, [], None
        if name in CELEX and is_disclosed_legal_work(name, url):
            yield f"https://publications.europa.eu/resource/cellar/{CELLAR[name]}/DOC_1", [
                "-H", "Accept: application/xhtml+xml", "-H", "Accept-Language: eng",
            ], None


def is_disclosed_legal_work(name, url):
    parts = urlsplit(url)
    if parts.hostname != "eur-lex.europa.eu":
        return False
    identifier = parse_qs(parts.query).get("uri", [""])[0].upper()
    return (
        identifier == f"CELEX:{CELEX[name]}"
        or (name == "OJ" and parts.path.rstrip("/") == "/eli/reg/2024/1689/oj/eng")
        or (name == "AMEND" and identifier == "OJ:L_202601744")
    )


def policy_blocks(text, page_id):
    data = json.loads(text)
    blocks = data["recordMap"]["block"]
    if data["cursor"]["stack"]:
        raise ValueError("Policy block response is incomplete")

    def walk(key):
        value = blocks[key]["value"]
        value = value.get("value", value)
        title = "".join(part[0] for part in value.get("properties", {}).get("title", []))
        yield key, value["type"], title
        for child in value.get("content", []):
            yield from walk(child)

    return list(walk(page_id))


def policy_metadata(text, page_id):
    if page_id is None:
        raise ValueError("Published HTML did not provide a verified policy block tree")
    ordered = policy_blocks(text, page_id)
    if "Current Internal Policies" not in ordered[0][2]:
        raise ValueError("Unexpected Notion page title")
    prefixes = {
        "policy_version": "Policy version:",
        "context_revision": "Context revision:",
        "case_dates": "Business observation date:",
    }
    metadata = {"page_id": page_id, "title": ordered[0][2]}
    for field, prefix in prefixes.items():
        matches = [line.strip() for _, _, line in ordered if line.strip().startswith(prefix)]
        if len(matches) != 1:
            raise ValueError(f"Expected one {prefix} block")
        metadata[field] = matches[0] if field == "case_dates" else matches[0][len(prefix):].strip()
    dates = re.fullmatch(
        r"Business observation date: (.+?)\. Authoring clarification date: (.+?)\..*",
        metadata["case_dates"], re.S,
    )
    if not dates:
        raise ValueError("Could not identify the operating context dates")
    for field, value in zip(("observed_on", "clarified_on"), dates.groups()):
        metadata[field] = datetime.strptime(value, "%d %B %Y").date().isoformat()
    return metadata, [key for key, _, line in ordered if line.strip()]


def inspect(name, raw, mime, page_id):
    text = raw.decode("utf-8-sig")
    if name in CSV_FIELDS:
        if not mime.startswith("text/csv"):
            raise ValueError("Expected CSV content")
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        fields = reader.fieldnames or []
        required = CSV_FIELDS[name]
        if len(fields) != len(set(fields)) or not set(required).issubset(fields):
            raise ValueError("Missing or ambiguous register headers")
        ids, versions = {}, set()
        for row in reader:
            key = row[required[0]]
            if None in row or None in row.values() or not key.strip() or key in ids:
                raise ValueError("Malformed row or missing or duplicate primary ID")
            ids[key] = reader.line_num
            for field in required:
                if field.endswith("_version") and row[field].strip():
                    versions.add(row[field])
        return {"versions": sorted(versions), "record_count": len(ids)}, ids
    if name == "POLICY":
        if page_id and not mime.startswith("application/json"):
            raise ValueError("Expected JSON content")
        return policy_metadata(text, page_id)
    if "html" not in mime or not re.search(HTML_MARKERS[name], text, re.I):
        raise ValueError("Document identity marker missing")
    title = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    metadata = {"title": html.unescape(title[1]).strip()} if title else {}
    if name in CELEX:
        metadata["celex"] = CELEX[name]
    return metadata or None, {"document_identity_marker": HTML_MARKERS[name]}


def capture(name, url, evidence_dir, as_of):
    if name in ("OJ", "AMEND"):
        role, authority = "binding-regulation", "binding"
    elif name == "CONSOLIDATED":
        role, authority = "other", "unknown"
    elif name == "POLICY":
        role, authority = "internal-policy", "internal-control"
    elif name in CSV_FIELDS:
        role, authority = "operational-record", "operational-evidence"
    else:
        role, authority = "official-guidance", "advisory"
    records = []
    try:
        attempts = list(routes(name, url))
        route_error = None
    except ValueError as error:
        attempts = [(url, [], None)]
        route_error = str(error)
    for index, (route, flags, page_id) in enumerate(attempts, 1):
        record_id = f"{name}-{index:02}"
        record = {
            "id": record_id, "source_id": name, "evidence_ids": [],
            "source_role": role, "authority": authority,
            "locator": route, "disclosed_locator": url, "content_type": "unknown",
            "retrieval_status": "unavailable", "version_metadata": None,
            "local_reference": None, "content_hash": None, "locators": None,
        }
        path = evidence_dir / f"{record_id}.body"
        if route_error:
            record.update(retrieved_at=utc_now(), retrieval_status="invalid",
                          summary=route_error)
        else:
            result = subprocess.run(
                ["curl", "--silent", "--show-error", "--location", "--max-time", "25",
                 "--max-filesize", "12000000", "--proto", "=https",
                 "--proto-redir", "=https", "--output", str(path), "--write-out",
                 "%{http_code}\\n%{content_type}\\n%{url_effective}",
                 *flags, route], capture_output=True, text=True,
            )
            record["retrieved_at"] = utc_now()
            code, mime, final_url = (result.stdout.splitlines() + ["", "", ""])[:3]
            record.update(http_status=int(code) if code.isdigit() else None,
                          content_type=mime or "unknown", effective_locator=final_url or None)
            raw = path.read_bytes() if path.exists() else b""
            if raw and not result.returncode:
                record.update(local_reference=f"deliverables/sources/{path.name}",
                              content_hash=digest(raw))
                record["evidence_ids"] = [record_id]
            else:
                path.unlink(missing_ok=True)
            if result.returncode or code != "200" or not raw:
                record["summary"] = f"No complete document returned (HTTP {code or 'unknown'}, curl {result.returncode})"
            else:
                try:
                    metadata, locators = inspect(name, raw, mime, page_id)
                    record.update(retrieval_status="retrieved", version_metadata=metadata,
                                  locators=locators, summary=f"{name} document retrieved")
                    if name in CELEX or name in ("LAW", "TIME", "FAQ"):
                        record["review_date_check"] = "pending authority-and-timing review"
                    if name == "POLICY" and metadata["observed_on"] != as_of:
                        record["review_date_check"] = "context observation date differs from the requested review date"
                except (ValueError, KeyError, TypeError, csv.Error, RecursionError) as error:
                    record.update(retrieval_status="unverified", summary=str(error))
        records.append(record)
        if record["retrieval_status"] == "retrieved":
            break
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=Path("sources.json"))
    parser.add_argument("--output", type=Path, required=True, help="new run directory")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 8, 26))
    parser.add_argument("--system-id", action="append", required=True,
                        help="system ID in requested scope, repeat for each ID")
    args = parser.parse_args()
    sources = json.loads(args.sources.read_text(encoding="utf-8"))
    if set(sources) != set(NAMES) or any(
        not isinstance(url, str) or urlsplit(url).scheme != "https" or not urlsplit(url).hostname
        for url in sources.values()
    ):
        parser.error("Source configuration must map each of the ten source IDs to an HTTPS URL")
    if any(not key.strip() for key in args.system_id) or len(set(args.system_id)) != len(args.system_id):
        parser.error("System IDs must be nonblank and unique")
    if shutil.which("curl") is None:
        parser.error("curl is required")
    args.output.mkdir(parents=True)
    snapshots = args.output / "snapshots"
    snapshots.mkdir()
    evidence = args.output / "deliverables" / "sources"
    evidence.mkdir(parents=True)
    run_id = str(uuid4())
    scope_id = "SCOPE-01"
    as_of = args.as_of.isoformat()

    def snapshot(stage, sequence, status, state, produced, predecessor=None, unresolved=None):
        return {
            "schema_version": "regulatory-compliance-stage-snapshot/2",
            "snapshot_id": f"{run_id}-{sequence:02}", "run_id": run_id,
            "stage": stage, "sequence": sequence, "created_at": utc_now(),
            "status": status, "predecessor": predecessor,
            "consumed_record_ids": [scope_id] if predecessor else [],
            "produced_record_ids": produced, "state": state,
            "unresolved": unresolved or [], "decisions": [],
        }

    scope = snapshot("scope", 1, "complete", {
        "as_of": f"{as_of}T00:00:00Z", "review_type": "EU AI Act Article 50",
        "systems_in_scope": args.system_id, "audiences": ["Legal", "Operations"],
        "approval_gates": ["Legal interpretation and exceptions",
                           "Operations activation and operational deadlines"],
        "scope_record": {"id": scope_id, "summary": "Requested Article 50 review scope",
                         "evidence_ids": [], "as_of": as_of, "system_ids": args.system_id},
    }, [scope_id])
    first = snapshots / "01-scope.json"
    write_json(first, scope)
    with ThreadPoolExecutor(max_workers=5) as executor:
        groups = list(executor.map(lambda name: capture(name, sources[name], evidence, as_of), NAMES))
    records = [record for group in groups for record in group]
    unresolved = [
        {"id": f"MISSING-{group[-1]['source_id']}",
         "summary": f"No verified document retrieved for {group[-1]['source_id']}",
         "evidence_ids": [record["id"] for record in group],
         "resolution_need": "Verify the live source route and document identity"}
        for group in groups if group[-1]["retrieval_status"] != "retrieved"
    ]
    status = "complete" if not unresolved else "partial"
    if any(item["id"] in {"MISSING-OJ", "MISSING-AMEND"} for item in unresolved):
        status = "blocked"
    second = snapshot("source-capture", 2, status, {"sources": records},
                      [record["id"] for record in records] + [item["id"] for item in unresolved],
                      {"snapshot_id": scope["snapshot_id"], "path": "snapshots/01-scope.json",
                       "sha256": digest(first.read_bytes())}, unresolved)
    write_json(snapshots / "02-source-capture.json", second)
    print(f"{status}: {sum(group[-1]['retrieval_status'] == 'retrieved' for group in groups)}"
          f"/{len(NAMES)} sources retrieved in {len(records)} attempts")
    return 0 if status == "complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
