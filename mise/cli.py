"""Click entry points for mise."""

import json

import click

from . import cook as cook_mod
from . import db, ingest
from .config import CONFIG_PATH, DB_PATH, MISE_DIR, load_config, model, require
from .errors import MiseError
from .plan import generate_plan


@click.group()
@click.version_option(package_name="mise")
def cli():
    """mise — extract recipes from YouTube cooking videos."""


@cli.command()
def init():
    """Create ~/.mise, prompt for API keys, and build the database."""
    MISE_DIR.mkdir(parents=True, exist_ok=True)
    existing = {}
    if CONFIG_PATH.exists():
        existing = json.loads(CONFIG_PATH.read_text())

    youtube_key = click.prompt(
        "YouTube Data API v3 key (blank to skip; needed for channel ingest)",
        default=existing.get("YOUTUBE_API_KEY", ""),
        show_default=False,
    )
    anthropic_key = click.prompt(
        "Anthropic API key (required for extraction)",
        default=existing.get("ANTHROPIC_API_KEY", ""),
        show_default=False,
    )

    CONFIG_PATH.write_text(
        json.dumps(
            {
                "YOUTUBE_API_KEY": youtube_key.strip(),
                "ANTHROPIC_API_KEY": anthropic_key.strip(),
            },
            indent=2,
        )
    )
    db.init_db()
    click.echo(f"Wrote config to {CONFIG_PATH}")
    click.echo(f"Initialized database at {DB_PATH}")


@cli.command()
@click.argument("url", required=False)
@click.option("--channel", "channel", help="Channel @handle to ingest.")
@click.option("--limit", type=int, default=None, help="Max videos for --channel.")
def add(url, channel, limit):
    """Ingest a single video URL, or a whole channel with --channel."""
    config = load_config()
    conn = db.connect()

    if channel:
        any_seen = False
        for video_id, title, status, dish in ingest.add_channel(
            conn, config, channel, limit
        ):
            any_seen = True
            label = dish or title or video_id
            click.echo(f"  [{status:12}] {label}")
        if not any_seen:
            click.echo("No videos found for that channel.")
        return

    if not url:
        raise click.UsageError("Provide a YouTube URL or --channel <handle>.")

    status, recipe_id, dish = ingest.add_video(conn, config, url)
    if status == "skipped":
        click.echo("Already in your collection.")
    elif status == "no_transcript":
        click.echo(f"Stored (no transcript available) as recipe {recipe_id}.")
    else:
        click.echo(f"Added '{dish or 'untitled'}' as recipe {recipe_id}.")


@cli.command()
@click.argument("query")
def search(query):
    """Full-text search across dish, ingredients, and channel."""
    conn = db.connect()
    rows = db.search(conn, query)
    if not rows:
        click.echo("No matches.")
        return
    for row in rows:
        name = row["dish_name"] or row["title"] or "(untitled)"
        click.echo(f"  {row['id']:>4}  {name}  —  {row['channel'] or 'unknown'}")


@cli.command(name="list")
@click.option("--tag", default=None, help="Only recipes with this tag.")
@click.option("--limit", type=int, default=None, help="Max recipes to show.")
def list_cmd(tag, limit):
    """Browse stored recipes, newest first."""
    conn = db.connect()
    rows = db.list_recipes(conn, tag=tag, limit=limit)
    if not rows:
        click.echo("No recipes yet. Add one with `mep add <url>`.")
        return
    for row in rows:
        name = row["dish_name"] or row["title"] or "(untitled)"
        click.echo(f"  {row['id']:>4}  {name}  —  {row['channel'] or 'unknown'}")


@cli.command()
@click.argument("recipe_id", type=int)
def show(recipe_id):
    """Display one recipe in full."""
    conn = db.connect()
    data = db.get_recipe(conn, recipe_id)
    if data is None:
        raise MiseError(f"No recipe with id {recipe_id}.")
    _render(data)


@cli.command()
@click.argument("recipe_id", type=int)
@click.option("--regenerate", is_flag=True, help="Rebuild the plan from scratch.")
def plan(recipe_id, regenerate):
    """Generate a cooking timeline for a recipe (experimental)."""
    config = load_config()
    db.init_db()  # ensure plan_steps exists on older databases
    conn = db.connect()
    data = db.get_recipe(conn, recipe_id)
    if data is None:
        raise MiseError(f"No recipe with id {recipe_id}.")
    tasks = _ensure_plan(conn, config, data, regenerate)
    _render_plan(data["recipe"], tasks)


@cli.command()
@click.argument("recipe_id", type=int)
def cook(recipe_id):
    """Walk through a recipe's timeline step by step (experimental)."""
    config = load_config()
    db.init_db()
    conn = db.connect()
    data = db.get_recipe(conn, recipe_id)
    if data is None:
        raise MiseError(f"No recipe with id {recipe_id}.")
    tasks = _ensure_plan(conn, config, data, regenerate=False)
    cook_mod.run(data["recipe"], tasks)


def _ensure_plan(conn, config, data, regenerate):
    """Return cached tasks, generating and caching them if needed."""
    tasks = db.get_plan(conn, data["recipe"]["id"])
    if tasks and not regenerate:
        return tasks
    if not data["steps"]:
        raise MiseError("This recipe has no steps to plan.")
    click.echo("Generating cook plan...")
    tasks = generate_plan(
        data, api_key=require(config, "ANTHROPIC_API_KEY"), model=model(config)
    )
    db.save_plan(conn, data["recipe"]["id"], tasks)
    return tasks


def _render_plan(recipe: dict, tasks: list[dict]) -> None:
    name = recipe["dish_name"] or recipe["title"] or "recipe"
    hands_on = sum(t["duration_minutes"] or 0 for t in tasks if t["mode"] == "active")
    total = sum(t["duration_minutes"] or 0 for t in tasks)
    click.echo()
    click.secho(f"Cook plan: {name}", bold=True)
    click.echo(
        f"  hands-on ~{cook_mod.fmt_duration(hands_on)}"
        f"  ·  total ~{cook_mod.fmt_duration(total)}\n"
    )
    for i, task in enumerate(tasks, start=1):
        tag = f"{task['mode']} {cook_mod.fmt_duration(task['duration_minutes'])}"
        click.echo(f"  {i:>2}  [{tag}]  {task['instruction']}")
        if task.get("overlap_hint"):
            click.echo(f"      └ {task['overlap_hint']}")
    click.echo()


def _render(data: dict) -> None:
    r = data["recipe"]
    title = r["dish_name"] or r["title"] or "(untitled)"
    click.echo()
    click.secho(title, bold=True)

    meta = []
    if r["channel"]:
        meta.append(r["channel"])
    for field in ("cook_time", "servings", "difficulty"):
        if r[field]:
            meta.append(f"{field.replace('_', ' ')}: {r[field]}")
    if meta:
        click.echo("  " + "  |  ".join(meta))
    if r["url"]:
        click.echo(f"  {r['url']}")

    if data["tags"]:
        click.echo("  tags: " + ", ".join(data["tags"]))

    if data["ingredients"]:
        click.echo()
        click.secho("Ingredients", underline=True)
        for ing in data["ingredients"]:
            parts = [p for p in (ing["quantity"], ing["unit"], ing["name"]) if p]
            line = " ".join(parts)
            if ing["prep"]:
                line += f", {ing['prep']}"
            click.echo(f"  - {line}")

    if data["steps"]:
        click.echo()
        click.secho("Steps", underline=True)
        for step in data["steps"]:
            click.echo(f"  {step['step_number']}. {step['instruction']}")

    if not data["ingredients"] and not data["steps"]:
        click.echo()
        click.echo("  (No recipe was extracted from this video.)")
    click.echo()


def main():
    try:
        cli(prog_name="mep", standalone_mode=False)
    except MiseError as exc:
        click.secho(f"Error: {exc}", fg="red", err=True)
        raise SystemExit(1)
    except click.ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code)
    except click.exceptions.Abort:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
