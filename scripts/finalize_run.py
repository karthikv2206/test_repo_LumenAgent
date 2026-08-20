"""Last step of sdlc-from-ticket.yml: record the chain's terminal verdict
(the terminal_review-gated agent's gate choice -- REV-01 today) to a
ticket-scoped status file.

The workflow run itself always completes (Spec Kit has no notion of a
"successful rejection" -- see the terminal gate's on_reject: skip), so the
real approve/fix_required decision has to be recorded somewhere a human or
Copilot can read it after the run ends. This step is that recording, kept
to a single deterministic file write -- no interpretation of the verdict
happens here (e.g. deciding whether to invoke fix-cycle.yml is the
trigger's job, per the plan's Copilot-does-only-two-things design).

--verdict is always one of a small, fixed set of literal strings the
codegen itself writes into workflow.yml (the terminal gate's own declared
options), never free text -- safe to pass as a CLI argument.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_instructions import atomic_write  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticket-id", required=True)
    parser.add_argument("--workflow-dir", default=".")
    parser.add_argument("--terminal-agent-id", required=True, help="agent id whose gate choice is the chain's final verdict")
    parser.add_argument("--verdict", required=True)
    args = parser.parse_args()

    hdir = Path(args.workflow_dir).resolve() / ".specify" / "workflows" / "handoffs" / args.ticket_id
    hdir.mkdir(parents=True, exist_ok=True)

    atomic_write(
        hdir / "_final_verdict.txt",
        f"terminal_agent={args.terminal_agent_id}\nverdict={args.verdict}\n",
    )
    print(f"finalize: recorded verdict={args.verdict!r} from {args.terminal_agent_id} for ticket {args.ticket_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
