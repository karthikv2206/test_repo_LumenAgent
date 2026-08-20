"""First step of sdlc-from-ticket.yml / fix-cycle.yml: verify the ticket
fields Copilot already wrote to the handoff directory are present, before
any agent hop runs against them.

Deliberately does NOT accept ticket content (summary/description/type/
labels) as CLI arguments -- only --ticket-id, which is short and safe to
embed in a workflow.yml `run:` line. Real ticket text is written directly
to files by whatever detected the ticket mention (Copilot, per the
copilot-instructions.md trigger), never passed through shell templating --
this is what keeps the workflow immune to the apostrophes/quotes-in-ticket-
text class of bug the reference workflow hit.

Fails fast and loud (nonzero exit, clear message) when a required file is
missing or empty, so a misconfigured trigger is caught at the very first
step, not after several agent hops have already run against a
half-populated handoff directory.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REQUIRED_FIELDS = ("ticket_id", "ticket_summary", "ticket_description", "ticket_type", "ticket_labels")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticket-id", required=True)
    parser.add_argument("--workflow-dir", default=".")
    args = parser.parse_args()

    hdir = Path(args.workflow_dir).resolve() / ".specify" / "workflows" / "handoffs" / args.ticket_id
    hdir.mkdir(parents=True, exist_ok=True)

    missing = []
    for name in REQUIRED_FIELDS:
        path = hdir / f"_{name}.txt"
        if not path.is_file() or not path.read_text(encoding="utf-8", errors="replace").strip():
            missing.append(str(path))

    if missing:
        print(
            "FAILED: ticket bootstrap data is missing or empty for the following file(s):\n  "
            + "\n  ".join(missing)
            + "\n\nThe trigger that invoked this workflow must write each ticket field to its own "
            f"file under {hdir} (_ticket_id.txt, _ticket_summary.txt, _ticket_description.txt, "
            "_ticket_type.txt, _ticket_labels.txt) before calling `specify workflow run`.",
            file=sys.stderr,
        )
        return 1

    print(f"bootstrap: OK ({hdir})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
