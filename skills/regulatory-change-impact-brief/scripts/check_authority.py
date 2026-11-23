"""Verify captured authority and write stage 03 for the same run."""
import argparse
import hashlib
import hmac
import json
import re
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

ARTICLE_50 = {
    1: ("Providers of systems intended to interact directly with people must ensure AI notice unless the AI nature is obvious.",
        r"Providers shall ensure that AI systems intended to interact directly"),
    2: ("Providers of qualifying synthetic-content systems must make outputs machine-readable and detectable.",
        r"Providers of AI systems.*?marked in a machine-readable format"),
    3: ("Deployers of emotion recognition or biometric categorisation systems must inform exposed people.",
        r"Deployers of an emotion recognition system or a biometric categorisation system"),
    4: ("Deployers must disclose qualifying deepfakes and public-interest text, subject to the applicable exceptions.",
        r"Deployers of an AI system.*?constituting a deep fake.*?matters of public interest"),
    5: ("Required information must be clear, accessible and provided by first interaction or exposure.",
        r"The information referred to in paragraphs 1 to 4.*?first interaction or exposure"),
}
GENERAL_DATE = date(2026, 8, 2)
TRANSITION_DATE = date(2026, 12, 2)
AMENDMENT_PUBLICATION = date(2026, 7, 24)
AMENDMENT_IN_FORCE = AMENDMENT_PUBLICATION + timedelta(days=3)
ARTICLE_TITLE = "Article 50 Transparency obligations for providers and deployers of certain AI systems"
GENERAL_TEXT = "It shall apply from 2 August 2026"
TRANSITION_TEXT = (
    "placed on the market before 2 August 2026 shall take the necessary steps "
    "in order to comply with Article 50(2) by 2 December 2026"
)


def select_binding_attempts(snapshot, source_ids=("OJ", "AMEND")):
    if snapshot.get("stage") != "source-capture" or snapshot.get("sequence") != 2:
        raise ValueError("Expected a stage 02 source-capture snapshot")

    selections = {}
    for source_id in source_ids:
        attempts = [
            record for record in snapshot["state"]["sources"]
            if record["source_id"] == source_id
        ]
        retrieved = [
            record for record in attempts if record["retrieval_status"] == "retrieved"
        ]
        selections[source_id] = {
            "selected": retrieved[0] if len(retrieved) == 1 else None,
            "attempts": attempts,
            "status": {0: "missing", 1: "selected"}.get(len(retrieved), "ambiguous"),
        }
    return selections


def verified_content(attempt, run_dir):
    reference = attempt.get("local_reference")
    expected = attempt.get("content_hash")
    if not isinstance(reference, str) or not reference or Path(reference).is_absolute():
        raise ValueError("Missing or invalid local evidence path")
    if not isinstance(expected, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected):
        raise ValueError("Missing or invalid recorded SHA-256")
    run_dir = run_dir.resolve()
    path = (run_dir / reference).resolve()
    if not path.is_relative_to(run_dir):
        raise ValueError("Evidence path escapes the run directory")
    data = path.read_bytes()
    actual = sha256(data)
    if not hmac.compare_digest(actual, expected):
        raise ValueError("SHA-256 mismatch")
    return data


def sha256(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def verify_content_hash(attempt, run_dir):
    return sha256(verified_content(attempt, run_dir))


class DocumentText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def normal_text(data):
    parser = DocumentText()
    parser.feed(data.decode("utf-8-sig"))
    parser.close()
    return " ".join(" ".join(parser.parts).split())


def require(condition, provision):
    if not condition:
        raise ValueError(f"Missing expected identity or provision: {provision}")


def check_provisions(source_id, text):
    if source_id == "AMEND":
        require("2026/1744 24.7.2026 REGULATION (EU) 2026/1744" in text,
                "Regulation (EU) 2026/1744 and its publication date")
        _, marker, tail = text.partition("(39) Article 111 is amended as follows:")
        require(marker and TRANSITION_TEXT in tail.split("(40)", 1)[0], "Article 1(39), adding Article 111(4)")
        require("Article 4 Entry into force and application This Regulation shall enter into force "
                "on the third day following" in text, "amendment Article 4")
        return {}

    if source_id == "OJ":
        require("2024/1689 12.7.2024 REGULATION (EU) 2024/1689" in text,
                "Regulation (EU) 2024/1689")
    else:
        require("02024R1689 — EN — 27.07.2026" in text, "27 July 2026 consolidation")
        require(TRANSITION_TEXT in text, "consolidated Article 111(4)")
    _, heading, article = text.partition(ARTICLE_TITLE)
    require(heading, "Article 50")
    paragraphs = dict(re.findall(r"(?<!\w)([1-7])\. (.*?)(?=\s+[1-7]\. |$)",
                                 article.split("CHAPTER V", 1)[0]))
    for number, (_, pattern) in ARTICLE_50.items():
        require(re.search(pattern, paragraphs.get(str(number), "")), f"Article 50({number})")
    _, heading, article113 = text.partition("Article 113 Entry into force and application")
    require(heading and GENERAL_TEXT in article113[:500], "Article 113 application date")
    return {number: paragraphs[str(number)] for number in ARTICLE_50}


def record(record_id, summary, evidence_ids, **fields):
    return {"id": record_id, "summary": summary, "evidence_ids": evidence_ids, **fields}


def write_authority_snapshot(path):
    raw = path.read_bytes()
    upstream = json.loads(raw)
    run_dir = path.parent.parent
    first = (run_dir / upstream["predecessor"]["path"]).resolve()
    if not first.is_relative_to(run_dir):
        raise ValueError("Scope predecessor path escapes the run directory")
    scope_bytes = first.read_bytes()
    if sha256(scope_bytes) != upstream["predecessor"]["sha256"]:
        raise ValueError("Scope predecessor SHA-256 mismatch")
    scope = json.loads(scope_bytes)
    if scope["run_id"] != upstream["run_id"] or scope["snapshot_id"] != upstream["predecessor"]["snapshot_id"]:
        raise ValueError("Scope predecessor identity mismatch")
    as_of = datetime.fromisoformat(scope["state"]["as_of"]).date()
    checks = select_binding_attempts(upstream, ("OJ", "AMEND", "CONSOLIDATED", "LAW", "TIME", "FAQ"))
    verified, provisions, blockers, unresolved = {}, {}, [], []
    consumed = []
    for source_id, result in checks.items():
        attempt = result["selected"]
        ids = [entry["id"] for entry in result["attempts"]]
        consumed.extend(ids)
        binding = source_id in ("OJ", "AMEND")
        try:
            if attempt is None:
                raise ValueError(f"Source selection {result['status']}")
            text = normal_text(verified_content(attempt, run_dir))
            if binding or source_id == "CONSOLIDATED":
                provisions[source_id] = check_provisions(source_id, text)
            if source_id == "AMEND" and as_of < AMENDMENT_IN_FORCE:
                raise ValueError("Amendment is outside the supported review period")
        except (OSError, ValueError, RuntimeError) as error:
            issue = record(f"AUTH-{source_id}-ISSUE", f"{source_id}: {error}", ids,
                           resolution_need="Retrieve and verify the expected source and provisions")
            (blockers if binding else unresolved).append(issue)
        else:
            verified[source_id] = attempt["id"]

    rules, timing, guidance = [], [], []
    if "OJ" in verified:
        for number, (summary, _) in ARTICLE_50.items():
            rules.append(record(f"RULE-A50-{number}", summary, [verified["OJ"]],
                                locator=f"Regulation (EU) 2024/1689, Article 50({number})",
                                provision_text=provisions["OJ"][number]))
        timing.append(record("TIMING-GENERAL", "General Article 50 application date",
                             [verified["OJ"]], locator="Article 113",
                             applies_from=GENERAL_DATE.isoformat(), applicable_as_of=as_of >= GENERAL_DATE))
    if "OJ" in verified and "AMEND" in verified:
        timing.append(record(
            "TIMING-A50-2-TRANSITION", "Limited transition for qualifying Article 50(2) providers",
            [verified["OJ"], verified["AMEND"]], locator="Article 111(4), added by amendment Article 1(39)",
            source_publication_date=AMENDMENT_PUBLICATION.isoformat(),
            amendment_in_force_from=AMENDMENT_IN_FORCE.isoformat(),
            market_placement_before=GENERAL_DATE.isoformat(), compliance_due=TRANSITION_DATE.isoformat(),
            required_facts=["provider role", "qualifying synthetic output", "market-placement date"],
        ))
    for source_id in ("LAW", "TIME", "FAQ"):
        if source_id in verified:
            guidance.append(record(f"CONTEXT-{source_id}", f"{source_id} interpretive context",
                                   [verified[source_id]], authority="advisory"))
    if "CONSOLIDATED" in verified and "OJ" in verified and "AMEND" in verified:
        def canonical(paragraph):
            return re.sub(r"\s+|[▼▶][A-Z]\d*", "", paragraph)
        agreement = all(canonical(provisions["OJ"][n]) == canonical(provisions["CONSOLIDATED"][n])
                        for n in ARTICLE_50)
        crosscheck = record("CROSSCHECK-CONSOLIDATED", "Consolidated Article 50 and timing cross-check",
                            [verified[name] for name in ("OJ", "AMEND", "CONSOLIDATED")],
                            locator="Articles 50(1)–(5), 111(4), and 113",
                            result="matched" if agreement else "requires review")
        (guidance if agreement else unresolved).append(crosscheck)
    status = "blocked" if blockers else "partial" if unresolved else "complete"
    state = {"as_of": as_of.isoformat(), "binding_rules": rules, "timing_rules": timing,
             "guidance_context": guidance, "authority_blockers": blockers}
    snapshot = {
        "schema_version": upstream["schema_version"], "run_id": upstream["run_id"],
        "snapshot_id": f"{upstream['run_id']}-03", "sequence": 3, "stage": "authority-and-timing",
        "created_at": datetime.now(timezone.utc).isoformat(), "status": status,
        "predecessor": {"snapshot_id": upstream["snapshot_id"], "path": str(path.relative_to(run_dir)),
                        "sha256": sha256(raw)},
        "consumed_record_ids": consumed,
        "produced_record_ids": [item["id"] for item in rules + timing + guidance + blockers + unresolved],
        "state": state, "unresolved": blockers + unresolved, "decisions": [],
    }
    output = path.with_name("03-authority-and-timing.json")
    with output.open("x", encoding="utf-8") as file:
        json.dump(snapshot, file, indent=2, ensure_ascii=False)
        file.write("\n")
    return output, snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="path to snapshot 02")
    parser.add_argument("--write-snapshot", action="store_true", help="write stage 03 alongside snapshot 02")
    args = parser.parse_args()
    try:
        if args.write_snapshot:
            output, snapshot = write_authority_snapshot(args.snapshot.resolve())
            print(f"{snapshot['status']}: {output}")
            for issue in snapshot["unresolved"]:
                print(issue["summary"])
            return 0 if snapshot["status"] == "complete" else 1
        selections = select_binding_attempts(json.loads(args.snapshot.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))

    verified = True
    run_dir = args.snapshot.resolve().parent.parent
    for source_id, result in selections.items():
        selected = result["selected"]
        print(f"{source_id}: {selected['id'] if selected else result['status']}")
        for attempt in result["attempts"]:
            print(f"  {attempt['id']}: {attempt['retrieval_status']}")
        if selected is None:
            verified = False
            continue
        try:
            content_hash = verify_content_hash(selected, run_dir)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"  Hash verification failed: {error}")
            verified = False
        else:
            print(f"  Verified {content_hash}")
    return 0 if verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
