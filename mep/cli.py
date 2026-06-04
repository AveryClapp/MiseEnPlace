"""Click entry points for mep."""

import json
from pathlib import Path

import click

from . import adapt as adapt_mod
from . import cook as cook_mod
from . import db, ingest, scale, shopping
from .components import analyze_components
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
from .plan import generate_plan


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
        existing = json.loads(CONFIG_PATH.read_text())

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
@click.argument("recipe_id", type=int)
@click.option(
    "-o", "--output", type=click.Path(dir_okay=False, writable=True), default=None,
    help="Write to a file instead of stdout.",
)
def export(recipe_id, output):
    """Export a recipe as Markdown (to stdout, or a file with -o)."""
    conn = db.connect()
    data = db.get_recipe(conn, recipe_id)
    if data is None:
        raise MepError(f"No recipe with id {recipe_id}.")
    md = _to_markdown(data)
    if output:
        Path(output).write_text(md)
        click.echo(f"Wrote {output}.")
    else:
        click.echo(md, nl=False)


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
@click.argument("recipe_id", type=int)
@click.option("--servings", type=int, default=None, help="Scale ingredients to N servings.")
@click.option("--parts", is_flag=True, help="Show the recipe's component breakdown.")
@click.option("--macros", is_flag=True, help="Show an estimated nutrition breakdown.")
def show(recipe_id, servings, parts, macros):
    """Display one recipe in full, its components with --parts, or estimated
    nutrition with --macros."""
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
    _render(data, servings)


@cli.command()
@click.argument("recipe_id", type=int)
@click.option("--regenerate", is_flag=True, help="Rebuild the plan from scratch.")
@click.option("--servings", type=int, default=None, help="Scale ingredients to N servings.")
def plan(recipe_id, regenerate, servings):
    """Generate a cooking timeline for a recipe (experimental)."""
    config = load_config()
    db.init_db()  # ensure plan_steps exists / is migrated on older databases
    conn = db.connect()
    data = db.get_recipe(conn, recipe_id)
    if data is None:
        raise MepError(f"No recipe with id {recipe_id}.")
    tasks = _ensure_plan(conn, config, data, regenerate)
    gather, note = _gather_lines(data, servings)
    _render_plan(data["recipe"], tasks, gather, note)


@cli.command()
@click.argument("recipe_id", type=int)
@click.option("--servings", type=int, default=None, help="Scale ingredients to N servings.")
@click.option("--have", default=None, help="Parts you already have (comma-separated); adapts this cook only.")
@click.option("--sub", "subs", multiple=True, help='Ingredient swap "x=y" (repeatable).')
def cook(recipe_id, servings, have, subs):
    """Walk through a recipe's timeline step by step (experimental)."""
    config = load_config()
    db.init_db()
    conn = db.connect()
    data = db.get_recipe(conn, recipe_id)
    if data is None:
        raise MepError(f"No recipe with id {recipe_id}.")

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
        click.echo(f"  {i:>2}  [{tag}]  {task['instruction']}")
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


def _to_markdown(data: dict) -> str:
    """Render a recipe as a portable Markdown card. Pure formatting."""
    r = data["recipe"]
    title = r["dish_name"] or r["title"] or "(untitled)"
    lines = [f"# {title}", ""]

    meta = []
    if r["channel"]:
        meta.append(r["channel"])
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

    return "\n".join(lines).rstrip() + "\n"


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


def _render(data: dict, target_servings=None) -> None:
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
    if r["times_cooked"]:
        meta.append(f"cooked {r['times_cooked']}x")
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
