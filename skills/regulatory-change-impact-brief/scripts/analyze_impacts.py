"""Write the stage 05 review draft from verified rules and reconciled case facts."""
import argparse
import json
from pathlib import Path

from capture_sources import utc_now
from check_authority import record, sha256
from reconcile_evidence import earlier, predecessor

BASIS_FIELDS = (
    "system_name", "use_case", "owner", "provider_role", "deployer_role", "exposed_group",
    "output_type", "current_notice", "human_review", "evidence_status",
)
EVIDENCE_FIELDS = ("record_id", "record_type", "owner", "status", "evidence_ref", "evidence_state", "notes")
STATES = {"supported-impact", "supported-no-impact", "conflicting", "unresolved"}
TRANSITION = "TIMING-A50-2-TRANSITION"
GENERAL = "TIMING-GENERAL"
BASELINE = Path(__file__).with_name("impact_case.json")


def fingerprint(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def facts_for(state, system_id):
    system = next((r for r in state["system_facts"] if r["system_id"] == system_id), None)
    context_ids = set(system["operating_context_ids"]) if system else set()
    evidence_ids = set(system["incident_evidence_ids"]) if system else set()
    context = sorted((r for r in state["operating_context"] if r["id"] in context_ids),
                     key=lambda r: r["summary"])
    evidence = sorted((r for r in state["incident_evidence"] if r["id"] in evidence_ids),
                      key=lambda r: r["id"])
    return system, context, evidence


def case_fingerprint(system, context, evidence):
    if system is None:
        return None
    return fingerprint({
        "register": {key: system["values"][key] for key in BASIS_FIELDS},
        "context": sorted(r["summary"] for r in context),
        "evidence": [{key: r["values"][key] for key in EVIDENCE_FIELDS} for r in evidence],
    })


def analyze(path):
    reconciliation = predecessor(path, "evidence-reconciliation", 4)
    authority_path, authority = earlier(path, reconciliation, "authority-and-timing", 3)
    capture_path, capture = earlier(authority_path, authority, "source-capture", 2)
    _, scope = earlier(capture_path, capture, "scope", 1)
    config = json.loads(BASELINE.read_bytes())
    facts = reconciliation["state"]
    legal = {r["id"]: r for key in ("binding_rules", "timing_rules") for r in authority["state"][key]}
    issues = facts["conflicts"] + facts["evidence_gaps"]
    controls = facts["policy_controls"]
    controls_match = fingerprint(sorted(r["summary"] for r in controls)) == config["policy_sha256"]
    impacts, decisions, consumed = [], [], set()
    as_of = facts["as_of"]
    authority_blocked = authority["status"] in {"blocked", "failed"}

    for system_id in scope["state"]["systems_in_scope"]:
        system, context, evidence = facts_for(facts, system_id)
        profile = config["systems"].get(system_id, {})
        system_issues = [r for r in issues if r.get("system_id") in (system_id, None)]
        company_records = ([system] if system else []) + context + evidence
        company_match = (
            as_of == config["as_of"]
            and case_fingerprint(system, context, evidence) == profile.get("facts_sha256", "")
            and not any(r.get("evidence_state") == "unverified" for r in system_issues
                        if r["id"] != "GAP:CALENDAR")
        )
        owner = (system["values"]["owner"] or None) if system else None
        slot_ids = [f"A50-{n}" for n in range(1, 6)] + ["POLICY"]
        for slot in slot_ids:
            legal_rule = legal.get(f"RULE-{slot}")
            rule_ref = "Internal AI-use policy" if slot == "POLICY" else f"Article 50({slot[-1]})"
            reviewed = profile.get("assessments", {}).get(slot)
            basis = list(company_records)
            if slot == "POLICY":
                basis.extend(controls)
                rule_matches = controls_match
            else:
                basis.extend(r for r in (legal_rule, legal.get(GENERAL)) if r)
                rule_matches = (
                    not authority_blocked and legal_rule is not None
                    and fingerprint(legal_rule["provision_text"]) == config["rule_sha256"].get(slot)
                    and legal.get(GENERAL, {}).get("applicable_as_of") is True
                )
            if not rule_matches or not company_match or reviewed is None:
                if not rule_matches:
                    reason = ("Policy controls are unavailable or changed." if slot == "POLICY"
                              else "Verified binding authority or applicable reviewed timing is unavailable.")
                else:
                    reason = "Current records do not establish the reviewed facts for this assessment."
                state = "unresolved"
                need = "Review the current authority and company evidence before reaching a conclusion."
                basis.extend(system_issues)
                if slot != "POLICY" and authority_blocked:
                    basis.extend(authority["state"]["authority_blockers"])
            else:
                state, reason, need = reviewed
                if state in {"conflicting", "unresolved"}:
                    basis.extend(system_issues)
            if state not in STATES:
                raise ValueError(f"Invalid assessment state for {system_id} {slot}")
            impact_id = f"IMP:{system_id}:{slot}"
            fields = {
                "system_id": system_id, "rule_ref": rule_ref, "state": state,
                "basis_type": "internal-policy" if slot == "POLICY" else "statutory",
                "owner": owner, "reason": reason, "resolution_need": need,
            }
            if slot == "A50-2" and state == "supported-no-impact" and system_id in config["supplier_transition_system_ids"]:
                transition = legal.get(TRANSITION)
                if not authority_blocked and transition and transition.get("market_placement_before") == "2026-08-02" and transition.get("compliance_due") == "2026-12-02":
                    basis.append(transition)
                    fields["supplier_timing"] = {
                        "reported_eu_market_placement": "2026-06-01",
                        "provider_compliance_due": transition["compliance_due"],
                        "scope": "supplier-side Article 50(2) only",
                    }
            refs = sorted({row["id"] for row in basis})
            consumed.update(refs)
            impacts.append(record(impact_id, reason, refs, **fields))

    if consumed:
        decisions.append(record(
            "DEC:REVIEWED-FACTS", "Use reviewed case assessments only while the cited facts and provisions match.",
            sorted(consumed),
            concern="Changed records could invalidate a previously supported assessment.",
            options=["Reuse prior outcomes by system ID", "Verify the basis and defer changed cases for review"],
            chosen_behavior="Verify the basis and mark affected assessments unresolved when it changes.",
            rationale="A stable system identity does not establish that the use, evidence or legal text remains the same.",
            tradeoffs=["A material change requires review before a supported assessment can be restored."],
            downstream_effect="Later stages carry the unresolved item without inventing an approval or action date.",
        ))
    unresolved = [r for r in impacts if r["state"] == "unresolved"]
    conflicts = [r for r in impacts if r["state"] == "conflicting"]
    unaffected = [r for r in impacts if r["state"] == "supported-no-impact"]
    status = "complete"
    if unresolved or conflicts or reconciliation["status"] == "partial":
        status = "partial"
    if authority_blocked or reconciliation["status"] in {"blocked", "failed"}:
        status = "blocked"
    if not consumed:
        consumed.add(scope["state"]["scope_record"]["id"])
    return {
        "schema_version": reconciliation["schema_version"], "run_id": reconciliation["run_id"],
        "snapshot_id": f"{reconciliation['run_id']}-05", "sequence": 5, "stage": "impact-analysis",
        "created_at": utc_now(), "status": status,
        "predecessor": {"snapshot_id": reconciliation["snapshot_id"], "path": f"snapshots/{path.name}",
                        "sha256": sha256(path.read_bytes())},
        "consumed_record_ids": sorted(consumed),
        "produced_record_ids": [r["id"] for r in impacts + decisions],
        "state": {"as_of": as_of, "impacts": impacts, "unaffected_items": unaffected,
                  "conflicts": conflicts, "unresolved_items": unresolved},
        "unresolved": unresolved + conflicts, "decisions": decisions,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="path to stage 04")
    args = parser.parse_args()
    path = args.snapshot.resolve()
    output = path.with_name("05-impact-analysis.json")
    try:
        snapshot = analyze(path)
        with output.open("x", encoding="utf-8") as file:
            json.dump(snapshot, file, indent=2, ensure_ascii=False)
            file.write("\n")
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    print(f"{snapshot['status']}: {output}")
    print(f"{len(snapshot['state']['impacts'])} assessments, {len(snapshot['unresolved'])} open findings")
    return 1 if snapshot["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
