# Regulatory Change Impact

The [regulatory-change-impact-brief Agent Skill](skills/regulatory-change-impact-brief/SKILL.md) connects regulatory changes to Quillhaven Academy’s operations and drafts actions for Legal and Operations review.

## Run the review

Install Python 3.10 or newer and `curl` with HTTPS support. No Python packages are required. From the repository root, choose a new output directory and run:

```sh
python3 -B skills/regulatory-change-impact-brief/scripts/run_review.py --output runs/article-50-review
```

The command defaults to 26 August 2026 and all eight registered systems. It reads the ten live sources, writes the seven linked snapshots under `snapshots/`, and writes the register, brief, calendar, and captured evidence under `deliverables/` in the run directory. It exits with status 0 for a validated draft, 1 when publication is blocked or fails validation, and 2 when execution cannot finish. Review requests remain pending.

The Skill directory contains the implementation and default source configuration and can be copied as a unit. The individual stage commands below remain available from the repository root.

## Start

1. Read the [formal assignment](https://private-pecorino-70e.notion.site/Project-B-Regulatory-Change-Impact-Compliance-Actions-Learner-assignment-3da0b700541e8152b6d1c638fd1c34fa?source=copy_link) for the work and acceptance requirements.
2. Create your own repository from [this starter](https://github.com/GitRollTraining/regulatory-compliance) using **Fork**, then clone your copy and work there.

## Supplied files

| File | Purpose |
|---|---|
| `README.md` | Starting instructions and links. |
| `snapshot.schema.json` | Public snapshot contract; keep it unchanged. |

## Before you work

**Interview rule.** You conduct the stakeholder interview yourself, and the questions are yours. Do not connect a coding agent or any other AI to the interview to run, script, or automate it. The interview transcript is assessed together with the code; a project whose interview was run by an agent is not scored.

- Export your interview as the original Work Sim Markdown, save one final complete file per session under `interviews/`, and commit and push it with your code. Do not rewrite the export. If the export is unavailable, contact the facilitator.

- The opening remarks in the interview export refer to “Guildhawk Academy.” This was a misspelling of “Quillhaven Academy,” the name used in the formal assignment and throughout this project.

- Use an Agent Skills-capable coding environment. Choose and document your implementation runtime and dependencies; no runtime or install command is supplied here.
- Follow the [shared course guide for session capture](https://classroom.google.com/c/ODcyMjA4NTkwNDk2/m/ODc0NzI2NzQzMzQ2/details) and verify capture is active before implementation. Keep credentials out of the repository.
- Meet the [stakeholder](https://work-sim.catalyte.ai/s/project-b-regulatory-compliance) to understand the work and relevant business sources. Read those online sources through their intended access route; an unavailable source is not permission to substitute repository data.

## Live source capture

Requires Python 3.10 or newer and `curl` with HTTPS support. Run from the repository root with a new output directory and one `--system-id` per system in scope.

```sh
python3 scripts/capture_sources.py --output runs/first-capture \
  --system-id AI-001 --system-id AI-002 --system-id AI-003 --system-id AI-004 \
  --system-id AI-005 --system-id AI-006 --system-id AI-007 --system-id AI-008
```

The command defaults to the assigned review date, 26 August 2026. Use `--sources` to supply a JSON map of the ten disclosed source routes in the format of [sources.json](sources.json). Each run writes scope and source-capture snapshots to `snapshots/` and retrieved response bodies to `deliverables/sources/` inside the selected output directory. Source and predecessor paths are relative to that directory. A partial or blocked capture still writes both snapshots and exits with status 1.

The Sheets CSV routes use the default tab of each disclosed workbook. EUR-Lex reads fall back to the corresponding official Cellar documents over HTTPS. The published policy falls back to Notion’s undocumented read-only page-content endpoint, which may change. Each snapshot records the actual attempts, response types, times, and evidence hashes. Document identity checks at capture time establish inputs for the later authority and timing review.

## Authority and timing

Generate stage 03 from the same run directory after source capture.

```sh
python3 scripts/check_authority.py runs/first-capture/snapshots/02-source-capture.json --write-snapshot
```

The command writes `03-authority-and-timing.json` beside snapshot 02. It checks the captured documents and provisions for the assigned review and records authority blockers when a binding document fails those checks. Use a new capture directory for a new run.

## Evidence reconciliation

```sh
python3 -B scripts/reconcile_evidence.py runs/first-capture/snapshots/03-authority-and-timing.json
```

Stage 04 verifies the predecessor chain and company-source hashes. It writes `04-evidence-reconciliation.json` beside snapshot 03 with the joined register records, policy controls, operating context, conflicts, and gaps. Source row and Notion block locators are preserved. Calendar records with scope `ALL` are kept at register level. A partial snapshot can be used for the next stage with its unresolved records intact. A binding-authority blocker is carried forward and exits with status 1.

## Impact analysis

```sh
python3 -B scripts/analyze_impacts.py runs/first-capture/snapshots/04-evidence-reconciliation.json
```

Stage 05 writes `05-impact-analysis.json`. The draft assesses each in-scope system against Article 50(1)–(5) and the internal AI-use policy. The case assessment file records interpretations for the assigned review. The command checks the current substantive records and provision text against that reviewed basis. Changed or missing facts leave dependent assessments unresolved until reviewed. Supplier timing is recorded as context for the supplier-side duty. It does not assign a company action or deadline.

## Actions and approvals

```sh
python3 -B scripts/draft_actions.py runs/first-capture/snapshots/05-impact-analysis.json
```

Stage 06 writes `06-actions-and-approvals.json` with draft actions, approval gates, and review requests. Calendar owners and dates come from matched source rows. Unscheduled proposals remain visible with blank owner and date fields. Review requests stay pending, and a binding-authority blocker defers calendar events while retaining the source values and review questions.

## Publication validation

```sh
python3 -B scripts/publish_drafts.py runs/first-capture/snapshots/06-actions-and-approvals.json
```

Stage 07 writes the impact register, brief, and tentative action calendar under `deliverables/`. The publication snapshot records output hashes, cross-file checks, and detached review-request bindings to the final brief. When binding authority is blocked, the command still writes the local register and brief, generates an empty calendar, and exits with status 1.
