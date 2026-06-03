"""Offline unit tests: no network, no API keys.

Covers the pure logic: video-id parsing, defensive JSON parsing, and a full
SQLite round-trip including FTS search. Set MISE_HOME to a tmp dir before
importing mise modules that read config paths.
"""

import os
import tempfile

import pytest

os.environ["MISE_HOME"] = tempfile.mkdtemp()

from mise import cook, db  # noqa: E402
from mise.errors import MiseError  # noqa: E402
from mise.extract import _parse_json  # noqa: E402
from mise.transcript import extract_video_id  # noqa: E402


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10s", "dQw4w9WgXcQ"),
        ("dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ],
)
def test_extract_video_id(url, expected):
    assert extract_video_id(url) == expected


def test_extract_video_id_rejects_garbage():
    with pytest.raises(MiseError):
        extract_video_id("https://example.com/not-a-video")


def test_parse_json_plain():
    assert _parse_json('{"dish_name": "soup"}') == {"dish_name": "soup"}


def test_parse_json_with_fences_and_prose():
    text = 'Here you go:\n```json\n{"dish_name": "soup", "tags": ["warm"]}\n```'
    assert _parse_json(text) == {"dish_name": "soup", "tags": ["warm"]}


def test_parse_json_non_recipe():
    assert _parse_json('{"dish_name": null}') == {"dish_name": None}


def test_parse_json_rejects_non_json():
    with pytest.raises(MiseError):
        _parse_json("sorry, no JSON here")


def test_db_roundtrip_and_search():
    db.init_db()
    conn = db.connect()
    extracted = {
        "dish_name": "Garlic Butter Pasta",
        "cook_time": "20 minutes",
        "servings": "2",
        "difficulty": "easy",
        "ingredients": [
            {"name": "spaghetti", "quantity": "200", "unit": "g", "prep": None},
            {"name": "garlic", "quantity": "a handful", "unit": None, "prep": "minced"},
        ],
        "steps": ["boil the pasta", "melt butter and garlic", "toss together"],
        "tags": ["italian", "pasta"],
    }
    rid = db.insert_recipe(
        conn,
        video_id="vid123abcde",
        title="Best Pasta",
        channel="Test Kitchen",
        url="https://youtu.be/vid123abcde",
        raw_transcript="boil pasta...",
        extracted=extracted,
    )

    assert db.video_exists(conn, "vid123abcde")

    full = db.get_recipe(conn, rid)
    assert full["recipe"]["dish_name"] == "Garlic Butter Pasta"
    assert len(full["ingredients"]) == 2
    assert full["ingredients"][1]["quantity"] == "a handful"  # kept verbatim
    assert [s["instruction"] for s in full["steps"]] == extracted["steps"]
    assert full["tags"] == ["italian", "pasta"]

    assert any(r["id"] == rid for r in db.search(conn, "garlic"))
    assert any(r["id"] == rid for r in db.search(conn, "Test Kitchen"))
    assert any(r["id"] == rid for r in db.list_recipes(conn, tag="italian"))


def test_non_recipe_stub_stores_cleanly():
    db.init_db()
    conn = db.connect()
    rid = db.insert_recipe(
        conn,
        video_id="novid00000x",
        title="Vlog: my day",
        channel="Someone",
        url="https://youtu.be/novid00000x",
        raw_transcript=None,
        extracted={"dish_name": None, "ingredients": [], "steps": [], "tags": []},
    )
    full = db.get_recipe(conn, rid)
    assert full["recipe"]["dish_name"] is None
    assert full["ingredients"] == []


def _seed_recipe(conn, video_id):
    return db.insert_recipe(
        conn,
        video_id=video_id,
        title="t",
        channel="c",
        url="u",
        raw_transcript="x",
        extracted={"dish_name": "d", "ingredients": [], "steps": [], "tags": []},
    )


def test_save_get_plan_roundtrip():
    db.init_db()
    conn = db.connect()
    rid = _seed_recipe(conn, "planvid0001")
    tasks = [
        {"instruction": "chop onion", "duration_minutes": 5, "mode": "active", "overlap_hint": None},
        {"instruction": "marinate", "duration_minutes": 120, "mode": "passive", "overlap_hint": "make sauce"},
    ]
    db.save_plan(conn, rid, tasks)
    got = db.get_plan(conn, rid)
    assert [t["instruction"] for t in got] == ["chop onion", "marinate"]
    assert got[1]["mode"] == "passive"
    assert got[1]["overlap_hint"] == "make sauce"

    # Regenerate is a clean overwrite, not an append.
    db.save_plan(conn, rid, tasks[:1])
    assert len(db.get_plan(conn, rid)) == 1


def test_plan_cascades_on_recipe_delete():
    db.init_db()
    conn = db.connect()
    rid = _seed_recipe(conn, "planvid0002")
    db.save_plan(conn, rid, [{"instruction": "a", "duration_minutes": 1, "mode": "active"}])
    with conn:
        conn.execute("DELETE FROM recipes WHERE id = ?", (rid,))
    assert db.get_plan(conn, rid) == []


@pytest.mark.parametrize(
    "minutes,expected",
    [(0, "0m"), (5, "5m"), (60, "1h"), (90, "1h30m"), (2.4, "2m")],
)
def test_fmt_duration(minutes, expected):
    assert cook.fmt_duration(minutes) == expected


@pytest.mark.parametrize(
    "seconds,expected",
    [(0, "0:00"), (42, "0:42"), (605, "10:05"), (3661, "1:01:01")],
)
def test_fmt_clock(seconds, expected):
    assert cook.fmt_clock(seconds) == expected


def test_status_line():
    assert cook.status_line(40, 300, passive=True) == "remaining 4:20"
    assert cook.status_line(360, 300, passive=True) == "over by 1:00"
    assert cook.status_line(42, 0, passive=False) == "elapsed 0:42"
    # An active task ignores duration and reports elapsed.
    assert cook.status_line(70, 300, passive=False) == "elapsed 1:10"
