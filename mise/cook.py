"""Live cook-along: walk a cached plan one task at a time.

Manual advance (Enter moves on) with a live pacing timer per task. Active tasks
count up; passive waits count down and ring the bell at zero. Neither
auto-advances. The formatting helpers are pure and unit-tested; the I/O loop is
thin and not.
"""

import select
import sys
import time

import click


def fmt_duration(minutes) -> str:
    """Human duration from minutes: '0m', '45m', '2h', '1h30m'."""
    total = int(round(minutes or 0))
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f"{hours}h{mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


def fmt_clock(seconds) -> str:
    """Clock from seconds: 'M:SS', or 'H:MM:SS' past an hour."""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def status_line(elapsed_s: float, duration_s: float, passive: bool) -> str:
    """Pacing text for a running task."""
    if passive and duration_s:
        remaining = duration_s - elapsed_s
        if remaining >= 0:
            return f"remaining {fmt_clock(remaining)}"
        return f"over by {fmt_clock(-remaining)}"
    return f"elapsed {fmt_clock(elapsed_s)}"


def run(recipe: dict, tasks: list[dict]) -> None:
    name = recipe["dish_name"] or recipe["title"] or "recipe"
    click.echo()
    click.secho(f"Cooking: {name}", bold=True)
    click.echo(f"{len(tasks)} steps. Press Enter to advance, Ctrl-C to stop.\n")
    for i, task in enumerate(tasks, start=1):
        _run_task(i, len(tasks), task)
    click.echo("\nDone. Plate up and enjoy.\n")


def _run_task(index: int, total: int, task: dict) -> None:
    passive = (task.get("mode") or "active") == "passive"
    duration_s = (task.get("duration_minutes") or 0) * 60
    tag = f"{'passive' if passive else 'active'} ~{fmt_duration(task.get('duration_minutes'))}"

    click.secho(f"Step {index} of {total}   [{tag}]", fg="cyan")
    click.echo(f"  {task.get('instruction')}")
    if task.get("overlap_hint"):
        click.echo(f"  └ {task['overlap_hint']}")

    # Piped / non-interactive: no live clock, just block for a line.
    if not sys.stdin.isatty():
        sys.stdin.readline()
        click.echo()
        return

    start = time.monotonic()
    rang = False
    while True:
        elapsed = time.monotonic() - start
        line = status_line(elapsed, duration_s, passive)
        sys.stdout.write(f"\r  {line}        [Enter] next  ·  [Ctrl-C] quit   ")
        sys.stdout.flush()
        if passive and duration_s and not rang and elapsed >= duration_s:
            sys.stdout.write("\a")
            rang = True
        ready, _, _ = select.select([sys.stdin], [], [], 1.0)
        if ready:
            sys.stdin.readline()
            break
    sys.stdout.write("\n\n")
    sys.stdout.flush()
