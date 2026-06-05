"""Live cook-along: walk a plan while juggling real background timers.

You advance steps manually (Enter). Reaching a hands-off step starts a named
timer that keeps running while you move on to the next step, just like setting a
kitchen timer and getting on with prep. Running timers show in the status line
and ring + banner when they finish. Ctrl-C stops cleanly and reports any timers
still going.

The formatting/lookup helpers are pure and unit-tested; the terminal loop is
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
    """Pacing text for the current step."""
    if passive and duration_s:
        remaining = duration_s - elapsed_s
        if remaining >= 0:
            return f"remaining {fmt_clock(remaining)}"
        return f"over by {fmt_clock(-remaining)}"
    return f"elapsed {fmt_clock(elapsed_s)}"


def timers_summary(timers: list[dict], now: float) -> str:
    """Inline summary of still-running background timers."""
    parts = [
        f"⏲ {t['label']} {fmt_clock(t['end'] - now)}"
        for t in timers
        if not t["fired"]
    ]
    return "   ".join(parts)


def estimate_wallclock_minutes(tasks: list[dict]) -> float:
    """Realistic total time for the hybrid model: active work runs sequentially
    while passive timers run in the background. Wall-clock is the active spine
    plus any passive tail that outlasts it."""
    active_clock = 0.0
    finish = 0.0
    for task in tasks:
        duration = task.get("duration_minutes") or 0
        if task.get("mode") == "passive":
            finish = max(finish, active_clock + duration)
        else:
            active_clock += duration
            finish = max(finish, active_clock)
    return max(active_clock, finish)


def all_equipment(tasks: list[dict]) -> list[str]:
    """Unique equipment across the whole plan, in first-seen order."""
    seen: list[str] = []
    for task in tasks:
        for item in task.get("equipment") or []:
            if item not in seen:
                seen.append(item)
    return seen


def _is_heat_equipment(name: str) -> bool:
    return any(k in name.lower() for k in ("oven", "grill", "broil", "griddle"))


def preheat_cue(tasks: list[dict], i: int) -> str | None:
    """If a step in the next couple needs heat the current step doesn't, nudge
    the cook to start preheating."""
    current = tasks[i].get("equipment") or []
    if any(_is_heat_equipment(e) for e in current):
        return None
    for j in range(i + 1, min(i + 3, len(tasks))):
        hot = sorted({e for e in (tasks[j].get("equipment") or []) if _is_heat_equipment(e)})
        if hot:
            return f"Coming up (step {j + 1}): {hot[0]} — start preheating now."
    return None


# --- interactive loop (thin, not unit-tested) ---------------------------------


def run(recipe: dict, tasks: list[dict], gather_lines=None, scale_note=None) -> None:
    name = recipe["dish_name"] or recipe["title"] or "recipe"
    click.echo()
    click.secho(f"Cooking: {name}", bold=True)
    if scale_note:
        click.secho(f"  {scale_note}", fg="yellow")

    _print_mise_en_place(gather_lines, tasks)
    interactive = sys.stdin.isatty()
    if interactive:
        click.echo("Press Enter once everything is gathered to begin...", nl=False)
        sys.stdin.readline()
        click.echo()

    timers: list[dict] = []
    try:
        for i in range(len(tasks)):
            _present_step(i, tasks, timers, interactive)
            task = tasks[i]
            if task.get("mode") == "passive" and (task.get("duration_minutes") or 0) > 0:
                _start_timer(timers, task)
        _drain_timers(timers, interactive)
    except KeyboardInterrupt:
        _abort(timers)
        return

    click.echo()
    click.secho("Done. Plate up and enjoy.", bold=True)
    click.echo()


def _print_mise_en_place(gather_lines, tasks) -> None:
    click.echo()
    click.secho("Mise en place", underline=True)
    if gather_lines:
        click.echo("  Gather:")
        for line in gather_lines:
            click.echo(f"    - {line}")
    equipment = all_equipment(tasks)
    if equipment:
        click.echo("  Equipment: " + ", ".join(equipment))
    click.echo()


def _present_step(i, tasks, timers, interactive) -> None:
    task = tasks[i]
    passive = task.get("mode") == "passive"
    tag = f"{'passive' if passive else 'active'} ~{fmt_duration(task.get('duration_minutes'))}"

    head = f"Step {i + 1} of {len(tasks)}   [{tag}]"
    if task.get("dish"):
        head += f"   {task['dish']}"
    click.secho(head, fg="cyan")
    click.echo(f"  {task.get('instruction')}")
    if task.get("ingredients"):
        click.echo("  uses: " + ", ".join(task["ingredients"]))
    if task.get("equipment"):
        click.echo("  equipment: " + ", ".join(task["equipment"]))
    cue = preheat_cue(tasks, i)
    if cue:
        click.secho(f"  {cue}", fg="yellow")
    if passive and task.get("overlap_hint"):
        click.echo(f"  while it waits: {task['overlap_hint']}")

    hint = "[Enter] start timer & continue" if passive else "[Enter] done, next"
    _wait_for_enter(timers, interactive, hint)


def _wait_for_enter(timers, interactive, hint) -> None:
    if not interactive:
        sys.stdin.readline()
        click.echo()
        return
    start = time.monotonic()
    while True:
        now = time.monotonic()
        _fire_due(timers, now)
        line = f"  {status_line(now - start, 0, False)}"
        summary = timers_summary(timers, now)
        if summary:
            line += "   " + summary
        line += f"        {hint}"
        sys.stdout.write("\r\033[K" + line)
        sys.stdout.flush()
        ready, _, _ = select.select([sys.stdin], [], [], 1.0)
        if ready:
            sys.stdin.readline()
            break
    sys.stdout.write("\n\n")
    sys.stdout.flush()


def _start_timer(timers, task) -> None:
    label = task.get("timer_label") or " ".join(task["instruction"].split()[:3])
    duration = (task.get("duration_minutes") or 0) * 60
    timers.append(
        {"label": label, "end": time.monotonic() + duration, "duration": duration, "fired": False}
    )
    click.secho(
        f"  ⏲ timer set: {label} ({fmt_duration(task.get('duration_minutes'))})",
        fg="blue",
    )
    click.echo()


def _fire_due(timers, now) -> None:
    for t in timers:
        if not t["fired"] and now >= t["end"]:
            t["fired"] = True
            sys.stdout.write("\r\033[K\a")
            click.secho(f"  ⏰ {t['label']} is ready!", fg="green", bold=True)


def _drain_timers(timers, interactive) -> None:
    if all(t["fired"] for t in timers):
        return
    click.echo()
    click.secho("All steps done — waiting on timers. [Ctrl-C] to exit.", bold=True)
    while not all(t["fired"] for t in timers):
        now = time.monotonic()
        _fire_due(timers, now)
        if all(t["fired"] for t in timers):
            break
        if not interactive:
            break
        sys.stdout.write("\r\033[K  " + timers_summary(timers, now))
        sys.stdout.flush()
        select.select([sys.stdin], [], [], 1.0)
    if interactive:
        sys.stdout.write("\n")
        sys.stdout.flush()


def _abort(timers) -> None:
    click.echo()
    pending = [t for t in timers if not t["fired"]]
    if pending:
        now = time.monotonic()
        click.secho("Stopped. Timers still running:", fg="red")
        for t in pending:
            click.echo(f"  - {t['label']}: {fmt_clock(t['end'] - now)} left")
    else:
        click.secho("Stopped.", fg="red")
    click.echo()
