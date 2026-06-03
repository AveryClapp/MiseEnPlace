"""SQLite storage: schema, inserts, FTS5 search, and read queries.

No ORM. Connections set row_factory to sqlite3.Row so callers get dict-like
rows, and enable foreign keys so deleting a recipe cascades to its children.
"""

import sqlite3

from .config import DB_PATH

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
    created_at     TEXT DEFAULT (datetime('now'))
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
    overlap_hint     TEXT
);

-- Contentless FTS index keyed by rowid = recipes.id.
CREATE VIRTUAL TABLE IF NOT EXISTS recipe_fts USING fts5(
    dish_name, channel, ingredients, content=''
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def video_exists(conn: sqlite3.Connection, video_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM recipes WHERE video_id = ?", (video_id,)
    ).fetchone()
    return row is not None


def insert_recipe(
    conn: sqlite3.Connection,
    *,
    video_id: str,
    title: str | None,
    channel: str | None,
    url: str | None,
    raw_transcript: str | None,
    extracted: dict,
) -> int:
    """Insert a recipe and its children. `extracted` is the parsed Claude dict
    (or a minimal stub for non-recipe / no-transcript videos). Returns the new
    recipe id. Runs in a single transaction."""
    ingredients = extracted.get("ingredients") or []
    steps = extracted.get("steps") or []
    tags = extracted.get("tags") or []

    with conn:
        cur = conn.execute(
            """INSERT INTO recipes
               (video_id, title, channel, url, dish_name, cook_time,
                servings, difficulty, raw_transcript)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            ),
        )
        recipe_id = cur.lastrowid

        conn.executemany(
            "INSERT INTO ingredients (recipe_id, name, quantity, unit, prep)"
            " VALUES (?, ?, ?, ?, ?)",
            [
                (
                    recipe_id,
                    ing.get("name"),
                    ing.get("quantity"),
                    ing.get("unit"),
                    ing.get("prep"),
                )
                for ing in ingredients
                if ing.get("name")
            ],
        )

        conn.executemany(
            "INSERT INTO steps (recipe_id, step_number, instruction)"
            " VALUES (?, ?, ?)",
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
            "INSERT INTO recipe_fts (rowid, dish_name, channel, ingredients)"
            " VALUES (?, ?, ?, ?)",
            (
                recipe_id,
                extracted.get("dish_name") or "",
                channel or "",
                ingredient_blob,
            ),
        )

    return recipe_id


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
            " (recipe_id, position, instruction, duration_minutes, mode, overlap_hint)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    recipe_id,
                    i,
                    task.get("instruction"),
                    task.get("duration_minutes"),
                    task.get("mode") or "active",
                    task.get("overlap_hint"),
                )
                for i, task in enumerate(tasks)
            ],
        )


def get_plan(conn: sqlite3.Connection, recipe_id: int) -> list[dict]:
    """Return the cached plan tasks in order, or [] if none."""
    rows = conn.execute(
        "SELECT instruction, duration_minutes, mode, overlap_hint"
        " FROM plan_steps WHERE recipe_id = ? ORDER BY position",
        (recipe_id,),
    ).fetchall()
    return [dict(r) for r in rows]


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
