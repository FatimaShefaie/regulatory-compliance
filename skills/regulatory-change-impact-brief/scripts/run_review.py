"""Run the seven-stage Quillhaven Article 50 review and write a local draft package."""
import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
DEFAULT_SOURCES = SCRIPTS.parent / "sources.json"
DEFAULT_SYSTEMS = tuple(f"AI-{number:03d}" for number in range(1, 9))
STAGES = (
    ("check_authority.py", "02-source-capture.json", "03-authority-and-timing.json", ("--write-snapshot",)),
    ("reconcile_evidence.py", "03-authority-and-timing.json", "04-evidence-reconciliation.json", ()),
    ("analyze_impacts.py", "04-evidence-reconciliation.json", "05-impact-analysis.json", ()),
    ("draft_actions.py", "05-impact-analysis.json", "06-actions-and-approvals.json", ()),
    ("publish_drafts.py", "06-actions-and-approvals.json", "07-publication-validation.json", ()),
)


def run_stage(script, arguments, snapshot):
    completed = subprocess.run([sys.executable, "-B", str(SCRIPTS / script), *arguments])
    if completed.returncode not in (0, 1) or not snapshot.is_file():
        raise RuntimeError(f"{script} did not produce {snapshot.name} (exit {completed.returncode})")
    return json.loads(snapshot.read_bytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new run directory")
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES, help="map of the ten disclosed source routes")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 8, 26))
    parser.add_argument("--system-id", action="append", help="include a system ID, repeat to include multiple")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error("Output already exists. Select a new run directory.")
    snapshots = output / "snapshots"
    capture_args = ["--sources", str(args.sources.resolve()), "--output", str(output),
                    "--as-of", args.as_of.isoformat()]
    for system_id in args.system_id or DEFAULT_SYSTEMS:
        capture_args.extend(("--system-id", system_id))

    try:
        run_stage("capture_sources.py", capture_args, snapshots / "02-source-capture.json")
        for script, input_name, output_name, options in STAGES:
            result = run_stage(script, [str(snapshots / input_name), *options], snapshots / output_name)
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Review could not finish: {error}", file=sys.stderr)
        return 2

    status = result["state"]["publication_status"]
    print(f"Run {result['run_id']} | {result['status']} | publication {status}")
    print(f"Draft package: {output / 'deliverables'}")
    return 0 if status == "validated" else 1


if __name__ == "__main__":
    raise SystemExit(main())
