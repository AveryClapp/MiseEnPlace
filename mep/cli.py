"""Click entry points for mep."""

import json
from pathlib import Path

import click

from . import adapt as adapt_mod
from . import cook as cook_mod
from . import tui as tui_mod
from . import classify, cookware, db, ingest, scale, shopping
from .components import analyze_components
from .gaps import find_gaps
from .nutrition import estimate_macros
from .config import (
    CONFIG_PATH,
    DB_PATH,
    MEP_DIR,
    load_config,
    migrate_legacy_home,
    require_api_key,
)
from .errors import MepError
from .plan import generate_combined_plan, generate_plan


@click.group()
@click.version_option(package_name="mise-en-place")
def cli():
    """mep — extract recipes from YouTube cooking videos."""


@cli.command()
def init():
    """Create ~/.mep, prompt for API keys, and build the database."""
    MEP_DIR.mkdir(parents=True, exist_ok=True)
    existing = {}
    if CONFIG_PATH.exists():
        try:
            existing = json.loads(CONFIG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            click.secho("(existing config was unreadable; starting fresh)", fg="yellow")

    youtube_key = click.prompt(
        "YouTube Data API v3 key (blank to skip; needed for channel ingest)",
        default=existing.get("YOUTUBE_API_KEY", ""),
        show_default=False,
    )
    llm_provider = click.prompt(
        "LLM provider (anthropic/openai; blank = auto-detect from keys)",
        default=existing.get("LLM_PROVIDER", ""),
        show_default=False,
    ).strip().lower()
    anthropic_key = click.prompt(
        "Anthropic API key (blank to skip)",
        default=existing.get("ANTHROPIC_API_KEY", ""),
        show_default=False,
    )
    openai_key = click.prompt(
        "OpenAI API key (blank to skip)",
        default=existing.get("OPENAI_API_KEY", ""),
        show_default=False,
    )

    CONFIG_PATH.write_text(
        json.dumps(
            {
                "YOUTUBE_API_KEY": youtube_key.strip(),
                "LLM_PROVIDER": llm_provider,
                "ANTHROPIC_API_KEY": anthropic_key.strip(),
                "OPENAI_API_KEY": openai_key.strip(),
            },
            indent=2,
        )
    )
    db.init_db()
    click.echo(f"Wrote config to {CONFIG_PATH}")
    click.echo(f"Initialized database at {DB_PATH}")


@cli.command()
@click.argument("source", required=False)
@click.option("--channel", "channel", help="YouTube channel @handle to ingest.")
@click.option("--text", "text", default=None, help="Ingest pasted recipe text directly.")
@click.option(
    "--image", "images", multiple=True, type=click.Path(exists=True, dir_okay=False),
    help="Recipe photo(s); repeat to combine pages/frames into one recipe.",
)
@click.option(
    "--pair", is_flag=True,
    help="Also suggest pairings and link to recipes you already have (extra call).",
)
@click.option("--limit", type=int, default=None, help="Max videos for --channel.")
def add(source, channel, text, images, pair, limit):
    """Ingest a recipe from a YouTube/web URL, an image or text file, or pasted text.

    SOURCE may be a YouTube URL, a recipe web page URL, or a path to an image or
    text file. Use --channel for a whole YouTube channel, --image for one or more
    recipe photos, or --text to paste a recipe.
    """
    config = load_config()
    db.init_db()
    conn = db.connect()

    if channel:
        any_seen = False
        for video_id, title, status, results in ingest.add_channel(
            conn, config, channel, limit
        ):
            any_seen = True
            if results:
                dish = results[0][1]
                extra = f" (+{len(results) - 1} more)" if len(results) > 1 else ""
                label = (dish or title or video_id) + extra
            else:
                label = title or video_id
            click.echo(f"  [{status:12}] {label}")
            if pair and status == "added":
                _pair_new(conn, config, results)
        if not any_seen:
            click.echo("No videos found for that channel.")
        return

    if images:
        status, results = ingest.add_images(conn, config, list(images))
    elif text:
        status, results = ingest.add_text(conn, config, text)
    elif source:
        status, results = ingest.add_source(conn, config, source)
    else:
        raise click.UsageError(
            "Provide a URL or file, --image, --text, or --channel <handle>."
        )

    _report_add(status, results)
    if pair and status == "added":
        _pair_new(conn, config, results)


def _pair_new(conn, config, results) -> None:
    for recipe_id, dish, _meal_type, _health in results:
        if dish:  # skip empty stubs
            click.echo(f"  finding pairings for {dish}...")
            ingest.pair_recipe(conn, config, recipe_id)


def _report_add(status, results) -> None:
    if status == "skipped":
        click.echo("Already in your collection.")
    elif status == "no_transcript":
        click.echo(f"Stored (no transcript available) as recipe {results[0][0]}.")
    elif len(results) == 1:
        recipe_id, dish, meal_type, health = results[0]
        click.echo(f"Added '{dish or 'untitled'}' as recipe {recipe_id}.")
        if meal_type or health is not None:
            click.echo(f"  (classified: {_classification_str(meal_type, health)})")
    else:
        click.echo(f"Added {len(results)} recipes:")
        for recipe_id, dish, meal_type, health in results:
            suffix = _classification_suffix(meal_type, health)
            click.echo(f"  {recipe_id:>4}  {dish or 'untitled'}{suffix}")


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
@click.option(
    "-t", "--type", "meal_type", type=click.Choice(classify.MEAL_TYPES), default=None,
    help="Only this meal type.",
)
@click.option(
    "--max-time", type=int, default=None,
    help="Only recipes that cook in this many minutes or less.",
)
@click.option("--limit", type=int, default=None, help="Max recipes to show.")
def list_cmd(tag, meal_type, max_time, limit):
    """Browse stored recipes, newest first."""
    conn = db.connect()
    rows = db.list_recipes(conn, tag=tag, meal_type=meal_type, limit=limit, max_time=max_time)
    if not rows:
        click.echo("No recipes yet. Add one with `mep add <url>`.")
        return
    for row in rows:
        name = row["dish_name"] or row["title"] or "(untitled)"
        click.echo(f"  {row['id']:>4}  {name}  —  {row['channel'] or 'unknown'}")


@cli.command(name="set-servings")
@click.argument("recipe_id", type=int)
@click.argument("servings")
def set_servings(recipe_id, servings):
    """Record or correct how many servings a recipe makes (stored verbatim)."""
    conn = db.connect()
    if db.get_recipe(conn, recipe_id) is None:
        raise MepError(f"No recipe with id {recipe_id}.")
    db.set_servings(conn, recipe_id, servings)
    click.echo(f"Recipe {recipe_id} now makes {servings} servings.")


@cli.command()
@click.argument("recipe_id", type=int, required=False)
@click.option("--all", "export_all", is_flag=True, help="Export every recipe as a JSON backup.")
@click.option("--json", "as_json", is_flag=True, help="Export one recipe as importable JSON instead of Markdown.")
@click.option(
    "-o", "--output", type=click.Path(dir_okay=False, writable=True), default=None,
    help="Write to a file instead of stdout.",
)
def export(recipe_id, export_all, as_json, output):
    """Export one recipe (Markdown, or importable JSON with --json), or the whole
    collection as JSON with --all. Anything JSON can be fed back to `mep import`."""
    conn = db.connect()
    if export_all:
        records = [_to_export(db.get_recipe(conn, rid)) for rid in db.all_recipe_ids(conn)]
        _emit(json.dumps(records, indent=2), output, f"Backed up {len(records)} recipe(s) to ")
        return
    if recipe_id is None:
        raise click.UsageError("Give a recipe id, or --all for a full backup.")
    data = db.get_recipe(conn, recipe_id)
    if data is None:
        raise MepError(f"No recipe with id {recipe_id}.")
    if as_json:
        _emit(json.dumps(_to_export(data), indent=2), output, "Wrote ")
    else:
        _emit(_to_markdown(data), output, "Wrote ", trailing_newline=False)


def _emit(payload, output, wrote_prefix, trailing_newline=True):
    """Write a payload to a file (with a confirmation) or stdout."""
    if output:
        Path(output).write_text(payload)
        click.echo(f"{wrote_prefix}{output}.")
    else:
        click.echo(payload, nl=trailing_newline)


@cli.command(name="import")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
def import_cmd(path):
    """Load recipes from `export` JSON (a single recipe or an `--all` backup),
    skipping any whose source you already have."""
    conn = db.connect()
    try:
        text = Path(path).read_text()
    except OSError as exc:
        raise MepError(f"Couldn't read {path}: {exc}")
    added = skipped = 0
    for record in _load_import_records(text):
        if isinstance(record, dict) and db.import_recipe(conn, record) is not None:
            added += 1
        else:
            skipped += 1
    click.echo(f"Imported {added} recipe(s); skipped {skipped} already present.")


def _load_import_records(text: str) -> list:
    """Parse import JSON into a list of records, accepting either a single recipe
    object or a list of them."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MepError(f"That isn't valid JSON: {exc}")
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data
    raise MepError("Expected a recipe object or a list of recipes.")


_ICLOUD_ROOT = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs"


@cli.command()
@click.argument("recipe_ids", type=int, nargs=-1, required=True)
@click.option("--ingredients", "only_ingredients", is_flag=True, help="Only include the ingredients list.")
@click.option("--plan", "only_plan", is_flag=True, help="Only include the step-by-step plan.")
def send(recipe_ids, only_ingredients, only_plan):
    """Write recipes to iCloud Drive so they appear in the Files app on your phone.

    Pass --ingredients or --plan to limit what's included; both by default.
    Files land in iCloud Drive > mep/.
    """
    if only_ingredients and only_plan:
        raise click.UsageError("--ingredients and --plan are mutually exclusive.")
    if not _ICLOUD_ROOT.exists():
        raise MepError(
            "iCloud Drive not found. Make sure iCloud Drive is enabled in "
            "System Settings > Apple ID > iCloud."
        )
    dest = _ICLOUD_ROOT / "mep"
    dest.mkdir(exist_ok=True)
    conn = db.connect()
    for rid in recipe_ids:
        data = db.get_recipe(conn, rid)
        if data is None:
            raise MepError(f"No recipe with id {rid}.")
        name = data["recipe"]["dish_name"] or data["recipe"]["title"] or f"recipe_{rid}"
        safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in name).strip()
        filename = f"{rid}_{safe}.md"
        (dest / filename).write_text(_to_send_markdown(data, only_ingredients, only_plan))
        click.echo(f"  {filename}")
    click.secho(f"Saved to iCloud Drive > mep/ ({len(recipe_ids)} file(s))", fg="green")


def _to_send_markdown(data: dict, ingredients_only: bool, plan_only: bool) -> str:
    r = data["recipe"]
    title = r["dish_name"] or r["title"] or "(untitled)"
    lines = [f"# {title}", ""]
    if not plan_only and data["ingredients"]:
        lines += ["## Ingredients", ""]
        lines += [f"- {_ingredient_line(ing)}" for ing in data["ingredients"]]
        lines.append("")
    if not ingredients_only and data["steps"]:
        lines += ["## Steps", ""]
        lines += [f"{s['step_number']}. {s['instruction']}" for s in data["steps"]]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


@cli.command()
@click.argument("recipe_id", type=int)
@click.option("-f", "--force", is_flag=True, help="Skip the confirmation prompt.")
def delete(recipe_id, force):
    """Delete a recipe and everything stored with it."""
    conn = db.connect()
    data = db.get_recipe(conn, recipe_id)
    if data is None:
        raise MepError(f"No recipe with id {recipe_id}.")
    name = data["recipe"]["dish_name"] or data["recipe"]["title"] or "(untitled)"
    if not force:
        click.confirm(f"Delete recipe {recipe_id} ({name})?", abort=True)
    db.delete_recipe(conn, recipe_id)
    click.echo(f"Deleted recipe {recipe_id} ({name}).")


@cli.command()
@click.argument("recipe_id", type=int)
@click.argument("stars", type=click.IntRange(1, 5))
def rate(recipe_id, stars):
    """Rate a recipe 1-5 (used by `discover --favorites`)."""
    conn = db.connect()
    if db.get_recipe(conn, recipe_id) is None:
        raise MepError(f"No recipe with id {recipe_id}.")
    db.set_rating(conn, recipe_id, stars)
    click.echo(f"Rated recipe {recipe_id} {'★' * stars}{'☆' * (5 - stars)}.")


@cli.command()
@click.argument("recipe_id", type=int)
@click.argument("text")
def note(recipe_id, text):
    """Add a dated note to a recipe (how it went, tweaks to try)."""
    conn = db.connect()
    if db.get_recipe(conn, recipe_id) is None:
        raise MepError(f"No recipe with id {recipe_id}.")
    db.add_note(conn, recipe_id, text)
    click.echo(f"Noted on recipe {recipe_id}.")


@cli.command(name="set-time")
@click.argument("recipe_id", type=int)
@click.argument("cook_time")
def set_time(recipe_id, cook_time):
    """Record or correct how long a recipe takes (stored verbatim; for --max-time)."""
    conn = db.connect()
    if db.get_recipe(conn, recipe_id) is None:
        raise MepError(f"No recipe with id {recipe_id}.")
    db.set_cook_time(conn, recipe_id, cook_time)
    click.echo(f"Recipe {recipe_id} now takes {cook_time}.")


@cli.command()
@click.argument("recipe_id", type=int)
def edit(recipe_id):
    """Fix a recipe by hand: opens its data as JSON in your $EDITOR."""
    conn = db.connect()
    data = db.get_recipe(conn, recipe_id)
    if data is None:
        raise MepError(f"No recipe with id {recipe_id}.")
    before = json.dumps(_to_editable(data), indent=2)
    edited = click.edit(before, extension=".json")
    if edited is None or edited.strip() == before.strip():
        click.echo("No changes.")
        return
    try:
        new = json.loads(edited)
    except json.JSONDecodeError as exc:
        raise MepError(f"That isn't valid JSON: {exc}")
    db.replace_recipe_content(conn, recipe_id, _validate_editable(new))
    click.echo(f"Updated recipe {recipe_id}. (Re-run classify/pair if you want them refreshed.)")


@cli.command()
@click.option("-n", "--limit", type=int, default=20, help="How many recent cooks to show.")
def history(limit):
    """Show what you've cooked recently."""
    conn = db.connect()
    rows = db.cook_history(conn, limit=max(1, limit))
    if not rows:
        click.echo("No cooks logged yet. Cook something with `mep cook <id>`.")
        return
    for row in rows:
        when = (row["cooked_at"] or "")[:16]
        click.echo(f"  {when}   {row['dish_name'] or '(untitled)'}  (#{row['recipe_id']})")


@cli.command(name="cook-now")
@click.option("-n", "--limit", type=int, default=10, help="How many recipes to show.")
def cook_now_cmd(limit):
    """Rank recipes by how little you'd need to buy, given your pantry."""
    conn = db.connect()
    if not db.pantry_list(conn):
        raise MepError("Your pantry is empty. Add items with `mep pantry add eggs milk ...`.")
    rows = db.cook_now(conn, limit=max(1, limit))
    if not rows:
        click.echo("No recipes with ingredients yet.")
        return
    click.echo()
    for r in rows:
        if not r["missing"]:
            tag = click.style("have everything!", fg="green")
        else:
            tag = "need " + ", ".join(r["missing"][:6])
            if len(r["missing"]) > 6:
                tag += f", +{len(r['missing']) - 6} more"
        click.echo(f"  {r['id']:>4}  {r['dish_name']:<28}  {tag}")
    click.echo()


@cli.group()
def pantry():
    """Manage the ingredients you keep on hand (for `cook-now`)."""


@pantry.command("add")
@click.argument("items", nargs=-1, required=True)
def pantry_add_cmd(items):
    """Add ingredients to your pantry."""
    conn = db.connect()
    added = db.pantry_add(conn, list(items))
    click.echo(f"Added {added} item(s). Pantry now has {len(db.pantry_list(conn))}.")


@pantry.command("remove")
@click.argument("items", nargs=-1, required=True)
def pantry_remove_cmd(items):
    """Remove ingredients from your pantry."""
    conn = db.connect()
    removed = db.pantry_remove(conn, list(items))
    click.echo(f"Removed {removed} item(s).")


@pantry.command("list")
def pantry_list_cmd():
    """Show everything in your pantry."""
    conn = db.connect()
    items = db.pantry_list(conn)
    if not items:
        click.echo("Pantry is empty. Add items with `mep pantry add eggs milk ...`.")
        return
    for item in items:
        click.echo(f"  - {item}")


@cli.command(name="shopping-list")
@click.argument("recipe_ids", type=int, nargs=-1, required=True)
def shopping_list(recipe_ids):
    """Combine one or more recipes into a single grocery shopping list."""
    config = load_config()
    conn = db.connect()
    recipes = []
    for rid in recipe_ids:
        data = db.get_recipe(conn, rid)
        if data is None:
            raise MepError(f"No recipe with id {rid}.")
        recipes.append(data)
    require_api_key(config)
    click.echo("Building shopping list...")
    sections = shopping.build_list(recipes, config=config)
    _render_shopping(recipes, sections)


@cli.command()
@click.option(
    "-t", "--type", "meal_type", type=click.Choice(classify.MEAL_TYPES), default=None,
    help="Only this meal type.",
)
@click.option("--healthy", is_flag=True, help="Only healthy meals (health score >= 7).")
@click.option("--indulgent", is_flag=True, help="Only indulgent meals (health score <= 4).")
@click.option("--min-health", type=click.IntRange(1, 10), default=None, help="Lowest health score.")
@click.option("--max-health", type=click.IntRange(1, 10), default=None, help="Highest health score.")
@click.option(
    "-i", "--ingredient", "ingredients", multiple=True,
    help="Must use this ingredient (repeatable).",
)
@click.option(
    "--max-time", type=int, default=None,
    help="Only recipes that cook in this many minutes or less.",
)
@click.option("--min-rating", type=click.IntRange(1, 5), default=None, help="Only recipes rated at least this.")
@click.option("--favorites", is_flag=True, help="Only your favorites (rated 4+).")
@click.option("-n", "--count", type=int, default=1, help="How many to pick (default 1).")
def discover(meal_type, healthy, indulgent, min_health, max_health, ingredients, max_time, min_rating, favorites, count):
    """Pick a random recipe by type, health, ingredients, time, or rating."""
    conn = db.connect()
    lo, hi = _health_range(healthy, indulgent, min_health, max_health)
    wants = [i.strip() for i in ingredients if i.strip()]
    rating = min_rating if min_rating is not None else (4 if favorites else None)
    rows = db.discover(
        conn, meal_type=meal_type, min_health=lo, max_health=hi,
        ingredients=wants, max_time=max_time, min_rating=rating, count=max(1, count),
    )
    if not rows:
        click.echo("No matching recipes.")
        _classify_hint(conn, meal_type, lo, hi)
        if max_time is not None:
            click.secho(
                "  (Recipes without a recorded cook time are excluded by --max-time.)",
                fg="yellow",
            )
        return
    if count == 1 and len(rows) == 1:
        _render(db.get_recipe(conn, rows[0]["id"]))
    else:
        _render_discover(rows)
    _classify_hint(conn, meal_type, lo, hi)


@cli.command(name="classify")
@click.option("--all", "reclassify", is_flag=True, help="Re-classify every recipe, not just new ones.")
def classify_cmd(reclassify):
    """Fill in meal type and health score for recipes (used by `discover`)."""
    config = load_config()
    conn = db.connect()
    ids = db.recipe_ids_for_classify(conn, include_classified=reclassify)
    if not ids:
        click.echo("All recipes are already classified.")
        return
    require_api_key(config)
    for rid in ids:
        data = db.get_recipe(conn, rid)
        cls = classify.classify_recipe(data, config=config)
        db.save_classification(conn, rid, cls["meal_type"], cls["health_score"])
        name = data["recipe"]["dish_name"] or data["recipe"]["title"] or "recipe"
        click.echo(f"  {rid:>4}  {name}  ->  {_classification_str(cls['meal_type'], cls['health_score'])}")
    click.echo(f"Classified {len(ids)} recipe(s).")


@cli.command(name="clarify")
@click.argument("recipe_ids", type=int, nargs=-1)
@click.option("--all", "do_all", is_flag=True, help="Clarify every recipe that has steps.")
def clarify_cmd(recipe_ids, do_all):
    """Rewrite stored steps to name the pots/pans they use (opt-in).

    Give recipe ids, or --all for the whole collection. New recipes already get
    cookware named at ingest; this backfills ones added earlier.
    """
    config = load_config()
    conn = db.connect()
    if recipe_ids:
        ids = list(recipe_ids)
        for rid in ids:
            if db.get_recipe(conn, rid) is None:
                raise MepError(f"No recipe with id {rid}.")
    elif do_all:
        ids = db.recipe_ids_with_steps(conn)
    else:
        raise click.UsageError("Give recipe ids, or --all.")
    require_api_key(config)
    changed = 0
    for rid in ids:
        data = db.get_recipe(conn, rid)
        if not data["steps"]:
            continue
        steps = cookware.clarify_steps(data, config=config)
        db.replace_steps(conn, rid, steps)
        name = data["recipe"]["dish_name"] or data["recipe"]["title"] or f"recipe {rid}"
        click.echo(f"  clarified {name}")
        changed += 1
    click.echo(f"Clarified {changed} recipe(s).")


@cli.command(name="pair")
@click.argument("recipe_ids", type=int, nargs=-1)
@click.option("--all", "repair", is_flag=True, help="Re-pair every recipe, not just unpaired ones.")
def pair_cmd(recipe_ids, repair):
    """Suggest pairings and build the 'goes well with' graph (opt-in).

    Give recipe ids, or use --all to (re)pair the whole collection. With no
    arguments, pairs recipes that don't have pairings yet.
    """
    config = load_config()
    conn = db.connect()
    if recipe_ids:
        ids = list(recipe_ids)
        for rid in ids:
            if db.get_recipe(conn, rid) is None:
                raise MepError(f"No recipe with id {rid}.")
    else:
        ids = db.recipe_ids_for_pairing(conn, include_paired=repair)
    if not ids:
        click.echo("All recipes already have pairings (use --all to redo).")
        return
    require_api_key(config)
    for rid in ids:
        name = db.get_recipe(conn, rid)["recipe"]["dish_name"] or f"recipe {rid}"
        click.echo(f"  pairing {name}...")
        ingest.pair_recipe(conn, config, rid)
    click.echo(f"Paired {len(ids)} recipe(s).")


@cli.command()
@click.argument("recipe_id", type=int)
@click.option("--servings", type=int, default=None, help="Scale ingredients to N servings.")
@click.option("--parts", is_flag=True, help="Show the recipe's component breakdown.")
@click.option("--macros", is_flag=True, help="Show an estimated nutrition breakdown.")
@click.option("--check", is_flag=True, help="Check for likely missing steps or gaps.")
def show(recipe_id, servings, parts, macros, check):
    """Display one recipe in full, its components with --parts, estimated
    nutrition with --macros, or likely gaps with --check."""
    config = load_config()
    db.init_db()
    conn = db.connect()
    data = db.get_recipe(conn, recipe_id)
    if data is None:
        raise MepError(f"No recipe with id {recipe_id}.")
    if parts:
        _render_parts(data["recipe"], _ensure_components(conn, config, data))
        return
    if macros:
        _render_macros(data["recipe"], _ensure_macros(conn, config, data))
        return
    if check:
        _render_gaps(data["recipe"], _ensure_gaps(conn, config, data))
        return
    _render(data, servings, _gather_pairings(conn, recipe_id), db.last_cooked(conn, recipe_id))


@cli.command()
@click.argument("recipe_id", type=int)
@click.option("--regenerate", is_flag=True, help="Rebuild the plan from scratch.")
@click.option("--servings", type=int, default=None, help="Scale ingredients to N servings.")
@click.option(
    "--with", "with_ids", multiple=True, type=int,
    help="Also cook this recipe in the same timeline (repeatable).",
)
def plan(recipe_id, regenerate, servings, with_ids):
    """Generate a cooking timeline for a recipe (experimental).

    Pass --with <id> (repeatable) to interleave a side or second dish into one
    combined timeline.
    """
    config = load_config()
    db.init_db()  # ensure plan_steps exists / is migrated on older databases
    conn = db.connect()
    data = db.get_recipe(conn, recipe_id)
    if data is None:
        raise MepError(f"No recipe with id {recipe_id}.")

    if with_ids:
        recipes = _load_combined(conn, recipe_id, with_ids)
        require_api_key(config)
        click.echo("Generating combined cook plan...")
        tasks = generate_combined_plan(recipes, config=config)
        _render_plan(_combined_recipe(recipes), tasks, _combined_gather(recipes))
        return

    tasks = _ensure_plan(conn, config, data, regenerate)
    gather, note = _gather_lines(data, servings)
    _render_plan(data["recipe"], tasks, gather, note)


@cli.command()
@click.argument("recipe_id", type=int)
@click.option("--servings", type=int, default=None, help="Scale ingredients to N servings.")
@click.option("--have", default=None, help="Parts you already have (comma-separated); adapts this cook only.")
@click.option("--sub", "subs", multiple=True, help='Ingredient swap "x=y" (repeatable).')
@click.option(
    "--with", "with_ids", multiple=True, type=int,
    help="Also cook this recipe in the same session (repeatable).",
)
@click.option(
    "--tui", is_flag=True,
    help="Full-screen cook-along that visualizes each pot/pan (needs the [tui] extra).",
)
def cook(recipe_id, servings, have, subs, with_ids, tui):
    """Walk through a recipe's timeline step by step (experimental).

    Pass --with <id> (repeatable) to cook a side or second dish in the same
    interleaved session, or --tui for a full-screen view of every pot and pan.
    """
    config = load_config()
    db.init_db()
    conn = db.connect()
    data = db.get_recipe(conn, recipe_id)
    if data is None:
        raise MepError(f"No recipe with id {recipe_id}.")

    if with_ids:
        if have or subs:
            raise click.UsageError("--with can't be combined with --have/--sub.")
        recipes = _load_combined(conn, recipe_id, with_ids)
        require_api_key(config)
        click.echo("Generating combined cook plan...")
        tasks = generate_combined_plan(recipes, config=config)
        if tui:
            tui_mod.run(_combined_recipe(recipes), tasks)
        else:
            cook_mod.run(
                _combined_recipe(recipes), tasks, gather_lines=_combined_gather(recipes)
            )
        for data in recipes:
            db.increment_cook_count(conn, data["recipe"]["id"])
        click.secho(f"Cooked {len(recipes)} dishes together.", fg="green")
        return

    have_list = _split_csv(have)
    sub_map = _collect_subs(subs)
    if have_list or sub_map:
        # Adapt in memory for this cook only; the cached plan belongs to the
        # original recipe, so generate a fresh (uncached) plan for the result.
        require_api_key(config)
        click.echo("Adapting for this cook...")
        adapted = adapt_mod.adapt_recipe(
            data, have=have_list, subs=sub_map, config=config
        )
        data = _adapted_data(data, adapted)
        if not data["steps"]:
            raise MepError("Nothing left to cook after adapting.")
        click.echo("Generating cook plan...")
        tasks = generate_plan(data, config=config)
    else:
        tasks = _ensure_plan(conn, config, data, regenerate=False)

    if tui:
        tui_mod.run(data["recipe"], tasks)
    else:
        gather, note = _gather_lines(data, servings)
        cook_mod.run(data["recipe"], tasks, gather_lines=gather, scale_note=note)
    total = db.increment_cook_count(conn, recipe_id)
    click.secho(f"Cooked {total}x total.", fg="green")


@cli.command()
@click.argument("recipe_id", type=int)
@click.option("--have", default=None, help="Parts you already have (comma-separated).")
@click.option("--sub", "subs", multiple=True, help='Ingredient swap "x=y" (repeatable).')
@click.option("-i", "--interactive", is_flag=True, help="Pick what you have from a list.")
def adapt(recipe_id, have, subs, interactive):
    """Rewrite a recipe around what you already have (experimental)."""
    config = load_config()
    db.init_db()
    conn = db.connect()
    data = db.get_recipe(conn, recipe_id)
    if data is None:
        raise MepError(f"No recipe with id {recipe_id}.")

    have_list = _split_csv(have)
    sub_map = _collect_subs(subs)
    if interactive or (not have_list and not sub_map):
        have_list, sub_map = _interactive_pick(conn, config, data, have_list, sub_map)
    if not have_list and not sub_map:
        click.echo("Nothing to adapt.")
        return

    click.echo("Adapting...")
    adapted = adapt_mod.adapt_recipe(
        data, have=have_list, subs=sub_map, config=config
    )
    _preview_adapt(data, adapted, have_list, sub_map)
    _save_adapted(conn, data, adapted)


def _ensure_plan(conn, config, data, regenerate):
    """Return cached tasks, generating and caching them if needed."""
    tasks = db.get_plan(conn, data["recipe"]["id"])
    if tasks and not regenerate:
        return tasks
    if not data["steps"]:
        raise MepError("This recipe has no steps to plan.")
    click.echo("Generating cook plan...")
    tasks = generate_plan(data, config=config)
    db.save_plan(conn, data["recipe"]["id"], tasks)
    return tasks


def _load_combined(conn, recipe_id, with_ids):
    """Validate the main recipe plus each --with id, returning their full
    db.get_recipe() dicts in order (main first). Each must exist and have steps."""
    ids = [recipe_id] + [i for i in with_ids if i != recipe_id]
    recipes = []
    for rid in ids:
        data = db.get_recipe(conn, rid)
        if data is None:
            raise MepError(f"No recipe with id {rid}.")
        if not data["steps"]:
            name = data["recipe"]["dish_name"] or data["recipe"]["title"] or f"recipe {rid}"
            raise MepError(f"'{name}' has no steps to cook.")
        recipes.append(data)
    if len(recipes) < 2:
        raise MepError("Give at least one different recipe with --with.")
    return recipes


def _combined_recipe(recipes: list[dict]) -> dict:
    """A recipe-shaped dict whose name is all the dishes joined, for headers."""
    names = [
        r["recipe"]["dish_name"] or r["recipe"]["title"] or "recipe" for r in recipes
    ]
    return {"dish_name": " + ".join(names), "title": None}


def _combined_gather(recipes: list[dict]) -> list[str]:
    """Merged mise-en-place across dishes, each line prefixed with its dish."""
    lines = []
    for r in recipes:
        name = r["recipe"]["dish_name"] or r["recipe"]["title"] or "recipe"
        for ing in r["ingredients"]:
            line = _ingredient_line(ing)
            if line:
                lines.append(f"[{name}] {line}")
    return lines


def _render_plan(recipe: dict, tasks: list[dict], gather=None, note=None) -> None:
    name = recipe["dish_name"] or recipe["title"] or "recipe"
    hands_on = sum(t["duration_minutes"] or 0 for t in tasks if t["mode"] == "active")
    total = cook_mod.estimate_wallclock_minutes(tasks)
    click.echo()
    click.secho(f"Cook plan: {name}", bold=True)
    if note:
        click.secho(f"  {note}", fg="yellow")
    click.echo(
        f"  hands-on ~{cook_mod.fmt_duration(hands_on)}"
        f"  ·  total ~{cook_mod.fmt_duration(total)}\n"
    )
    if gather:
        click.secho("  Gather:", underline=True)
        for line in gather:
            click.echo(f"    - {line}")
        click.echo()
    for i, task in enumerate(tasks, start=1):
        tag = f"{task['mode']} {cook_mod.fmt_duration(task['duration_minutes'])}"
        prefix = click.style(f"[{task['dish']}] ", fg="magenta") if task.get("dish") else ""
        click.echo(f"  {i:>2}  [{tag}]  {prefix}{task['instruction']}")
        if task.get("equipment"):
            click.secho(f"      equipment: {', '.join(task['equipment'])}", fg="blue")
        if task.get("overlap_hint"):
            click.echo(f"      └ {task['overlap_hint']}")
    click.echo()


def _ingredient_line(ing: dict) -> str:
    """One human-readable ingredient line from a structured row."""
    parts = [p for p in (ing.get("quantity"), ing.get("unit"), ing.get("name")) if p]
    line = " ".join(parts)
    if ing.get("prep"):
        line += f", {ing['prep']}"
    return line


def _gather_lines(data: dict, target_servings):
    """Build mise-en-place gather lines from a recipe's ingredients, optionally
    scaled to target_servings. Returns (lines, note). note is a yellow banner
    string when scaling applied, else None."""
    ingredients = data["ingredients"]
    factor = 1.0
    note = None
    if target_servings:
        # A recipe with no recorded serving count is treated as a single unit
        # (the batch as written), so --servings N simply makes N times the recipe.
        base_raw = scale.parse_base_servings(data["recipe"]["servings"])
        base = base_raw or 1
        if base != target_servings:
            factor = target_servings / base
            if base_raw:
                note = f"Scaled {base} → {target_servings} servings (×{_fmt_factor(factor)})."
            else:
                note = f"Scaled ×{target_servings} (1 serving = the full recipe)."
    lines = []
    for ing in ingredients:
        if factor != 1.0:
            ing = {**ing, "quantity": scale.scale_quantity(ing.get("quantity"), factor)}
        line = _ingredient_line(ing)
        if line:
            lines.append(line)
    return lines, note


def _fmt_factor(factor: float) -> str:
    return f"{factor:.2f}".rstrip("0").rstrip(".")


# --- adapt / components -------------------------------------------------------


def _split_csv(value):
    if not value:
        return []
    return [p.strip() for p in value.split(",") if p.strip()]


def _collect_subs(subs):
    """Merge one or more --sub values (each may itself be a comma list)."""
    out = {}
    for raw in subs:
        out.update(adapt_mod.parse_subs(raw))
    return out


def _ensure_components(conn, config, data):
    """Return the cached component breakdown, analyzing and caching if needed."""
    comps = db.get_components(conn, data["recipe"]["id"])
    if comps:
        return comps
    if not data["steps"]:
        raise MepError("This recipe has no steps to break down.")
    click.echo("Analyzing components...")
    comps = analyze_components(data, config=config)
    db.save_components(conn, data["recipe"]["id"], comps)
    return comps


def _render_parts(recipe: dict, comps: list[dict]) -> None:
    name = recipe["dish_name"] or recipe["title"] or "recipe"
    click.echo()
    click.secho(f"{name} — components", bold=True)
    click.echo()
    for i, c in enumerate(comps, start=1):
        head = f"  {click.style(str(i), fg='cyan')}  " + click.style(c["name"], bold=True)
        if c["purpose"]:
            head += f"  {c['purpose']}"
        click.echo(head)
        if c["ingredients"]:
            click.echo("       " + ", ".join(c["ingredients"]))
        if c["make_steps"]:
            steps = ", ".join(str(s) for s in c["make_steps"])
            click.echo(f"       made in steps {steps}")
        click.echo()


def _ensure_macros(conn, config, data):
    """Return the cached macro estimate, computing it lazily on first request."""
    cached = db.get_macros(conn, data["recipe"]["id"])
    if cached:
        return cached
    click.echo("Estimating macros...")
    result = estimate_macros(data, config=config)
    db.save_macros(conn, data["recipe"]["id"], result)
    return result


def _macro_line(m: dict) -> str:
    parts = []
    if m["calories"] is not None:
        parts.append(f"{round(m['calories'])} kcal")
    for key, label in (("protein_g", "protein"), ("carbs_g", "carbs"), ("fat_g", "fat")):
        if m[key] is not None:
            parts.append(f"{round(m[key])}g {label}")
    return " · ".join(parts)


def _render_macros(recipe: dict, est: dict) -> None:
    name = recipe["dish_name"] or recipe["title"] or "recipe"
    macros = est["macros"]
    click.echo()
    click.secho(f"{name} — estimated macros", bold=True)
    click.echo("  whole recipe:  " + _macro_line(macros))
    # Prefer the recorded serving count; fall back to the model's estimate.
    base = scale.parse_base_servings(recipe["servings"]) or est["servings"]
    if base and base >= 1:
        per = {k: (v / base if v is not None else None) for k, v in macros.items()}
        click.echo(f"  per serving (~{round(base)}):  " + _macro_line(per))
    if est["note"]:
        click.secho(f"  {est['note']}", fg="yellow")
    click.secho("  Estimated from ingredients — approximate, not exact.", fg="yellow")
    click.echo()


def _ensure_gaps(conn, config, data):
    """Return the cached gap list, computing it lazily on first request. None in
    the cache means never checked; [] means checked and nothing found."""
    cached = db.get_gaps(conn, data["recipe"]["id"])
    if cached is not None:
        return cached
    click.echo("Checking for gaps...")
    gaps = find_gaps(data, config=config)
    db.save_gaps(conn, data["recipe"]["id"], gaps)
    return gaps


def _render_gaps(recipe: dict, gaps: list[str]) -> None:
    name = recipe["dish_name"] or recipe["title"] or "recipe"
    click.echo()
    if not gaps:
        click.secho(f"{name} — no obvious gaps found.", bold=True)
        click.echo()
        return
    click.secho(f"{name} — possible gaps", bold=True)
    for g in gaps:
        click.echo(f"  - {g}")
    click.secho(
        "  Flagged from the transcript only — the detail may be shown on screen"
        " instead.",
        fg="yellow",
    )
    click.echo()


def _preview_ingredients(ings: list[str]) -> str:
    text = ", ".join(ings)
    return text if len(text) <= 42 else text[:39] + "..."


def _interactive_pick(conn, config, data, have_list, sub_map):
    comps = _ensure_components(conn, config, data)
    name = data["recipe"]["dish_name"] or data["recipe"]["title"] or "recipe"
    click.echo()
    click.secho(f"{name} — what do you already have?", bold=True)
    click.echo()
    for i, c in enumerate(comps, start=1):
        n = len(c["make_steps"])
        steps = f"{n} step" + ("" if n == 1 else "s")
        num = click.style(f"{i}", fg="cyan")
        click.echo(f"  {num}  {c['name']:<14} {_preview_ingredients(c['ingredients'])}  · {steps}")
    click.echo()
    picks = click.prompt(
        "Have any already? numbers, e.g. 1,4  (Enter = none)", default="", show_default=False
    )
    for idx in adapt_mod.parse_selection(picks, len(comps)):
        if comps[idx]["name"] not in have_list:
            have_list.append(comps[idx]["name"])
    swaps = click.prompt(
        "Any swaps? ingredient=replacement  (Enter = none)", default="", show_default=False
    )
    sub_map.update(adapt_mod.parse_subs(swaps))
    return have_list, sub_map


def _preview_adapt(data, adapted, have_list, sub_map):
    click.echo()
    if have_list:
        click.secho("  cut:   " + ", ".join(have_list), fg="red")
    if sub_map:
        swaps = ", ".join(f"{k} -> {v}" for k, v in sub_map.items())
        click.secho("  swaps: " + swaps, fg="yellow")
    old_i = len(data["ingredients"])
    new_i = len([x for x in (adapted.get("ingredients") or []) if x.get("name")])
    old_s = len(data["steps"])
    new_s = len([x for x in (adapted.get("steps") or []) if x])
    name = adapted.get("dish_name") or data["recipe"]["dish_name"] or "recipe"
    click.secho(
        f"  {name} (adapted)   {old_i} -> {new_i} ingredients · {old_s} -> {new_s} steps",
        bold=True,
    )
    click.echo()


def _save_adapted(conn, data, adapted):
    choice = (
        click.prompt("Save?  [c] copy   [o] overwrite   [d] discard", default="c")
        .strip()
        .lower()[:1]
    )
    if choice == "o":
        db.replace_recipe_content(conn, data["recipe"]["id"], adapted)
        click.echo(f"Overwrote recipe {data['recipe']['id']}.")
    elif choice == "d":
        click.echo("Discarded.")
    else:
        r = data["recipe"]
        video_id = db.next_adapted_video_id(conn, r["video_id"])
        title = (r["title"] or r["dish_name"] or "recipe") + " (adapted)"
        new_id = db.insert_recipe(
            conn,
            video_id=video_id,
            title=title,
            channel=r["channel"],
            url=r["url"],
            raw_transcript=None,
            extracted=adapted,
        )
        click.echo(f"Saved adapted copy as recipe {new_id}.")


def _adapted_data(orig: dict, adapted: dict) -> dict:
    """Build a db.get_recipe-shaped dict from an adapted recipe, reusing the
    original's identity fields. In-memory only; nothing is written."""
    recipe = dict(orig["recipe"])
    for field in ("dish_name", "cook_time", "servings", "difficulty"):
        if adapted.get(field) is not None:
            recipe[field] = adapted.get(field)
    ingredients = [
        {
            "name": ing.get("name"),
            "quantity": ing.get("quantity"),
            "unit": ing.get("unit"),
            "prep": ing.get("prep"),
        }
        for ing in (adapted.get("ingredients") or [])
        if ing.get("name")
    ]
    steps = [
        {"step_number": i, "instruction": text}
        for i, text in enumerate(adapted.get("steps") or [], start=1)
        if text
    ]
    tags = [t for t in (adapted.get("tags") or []) if t]
    return {"recipe": recipe, "ingredients": ingredients, "steps": steps, "tags": tags}


def _classification_str(meal_type, health) -> str:
    bits = []
    if meal_type:
        bits.append(meal_type)
    if health is not None:
        bits.append(f"health {health}")
    return ", ".join(bits) if bits else "unclassified"


def _classification_suffix(meal_type, health) -> str:
    bits = [b for b in (meal_type, f"health {health}" if health is not None else None) if b]
    return ("   " + " · ".join(bits)) if bits else ""


def _health_range(healthy, indulgent, min_health, max_health):
    """Resolve the health filter. Explicit --min/--max win; otherwise --healthy
    means >= 7 and --indulgent means <= 4."""
    lo = min_health if min_health is not None else (7 if healthy else None)
    hi = max_health if max_health is not None else (4 if indulgent else None)
    return lo, hi


def _render_discover(rows) -> None:
    click.echo()
    for r in rows:
        name = r["dish_name"] or r["title"] or "(untitled)"
        suffix = _classification_suffix(r["meal_type"], r["health_score"])
        click.echo(f"  {r['id']:>4}  {name}{suffix}  —  {r['channel'] or 'unknown'}")
    click.echo()


def _classify_hint(conn, meal_type, lo, hi) -> None:
    """Nudge the user to backfill when a type/health filter could be hiding
    recipes that simply haven't been classified yet."""
    if not (meal_type or lo is not None or hi is not None):
        return
    n = len(db.recipe_ids_for_classify(conn, include_classified=False))
    if n:
        click.secho(
            f"  ({n} recipe(s) not yet classified — run `mep classify` to include them.)",
            fg="yellow",
        )


def _to_markdown(data: dict) -> str:
    """Render a recipe as a portable Markdown card. Pure formatting."""
    r = data["recipe"]
    title = r["dish_name"] or r["title"] or "(untitled)"
    lines = [f"# {title}", ""]

    meta = []
    if r["channel"]:
        meta.append(r["channel"])
    if r["rating"]:
        meta.append("★" * r["rating"] + "☆" * (5 - r["rating"]))
    for field in ("cook_time", "servings", "difficulty"):
        if r[field]:
            meta.append(f"{field.replace('_', ' ')}: {r[field]}")
    if r["times_cooked"]:
        meta.append(f"cooked {r['times_cooked']}x")
    if meta:
        lines += ["*" + " · ".join(meta) + "*", ""]
    if r["url"]:
        lines += [r["url"], ""]
    if data["tags"]:
        lines += ["Tags: " + ", ".join(data["tags"]), ""]

    if data["ingredients"]:
        lines += ["## Ingredients", ""]
        lines += [f"- {_ingredient_line(ing)}" for ing in data["ingredients"]]
        lines.append("")
    if data["steps"]:
        lines += ["## Steps", ""]
        lines += [f"{s['step_number']}. {s['instruction']}" for s in data["steps"]]
        lines.append("")
    if r["notes"]:
        lines += ["## Notes", ""]
        lines += [r["notes"], ""]

    return "\n".join(lines).rstrip() + "\n"


def _to_export(data: dict) -> dict:
    """A recipe as a self-contained JSON record: extracted content plus the saved
    metadata (rating, notes, classification, cook count). Round-trips through
    db.import_recipe. Caches (macros/gaps/plan/pairings) are intentionally left
    out — they regenerate on demand."""
    r = data["recipe"]
    return {
        "video_id": r["video_id"], "title": r["title"], "channel": r["channel"],
        "url": r["url"], "source_type": r["source_type"],
        "dish_name": r["dish_name"], "cook_time": r["cook_time"],
        "servings": r["servings"], "difficulty": r["difficulty"],
        "ingredients": data["ingredients"],
        "steps": [s["instruction"] for s in data["steps"]],
        "tags": data["tags"],
        "rating": r["rating"], "notes": r["notes"],
        "meal_type": r["meal_type"], "health_score": r["health_score"],
        "times_cooked": r["times_cooked"],
    }


def _to_editable(data: dict) -> dict:
    """The hand-editable subset of a recipe (content only) for `mep edit`."""
    r = data["recipe"]
    return {
        "dish_name": r["dish_name"], "cook_time": r["cook_time"],
        "servings": r["servings"], "difficulty": r["difficulty"],
        "ingredients": [
            {"name": i["name"], "quantity": i["quantity"], "unit": i["unit"], "prep": i["prep"]}
            for i in data["ingredients"]
        ],
        "steps": [s["instruction"] for s in data["steps"]],
        "tags": data["tags"],
    }


def _validate_editable(new: dict) -> dict:
    """Coerce edited JSON back into the extracted shape replace_recipe_content
    expects, dropping ingredients without a name and blank steps/tags."""
    if not isinstance(new, dict):
        raise MepError("Expected a JSON object with the recipe's fields.")
    ingredients = []
    for item in new.get("ingredients") or []:
        if isinstance(item, dict) and _opt_str(item.get("name")):
            ingredients.append({
                "name": _opt_str(item.get("name")),
                "quantity": _opt_str(item.get("quantity")),
                "unit": _opt_str(item.get("unit")),
                "prep": _opt_str(item.get("prep")),
            })
    return {
        "dish_name": _opt_str(new.get("dish_name")),
        "cook_time": _opt_str(new.get("cook_time")),
        "servings": _opt_str(new.get("servings")),
        "difficulty": _opt_str(new.get("difficulty")),
        "ingredients": ingredients,
        "steps": [str(s).strip() for s in (new.get("steps") or []) if str(s).strip()],
        "tags": [str(t).strip() for t in (new.get("tags") or []) if str(t).strip()],
    }


def _opt_str(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _render_shopping(recipes: list[dict], sections: list[dict]) -> None:
    names = [
        r["recipe"]["dish_name"] or r["recipe"]["title"] or "recipe" for r in recipes
    ]
    plural = "" if len(recipes) == 1 else "s"
    click.echo()
    click.secho(f"Shopping list — {len(recipes)} recipe{plural}", bold=True)
    click.echo("  " + ", ".join(names))
    click.echo()
    for sec in sections:
        click.secho(f"  {sec['aisle']}", underline=True)
        for item in sec["items"]:
            click.echo(f"    - {item}")
        click.echo()
    click.secho(
        "  Combined amounts are estimates — double-check before you shop.", fg="yellow"
    )
    click.echo()


def _gather_pairings(conn, recipe_id):
    """Collect a recipe's pairings for display, or None if it has none."""
    generic = db.get_pairings(conn, recipe_id)
    edges = db.get_pairing_edges(conn, recipe_id)
    if not generic and not edges:
        return None
    return {"generic": generic or [], "edges": edges}


def _render(data: dict, target_servings=None, pairings=None, last_cooked=None) -> None:
    r = data["recipe"]
    title = r["dish_name"] or r["title"] or "(untitled)"
    click.echo()
    click.secho(title, bold=True)

    meta = []
    if r["channel"]:
        meta.append(r["channel"])
    source_label = {"web": "web", "text": "pasted", "image": "photo"}.get(r["source_type"])
    if source_label:
        meta.append(source_label)
    if r["rating"]:
        meta.append("★" * r["rating"] + "☆" * (5 - r["rating"]))
    if r["meal_type"]:
        meta.append(r["meal_type"])
    if r["health_score"] is not None:
        meta.append(f"health {r['health_score']}/10")
    for field in ("cook_time", "servings", "difficulty"):
        if r[field]:
            meta.append(f"{field.replace('_', ' ')}: {r[field]}")
    if r["times_cooked"]:
        cooked = f"cooked {r['times_cooked']}x"
        if last_cooked:
            cooked += f", last {last_cooked[:10]}"
        meta.append(cooked)
    if meta:
        click.echo("  " + "  |  ".join(meta))
    if r["url"]:
        click.echo(f"  {r['url']}")

    if data["tags"]:
        click.echo("  tags: " + ", ".join(data["tags"]))

    if data["ingredients"]:
        gather, note = _gather_lines(data, target_servings)
        click.echo()
        click.secho("Ingredients", underline=True)
        if note:
            click.secho(f"  {note}", fg="yellow")
        for line in gather:
            click.echo(f"  - {line}")

    if data["steps"]:
        click.echo()
        click.secho("Steps", underline=True)
        for step in data["steps"]:
            click.echo(f"  {step['step_number']}. {step['instruction']}")

    if pairings:
        if pairings["generic"]:
            click.echo()
            click.secho("Serve with", underline=True)
            for item in pairings["generic"]:
                why = f" ({item['why']})" if item.get("why") else ""
                click.echo(f"  - {item['name']}{why}")
        if pairings["edges"]:
            click.echo()
            click.secho("Pairs with (from your collection)", underline=True)
            for edge in pairings["edges"]:
                why = f" ({edge['reason']})" if edge.get("reason") else ""
                name = edge["dish_name"] or f"recipe {edge['id']}"
                click.echo(f"  {edge['id']:>4}  {name}{why}")

    if r["notes"]:
        click.echo()
        click.secho("Notes", underline=True)
        for line in r["notes"].splitlines():
            click.echo(f"  {line}")

    if not data["ingredients"] and not data["steps"]:
        click.echo()
        click.echo("  (No recipe was extracted from this video.)")
    click.echo()


def main():
    try:
        migrate_legacy_home()
        cli(prog_name="mep", standalone_mode=False)
    except MepError as exc:
        click.secho(f"Error: {exc}", fg="red", err=True)
        raise SystemExit(1)
    except click.ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code)
    except click.exceptions.Abort:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
