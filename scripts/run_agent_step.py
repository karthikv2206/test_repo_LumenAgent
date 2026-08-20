"""Per-hop dispatch for the SDLC orchestration workflow (sdlc-from-ticket,
fix-cycle, arch-correction). Each hop is a single `shell` step in one of
those workflow.yml files, invoking this script once for one agent, against
one real ticket.

Reuses generate_instructions.py's proven cross-platform copilot-dispatch
primitives (find_copilot_binary, call_copilot, atomic_write, the ~~~
newline substitute, the MAX_PROMPT_CHARS guard) rather than duplicating
them. What this script adds is specific to the orchestration case:

- Ticket-scoped handoff, not run-scoped: sdlc-from-ticket and every later
  fix-cycle invocation are different Spec Kit runs (different run_id)
  operating on the same delivery, so handoff data is keyed by ticket_id and
  must outlive any single run.
- Full-reply handoff, not an extracted section: each hop is a separate,
  stateless subprocess call to copilot -- there is no session memory
  carrying context between hops the way there would be in an interactive
  chat. Whatever isn't explicitly passed in the prompt is permanently lost
  to the next agent, so the full upstream reply is passed, not just its
  "Minimal next-agent context" section (that section is still surfaced
  first, for prominence, but never as a replacement for the rest).
- Multi-hop context via contextDepth: most agents only need their
  immediate parent's output, but a manifest-declared contextDepth > 1
  (REV-01 today) walks further back up the dependsOn chain.
- Autonomous-agent handling: an agent with autonomous=true (DEV-02 today)
  gets a different prompt (no "don't edit files" restriction), copilot
  invoked against the real target repo instead of a neutral cwd, and a
  longer timeout, all read from the manifest rather than hardcoded to a
  specific agent id. "Real target repo" defaults to this script's own
  process CWD, not --workflow-dir -- Spec Kit's shell step always runs
  this script with CWD = the run's project_root (the real application
  repo root), while --workflow-dir points at wherever the workflow
  package itself is installed, which can be a `.specify/workflows/<id>/`
  subdirectory when run by file path. Committing there instead of the
  repo root would be a real, silent-looking bug -- see main()'s comment.
- Neutral cwd defaults to a fresh, empty temp directory, never the target
  repo: --workflow-dir here is normally *inside* the developer's own
  application repo (that's where `specify workflow add` installs this
  package), and that repo's .github/copilot-instructions.md carries the
  "trigger the SDLC workflow on ticket mention" instructions. Every
  non-autonomous prompt this script builds deliberately includes the
  ticket id and description, so invoking copilot with a cwd anywhere
  copilot would auto-load that file risks it recursively re-triggering
  the workflow mid-hop. A throwaway temp dir has no copilot-instructions.md
  for it to find.

Live-tested end to end (not just dry-run mocks): the full sdlc-from-ticket
chain (all 10 agents, all 11 gates, including DEV-02's real commits),
fix-cycle (DEV-02 correctly receiving REV-01's prior verdict via
--extra-context-agent-id and fixing exactly the flagged gap), and
arch-correction all completed successfully against a real repo.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from generate_instructions import (  # noqa: E402
    NEWLINE_SUBSTITUTE,
    GenerationError,
    atomic_write,
    call_copilot,
    find_copilot_binary,
    load_agent_registry,
)

AUTONOMOUS_TIMEOUT_SECONDS = 900  # real edits/tests/commits take longer than a text reply
MIN_PLAUSIBLE_OUTPUT_CHARS = 300  # gates review the raw reply directly (via show_file); a shorter bar than generate_instructions.py's 500 is enough to catch an outright refusal here
# A live test caught a real gap: an autonomous agent's real deliverable is
# its actual tool actions (file edits, a commit), not necessarily a long
# chat reply -- a run that did real, correct work still produced a short
# text summary and was wrongly hard-failed by the 300-char bar above,
# which never even reached dev02_review_gate for a human to judge. Only
# catch a genuinely empty/near-empty reply here; whether the *report* is
# adequate for review is the human review gate's call, not this script's.
MIN_AUTONOMOUS_OUTPUT_CHARS = 10
MAX_PROMPT_CHARS = 25000  # same Windows CreateProcess headroom reasoning as generate_instructions.py

MINIMAL_CONTEXT_HEADING = "## Minimal next-agent context"


def handoff_dir(workflow_dir: Path, ticket_id: str) -> Path:
    return workflow_dir / ".specify" / "workflows" / "handoffs" / ticket_id


def read_ticket_bootstrap(hdir: Path) -> dict[str, str]:
    """Read the ticket fields a `bootstrap` step (or Copilot itself, per the
    copilot-instructions.md trigger) already wrote to ticket-scoped files.
    Never accepts ticket content as a CLI argument -- real ticket
    descriptions routinely exceed Windows' ~8191-char command-line limit,
    and re-embedding untrusted ticket text into any templated string is
    exactly the parse-time injection class of bug the reference workflow
    hit; reading it from a file this script itself opens sidesteps both."""
    fields = {}
    for name in ("ticket_id", "ticket_summary", "ticket_description", "ticket_type", "ticket_labels"):
        path = hdir / f"_{name}.txt"
        fields[name] = path.read_text(encoding="utf-8", errors="replace").strip() if path.is_file() else ""
    return fields


def resolve_upstream_context(
    agent_id: str,
    *,
    depends_on: dict[str, str | None],
    labels: dict[str, tuple[str, str]],
    context_depth: int,
    hdir: Path,
) -> str:
    """Walk up to `context_depth` hops back through dependsOn, collecting
    each ancestor's full reply (not an extracted section -- see module
    docstring). Missing an ancestor's file entirely is a hard stop, not a
    silent skip: it means that ancestor hasn't actually run yet for this
    ticket, and proceeding would generate against a broken/missing
    dependency, exactly the failure class generate_instructions.py already
    guards against for the simpler single-invocation case."""
    pieces: list[str] = []
    current = agent_id
    for _ in range(context_depth):
        parent_id = depends_on.get(current)
        if parent_id is None:
            break
        parent_path = hdir / f"{parent_id}.md"
        if not parent_path.is_file():
            raise GenerationError(
                agent_id,
                f"upstream agent {parent_id} has no recorded output yet at {parent_path} "
                "-- it must run (and its gate be approved) before this agent can.",
            )
        full_reply = parent_path.read_text(encoding="utf-8", errors="replace")
        label = labels.get(parent_id, (parent_id, ""))[0]
        heading_idx = full_reply.find(MINIMAL_CONTEXT_HEADING)
        if heading_idx != -1:
            minimal = full_reply[heading_idx:]
            ordered = f"{minimal}\n\n~~~ === FULL {label} REPLY (for anything beyond the summary above) === ~~~\n{full_reply}"
        else:
            ordered = full_reply
        pieces.append(f"~~~ === UPSTREAM: {label} === ~~~ {ordered.replace(chr(10), NEWLINE_SUBSTITUTE)} ")
        current = parent_id
    return "".join(pieces)


def resolve_extra_context(agent_ids: list[str], *, labels: dict[str, tuple[str, str]], hdir: Path) -> str:
    """Pull in named context beyond the normal dependsOn walk -- needed on a
    fix-cycle run, where the autonomous agent (DEV-02 today) must see the
    terminal reviewer's ("fix required") reasoning from the previous cycle,
    even though that reviewer is downstream of it in the primary chain's
    dependsOn graph, not upstream. fix-cycle.yml's codegen passes the
    relevant agent id(s) explicitly via --extra-context-agent-id -- this
    function itself has no agent-id-specific knowledge, same as
    resolve_upstream_context."""
    pieces: list[str] = []
    for agent_id in agent_ids:
        path = hdir / f"{agent_id}.md"
        if not path.is_file():
            raise GenerationError(
                agent_id,
                f"--extra-context-agent-id {agent_id} has no recorded output yet at {path}.",
            )
        full_reply = path.read_text(encoding="utf-8", errors="replace")
        label = labels.get(agent_id, (agent_id, ""))[0]
        flat = full_reply.replace("\r\n", "\n").replace("\n", NEWLINE_SUBSTITUTE)
        pieces.append(f"~~~ === ADDITIONAL CONTEXT (fix-cycle): {label}'s prior verdict === ~~~ {flat} ")
    return "".join(pieces)


def archive_if_exists(path: Path) -> None:
    """Before overwriting an autonomous agent's handoff file, preserve the
    previous version as {agent_id}.v{N}.md -- fix-cycle history (what DEV-02
    tried on each prior attempt) stays inspectable rather than being
    silently overwritten on every retry. No-op on the first run, when there
    is nothing yet to archive."""
    if not path.is_file():
        return
    version_pattern = re.compile(rf"^{re.escape(path.stem)}\.v(\d+)\.md$")
    existing_versions = [
        int(m.group(1))
        for p in path.parent.glob(f"{path.stem}.v*.md")
        for m in (version_pattern.match(p.name),)
        if m
    ]
    next_version = max(existing_versions, default=0) + 1
    archived_path = path.parent / f"{path.stem}.v{next_version}.md"
    archived_path.write_text(path.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")


def build_orchestration_prompt(
    *,
    agent_def: str,
    ticket: dict[str, str],
    upstream_context: str,
    extra_context: str = "",
    autonomous: bool,
) -> str:
    agent_def_flat = agent_def.replace("\r\n", "\n").replace("\n", NEWLINE_SUBSTITUTE)
    ticket_block = (
        f"Ticket ID: {ticket['ticket_id']} ~~~ "
        f"Summary: {ticket['ticket_summary']} ~~~ "
        f"Description: {ticket['ticket_description'].replace(chr(10), NEWLINE_SUBSTITUTE)} ~~~ "
        f"Type: {ticket['ticket_type']} ~~~ "
        f"Labels: {ticket['ticket_labels']}"
    )

    if autonomous:
        # DEV-02's own template has no "don't edit files" line -- it is the
        # one agent in the chain explicitly permitted to take real action.
        # No neutral-cwd/no-tool-use framing here; the caller also invokes
        # copilot with -C pointed at the real target repo, not a neutral one.
        #
        # A live test caught a real gap here: with only "use your tools as
        # your Charter describes," copilot correctly did the real file
        # edits/commit but replied with a one-line acknowledgment instead of
        # the structured report the Charter's own "## Output" section
        # defines -- leaving REV-01 (which only ever sees this text reply,
        # never the raw diff) with almost nothing to review. The Output
        # section is spelled out explicitly below so it isn't just one part
        # of a long charter document competing for attention.
        behavior = (
            "IMPORTANT: this prompt uses ~~~ as a section separator instead of real newlines -- "
            "some CLI argument-passing paths truncate real newlines, this does not. "
            "You are acting as this agent for real, right now, against the real request below "
            "-- this is not a dry run and not a template-compilation exercise. Use your tools "
            "(file edits, shell/git commands) as your Charter and Responsibilities describe. "
            "After you finish, your chat reply itself MUST be the full structured report your "
            "Charter's own \"## Output\" section defines (Request, FR-ID/step implemented, Files "
            "changed, Tests run and result, Evidence, Commit/PR or manual execution pack, Context "
            "used, Minimal next-agent context) -- not a short acknowledgment. The downstream "
            "reviewer agent never sees your raw tool calls or diff, only this reply, so it must be "
            "complete and self-contained."
        )
    else:
        behavior = (
            "IMPORTANT: this prompt uses ~~~ as a section separator instead of real newlines -- "
            "some CLI argument-passing paths truncate real newlines, this does not. "
            "You are acting as this agent for real, right now, against the real request below "
            "-- this is not a dry run. Do not explore the filesystem, list directories, run "
            "tools, or read any files -- everything you need is provided below. Respond "
            "directly in your reply, do not use any file-editing tool."
        )

    prompt = (
        f"{behavior} ~~~ "
        f"=== REQUEST (Jira ticket) === ~~~ {ticket_block} ~~~ "
        f"{upstream_context}"
        f"{extra_context}"
        f"=== YOUR ROLE === ~~~ {agent_def_flat}"
    )
    return prompt.replace('"', "")


def run_dev02_sanity_check(target_repo: Path) -> str:
    """Belt-and-suspenders check for the one agent allowed to claim it made
    real commits: DEV-02's own template already warns against ever
    reporting a push that didn't happen, but a cheap post-hoc git check
    catches a false-positive report before dev02_review_gate shows it to a
    human as ground truth."""
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain"], cwd=target_repo, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        log = subprocess.run(
            ["git", "log", "-1", "--oneline"], cwd=target_repo, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
        return f"git status --porcelain: {status.stdout.strip() or '(clean)'} | git log -1: {log.stdout.strip() or '(no commits found)'}"
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"(sanity check itself failed: {e})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-id", required=True)
    parser.add_argument("--ticket-id", required=True)
    parser.add_argument("--workflow-dir", default=".", help="directory containing agents/ and .specify/ (defaults to CWD)")
    parser.add_argument(
        "--target-repo", default=None,
        help="real application repo root for autonomous agents; defaults to the process's own CWD "
             "(NOT --workflow-dir -- see module docstring on why these two differ)",
    )
    parser.add_argument(
        "--neutral-cwd", default=None,
        help="cwd override for non-autonomous agents; defaults to a fresh temp dir "
             "(never --workflow-dir -- see module docstring on recursive-trigger risk)",
    )
    parser.add_argument(
        "--extra-context-agent-id", action="append", default=[],
        help="agent id(s) whose recorded output to include beyond the normal dependsOn walk "
             "(e.g. the terminal reviewer's prior verdict, on a fix-cycle run). Repeatable.",
    )
    args = parser.parse_args()

    workflow_dir = Path(args.workflow_dir).resolve()
    # Spec Kit's `shell` step always runs this script with CWD = the run's
    # project_root (the directory the developer invoked `specify workflow
    # run` from -- i.e. the real application repo root), regardless of
    # where the workflow YAML/its companion scripts+agents happen to be
    # installed. `--workflow-dir` (== `{{ context.workflow_dir }}`) points
    # at the *installed package* location instead -- for a workflow run by
    # file path (as sdlc-from-ticket.yml/fix-cycle.yml are), that is a
    # `.specify/workflows/<id>/` subdirectory, NOT the repo root. Using it
    # as DEV-02's commit target would make it `git commit` inside the
    # installed package directory instead of the real application repo.
    target_repo = Path(args.target_repo).resolve() if args.target_repo else Path.cwd()
    hdir = handoff_dir(workflow_dir, args.ticket_id)
    hdir.mkdir(parents=True, exist_ok=True)

    auto_neutral_dir: str | None = None
    if args.neutral_cwd is None:
        auto_neutral_dir = tempfile.mkdtemp(prefix="run-agent-step-neutral-")

    try:
        depends_on, labels, orchestration = load_agent_registry(workflow_dir)
        if args.agent_id not in depends_on:
            raise GenerationError(args.agent_id, "unknown agent id -- not in agents/_manifest.json or the built-in fallback list")
        flags = orchestration.get(args.agent_id, {"autonomous": False, "gateType": "standard", "contextDepth": 1})
        autonomous = bool(flags.get("autonomous", False))
        context_depth = int(flags.get("contextDepth", 1))

        agent_def_path = workflow_dir / "agents" / f"{args.agent_id}.md"
        if not agent_def_path.is_file():
            raise GenerationError(args.agent_id, f"agent definition not found at {agent_def_path}")
        agent_def = agent_def_path.read_text(encoding="utf-8", errors="replace")

        ticket = read_ticket_bootstrap(hdir)
        if not ticket.get("ticket_id"):
            raise GenerationError(args.agent_id, f"no ticket bootstrap data found at {hdir} -- the bootstrap step must run first")

        upstream_context = resolve_upstream_context(
            args.agent_id, depends_on=depends_on, labels=labels, context_depth=context_depth, hdir=hdir,
        )
        extra_context = resolve_extra_context(args.extra_context_agent_id, labels=labels, hdir=hdir)

        prompt = build_orchestration_prompt(
            agent_def=agent_def, ticket=ticket, upstream_context=upstream_context,
            extra_context=extra_context, autonomous=autonomous,
        )
        if len(prompt) > MAX_PROMPT_CHARS:
            raise GenerationError(
                args.agent_id,
                f"prompt is {len(prompt)} chars, over the {MAX_PROMPT_CHARS} safety limit -- "
                "likely too much upstream context accumulated; check contextDepth for this agent.",
            )

        copilot_bin = find_copilot_binary()
        cwd_for_call = str(target_repo) if autonomous else (args.neutral_cwd or auto_neutral_dir)
        timeout = AUTONOMOUS_TIMEOUT_SECONDS if autonomous else 180

        resolved = call_copilot(copilot_bin, prompt, cwd_for_call, timeout=timeout)
        min_chars = MIN_AUTONOMOUS_OUTPUT_CHARS if autonomous else MIN_PLAUSIBLE_OUTPUT_CHARS
        if len(resolved.strip()) < min_chars:
            raise GenerationError(args.agent_id, f"output too short ({len(resolved.strip())} chars) -- likely a refusal, not a real reply")

        dest = hdir / f"{args.agent_id}.md"
        if autonomous:
            archive_if_exists(dest)  # preserve the prior attempt before a fix-cycle overwrites it
        atomic_write(dest, resolved)

        if autonomous:
            sanity = run_dev02_sanity_check(target_repo)
            atomic_write(hdir / f"{args.agent_id}.sanity-check.txt", sanity)
            print(f"{args.agent_id}: OK ({len(resolved)} chars). Sanity check: {sanity}")
        else:
            print(f"{args.agent_id}: OK ({len(resolved)} chars)")
        return 0
    except GenerationError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 1
    finally:
        if auto_neutral_dir is not None:
            shutil.rmtree(auto_neutral_dir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
