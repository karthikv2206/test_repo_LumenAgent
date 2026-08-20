"""Cross-platform agent-instruction generator with dependency-aware handoff.

Replaces the earlier Windows-only PowerShell `generate` step in workflow.yml.
Runs identically on Windows, Linux, and macOS -- no shell string-quoting
hacks, no OS-specific copilot.cmd/.exe path, no PowerShell dependency.

Usage (invoked by workflow.yml, or directly by a developer):
    python generate_instructions.py --agent-ids gov-01,feat-01,arch-01

Must be run with CWD = the target application repo (repo B), the same repo
that already has real Lumen-generated context under .github/instructions/.
Resolves each requested agent in real dependency order, read from the
published agents/_manifest.json (see load_agent_registry -- this covers both
the 10 built-in agents and any custom agent added via the "Add Agent"
feature; falls back to a hardcoded 10-agent default only when no manifest
ships, i.e. running straight against the source speckit-agents/ folder). An
agent's prompt includes its upstream dependency's just-generated output --
this is the actual handoff mechanism. If any agent's generation fails or
comes back suspiciously short (a refusal, not a real instruction file), the
chain halts immediately and reports exactly which agent broke, instead of
silently generating downstream agents against a broken/missing dependency.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Fallback used only when no agents/_manifest.json ships with the package
# (e.g. running this script directly against the source speckit-agents/agents/
# folder during development, bypassing publish.py). Any real published package
# carries its own manifest (see publish.py's build_manifest) covering both the
# 10 built-in agents AND any custom agent added via the "Add Agent" feature --
# the manifest is what actually gets used whenever it's present.
FALLBACK_DEPENDS_ON: dict[str, str | None] = {
    "gov-01": None,
    "feat-01": "gov-01",
    "arch-01": "feat-01",
    "feat-02": "arch-01",
    "req-01": "feat-02",
    "req-02": "req-01",
    "dev-01": "req-02",
    "tdd-01": "dev-01",
    "dev-02": "tdd-01",
    "rev-01": "dev-02",
}
FALLBACK_AGENT_LABELS: dict[str, tuple[str, str]] = {
    "gov-01": ("GOV-01", "Governance"),
    "feat-01": ("FEAT-01", "Feature Intake"),
    "arch-01": ("ARCH-01", "Architecture Decision"),
    "feat-02": ("FEAT-02", "Decomposition"),
    "req-01": ("REQ-01", "Requirements & BDD"),
    "req-02": ("REQ-02", "Readiness Gate"),
    "dev-01": ("DEV-01", "Implementation Plan"),
    "tdd-01": ("TDD-01", "Test-First Design"),
    "dev-02": ("DEV-02", "Scoped Build"),
    "rev-01": ("REV-01", "Review & Fix Loop"),
}
MANIFEST_FILENAME = "_manifest.json"
REGISTRY_FILENAME = "registry.instructions.md"
# Fallback orchestration flags, paired with FALLBACK_DEPENDS_ON/LABELS above --
# real published manifests always carry these explicitly (see publish.py's
# build_manifest); this only matters for dev-time runs with no manifest at all.
FALLBACK_ORCHESTRATION: dict[str, dict] = {
    agent_id: {
        "autonomous": agent_id == "dev-02",
        "gateType": "terminal_review" if agent_id == "rev-01" else "standard",
        "contextDepth": 3 if agent_id == "rev-01" else 1,
    }
    for agent_id in FALLBACK_DEPENDS_ON
}


def load_agent_registry(
    workflow_dir: Path,
) -> tuple[dict[str, str | None], dict[str, tuple[str, str]], dict[str, dict]]:
    """Load DEPENDS_ON + AGENT_LABELS + per-agent orchestration flags
    (autonomous, gateType -- used by the SDLC orchestration workflow's step
    generation, not by this script's own template-filling job) from the
    published manifest if present, else fall back to the hardcoded 10-agent
    defaults (dev-time convenience only). The manifest is what makes custom
    agents actually generatable and correctly placed in the orchestration."""
    manifest_path = workflow_dir / "agents" / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return dict(FALLBACK_DEPENDS_ON), dict(FALLBACK_AGENT_LABELS), dict(FALLBACK_ORCHESTRATION)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise GenerationError("*", f"agents/{MANIFEST_FILENAME} is corrupt: {e}")
    depends_on: dict[str, str | None] = {}
    labels: dict[str, tuple[str, str]] = {}
    orchestration: dict[str, dict] = {}
    for agent_id, meta in raw.items():
        depends_on[agent_id] = meta.get("dependsOn")
        labels[agent_id] = (meta.get("name", agent_id), meta.get("role", ""))
        orchestration[agent_id] = {
            "autonomous": bool(meta.get("autonomous", False)),
            "gateType": meta.get("gateType", "standard"),
            "contextDepth": int(meta.get("contextDepth", 1)),
        }
    return depends_on, labels, orchestration

MIN_PLAUSIBLE_OUTPUT_CHARS = 500  # below this, treat output as a likely refusal
MAX_ATTEMPTS_PER_AGENT = 3
NEWLINE_SUBSTITUTE = " ~~~ "  # copilot's -p argument truncates at the first real newline


class GenerationError(Exception):
    def __init__(self, agent_id: str, reason: str):
        super().__init__(f"{agent_id}: {reason}")
        self.agent_id = agent_id
        self.reason = reason


def order_agents(requested: list[str], depends_on: dict[str, str | None]) -> list[str]:
    """Return requested agents in real dependency order (upstream first)."""
    if not requested:
        raise GenerationError("*", "no agent ids given (--agent-ids was empty)")
    unknown = [a for a in requested if a not in depends_on]
    if unknown:
        raise GenerationError(",".join(unknown), "unknown agent id(s) -- not in agents/_manifest.json or the built-in fallback list")
    chain_order = list(depends_on)
    return [a for a in chain_order if a in requested]


def find_copilot_binary() -> str:
    env_override = os.environ.get("COPILOT_CLI_PATH")
    if env_override:
        return env_override
    # Prefer a native binary (.exe on Windows, bare name on Linux/macOS) over a
    # .cmd wrapper: .cmd files route through cmd.exe, which caps the whole
    # command line at 8191 chars -- our prompts (real context + upstream
    # agent output + agent definition) routinely exceed that. Native exe
    # invocation goes through CreateProcess directly (~32K char limit) and
    # has no such wrapper on Linux/macOS to begin with.
    for candidate in ("copilot.exe", "copilot"):
        found = shutil.which(candidate)
        if found:
            return found
    found = shutil.which("copilot.cmd")
    if found:
        print(
            "WARNING: only found copilot.cmd (routes through cmd.exe, 8191-char "
            "command-line limit) -- large prompts may fail with 'command line is "
            "too long'. Install/use the native copilot.exe if this happens.",
            file=sys.stderr,
        )
        return found
    raise GenerationError("*", "copilot CLI not found on PATH; set COPILOT_CLI_PATH to its absolute path")


def load_real_context(instructions_dir: Path, known_agent_ids: set[str]) -> str:
    """Concatenate the target repo's real, non-agent-output instruction files."""
    if not instructions_dir.is_dir():
        return ""
    pieces: list[str] = []
    for f in sorted(instructions_dir.glob("*.instructions.md")):
        stem = f.name.removesuffix(".instructions.md")
        if stem in known_agent_ids or f.name == REGISTRY_FILENAME:
            continue  # never mistake agent output (or the registry itself) for real context
        # errors="replace" rather than letting a bad-encoding file crash the whole
        # run uncaught (which would also skip the final registry write) -- a
        # mis-encoded byte in one context file shouldn't take down generation.
        content = f.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n").replace("\n", NEWLINE_SUBSTITUTE)
        pieces.append(f"~~~ === {f.name} === ~~~ {content} ")
    return "".join(pieces)


def build_prompt(agent_def: str, real_context: str, upstream_output: str | None, upstream_id: str | None) -> str:
    agent_def_flat = agent_def.replace("\r\n", "\n").replace("\n", NEWLINE_SUBSTITUTE)

    upstream_section = ""
    if upstream_output:
        upstream_flat = upstream_output.replace("\r\n", "\n").replace("\n", NEWLINE_SUBSTITUTE)
        upstream_section = (
            f" ~~~ === UPSTREAM AGENT OUTPUT ({upstream_id}, already generated this run -- "
            f"treat as authoritative real context for the handoff this agent depends on) === ~~~ "
            f"{upstream_flat} "
        )

    prompt = (
        "IMPORTANT: this prompt uses ~~~ as a section separator instead of real newlines -- "
        "some CLI argument-passing paths truncate real newlines, this does not. "
        "Do not explore the filesystem, list directories, run tools, or read any files -- "
        "everything you need is provided below. Respond directly in your reply, do not use "
        "any file-editing tool. ~~~ "
        "IMPORTANT: you are not fulfilling a live request right now, there is no request to "
        "fulfill. Your task is to produce the FINAL customized instruction file for this "
        "specific repository, to be read later whenever a real request comes in. Take the "
        "agent definition below and wherever it has a placeholder or says to resolve context "
        "dynamically, replace it with real specific facts drawn from the REAL REPOSITORY "
        "CONTEXT (and upstream agent output, if provided) below. Do not attempt to complete "
        "the task described in that agent Charter section -- do not draft a governance "
        "document, do not process a feature request, do not make an architecture decision, do "
        "not perform the job itself. Only produce the finished instruction file, ready to be "
        "read later. Preserve the Charter, Scope boundary, Responsibilities, Output, and Stop "
        "conditions sections mostly as written since those are standing operating rules, not "
        "something resolved per repository -- only the context/placeholder section should be "
        "filled in with real facts. Respond with the complete final instruction file content "
        "only, no preamble, no commentary about what you are doing. ~~~ "
        f"=== REAL REPOSITORY CONTEXT (already generated for this application) === ~~~ {real_context} "
        f"{upstream_section}"
        f"=== AGENT DEFINITION TO COMPILE INTO A FINAL INSTRUCTION FILE === ~~~ {agent_def_flat}"
    )
    return prompt.replace('"', "")


COPILOT_TIMEOUT_SECONDS = 180
# Windows CreateProcess caps a whole command line around 32K chars; leave real
# headroom below that. If this trips, it's almost always agents/_manifest.json
# being incomplete/stale (so load_real_context() misclassifies old generated
# agent output as "real context" and stuffs it all into the prompt) -- a
# manifest built by publish.py's cumulative merge should never hit this.
MAX_PROMPT_CHARS = 25000


def call_copilot(copilot_bin: str, prompt: str, neutral_cwd: str | None, *, timeout: int = COPILOT_TIMEOUT_SECONDS) -> str:
    cmd = [copilot_bin]
    if neutral_cwd:
        cmd += ["-C", neutral_cwd]
    cmd += ["-p", prompt, "--allow-all-tools", "--no-custom-instructions", "--output-format", "json"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise GenerationError("*", f"copilot CLI did not respond within {timeout}s (hung or network stalled)")
    except OSError as e:
        # A bad COPILOT_CLI_PATH override, or a resolved binary that's missing/not
        # executable, raises FileNotFoundError/PermissionError here -- without this
        # catch it propagates uncaught past the retry loop and crashes the whole
        # run with a raw traceback instead of a clean, reported failure.
        raise GenerationError("*", f"could not launch copilot CLI ('{copilot_bin}'): {e}")
    # Take the LAST assistant.message event, not the first: a turn that uses
    # tools emits one assistant.message per turn (often empty, or a brief
    # "I'll do X" preamble alongside tool_requests), with the actual final
    # reply arriving as the last one after all tool calls complete. A
    # single-shot, no-tool-use reply (the common case for the non-autonomous
    # template-generation flow this was originally written for) only ever
    # emits one, so this is a strict improvement, not a behavior change,
    # for that case. Confirmed empirically: a live autonomous run emitted 6
    # assistant.message events -- the first was an empty tool-call-only
    # message, the last was the complete structured report.
    last_content: str | None = None
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant.message":
            content = event.get("data", {}).get("content")
            if content:
                last_content = content
    if last_content is not None:
        return last_content.replace(NEWLINE_SUBSTITUTE.strip(), "\n")
    raise GenerationError("*", f"no assistant.message found in copilot output (exit={result.returncode})")


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def resolve_upstream_output(
    upstream_id: str | None,
    *,
    generated: dict[str, str],
    instructions_dir: Path,
) -> str | None:
    """Return the upstream agent's output for handoff, checking this run's
    in-memory results first, then falling back to a previously-generated
    file already on disk (the realistic case: agents are usually generated
    across separate invocations over time, not all in one sitting)."""
    if upstream_id is None:
        return None
    if upstream_id in generated:
        return generated[upstream_id]
    on_disk = instructions_dir / f"{upstream_id}.instructions.md"
    if on_disk.is_file():
        return on_disk.read_text(encoding="utf-8", errors="replace")
    return None


def generate_one(
    agent_id: str,
    *,
    workflow_dir: Path,
    instructions_dir: Path,
    upstream_output: str | None,
    upstream_id: str | None,
    known_agent_ids: set[str],
    copilot_bin: str,
    neutral_cwd: str | None,
) -> str:
    agent_def_path = workflow_dir / "agents" / f"{agent_id}.md"
    if not agent_def_path.is_file():
        raise GenerationError(agent_id, f"agent definition not found at {agent_def_path}")
    agent_def = agent_def_path.read_text(encoding="utf-8", errors="replace")

    real_context = load_real_context(instructions_dir, known_agent_ids)
    if not real_context:
        raise GenerationError(
            agent_id,
            "NO_CONTEXT_FOUND: no real instruction files found in .github/instructions -- "
            "run Lumen context generation for this repository first.",
        )

    prompt = build_prompt(agent_def, real_context, upstream_output, upstream_id)
    if len(prompt) > MAX_PROMPT_CHARS:
        # Fail fast, no retry -- prompt size won't change between attempts, so
        # retrying would just waste time before reporting the same problem.
        raise GenerationError(
            agent_id,
            f"prompt is {len(prompt)} chars, over the {MAX_PROMPT_CHARS} safety limit -- "
            "likely agents/_manifest.json is incomplete or stale (missing entries for "
            "already-generated agents makes load_real_context() treat their output as "
            "real repository context instead of excluding it). Re-publish through the "
            "normal flow so the manifest is rebuilt cumulatively, rather than editing it "
            "by hand.",
        )

    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS_PER_AGENT + 1):
        try:
            resolved = call_copilot(copilot_bin, prompt, neutral_cwd)
        except GenerationError as e:
            last_error = e
            time.sleep(min(2 ** attempt, 8))
            continue

        if len(resolved.strip()) < MIN_PLAUSIBLE_OUTPUT_CHARS:
            last_error = GenerationError(
                agent_id,
                f"output too short ({len(resolved.strip())} chars) on attempt {attempt} -- "
                "likely a refusal rather than a resolved instruction file",
            )
            time.sleep(min(2 ** attempt, 8))
            continue

        dest = instructions_dir / f"{agent_id}.instructions.md"
        atomic_write(dest, resolved)
        return resolved

    raise last_error or GenerationError(agent_id, "generation failed for an unknown reason")


def build_registry_content(
    status_by_agent: dict[str, str],
    instructions_dir: Path,
    depends_on: dict[str, str | None],
    labels: dict[str, tuple[str, str]],
) -> str:
    """Deterministic (no LLM call) index file every agent template references
    as `registry.instructions.md` -- resolves the "classification resolution"
    pointer that ships in all 10 templates, and gives a developer one place
    to see the real dependency chain and which agents actually have a
    generated instruction file in *this* repository right now."""
    lines = [
        "# Agent Registry — classification resolution",
        "",
        "Deterministically generated by `generate_instructions.py` -- not LLM output,",
        "safe to treat as structural fact. Regenerated every time agents are generated",
        "for this repository; always reflects real, current state, not aspirational.",
        "",
        "## Ontology Plane categories used across agent instruction files",
        "",
        "| Tag | Meaning |",
        "|---|---|",
        "| Direction (Judgment) | The agent must weigh options and decide, not just check a box. |",
        "| Constraint (Constraint) | A hard boundary the agent must not cross, regardless of judgment. |",
        "| Structural (Provenance) | Factual, repository-specific grounding the agent's output must trace back to. |",
        "",
        "## Agent index (real pace-aidlc dependency chain, upstream first)",
        "",
        "| Agent | Role | Depends on | Status in this repository |",
        "|---|---|---|---|",
    ]
    for agent_id in depends_on:
        name, role = labels.get(agent_id, (agent_id, ""))
        dep_id = depends_on[agent_id]
        dep_label = labels.get(dep_id, (dep_id, ""))[0] if dep_id else "—"
        if agent_id in status_by_agent:
            status = status_by_agent[agent_id]
        elif (instructions_dir / f"{agent_id}.instructions.md").is_file():
            status = "generated in a previous run"
        else:
            status = "not generated"
        lines.append(f"| {name} | {role} | {dep_label} | {status} |")
    return "\n".join(lines) + "\n"


def write_registry(
    instructions_dir: Path,
    results: list[tuple[str, str]],
    depends_on: dict[str, str | None],
    labels: dict[str, tuple[str, str]],
) -> None:
    status_by_agent = {agent_id: status for agent_id, status in results}
    content = build_registry_content(status_by_agent, instructions_dir, depends_on, labels)
    atomic_write(instructions_dir / REGISTRY_FILENAME, content)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-ids", required=True, help="comma-separated agent ids to generate, any order")
    parser.add_argument("--workflow-dir", default=".", help="directory containing agents/ (defaults to CWD)")
    parser.add_argument("--instructions-dir", default=".github/instructions", help="target repo's real+generated instructions dir")
    parser.add_argument("--neutral-cwd", default=os.environ.get("COPILOT_NEUTRAL_CWD"), help="cwd to launch copilot from, avoids it auto-loading this repo's own instructions")
    args = parser.parse_args()

    workflow_dir = Path(args.workflow_dir).resolve()
    instructions_dir = Path(args.instructions_dir).resolve()

    requested = [a.strip() for a in args.agent_ids.split(",") if a.strip()]
    try:
        depends_on, labels, _orchestration = load_agent_registry(workflow_dir)  # orchestration flags unused by this script's own job
        ordered = order_agents(requested, depends_on)
        known_agent_ids = set(depends_on)
        copilot_bin = find_copilot_binary()
    except GenerationError as e:
        print(f"FAILED: {e}", file=sys.stderr)
        return 2

    generated: dict[str, str] = {}
    results: list[tuple[str, str]] = []  # (agent_id, status)

    for agent_id in ordered:
        upstream_id = depends_on.get(agent_id)
        upstream_output = resolve_upstream_output(
            upstream_id, generated=generated, instructions_dir=instructions_dir
        )
        if upstream_id and upstream_output is None:
            # Upstream has no output anywhere -- neither generated this run nor
            # already on disk from an earlier run. Never proceed without the
            # real handoff; halt here rather than generating a disconnected file.
            results.append((agent_id, f"SKIPPED (upstream {upstream_id} has no generated output yet, in this run or on disk)"))
            break
        try:
            resolved = generate_one(
                agent_id,
                workflow_dir=workflow_dir,
                instructions_dir=instructions_dir,
                upstream_output=upstream_output,
                upstream_id=upstream_id,
                known_agent_ids=known_agent_ids,
                copilot_bin=copilot_bin,
                neutral_cwd=args.neutral_cwd,
            )
            generated[agent_id] = resolved
            results.append((agent_id, f"OK ({len(resolved)} chars)"))
        except GenerationError as e:
            results.append((agent_id, f"FAILED: {e.reason}"))
            break  # halt the chain -- don't generate downstream agents against a broken dependency

    write_registry(instructions_dir, results, depends_on, labels)

    print("\n--- generation summary ---")
    for agent_id, status in results:
        print(f"{agent_id}: {status}")
    print(f"registry.instructions.md written to {instructions_dir}")

    return 0 if all(status.startswith("OK") for _, status in results) and len(results) == len(ordered) else 1


if __name__ == "__main__":
    raise SystemExit(main())
