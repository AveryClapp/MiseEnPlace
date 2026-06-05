"""Optional full-screen cook-along that visualizes the food in each spot.

A comprehensive alternative to the plain `cook` walkthrough: it draws one lane
per piece of cookware (oven, each pot/pan) and shows what is in it and how long
it has left. Nothing is assumed to be running — a lane only starts a countdown
when you start that step — and a finished timer rings but never advances on its
own; you acknowledge it.

Textual is an optional extra (`pip install 'mise-en-place[tui]'`). It is imported
lazily inside `run` so the rest of mep works without it, and so this module
imports fine in offline tests. The pure lane logic lives in `cook` and is tested
there; the app below is the thin terminal layer.
"""

import time

from . import cook
from .errors import MepError


def run(recipe: dict, tasks: list[dict]) -> None:
    """Launch the TUI for one (possibly combined) plan. `recipe` only needs
    dish_name/title for the header; `tasks` is a plan from `plan.generate_plan`."""
    _app_class()(recipe, tasks).run()


def _app_class():
    """Build the Textual app class lazily (so the extra is only needed when the
    TUI actually runs). Returned so tests can drive it with a headless pilot."""
    try:
        from textual.app import App
        from textual.widgets import Footer, Static
    except ModuleNotFoundError:
        raise MepError(
            "The cook TUI needs Textual. Install it with: "
            "pip install 'mise-en-place[tui]'"
        )

    class CookApp(App):
        CSS = """
        Screen { layout: vertical; }
        #title { padding: 0 1; height: 1; text-style: bold; }
        #lanes { border: round $primary; padding: 0 1; margin: 0 1; }
        #upnext { border: round $accent; padding: 0 1; margin: 0 1; height: 1fr; }
        """
        BINDINGS = [
            ("enter,space", "advance", "start / next"),
            ("a", "ack", "acknowledge timer"),
            ("q", "quit", "quit"),
        ]

        def __init__(self, recipe, tasks):
            super().__init__()
            self.recipe = recipe
            self.tasks = tasks
            self.lanes = cook.plan_lanes(tasks)
            self.state = ["pending"] * len(tasks)  # pending | running | done
            self.start = [None] * len(tasks)       # monotonic start of a timer
            self.rung = [False] * len(tasks)        # has its bell fired yet
            self.focus_i = self._next_pending(0)

        def compose(self):
            yield Static(id="title")
            yield Static(id="lanes")
            yield Static(id="upnext")
            yield Footer()

        def on_mount(self):
            self.query_one("#lanes", Static).border_title = "Equipment"
            self.query_one("#upnext", Static).border_title = "Up next"
            self.set_interval(0.25, self._render)
            self._render()

        # --- actions ---

        def action_advance(self):
            i = self.focus_i
            if i >= len(self.tasks):
                return
            if self._is_timed(self.tasks[i]):
                self.state[i] = "running"
                self.start[i] = time.monotonic()
            else:
                self.state[i] = "done"
            self.focus_i = self._next_pending(i + 1)
            self._render()

        def action_ack(self):
            now = time.monotonic()
            ready = [i for i in self._running_tasks() if self._remaining(i, now) <= 0]
            if ready:
                ready.sort(key=lambda i: self.start[i] or 0)
                self.state[ready[0]] = "done"  # free the spot
            self._render()

        # --- state helpers ---

        def _is_timed(self, task):
            return task.get("mode") == "passive" and (task.get("duration_minutes") or 0) > 0

        def _next_pending(self, start):
            j = start
            while j < len(self.tasks) and self.state[j] != "pending":
                j += 1
            return j

        def _running_tasks(self):
            return [i for i, s in enumerate(self.state) if s == "running"]

        def _remaining(self, i, now):
            dur = (self.tasks[i].get("duration_minutes") or 0) * 60
            return dur - (now - (self.start[i] or now))

        # --- rendering ---

        def _render(self):
            now = time.monotonic()
            for i in self._running_tasks():
                if not self.rung[i] and self._remaining(i, now) <= 0:
                    self.rung[i] = True
                    self.bell()
            self.query_one("#title", Static).update(self._title_text())
            self.query_one("#lanes", Static).update(self._lanes_text(now))
            self.query_one("#upnext", Static).update(self._upnext_text())

        def _title_text(self):
            return self.recipe.get("dish_name") or self.recipe.get("title") or "Cooking"

        def _lanes_text(self, now):
            if not self.lanes:
                return "[dim]No specific cookware in this recipe.[/dim]"
            width = max(len(lane) for lane in self.lanes)
            return "\n".join(
                f"{lane.upper():<{width}}   {self._occupant(lane, now)}"
                for lane in self.lanes
            )

        def _occupant(self, lane, now):
            running = [
                i for i in self._running_tasks()
                if (cook.lane_for_task(self.tasks[i]) or "").lower() == lane.lower()
            ]
            if running:
                i = max(running, key=lambda i: self.start[i] or 0)
                label = self._contents(self.tasks[i])
                remaining = self._remaining(i, now)
                if remaining <= 0:
                    return f"[bold green]{label}  READY ✓  press \\[a][/bold green]"
                dur = (self.tasks[i].get("duration_minutes") or 0) * 60
                bar = cook.progress_bar((dur - remaining) / dur if dur else 1)
                return f"[cyan]{label}[/cyan]  {bar}  {cook.fmt_clock(remaining)} left"
            # An ACTIVE step you're on right now also occupies its spot. A
            # passive step you haven't started yet leaves the spot idle — nothing
            # is assumed to be in it until you start it.
            fi = self.focus_i
            if fi < len(self.tasks):
                ft = self.tasks[fi]
                if ft.get("mode") == "active" and (cook.lane_for_task(ft) or "").lower() == lane.lower():
                    return f"[yellow]{self._contents(ft)}  (working)[/yellow]"
            return "[dim]idle[/dim]"

        def _contents(self, task):
            label = task.get("timer_label") or " ".join(task["instruction"].split()[:4])
            dish = task.get("dish")
            return f"{dish}: {label}" if dish else label

        def _upnext_text(self):
            i = self.focus_i
            if i >= len(self.tasks):
                if self._running_tasks():
                    return "[bold]All steps started — waiting on timers. Press \\[a] as they ring, \\[q] to finish.[/bold]"
                return "[bold green]Done. Plate up and enjoy. Press \\[q] to exit.[/bold green]"
            t = self.tasks[i]
            timed = self._is_timed(t)
            head = f"[cyan]Step {i + 1} of {len(self.tasks)}[/cyan]"
            if t.get("dish"):
                head += f"   [magenta]{t['dish']}[/magenta]"
            lines = [head, f"  {t['instruction']}"]
            if t.get("equipment"):
                lines.append(f"  [blue]equipment:[/blue] {', '.join(t['equipment'])}")
            if t.get("ingredients"):
                lines.append(f"  uses: {', '.join(t['ingredients'])}")
            if timed and t.get("overlap_hint"):
                lines.append(f"  [dim]while it waits: {t['overlap_hint']}[/dim]")
            hint = "start timer & continue" if timed else "done, next"
            lines.append(f"\n  [dim]\\[enter] {hint}[/dim]")
            return "\n".join(lines)

    return CookApp
