from __future__ import annotations

import argparse
import sys
from typing import Iterable, Optional

from . import __version__
from .config import Config, config_path, load_config, write_config
from .hooks import handle_agent_stop, handle_git_post_commit, latest_state
from .installers import (
    check_status,
    install_claude_hook,
    install_codex_hook,
    install_git_hook,
    uninstall_claude_hook,
    uninstall_codex_hook,
    uninstall_git_hook,
)


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hf traces",
        description="Publish coding-agent traces to HF buckets and attach Git notes.",
    )
    parser.add_argument(
        "--version", action="version", version=f"hf-traces {__version__}"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup", help="Configure agent hooks and Git notes.")
    setup.add_argument(
        "--bucket", help="HF bucket id, for example cfahlgren1/agent-traces."
    )
    setup.add_argument(
        "--agents",
        default="codex,claude",
        help="Comma-separated agents to enable. Supported: codex, claude.",
    )
    setup.add_argument(
        "--git",
        choices=("global", "local", "none"),
        default="local",
        help="Install Git hook in the current repo, globally, or not at all.",
    )
    setup.add_argument(
        "--note-ref",
        default="refs/notes/hf-traces",
        help="Git notes ref used for trace links.",
    )
    setup.add_argument(
        "--dry-run", action="store_true", help="Show changes without writing files."
    )
    setup.set_defaults(func=setup_command)

    status = subparsers.add_parser(
        "status", help="Show config and installed hook status."
    )
    status.set_defaults(func=status_command)

    latest = subparsers.add_parser(
        "latest", help="Print the latest trace state for this repo."
    )
    latest.set_defaults(func=latest_command)

    doctor = subparsers.add_parser(
        "doctor", help="Check required tools and bucket access."
    )
    doctor.set_defaults(func=doctor_command)

    uninstall = subparsers.add_parser("uninstall", help="Remove managed hooks.")
    uninstall.add_argument(
        "--agents",
        default="codex,claude",
        help="Comma-separated agents to remove. Supported: codex, claude.",
    )
    uninstall.add_argument(
        "--git",
        choices=("global", "local", "none"),
        default="local",
        help="Remove Git hook from the current repo, globally, or not at all.",
    )
    uninstall.add_argument(
        "--dry-run", action="store_true", help="Show changes without writing files."
    )
    uninstall.set_defaults(func=uninstall_command)

    hook = subparsers.add_parser("hook", help="Internal hook entrypoints.")
    hook_subparsers = hook.add_subparsers(dest="hook_target", required=True)

    codex = hook_subparsers.add_parser("codex", help=argparse.SUPPRESS)
    codex_subparsers = codex.add_subparsers(dest="hook_event", required=True)
    codex_stop = codex_subparsers.add_parser("stop", help=argparse.SUPPRESS)
    codex_stop.set_defaults(func=lambda _args: handle_agent_stop("codex"))

    claude = hook_subparsers.add_parser("claude", help=argparse.SUPPRESS)
    claude_subparsers = claude.add_subparsers(dest="hook_event", required=True)
    claude_stop = claude_subparsers.add_parser("stop", help=argparse.SUPPRESS)
    claude_stop.set_defaults(func=lambda _args: handle_agent_stop("claude"))

    git = hook_subparsers.add_parser("git", help=argparse.SUPPRESS)
    git_subparsers = git.add_subparsers(dest="hook_event", required=True)
    git_post_commit = git_subparsers.add_parser("post-commit", help=argparse.SUPPRESS)
    git_post_commit.set_defaults(func=lambda _args: handle_git_post_commit())

    return parser


def setup_command(args: argparse.Namespace) -> int:
    current = load_config()
    agents = parse_agents(args.agents)

    bucket = args.bucket or current.bucket
    if not bucket:
        raise SystemExit("--bucket is required the first time you run setup")

    next_config = Config(
        bucket=bucket,
        agents=agents,
        note_ref=args.note_ref,
        git_scope=args.git,
        dry_run=current.dry_run,
    )

    results = []
    results.append(write_config(next_config, dry_run=args.dry_run))

    if "codex" in agents:
        results.append(install_codex_hook(dry_run=args.dry_run))
    if "claude" in agents:
        results.append(install_claude_hook(dry_run=args.dry_run))
    if args.git != "none":
        results.extend(
            install_git_hook(args.git, next_config.note_ref, dry_run=args.dry_run)
        )

    print_results(results, dry_run=args.dry_run)
    return 0


def status_command(_args: argparse.Namespace) -> int:
    current = load_config()
    print(f"config: {config_path()}")
    print(f"bucket: {current.bucket or '(not configured)'}")
    print(f"agents: {', '.join(current.agents) if current.agents else '(none)'}")
    print(f"git scope: {current.git_scope}")
    print(f"note ref: {current.note_ref}")
    print("")

    for label, installed in check_status(current.note_ref, current.git_scope).items():
        state = "installed" if installed else "missing"
        print(f"{label}: {state}")

    return 0


def latest_command(_args: argparse.Namespace) -> int:
    state = latest_state()
    if not state:
        print("no trace state found for this repo")
        return 1

    print(state.get("trace_url", ""))
    return 0


def doctor_command(_args: argparse.Namespace) -> int:
    from .hooks import command_exists, run_command

    current = load_config()
    checks = {
        "git": command_exists("git"),
        "hf": command_exists("hf"),
        "bucket configured": bool(current.bucket),
    }

    for label, ok in checks.items():
        print(f"{label}: {'ok' if ok else 'missing'}")

    if command_exists("hf"):
        result = run_command(["hf", "auth", "whoami"])
        print(f"hf auth: {'ok' if result.returncode == 0 else 'check required'}")

    if current.bucket and command_exists("hf"):
        result = run_command(["hf", "buckets", "info", current.bucket])
        print(f"bucket access: {'ok' if result.returncode == 0 else 'check required'}")

    return 0 if all(checks.values()) else 1


def uninstall_command(args: argparse.Namespace) -> int:
    agents = parse_agents(args.agents)
    results = []

    if "codex" in agents:
        results.append(uninstall_codex_hook(dry_run=args.dry_run))
    if "claude" in agents:
        results.append(uninstall_claude_hook(dry_run=args.dry_run))
    if args.git != "none":
        results.append(uninstall_git_hook(args.git, dry_run=args.dry_run))

    print_results(results, dry_run=args.dry_run)
    return 0


def parse_agents(value: str) -> list[str]:
    agents = [agent.strip().lower() for agent in value.split(",") if agent.strip()]
    unsupported = sorted(set(agents) - {"codex", "claude"})
    if unsupported:
        raise SystemExit(f"unsupported agent(s): {', '.join(unsupported)}")
    return agents


def print_results(results: list[str], dry_run: bool = False) -> None:
    prefix = "would " if dry_run else ""
    for result in results:
        print(prefix + result)


if __name__ == "__main__":
    sys.exit(main())
