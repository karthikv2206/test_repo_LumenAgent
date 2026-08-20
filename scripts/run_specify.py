"""Thin wrapper around `specify`, fixing a real, confirmed-by-testing
reliability gap: `specify workflow run`/`resume` only takes the intended
non-interactive path (returning `"status": "paused"` in --json output) when
stdin is genuinely absent. On Windows, redirecting from the null device
(`subprocess.DEVNULL`, Bash `< /dev/null`) was observed to still behave
like an attached console -- the CLI fell back to an interactive prompt and
silently defaulted to the LAST listed gate option on EOF (`reject` for a
standard gate), aborting the run without the human (or Copilot relaying to
them) ever seeing or choosing anything.

Only a genuinely piped empty stdin reliably forces the real non-interactive
path. Rather than depend on every caller -- Copilot's own terminal
execution, a real backend's subprocess call, a developer's shell -- to get
this exactly right (and a shell command written to pipe empty input is
itself shell-specific: `printf '' |` on POSIX, `echo.|` on cmd.exe, `"" |`
on PowerShell), this wrapper does the one thing that's actually reliable:
call `specify` via Python's own subprocess module with `input=""`, which
unconditionally creates a real anonymous pipe for stdin regardless of the
calling shell or terminal. Whoever invokes this instead of `specify`
directly never has to think about the shell-piping problem at all.

Usage: identical to `specify`, just prefixed --
    python run_specify.py workflow run "<path-to-yml>" -i ticket_id=<id> --json
    python run_specify.py workflow resume <run_id> -i <gate>_verdict=<choice> --json

stdout/stderr and the exit code are passed through unchanged, so `--json`
output can be parsed exactly as if `specify` had been called directly.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys


def find_specify_binary() -> str:
    env_override = os.environ.get("SPECIFY_CLI_PATH")
    if env_override:
        return env_override
    # Same .exe/bare-name-over-.cmd preference as find_copilot_binary() in
    # generate_instructions.py, and for the same reason: a .cmd wrapper
    # routes through cmd.exe, which has its own argument-length limits and
    # quoting behavior -- prefer the native binary when available.
    for candidate in ("specify.exe", "specify"):
        found = shutil.which(candidate)
        if found:
            return found
    found = shutil.which("specify.cmd")
    if found:
        return found
    print(
        "run_specify.py: 'specify' CLI not found on PATH; set SPECIFY_CLI_PATH "
        "to its absolute path.",
        file=sys.stderr,
    )
    sys.exit(2)


def main() -> int:
    specify_bin = find_specify_binary()
    passthrough_args = sys.argv[1:]
    if not passthrough_args:
        print("run_specify.py: no arguments given -- pass the same arguments you would give to 'specify'.", file=sys.stderr)
        return 2

    try:
        result = subprocess.run(
            [specify_bin] + passthrough_args,
            input="",
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        print(f"run_specify.py: could not launch '{specify_bin}': {e}", file=sys.stderr)
        return 2
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
