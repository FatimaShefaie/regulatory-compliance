"""Write draft actions and review requests for stage 06."""
import argparse
import json
from pathlib import Path

from capture_sources import utc_now
from check_authority import record, sha256
from reconcile_evidence import earlier, predecessor

# A schedule change is acceptable. A change in action meaning, scope, or identity needs review.
CALENDAR_LINKS = {
    "ACT-001": ("AI-004", "Add and verify first-interaction AI notice", ("POLICY",)),
    "ACT-002": ("AI-007", "Resolve disclosure evidence conflict", ("POLICY",)),
    "ACT-003": ("AI-002", "Confirm recipient disclosure practice", ("POLICY",)),
    "ACT-004": ("AI-003", "Verify machine-readable provenance after publishing", ("POLICY",)),
    "ACT-005": ("AI-005", "Refresh provider-role and release evidence", ("POLICY", "A50-1", "A50-3", "A50-5")),
    "ACT-006": ("AI-008", "Complete provenance test", ("POLICY",)),
    "ACT-007": ("AI-008", "Review exception request", ("POLICY",)),
    "ACT-008": ("ALL", "Quarterly AI register evidence review", ()),
}
REVIEWERS = {"legal": "Legal", "operations": "Operations"}
OPEN = {"unresolved", "conflicting"}


def make_draft(path):
    raw = path.read_bytes()
    impact_snapshot = predecessor(path, "impact-analysis", 5)
    rec_path, reconciliation = earlier(path, impact_snapshot, "evidence-reconciliation", 4)
    auth_path, authority = earlier(rec_path, reconciliation, "authority-and-timing", 3)
    _, capture = earlier(auth_path, authority, "source-capture", 2)
    run_id = impact_snapshot["run_id"]
    as_of = impact_snapshot["state"]["as_of"]
    impacts = {item["id"]: item for item in impact_snapshot["state"]["impacts"]}
    calendar = reconciliation["state"]["calendar_records"]
    blocked = impact_snapshot["status"] in {"blocked", "failed"}

    indexed = {}
    for snapshot in (capture, authority, reconciliation, impact_snapshot):
        for collection in (*snapshot["state"].values(), snapshot["unresolved"], snapshot["decisions"]):
            if isinstance(collection, list):
                indexed.update((item["id"], item) for item in collection if isinstance(item, dict) and "id" in item)
    captured = {item["id"]: item for item in capture["state"]["sources"]}

    def versions(refs):
        todo, seen = list(refs), set()
        while todo:
            key = todo.pop()
            if key in seen:
                continue
            seen.add(key)
            todo.extend(indexed.get(key, {}).get("evidence_ids", []))
        sources = [captured[key] for key in sorted(seen & captured.keys())]
        return [
            {"evidence_id": source["id"], "source_id": source["source_id"],
             "version": source.get("version_metadata"), "retrieved_at": source["retrieved_at"],
             "content_hash": source.get("content_hash")}
            for source in sources
        ]

    actions, approvals, requests, escalations, decisions = [], [], [], [], []
    covered = set()

    def request(request_id, reviewer, question, refs, *, system_id, action_id=None, impact_ids=()):
        requests.append(record(
            request_id, question, refs, question=question, reviewer=reviewer, required_reviewer=reviewer,
            system_id=system_id, action_id=action_id, impact_ids=list(impact_ids),
            run_id=run_id, as_of=as_of, source_versions=versions(refs),
            status="pending", delivery_status="draft",
        ))

    for entry in calendar:
        values = entry["values"]
        action_id, system_id = values["action_id"], values["system_id"]
        mapping = CALENDAR_LINKS.get(action_id)
        gate = REVIEWERS.get(values["approval_required"])
        if not mapping or (system_id, values["action"]) != mapping[:2] or gate is None:
            escalation = record(f"ESC:CALENDAR:{action_id}", "Calendar action meaning or approval gate needs review",
                                [entry["id"]], system_id=system_id, calendar_values=values,
                                resolution_need="Operations should confirm the action scope and approval gate.")
            escalations.append(escalation)
            request(f"REQ:CALENDAR:{action_id}", "Operations", escalation["resolution_need"],
                    [entry["id"]], system_id=system_id)
            continue
        impact_ids = [f"IMP:{system_id}:{slot}" for slot in mapping[2]
                      if f"IMP:{system_id}:{slot}" in impacts]
        refs = [entry["id"], *impact_ids]
        covered.update(impact_ids)
        due = values["due_date"] or None
        owner = values["owner"] or None
        eligibility = bool(not blocked and due and owner and values["status"] in {"open", "planned", "scheduled"}
                           and (impact_ids or system_id == "ALL"))
        proposal = record(
            action_id, values["action"], refs, action_id=action_id, system_id=system_id,
            scope="register-wide" if system_id == "ALL" else "system", impact_ids=impact_ids,
            owner=owner, proposed_due_date=due, calendar_record_id=entry["id"],
            calendar_source_status=values["status"], calendar_source_version=values["source_version"],
            approval_required=values["approval_required"], approval_status="pending",
            calendar_eligible=eligibility, draft_status="proposed",
        )
        actions.append(proposal)
        questions = {
            "ACT-005": "What current-release records establish the provider role, and what biometric data, if any, does the system use to infer emotions or intentions or categorise people?",
            "ACT-007": "Does the exception request contain a rationale, named owner and expiry date, and what is Legal’s decision on it?",
            "ACT-008": "Confirm the scope and proposed date of the register-wide evidence review.",
        }
        question = questions.get(action_id, f"Confirm the proposed action and the evidence needed to address it. {values['action']}.")
        if not eligibility:
            reason = ("Calendar drafting is deferred while binding authority is blocked." if blocked else
                      "The calendar row is blocked or lacks a supported scope, owner, date, or schedulable status.")
            proposal["calendar_exclusion_reason"] = reason
            escalations.append(record(f"ESC:{action_id}", reason, refs, system_id=system_id,
                                      action_id=action_id, resolution_need="Confirm scheduling eligibility and any missing fields."))
        reviewers = [gate] if gate == "Operations" else [gate, "Operations"]
        for reviewer in reviewers:
            request_id = f"REQ:{action_id}:{reviewer.upper()}"
            review_question = question if reviewer == gate else "Confirm the operational owner, timing, and dependencies after Legal review."
            request(request_id, reviewer, review_question, refs, system_id=system_id,
                    action_id=action_id, impact_ids=impact_ids)
            approvals.append(record(
                f"APP:{action_id}:{reviewer.upper()}", f"{reviewer} review pending", refs,
                status="pending", reviewer=reviewer, action_id=action_id, request_id=request_id,
            ))

    for impact in impacts.values():
        impact_id = impact["id"]
        system_id = impact["system_id"]
        statutory = impact["basis_type"] == "statutory"
        if statutory and not blocked and (impact["state"] in OPEN or impact["state"] == "supported-impact"):
            request_id = f"REQ:{impact_id}:LEGAL"
            request(request_id, "Legal",
                    impact["resolution_need"] or "Confirm the draft interpretation and its evidence basis.",
                    [impact_id], system_id=system_id, impact_ids=[impact_id])
            approvals.append(record(f"APP:{impact_id}:LEGAL", "Legal interpretation pending", [impact_id],
                                    status="pending", reviewer="Legal", impact_id=impact_id, request_id=request_id))
        if impact_id in covered or (statutory and blocked) or not impact["resolution_need"]:
            continue
        action_id = f"DRAFT:{impact_id}"
        actions.append(record(action_id, impact["resolution_need"], [impact_id],
                              action_id=action_id, system_id=system_id, scope="system",
                              impact_ids=[impact_id], owner=None, proposed_due_date=None,
                              calendar_record_id=None, approval_status="pending",
                              calendar_eligible=False, draft_status="proposed",
                              calendar_exclusion_reason="No matching calendar row establishes an owner and date."))
        request_id = f"REQ:{action_id}:OPERATIONS"
        request(request_id, "Operations", "Confirm the owner and proposed date for this evidence action.",
                [impact_id], system_id=system_id, action_id=action_id, impact_ids=[impact_id])
        approvals.append(record(f"APP:{action_id}:OPERATIONS", "Operations review pending", [impact_id],
                                status="pending", reviewer="Operations", action_id=action_id, request_id=request_id))

    if blocked:
        refs = [r["id"] for r in authority["state"]["authority_blockers"]]
        if not refs:
            refs = [item["id"] for item in impact_snapshot["unresolved"]]
        escalation = record("ESC:AUTHORITY", "Binding authority remains blocked", refs,
                            resolution_need="Identify and verify the missing binding authority before statutory conclusions proceed.")
        escalations.append(escalation)
        request("REQ:AUTHORITY:LEGAL", "Legal", escalation["resolution_need"], refs, system_id=None)

    consumed = set(impacts)
    for item in actions + requests + escalations:
        consumed.update(item["evidence_ids"])
    decisions.append(record(
        "DEC:ACTION-SCHEDULING", "Reuse verified source dates only for matched, schedulable calendar rows.",
        sorted(consumed), concern="An unsupported date or owner could be mistaken for an authorized commitment.",
        options=["Infer an owner and deadline", "Use verified calendar values and leave unsupported fields blank"],
        chosen_behavior="Copy matched source fields, preserve blocked calendar status, and leave unsupported proposals undated.",
        rationale="Operations owns activation and scheduling, and Legal owns interpretation and exception approval.",
        tradeoffs=["Some proposed actions remain unscheduled until Operations supplies the missing fields."],
        downstream_effect="Stage 07 can render only eligible date-bearing proposals as tentative events.",
    ))
    status = "complete"
    if requests or escalations or impact_snapshot["status"] == "partial":
        status = "partial"
    if blocked:
        status = "blocked"
    return {
        "schema_version": impact_snapshot["schema_version"], "run_id": run_id,
        "snapshot_id": f"{run_id}-06", "sequence": 6, "stage": "actions-and-approvals",
        "created_at": utc_now(), "status": status,
        "predecessor": {"snapshot_id": impact_snapshot["snapshot_id"], "path": f"snapshots/{path.name}",
                        "sha256": sha256(raw)},
        "consumed_record_ids": sorted(consumed),
        "produced_record_ids": [r["id"] for r in actions + approvals + requests + escalations + decisions],
        "state": {"as_of": as_of, "proposed_actions": actions, "approval_requirements": approvals,
                  "review_requests": requests, "escalations": escalations},
        "unresolved": escalations, "decisions": decisions,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="path to snapshot 05")
    args = parser.parse_args()
    path = args.snapshot.resolve()
    output = path.with_name("06-actions-and-approvals.json")
    try:
        result = make_draft(path)
        with output.open("x", encoding="utf-8") as file:
            json.dump(result, file, indent=2, ensure_ascii=False)
            file.write("\n")
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.error(str(error))
    state = result["state"]
    print(f"{result['status']}: {output}")
    print(f"{len(state['proposed_actions'])} draft actions, {len(state['review_requests'])} draft review requests")
    return 1 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
