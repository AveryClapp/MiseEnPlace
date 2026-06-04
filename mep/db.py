"""SQLite storage: schema, inserts, FTS5 search, and read queries.

No ORM. Connections set row_factory to sqlite3.Row so callers get dict-like
rows, and enable foreign keys so deleting a recipe cascades to its children.
"""

import json
import sqlite3

from .config import DB_PATH

# Columns added after the first release; backfilled onto existing databases.
_PLAN_COLUMNS = {
    "ingredients_json": "TEXT",
    "equipment_json": "TEXT",
    "timer_label": "TEXT",
}
_RECIPE_COLUMNS = {
    "times_cooked": "INTEGER NOT NULL DEFAULT 0",
    "macros_json": "TEXT",
    "gaps_json": "TEXT",
    "meal_type": "TEXT",
    "health_score": "INTEGER",
    "source_type": "TEXT",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id       TEXT UNIQUE NOT NULL,
    title          TEXT,
    channel        TEXT,
    url            TEXT,
    dish_name      TEXT,
    cook_time      TEXT,
    servings       TEXT,
    difficulty     TEXT,
    raw_transcript TEXT,
    created_at     TEXT DEFAULT (datetime('now')),
    times_cooked   INTEGER NOT NULL DEFAULT 0,
    macros_json    TEXT,
    gaps_json      TEXT,
    meal_type      TEXT,
    health_score   INTEGER,
    source_type    TEXT
);

CREATE TABLE IF NOT EXISTS ingredients (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    name      TEXT,
    quantity  TEXT,
    unit      TEXT,
    prep      TEXT
);

CREATE TABLE IF NOT EXISTS steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id   INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    step_number INTEGER,
    instruction TEXT
);

CREATE TABLE IF NOT EXISTS tags (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    tag       TEXT
);

-- Cached AI-generated cooking timeline, one ordered set of tasks per recipe.
CREATE TABLE IF NOT EXISTS plan_steps (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id        INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    position         INTEGER,
    instruction      TEXT,
    duration_minutes REAL,
    mode             TEXT,
    overlap_hint     TEXT,
    ingredients_json TEXT,
    equipment_json   TEXT,
    timer_label      TEXT
);

-- Cached AI breakdown of a recipe into its components (marinade, pita, etc.).
CREATE TABLE IF NOT EXISTS recipe_components (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id        INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    position         INTEGER,
    name             TEXT,
    purpose          TEXT,
    ingredients_json TEXT,
    make_steps_json  TEXT
);

-- Contentless FTS index keyed by rowid = recipes.id.
CREATE VIRTUAL TABLE IF NOT EXISTS recipe_fts USING fts5(
    dish_name, channel, ingredients, content=''
);
"""


_schema_ready = False


def connect() -> sqlite3.Connection:
    """Open a connection, ensuring the schema exists and is migrated once per
    process. Self-healing, so every command works on an older database, not only
    the ones that happen to call init_db()."""
    global _schema_ready
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not _schema_ready:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
        _schema_ready = True
    return conn


def init_db() -> None:
    """Explicit setup entry point (used by `mep init`). connect() already ensures
    the schema, so this just triggers that once."""
    connect().close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created."""
    for table, columns in (("plan_steps", _PLAN_COLUMNS), ("recipes", _RECIPE_COLUMNS)):
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    # Recipes predate the multi-source feature; they were all from YouTube. New
    # inserts always set source_type, so the only NULLs are these legacy rows.
    conn.execute("UPDATE recipes SET source_type = 'youtube' WHERE source_type IS NULL")


def video_exists(conn: sqlite3.Connection, video_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM recipes WHERE video_id = ?", (video_id,)
    ).fetchone()
    return row is not None


def set_servings(conn: sqlite3.Connection, recipe_id: int, servings: str) -> None:
    """Record a recipe's serving count, stored verbatim like any other field."""
    with conn:
        conn.execute(
            "UPDATE recipes SET servings = ? WHERE id = ?", (servings, recipe_id)
        )


def save_macros(conn: sqlite3.Connection, recipe_id: int, macros: dict) -> None:
    """Cache a recipe's estimated nutrition (computed lazily on first request)."""
    with conn:
        conn.execute(
            "UPDATE recipes SET macros_json = ? WHERE id = ?",
            (json.dumps(macros), recipe_id),
        )


def get_macros(conn: sqlite3.Connection, recipe_id: int) -> dict | None:
    """Return the cached macro estimate, or None if not computed yet."""
    row = conn.execute(
        "SELECT macros_json FROM recipes WHERE id = ?", (recipe_id,)
    ).fetchone()
    if row is None or row["macros_json"] is None:
        return None
    return json.loads(row["macros_json"])


def save_gaps(conn: sqlite3.Connection, recipe_id: int, gaps: list) -> None:
    """Cache a recipe's gap check. An empty list is stored as a real result
    (checked, nothing found), distinct from NULL (never checked)."""
    with conn:
        conn.execute(
            "UPDATE recipes SET gaps_json = ? WHERE id = ?",
            (json.dumps(gaps), recipe_id),
        )


def get_gaps(conn: sqlite3.Connection, recipe_id: int) -> list | None:
    """Return the cached gap list, or None if the recipe was never checked.
    A returned [] means it was checked and looked complete."""
    row = conn.execute(
        "SELECT gaps_json FROM recipes WHERE id = ?", (recipe_id,)
    ).fetchone()
    if row is None or row["gaps_json"] is None:
        return None
    return json.loads(row["gaps_json"])


def save_classification(
    conn: sqlite3.Connection, recipe_id: int, meal_type: str | None, health_score: int | None
) -> None:
    """Store a recipe's meal type and 1-10 health score (either may be None)."""
    with conn:
        conn.execute(
            "UPDATE recipes SET meal_type = ?, health_score = ? WHERE id = ?",
            (meal_type, health_score, recipe_id),
        )


def recipe_ids_for_classify(
    conn: sqlite3.Connection, include_classified: bool = False
) -> list[int]:
    """Ids of real recipes (a dish_name) to classify. By default only those not
    yet classified (meal_type IS NULL); with include_classified, all of them."""
    sql = "SELECT id FROM recipes WHERE dish_name IS NOT NULL"
    if not include_classified:
        sql += " AND meal_type IS NULL"
    sql += " ORDER BY id"
    return [r["id"] for r in conn.execute(sql)]


def increment_cook_count(conn: sqlite3.Connection, recipe_id: int) -> int:
    """Bump a recipe's cooked counter by one and return the new total."""
    with conn:
        conn.execute(
            "UPDATE recipes SET times_cooked = times_cooked + 1 WHERE id = ?",
            (recipe_id,),
        )
    row = conn.execute(
        "SELECT times_cooked FROM recipes WHERE id = ?", (recipe_id,)
    ).fetchone()
    return row["times_cooked"] if row else 0


def insert_recipe(
    conn: sqlite3.Connection,
    *,
    video_id: str,
    title: str | None,
    channel: str | None,
    url: str | None,
    raw_transcript: str | None,
    extracted: dict,
    source_type: str = "youtube",
) -> int:
    """Insert a recipe and its children. `extracted` is the parsed model dict
    (or a minimal stub for non-recipe / no-transcript sources). `video_id` is the
    source's stable id (a YouTube id, a normalized URL, or a text hash). Returns
    the new recipe id. Runs in a single transaction."""
    with conn:
        cur = conn.execute(
            """INSERT INTO recipes
               (video_id, title, channel, url, dish_name, cook_time,
                servings, difficulty, raw_transcript, source_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                video_id,
                title,
                channel,
                url,
                extracted.get("dish_name"),
                extracted.get("cook_time"),
                extracted.get("servings"),
                extracted.get("difficulty"),
                raw_transcript,
                source_type,
            ),
        )
        recipe_id = cur.lastrowid
        _insert_children(conn, recipe_id, channel, extracted)

    return recipe_id


def _insert_children(
    conn: sqlite3.Connection, recipe_id: int, channel: str | None, extracted: dict
) -> None:
    """Insert a recipe's ingredients, steps, tags, and FTS row. Assumes any
    prior children/FTS for this recipe_id have already been removed."""
    ingredients = extracted.get("ingredients") or []
    steps = extracted.get("steps") or []
    tags = extracted.get("tags") or []

    conn.executemany(
        "INSERT INTO ingredients (recipe_id, name, quantity, unit, prep)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (recipe_id, ing.get("name"), ing.get("quantity"), ing.get("unit"), ing.get("prep"))
            for ing in ingredients
            if ing.get("name")
        ],
    )
    conn.executemany(
        "INSERT INTO steps (recipe_id, step_number, instruction) VALUES (?, ?, ?)",
        [(recipe_id, i, text) for i, text in enumerate(steps, start=1) if text],
    )
    conn.executemany(
        "INSERT INTO tags (recipe_id, tag) VALUES (?, ?)",
        [(recipe_id, t) for t in tags if t],
    )
    ingredient_blob = " ".join(
        ing.get("name", "") for ing in ingredients if ing.get("name")
    )
    conn.execute(
        "INSERT INTO recipe_fts (rowid, dish_name, channel, ingredients) VALUES (?, ?, ?, ?)",
        (recipe_id, extracted.get("dish_name") or "", channel or "", ingredient_blob),
    )


def get_recipe(conn: sqlite3.Connection, recipe_id: int) -> dict | None:
    recipe = conn.execute(
        "SELECT * FROM recipes WHERE id = ?", (recipe_id,)
    ).fetchone()
    if recipe is None:
        return None
    ingredients = conn.execute(
        "SELECT name, quantity, unit, prep FROM ingredients WHERE recipe_id = ?"
        " ORDER BY id",
        (recipe_id,),
    ).fetchall()
    steps = conn.execute(
        "SELECT step_number, instruction FROM steps WHERE recipe_id = ?"
        " ORDER BY step_number",
        (recipe_id,),
    ).fetchall()
    tags = conn.execute(
        "SELECT tag FROM tags WHERE recipe_id = ? ORDER BY tag", (recipe_id,)
    ).fetchall()
    return {
        "recipe": dict(recipe),
        "ingredients": [dict(r) for r in ingredients],
        "steps": [dict(r) for r in steps],
        "tags": [r["tag"] for r in tags],
    }


def search(conn: sqlite3.Connection, query: str) -> list[sqlite3.Row]:
    """FTS5 search over dish_name, channel, and ingredients. Falls back to a
    quoted phrase if the raw query is not valid FTS syntax."""
    sql = (
        "SELECT r.id, r.dish_name, r.channel, r.title FROM recipe_fts f"
        " JOIN recipes r ON r.id = f.rowid"
        " WHERE recipe_fts MATCH ? ORDER BY rank"
    )
    try:
        return conn.execute(sql, (query,)).fetchall()
    except sqlite3.OperationalError:
        phrase = '"' + query.replace('"', " ") + '"'
        return conn.execute(sql, (phrase,)).fetchall()


def save_plan(conn: sqlite3.Connection, recipe_id: int, tasks: list[dict]) -> None:
    """Replace any cached plan for the recipe with these ordered tasks."""
    with conn:
        conn.execute("DELETE FROM plan_steps WHERE recipe_id = ?", (recipe_id,))
        conn.executemany(
            "INSERT INTO plan_steps"
            " (recipe_id, position, instruction, duration_minutes, mode,"
            "  overlap_hint, ingredients_json, equipment_json, timer_label)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    recipe_id,
                    i,
                    task.get("instruction"),
                    task.get("duration_minutes"),
                    task.get("mode") or "active",
                    task.get("overlap_hint"),
                    json.dumps(task.get("ingredients") or []),
                    json.dumps(task.get("equipment") or []),
                    task.get("timer_label"),
                )
                for i, task in enumerate(tasks)
            ],
        )


def get_plan(conn: sqlite3.Connection, recipe_id: int) -> list[dict]:
    """Return the cached plan tasks in order, or [] if none."""
    rows = conn.execute(
        "SELECT instruction, duration_minutes, mode, overlap_hint,"
        " ingredients_json, equipment_json, timer_label"
        " FROM plan_steps WHERE recipe_id = ? ORDER BY position",
        (recipe_id,),
    ).fetchall()
    return [
        {
            "instruction": r["instruction"],
            "duration_minutes": r["duration_minutes"],
            "mode": r["mode"],
            "overlap_hint": r["overlap_hint"],
            "ingredients": json.loads(r["ingredients_json"] or "[]"),
            "equipment": json.loads(r["equipment_json"] or "[]"),
            "timer_label": r["timer_label"],
        }
        for r in rows
    ]


def save_components(
    conn: sqlite3.Connection, recipe_id: int, components: list[dict]
) -> None:
    """Replace any cached component breakdown for the recipe."""
    with conn:
        conn.execute("DELETE FROM recipe_components WHERE recipe_id = ?", (recipe_id,))
        conn.executemany(
            "INSERT INTO recipe_components"
            " (recipe_id, position, name, purpose, ingredients_json, make_steps_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    recipe_id,
                    i,
                    comp.get("name"),
                    comp.get("purpose"),
                    json.dumps(comp.get("ingredients") or []),
                    json.dumps(comp.get("make_steps") or []),
                )
                for i, comp in enumerate(components)
            ],
        )


def get_components(conn: sqlite3.Connection, recipe_id: int) -> list[dict]:
    """Return the cached component breakdown in order, or [] if none."""
    rows = conn.execute(
        "SELECT name, purpose, ingredients_json, make_steps_json"
        " FROM recipe_components WHERE recipe_id = ? ORDER BY position",
        (recipe_id,),
    ).fetchall()
    return [
        {
            "name": r["name"],
            "purpose": r["purpose"],
            "ingredients": json.loads(r["ingredients_json"] or "[]"),
            "make_steps": json.loads(r["make_steps_json"] or "[]"),
        }
        for r in rows
    ]


def next_adapted_video_id(conn: sqlite3.Connection, base_video_id: str) -> str:
    """A unique synthetic video_id for an adapted copy of base_video_id."""
    candidate = f"{base_video_id}~adapted"
    n = 1
    while video_exists(conn, candidate):
        n += 1
        candidate = f"{base_video_id}~adapted{n}"
    return candidate


def replace_recipe_content(
    conn: sqlite3.Connection, recipe_id: int, extracted: dict
) -> None:
    """Overwrite a recipe's ingredients, steps, tags, and descriptive fields
    in place (keeping its id, video_id, channel, url, transcript). Refreshes the
    FTS row and clears the now-stale cached plan and component breakdown."""
    with conn:
        old = conn.execute(
            "SELECT dish_name, channel FROM recipes WHERE id = ?", (recipe_id,)
        ).fetchone()
        old_blob = " ".join(
            r["name"]
            for r in conn.execute(
                "SELECT name FROM ingredients WHERE recipe_id = ? AND name IS NOT NULL"
                " ORDER BY id",
                (recipe_id,),
            )
        )
        # Contentless FTS5 rows can't be UPDATEd/DELETEd normally; remove with
        # the special 'delete' command using the originally-indexed values.
        conn.execute(
            "INSERT INTO recipe_fts (recipe_fts, rowid, dish_name, channel, ingredients)"
            " VALUES ('delete', ?, ?, ?, ?)",
            (recipe_id, old["dish_name"] or "", old["channel"] or "", old_blob),
        )
        conn.execute("DELETE FROM ingredients WHERE recipe_id = ?", (recipe_id,))
        conn.execute("DELETE FROM steps WHERE recipe_id = ?", (recipe_id,))
        conn.execute("DELETE FROM tags WHERE recipe_id = ?", (recipe_id,))
        conn.execute(
            "UPDATE recipes SET dish_name = ?, cook_time = ?, servings = ?,"
            " difficulty = ? WHERE id = ?",
            (
                extracted.get("dish_name"),
                extracted.get("cook_time"),
                extracted.get("servings"),
                extracted.get("difficulty"),
                recipe_id,
            ),
        )
        _insert_children(conn, recipe_id, old["channel"], extracted)
        conn.execute("DELETE FROM plan_steps WHERE recipe_id = ?", (recipe_id,))
        conn.execute("DELETE FROM recipe_components WHERE recipe_id = ?", (recipe_id,))
        conn.execute(
            "UPDATE recipes SET macros_json = NULL, gaps_json = NULL,"
            " meal_type = NULL, health_score = NULL WHERE id = ?",
            (recipe_id,),
        )


def delete_recipe(conn: sqlite3.Connection, recipe_id: int) -> None:
    """Delete a recipe and everything stored with it. Child tables (ingredients,
    steps, tags, plan_steps, recipe_components) cascade via foreign keys; the
    contentless FTS row can't cascade, so it's removed explicitly with the
    'delete' command using its originally-indexed values."""
    with conn:
        old = conn.execute(
            "SELECT dish_name, channel FROM recipes WHERE id = ?", (recipe_id,)
        ).fetchone()
        old_blob = " ".join(
            r["name"]
            for r in conn.execute(
                "SELECT name FROM ingredients WHERE recipe_id = ? AND name IS NOT NULL"
                " ORDER BY id",
                (recipe_id,),
            )
        )
        conn.execute(
            "INSERT INTO recipe_fts (recipe_fts, rowid, dish_name, channel, ingredients)"
            " VALUES ('delete', ?, ?, ?, ?)",
            (recipe_id, old["dish_name"] or "", old["channel"] or "", old_blob),
        )
        conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))


def discover(
    conn: sqlite3.Connection,
    *,
    meal_type: str | None = None,
    min_health: int | None = None,
    max_health: int | None = None,
    ingredients: list[str] | tuple[str, ...] = (),
    count: int = 1,
) -> list[sqlite3.Row]:
    """Randomly pick up to `count` real recipes matching the given filters. A
    recipe must include every listed ingredient (substring match). Recipes not
    yet classified are naturally excluded by the meal_type/health filters."""
    where = ["dish_name IS NOT NULL"]
    params: list = []
    if meal_type:
        where.append("meal_type = ?")
        params.append(meal_type)
    if min_health is not None:
        where.append("health_score >= ?")
        params.append(min_health)
    if max_health is not None:
        where.append("health_score <= ?")
        params.append(max_health)
    for ing in ingredients:
        where.append(
            "EXISTS (SELECT 1 FROM ingredients i"
            " WHERE i.recipe_id = recipes.id AND i.name LIKE ?)"
        )
        params.append(f"%{ing}%")
    sql = (
        "SELECT id, dish_name, channel, title, meal_type, health_score FROM recipes"
        " WHERE " + " AND ".join(where) + " ORDER BY RANDOM() LIMIT ?"
    )
    params.append(count)
    return conn.execute(sql, params).fetchall()


def list_recipes(
    conn: sqlite3.Connection, tag: str | None = None, limit: int | None = None
) -> list[sqlite3.Row]:
    params: list = []
    sql = "SELECT DISTINCT r.id, r.dish_name, r.channel, r.title FROM recipes r"
    if tag:
        sql += " JOIN tags t ON t.recipe_id = r.id WHERE t.tag = ?"
        params.append(tag)
    sql += " ORDER BY r.created_at DESC, r.id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return conn.execute(sql, params).fetchall()
