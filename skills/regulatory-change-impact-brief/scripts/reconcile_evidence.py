"""Reconcile company records and write evidence reconciliation snapshot 04."""
import argparse
import csv
import io
import json
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

from capture_sources import policy_blocks, policy_metadata, utc_now
from check_authority import record, select_binding_attempts, sha256, verified_content

TABLES = {
    "SYSTEMS": ("system_id", "system_name use_case owner provider_role deployer_role exposed_group "
                "output_type current_notice human_review evidence_status evidence_updated_at record_version"),
    "EVIDENCE": ("record_id", "system_id record_type reported_at owner status evidence_ref evidence_state notes"),
    "CALENDAR": ("action_id", "system_id action owner due_date status approval_required source_version"),
}
EVIDENCE_STATES = {"complete", "partial", "missing", "stale", "conflicting", "not_applicable", "not-applicable"}
SOURCES = ("SYSTEMS", "POLICY", "EVIDENCE", "CALENDAR")
COLLECTIONS = ("system_facts", "policy_controls", "operating_context", "incident_evidence",
               "calendar_records", "conflicts", "evidence_gaps")


def predecessor(path, stage, sequence):
    snapshot = json.loads(path.read_bytes())
    if (snapshot["stage"], snapshot["sequence"]) != (stage, sequence):
        raise ValueError(f"Expected stage {sequence:02} {stage}")
    return snapshot


def earlier(path, snapshot, stage, sequence):
    link = snapshot["predecessor"]
    run_dir = path.parent.parent
    target = (run_dir / link["path"]).resolve()
    if not target.is_relative_to(run_dir):
        raise ValueError("Predecessor path escapes the run directory")
    raw = target.read_bytes()
    value = json.loads(raw)
    if (sha256(raw) != link["sha256"] or value["snapshot_id"] != link["snapshot_id"]
            or value["run_id"] != snapshot["run_id"]
            or (value["stage"], value["sequence"]) != (stage, sequence)):
        raise ValueError(f"Stage {sequence:02} predecessor failed verification")
    return target, value


def table_rows(source, body, evidence_id):
    key, fields = TABLES[source]
    reader = csv.DictReader(io.StringIO(body, newline=""), strict=True)
    headers = reader.fieldnames or []
    if len(set(headers)) != len(headers) or not {key, *fields.split()}.issubset(headers):
        raise ValueError(f"{source} has missing or ambiguous required headers")
    rows = []
    for values in reader:
        if None in values or None in values.values():
            raise ValueError(f"{source} has a malformed CSV record ending at line {reader.line_num}")
        identity = values[key]
        rows.append(record(f"{source}:{identity}", f"{source} record {identity}", [evidence_id],
                           system_id=values["system_id"], locator={"csv_line": reader.line_num},
                           values=values))
    return rows


def policy_records(body, evidence_id, page_id, as_of, state):
    metadata, _ = policy_metadata(body, page_id)
    control, headings = True, {}
    for block_id, kind, text in policy_blocks(body, page_id):
        if kind == "header":
            control = text == "Internal AI-use policy"
            headings.clear()
        elif kind == "sub_header":
            headings = {"section": text}
        elif kind == "sub_sub_header":
            headings["subsection"] = text
        elif kind in ("text", "bulleted_list") and text.strip():
            system_id = headings.get("subsection")
            if control:
                bucket = "policy_controls"
                dates = {"policy_version": metadata["policy_version"]}
            else:
                bucket = "operating_context"
                dates = {
                    "context_revision": metadata["context_revision"], "observed_on": metadata["observed_on"],
                    "clarified_on": metadata["clarified_on"],
                    "matches_review_date": metadata["observed_on"] == as_of.isoformat(),
                }
            state[bucket].append(record(
                f"POLICY:{block_id}", text, [evidence_id],
                locator={"page_id": page_id, "block_id": block_id, **headings},
                system_id=system_id, **dates,
            ))
    if not state["policy_controls"] or not state["operating_context"]:
        raise ValueError("Policy controls or operating context not found")
    return metadata


def reason_for(source, values, as_of):
    primary_id = TABLES[source][0]
    if not values[primary_id].strip() or not values["system_id"].strip():
        return "Required primary or system identity is blank"
    date_field = {"SYSTEMS": "evidence_updated_at", "EVIDENCE": "reported_at", "CALENDAR": "due_date"}[source]
    value = values[date_field]
    if value or source != "CALENDAR":
        try:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError
        except ValueError:
            return f"{date_field} is missing or invalid"
        if source != "CALENDAR" and parsed > as_of:
            return f"{date_field} is after the review date"
    if source == "SYSTEMS" and any(values[key] not in {"yes", "no", "unknown"}
                                    for key in ("provider_role", "deployer_role")):
        return "Provider or deployer role value is unrecognized"
    state_field = {"SYSTEMS": "evidence_status", "EVIDENCE": "evidence_state"}.get(source)
    if state_field and values[state_field] not in EVIDENCE_STATES:
        return f"{state_field} is unrecognized"
    return None


def reconcile(path):
    authority_raw = path.read_bytes()
    authority = predecessor(path, "authority-and-timing", 3)
    capture_path, capture = earlier(path, authority, "source-capture", 2)
    _, scope = earlier(capture_path, capture, "scope", 1)
    as_of = datetime.fromisoformat(scope["state"]["as_of"]).date()
    in_scope = set(scope["state"]["systems_in_scope"])
    state = {name: [] for name in COLLECTIONS}
    state["as_of"] = as_of.isoformat()
    attempts = select_binding_attempts(capture, SOURCES)
    tables = {}
    consumed = []

    def issue(issue_id, summary, refs, need, **details):
        bucket = "conflicts" if details.get("evidence_state") == "conflicting" else "evidence_gaps"
        state[bucket].append(record(issue_id, summary, refs, resolution_need=need, **details))

    for source, selection in attempts.items():
        attempt = selection["selected"]
        refs = [entry["id"] for entry in selection["attempts"]]
        consumed.extend(refs)
        try:
            if attempt is None:
                raise ValueError(f"Source selection {selection['status']}")
            body = verified_content(attempt, path.parent.parent).decode("utf-8-sig")
            if source == "POLICY":
                parsed = {"policy_controls": [], "operating_context": []}
                metadata = policy_records(body, attempt["id"], attempt["version_metadata"]["page_id"], as_of, parsed)
                state.update(parsed)
                if metadata["observed_on"] != as_of.isoformat():
                    issue("GAP:POLICY-DATE", "Operating context observation date differs from the review date",
                          [attempt["id"]], "Obtain operating facts observed for this review", evidence_state="unverified")
            else:
                tables[source] = table_rows(source, body, attempt["id"])
        except (OSError, ValueError, KeyError, TypeError, csv.Error, RuntimeError) as error:
            issue(f"GAP:{source}", f"{source}: {error}", refs,
                  "Retrieve and verify the expected source records", evidence_state="unverified")

    valid = defaultdict(list)
    for source, rows in tables.items():
        counts = Counter(entry["id"] for entry in rows)
        for entry in rows:
            values = entry["values"]
            problem = ("Duplicate primary identity" if counts[entry["id"]] > 1
                       else reason_for(source, values, as_of))
            if problem:
                line = entry["locator"]["csv_line"]
                issue(f"GAP:{source}:LINE-{line}", problem, entry["evidence_ids"],
                      "Confirm the intended record and its values with the source owner",
                      system_id=entry["system_id"], owner=values.get("owner") or None,
                      evidence_state="conflicting" if counts[entry["id"]] > 1 else "unverified",
                      locator=entry["locator"], values=values)
                continue
            valid[source].append(entry)

    known = {entry["system_id"] for entry in valid["SYSTEMS"]}
    by_system = {entry["system_id"]: entry for entry in valid["SYSTEMS"]}
    for system_id in sorted(in_scope - known):
        refs = [entry["id"] for entry in attempts["SYSTEMS"]["attempts"]]
        issue(f"GAP:SYSTEMS:{system_id}", "No verified register entry for the requested system",
              refs, "Obtain the current system register record", system_id=system_id, evidence_state="missing")

    facts = defaultdict(list)
    general = []
    for entry in state["operating_context"]:
        if not entry["matches_review_date"]:
            continue
        if entry["system_id"]:
            facts[entry["system_id"]].append(entry["id"])
        else:
            general.append(entry["id"])

    joined = {"EVIDENCE": defaultdict(list), "CALENDAR": defaultdict(list)}
    for source, bucket in (("EVIDENCE", "incident_evidence"), ("CALENDAR", "calendar_records")):
        for entry in valid[source]:
            system_id = entry["system_id"]
            if source == "CALENDAR" and system_id == "ALL":
                entry["scope"] = "register-wide"
                state[bucket].append(entry)
                continue
            if system_id not in known:
                issue(f"GAP:LINK:{entry['id']}", f"{source} system ID has no verified register match",
                      entry["evidence_ids"], "Confirm the system identity against the register",
                      system_id=system_id, owner=entry["values"]["owner"] or None,
                      evidence_state="unverified", locator=entry["locator"], values=entry["values"])
            if system_id not in in_scope:
                continue
            state[bucket].append(entry)
            joined[source][system_id].append(entry)

    for system_id in sorted(in_scope):
        system = by_system.get(system_id)
        evidence = joined["EVIDENCE"][system_id]
        calendar = joined["CALENDAR"][system_id]
        if system:
            system["summary"] = system["values"]["system_name"]
            system["operating_context_ids"] = general + facts[system_id]
            system["incident_evidence_ids"] = [entry["id"] for entry in evidence]
            system["calendar_record_ids"] = [entry["id"] for entry in calendar]
            state["system_facts"].append(system)
        for entry in evidence + ([system] if system else []):
            values = entry["values"]
            condition = values.get("evidence_state", values.get("evidence_status"))
            if condition not in {"missing", "partial", "stale", "conflicting"}:
                continue
            related = list(dict.fromkeys(entry["evidence_ids"] + (system["evidence_ids"] if system else [])))
            issue(f"ISSUE:{entry['id']}", values.get("notes") or f"Register evidence is {condition}",
                  related, "Ask the record owner to resolve the reported evidence issue",
                  system_id=system_id, owner=values.get("owner") or None, evidence_state=condition,
                  source_record_id=entry["id"], context_record_ids=facts[system_id],
                  related_record_ids=list(dict.fromkeys(
                      [entry["id"]] + ([system["id"]] if system else [])
                      + [item["id"] for item in evidence])),
                  locator=entry["locator"])
        if system and system["values"]["provider_role"] == "unknown":
            issue(f"GAP:PROVIDER:{system_id}", "Current provider role is unresolved",
                  list(dict.fromkeys(system["evidence_ids"] + [ref for item in evidence for ref in item["evidence_ids"]]
                                    + [ref for item in state["operating_context"] if item["id"] in facts[system_id]
                                       for ref in item["evidence_ids"]])),
                  "Obtain current-release development, commissioning, branding and role evidence",
                  system_id=system_id, owner=system["values"]["owner"] or None, evidence_state="missing",
                  related_record_ids=[system["id"]] + [entry["id"] for entry in evidence] + facts[system_id])

    unresolved = state["conflicts"] + state["evidence_gaps"]
    if not consumed:
        consumed = authority["produced_record_ids"][:1]
    if not consumed:
        raise ValueError("No upstream records available to link stage 04")
    status = "blocked" if authority["status"] == "blocked" else "partial" if unresolved else "complete"
    result = {
        "schema_version": authority["schema_version"], "run_id": authority["run_id"],
        "snapshot_id": f"{authority['run_id']}-04", "sequence": 4, "stage": "evidence-reconciliation",
        "created_at": utc_now(), "status": status,
        "predecessor": {"snapshot_id": authority["snapshot_id"], "path": f"snapshots/{path.name}",
                        "sha256": sha256(authority_raw)},
        "consumed_record_ids": consumed,
        "produced_record_ids": [entry["id"] for key in COLLECTIONS for entry in state[key]],
        "state": state, "unresolved": unresolved, "decisions": [],
    }
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="path to snapshot 03")
    args = parser.parse_args()
    path = args.snapshot.resolve()
    output = path.with_name("04-evidence-reconciliation.json")
    try:
        result = reconcile(path)
        with output.open("x", encoding="utf-8") as file:
            json.dump(result, file, indent=2, ensure_ascii=False)
            file.write("\n")
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.error(str(error))
    state = result["state"]
    print(f"{result['status']}: {output}")
    print(f"{len(state['system_facts'])} systems, {len(state['incident_evidence'])} evidence records, "
          f"{len(state['calendar_records'])} calendar records, "
          f"{len(state['conflicts'])} conflicts, {len(state['evidence_gaps'])} gaps")
    return 1 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
