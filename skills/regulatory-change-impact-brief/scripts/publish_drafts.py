"""Render and validate the local review package for stage 07."""
import argparse
import csv
import json
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from capture_sources import utc_now
from check_authority import record, sha256
from reconcile_evidence import earlier, predecessor

REGISTER_FIELDS = (
    "impact_id", "system_id", "rule_ref", "state", "evidence_ids", "reason", "owner",
    "proposed_action", "proposed_due_date", "approval_status", "basis_type",
    "resolution_need", "action_ids", "linked_actions",
)
BRIEF = "compliance-brief.md"
REGISTER = "impact-register.csv"
CALENDAR = "action-calendar.ics"
SNAPSHOT = "07-publication-validation.json"


def register_rows(impacts, actions, approvals):
    by_impact = defaultdict(list)
    approval_impacts = {entry.get("impact_id") for entry in approvals}
    for action in actions:
        for impact_id in action["impact_ids"]:
            by_impact[impact_id].append(action)
    for impact in impacts:
        linked = by_impact[impact["id"]]
        primary = next((a for a in linked if a["calendar_eligible"]), linked[0] if linked else {})
        linked_values = [
            {key: action[key] for key in ("action_id", "owner", "proposed_due_date", "approval_status", "calendar_eligible")}
            for action in linked
        ]
        yield {
            "impact_id": impact["id"], "system_id": impact["system_id"], "rule_ref": impact["rule_ref"],
            "state": impact["state"], "evidence_ids": ";".join(impact["evidence_ids"]),
            "reason": impact["reason"], "owner": (primary.get("owner") if primary else impact["owner"]) or "",
            "proposed_action": primary.get("summary", ""), "proposed_due_date": primary.get("proposed_due_date") or "",
            "approval_status": primary.get("approval_status") or (
                "pending" if impact["id"] in approval_impacts else "not-required"
            ),
            "basis_type": impact["basis_type"], "resolution_need": impact["resolution_need"] or "",
            "action_ids": ";".join(a["action_id"] for a in linked),
            "linked_actions": json.dumps(linked_values, ensure_ascii=False, separators=(",", ":")),
        }


def md_cell(value):
    return str(value or "").replace("\\", "\\\\").replace("|", "\\|").replace("\r", "").replace("\n", " ")


def md_row(*cells):
    return "| " + " | ".join(md_cell(value) for value in cells) + " |"


def action_row(action):
    disposition = "Tentative event" if action["calendar_eligible"] else action["calendar_exclusion_reason"]
    return md_row(action["action_id"], action["system_id"], action["summary"], action["owner"],
                  action["proposed_due_date"], action["approval_status"], ", ".join(action["impact_ids"]), disposition)


def request_row(request):
    versions = ", ".join(
        f"{source['evidence_id']} ({source['retrieved_at']})" for source in request["source_versions"]
    )
    return md_row(request["id"], request["reviewer"], request["system_id"], request["action_id"],
                  ", ".join(request["impact_ids"]), request["question"], ", ".join(request["evidence_ids"]), versions)


def render_brief(stage6, impacts, sources, authority):
    state = stage6["state"]
    lines = [
        "# Quillhaven Academy Article 50 compliance brief", "",
        f"- Run ID: `{stage6['run_id']}`",
        f"- Review date: **{state['as_of']}**",
        "- Recipients: Legal and Operations",
        f"- Status: **Draft, {stage6['status']}**",
        "- Legal decisions and operational activation remain pending.",
        "", "## Source quality and limitations", "",
        "| Source attempt | Retrieval status | Retrieved at | Version or identity |",
        "| --- | --- | --- | --- |",
    ]
    for source in sources:
        metadata = source.get("version_metadata") or {}
        version = json.dumps(metadata, ensure_ascii=False) if metadata else source["summary"]
        lines.append(md_row(source["id"], source["retrieval_status"], source["retrieved_at"], version))
    if authority["state"]["authority_blockers"]:
        lines += ["", "### Binding authority blockers", ""]
        lines.extend(f"- **{item['id']}** — {item['summary']} ({', '.join(item['evidence_ids'])})"
                     for item in authority["state"]["authority_blockers"])
    lines += ["", "## Findings", ""]
    for basis, title in (("statutory", "Statutory findings"), ("internal-policy", "Internal policy findings")):
        lines += [f"### {title}", ""]
        for impact in impacts:
            if impact["basis_type"] != basis:
                continue
            if impact["state"] == "supported-no-impact" and "supplier_timing" not in impact:
                continue
            timing = impact.get("supplier_timing")
            lines.append(f"- **{impact['id']}** ({impact['state']}) — {impact['reason']} "
                         f"Evidence: {', '.join(impact['evidence_ids']) or 'unavailable'}.")
            if timing:
                lines.append(f"  Supplier market placement: {timing['reported_eu_market_placement']}. "
                             f"Supplier marking deadline: {timing['provider_compliance_due']}.")
            if impact["resolution_need"]:
                lines.append(f"  {impact['resolution_need']}")
        lines.append("")
    lines += ["The impact register contains every assessment, including supported findings of no impact.",
              "", "## Draft actions", "",
              "| Action ID | System | Proposed action | Owner | Proposed date | Approval | Impacts | Calendar disposition |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(action_row(action) for action in state["proposed_actions"])
    if state["escalations"]:
        lines += ["", "### Scheduling and evidence exceptions", ""]
        lines.extend(f"- **{item['id']}** — {item['summary']} {item['resolution_need']}"
                     for item in state["escalations"])
    lines += ["", "## Decisions requested", "",
              "These requests are drafts for Legal and Operations. Their detached review bindings are in "
              "`snapshots/07-publication-validation.json`, under `state.review_bindings`.",
              "", "| Request ID | Reviewer | System | Action ID | Impact IDs | Question | Evidence | Source attempt and retrieval time |",
              "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    lines.extend(request_row(request) for request in state["review_requests"])
    return "\n".join(lines) + "\n"


def ical_text(value):
    return (value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
            .replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n"))


def fold(line):
    lines, part, size = [], [], 0
    for char in line:
        width = len(char.encode("utf-8"))
        if size + width > 75:
            lines.append("".join(part))
            part, size = [" "], 1
        part.append(char)
        size += width
    return "\r\n".join([*lines, "".join(part)])


def calendar_properties(action, timestamp):
    action_id = action["action_id"]
    description = (
        f"Action {action_id}; system {action['system_id']}; responsible role {action['owner']}; "
        f"basis {', '.join(action['evidence_ids'])}; approval {action['approval_status']}."
    )
    return {
        "UID": f"{action_id}@quillhaven-academy.project-b",
        "DTSTAMP": timestamp,
        "DTSTART;VALUE=DATE": date.fromisoformat(action["proposed_due_date"]).strftime("%Y%m%d"),
        "SUMMARY": ical_text(action["summary"]), "DESCRIPTION": ical_text(description),
        "STATUS": "TENTATIVE",
    }


def calendar_header():
    return ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Quillhaven Academy//Project B Draft Actions//EN"]


def render_calendar(actions, timestamp):
    lines = calendar_header()
    for action in actions:
        lines.append("BEGIN:VEVENT")
        lines.extend(f"{key}:{value}" for key, value in calendar_properties(action, timestamp).items())
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return ("\r\n".join(map(fold, lines)) + "\r\n").encode("utf-8")


def read_events(raw):
    physical = raw.split(b"\r\n")
    if physical[-1] or any(len(line) > 75 or b"\n" in line or b"\r" in line for line in physical):
        raise ValueError("Invalid calendar line endings or folding")
    text = re.sub(r"\r\n[ \t]", "", raw.decode("utf-8")).splitlines()
    header = calendar_header()
    if text[:len(header)] != header or text[-1] != "END:VCALENDAR":
        raise ValueError("Invalid VCALENDAR envelope")
    events, current = {}, None
    for line in text[len(header):-1]:
        if line == "BEGIN:VEVENT" and current is None:
            current = {}
        elif line == "END:VEVENT" and current is not None:
            uid = current["UID"]
            if uid in events:
                raise ValueError("Duplicate calendar UID")
            events[uid], current = current, None
        elif current is not None:
            key, separator, value = line.partition(":")
            if not separator or key in current or key in {"BEGIN", "END"}:
                raise ValueError("Invalid calendar event property")
            current[key] = value
        else:
            raise ValueError("Unexpected calendar content")
    if current is not None:
        raise ValueError("Unclosed calendar event")
    return events


def publish(path):
    raw = path.read_bytes()
    stage6 = predecessor(path, "actions-and-approvals", 6)
    stage5_path, stage5 = earlier(path, stage6, "impact-analysis", 5)
    stage4_path, stage4 = earlier(stage5_path, stage5, "evidence-reconciliation", 4)
    stage3_path, stage3 = earlier(stage4_path, stage4, "authority-and-timing", 3)
    stage2_path, stage2 = earlier(stage3_path, stage3, "source-capture", 2)
    earlier(stage2_path, stage2, "scope", 1)
    run_dir = path.parent.parent
    output = path.with_name(SNAPSHOT)
    deliverables = run_dir / "deliverables"
    files = [deliverables / name for name in (REGISTER, BRIEF, CALENDAR)]
    if output.exists() or any(file.exists() for file in files):
        raise FileExistsError("Draft output already exists. Use a fresh run directory.")
    stage6_state = stage6["state"]
    impacts = stage5["state"]["impacts"]
    actions = stage6_state["proposed_actions"]
    requests = stage6_state["review_requests"]
    blocked = stage6["status"] in {"blocked", "failed"}
    scheduled = [a for a in actions if a["calendar_eligible"] and not blocked]
    timestamp = datetime.fromisoformat(stage6["created_at"]).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rows = list(register_rows(impacts, actions, stage6_state["approval_requirements"]))
    deliverables.mkdir(exist_ok=True)
    with files[0].open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=REGISTER_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    with files[1].open("x", encoding="utf-8") as stream:
        stream.write(render_brief(stage6, impacts, stage2["state"]["sources"], stage3))
    with files[2].open("xb") as stream:
        stream.write(render_calendar(scheduled, timestamp))

    checks = []
    actual_actions = {a["action_id"]: a for a in actions}
    impact_ids = {item["id"] for item in impacts}
    stage6_ids = set(stage6["produced_record_ids"])

    def check(key, summary, passed, refs):
        checks.append(record(f"CHECK:{key}", summary, sorted(set(refs)), passed=passed))

    with files[0].open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        actual_rows = list(reader)
    check("REGISTER", "The register matches stage 05 findings and stage 06 action fields.",
          reader.fieldnames == list(REGISTER_FIELDS) and actual_rows == rows
          and len({row["impact_id"] for row in actual_rows}) == len(impacts),
          impact_ids | set(actual_actions))
    brief = files[1].read_text(encoding="utf-8")
    brief_lines = brief.splitlines()
    action_section = brief.split("## Draft actions\n", 1)[1].split("## Decisions requested\n", 1)[0]
    request_section = brief.split("## Decisions requested\n", 1)[1]
    check("BRIEF", "The brief contains the matching action and review tables and a detached binding reference.",
          [line for line in action_section.splitlines() if line.startswith("| ")][2:] == [action_row(a) for a in actions]
          and [line for line in request_section.splitlines() if line.startswith("| ")][2:] == [request_row(r) for r in requests]
          and f"- Run ID: `{stage6['run_id']}`" in brief_lines and f"- Review date: **{stage6_state['as_of']}**" in brief_lines
          and "`state.review_bindings`" in brief, [*actual_actions, *(r["id"] for r in requests)])
    properties = [calendar_properties(action, timestamp) for action in scheduled]
    expected_events = {event["UID"]: event for event in properties}
    try:
        events_match = read_events(files[2].read_bytes()) == expected_events
    except (KeyError, ValueError):
        events_match = False
    check("CALENDAR", "Calendar UIDs, dates, owners, evidence, approval and tentative state match eligible actions.",
          events_match and len(expected_events) == len(scheduled), [a["action_id"] for a in scheduled])

    def valid_subject(request):
        action = actual_actions.get(request["action_id"])
        if request["action_id"] and (not action or action["system_id"] != request["system_id"]
                                     or action["impact_ids"] != request["impact_ids"]):
            return False
        return (request["run_id"] == stage6["run_id"] and request["status"] == "pending"
                and set(request["impact_ids"]) <= impact_ids and request["id"] in stage6_ids)

    check("REFERENCES", "Action and review request subjects agree with upstream records.",
          all(set(a["impact_ids"]) <= impact_ids and a["action_id"] in stage6_ids for a in actions)
          and len(actual_actions) == len(actions) and len({r["id"] for r in requests}) == len(requests)
          and all(valid_subject(request) for request in requests), [*actual_actions, *(r["id"] for r in requests)])
    passed = all(entry["passed"] for entry in checks)
    artifacts = [
        {"id": f"ARTIFACT:{file.name}", "path": str(file.relative_to(run_dir)),
         "sha256": sha256(file.read_bytes()), "validation_status": "valid" if passed else "invalid"}
        for file in files
    ]
    brief_artifact = artifacts[1]
    bindings = [
        record(f"BIND:{request['id']}", "Detached binding to the final draft brief",
               [request["id"], brief_artifact["id"]], request_id=request["id"],
               run_id=request["run_id"], required_reviewer=request["required_reviewer"],
               system_id=request["system_id"], impact_ids=request["impact_ids"], action_id=request["action_id"],
               path=brief_artifact["path"], sha256=brief_artifact["sha256"], response_status="pending")
        for request in requests
    ] if passed else []
    check("BINDINGS", "Each draft request is bound to the exact brief bytes.",
          len(bindings) == len(requests) and all(item["sha256"] == brief_artifact["sha256"] for item in bindings),
          [r["id"] for r in requests])
    passed = all(entry["passed"] for entry in checks)
    publication_status = "failed" if not passed else "blocked" if blocked else "validated"
    records = artifacts + checks + bindings
    consumed = set(impact_ids) | stage6_ids
    if not consumed:
        consumed.update(stage6["consumed_record_ids"])
    decision = record(
        "DEC:PUBLICATION", "Publish a local review draft with detached review bindings.", sorted(consumed),
        concern="An authority blocker or unverified feedback could be mistaken for clearance.",
        options=["Treat a rendered package as approved", "Validate files and preserve pending human decisions"],
        chosen_behavior="Validate file agreement and bind each request to the brief hash; defer the calendar if authority is blocked.",
        rationale="Human decisions need the exact reviewed draft and verified feedback.",
        tradeoffs=["Binding must be regenerated for any revised brief."],
        downstream_effect="A reviewer can identify the precise draft without embedding its own hash in it.",
    )
    snapshot = {
        "schema_version": stage6["schema_version"], "run_id": stage6["run_id"],
        "snapshot_id": f"{stage6['run_id']}-07", "sequence": 7, "stage": "publication-validation",
        "created_at": utc_now(), "status": "failed" if not passed else stage6["status"],
        "predecessor": {"snapshot_id": stage6["snapshot_id"], "path": f"snapshots/{path.name}", "sha256": sha256(raw)},
        "consumed_record_ids": sorted(consumed),
        "produced_record_ids": [item["id"] for item in records + [decision]],
        "state": {"artifacts": artifacts, "validation_checks": checks, "review_bindings": bindings,
                  "publication_status": publication_status, "tentative_event_count": len(scheduled)},
        "unresolved": [entry for entry in checks if not entry["passed"]], "decisions": [decision],
    }
    with output.open("x", encoding="utf-8") as stream:
        json.dump(snapshot, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return snapshot, output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="path to snapshot 06")
    args = parser.parse_args()
    try:
        snapshot, output = publish(args.snapshot.resolve())
    except (OSError, ValueError, KeyError, TypeError, IndexError, csv.Error) as error:
        parser.error(str(error))
    status = snapshot["state"]["publication_status"]
    print(f"{status}: {output}")
    print(f"{snapshot['state']['tentative_event_count']} tentative calendar events, "
          f"{len(snapshot['state']['review_bindings'])} detached review bindings")
    return 0 if status == "validated" else 1


if __name__ == "__main__":
    raise SystemExit(main())
