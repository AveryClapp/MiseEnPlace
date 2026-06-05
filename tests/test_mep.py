"""Offline unit tests: no network, no API keys.

Covers the pure logic: video-id parsing, defensive JSON parsing, and a full
SQLite round-trip including FTS search. Set MEP_HOME to a tmp dir before
importing mep modules that read config paths.
"""

import json
import os
import sqlite3
import tempfile

import pytest

os.environ["MEP_HOME"] = tempfile.mkdtemp()

from mep import adapt, cli, config, cook, cookware, db, ingest, plan, scale, shopping, web  # noqa: E402
from mep.classify import _normalize as _normalize_classification  # noqa: E402
from mep.pairing import _normalize as _normalize_pairings  # noqa: E402
from mep.components import _normalize_components  # noqa: E402
from mep.gaps import _normalize as _normalize_gaps  # noqa: E402
from mep.nutrition import _normalize as _normalize_macros  # noqa: E402
from mep.errors import MepError  # noqa: E402
from mep.extract import _parse_json, _parse_recipes  # noqa: E402
from mep.plan import _normalize_tasks  # noqa: E402
from mep.transcript import extract_video_id  # noqa: E402


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
    with pytest.raises(MepError):
        extract_video_id("https://example.com/not-a-video")


def test_parse_json_plain():
    assert _parse_json('{"dish_name": "soup"}') == {"dish_name": "soup"}


def test_parse_json_with_fences_and_prose():
    text = 'Here you go:\n```json\n{"dish_name": "soup", "tags": ["warm"]}\n```'
    assert _parse_json(text) == {"dish_name": "soup", "tags": ["warm"]}


def test_parse_json_non_recipe():
    assert _parse_json('{"dish_name": null}') == {"dish_name": None}


def test_parse_json_rejects_non_json():
    with pytest.raises(MepError):
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


def test_provider_defaults_to_anthropic():
    assert config.provider({}) == "anthropic"
    assert config.provider({"LLM_PROVIDER": "OpenAI"}) == "openai"


def test_provider_inferred_from_lone_key():
    assert config.provider({"OPENAI_API_KEY": "o"}) == "openai"
    assert config.provider({"ANTHROPIC_API_KEY": "a"}) == "anthropic"
    # Both set -> default anthropic; neither -> default anthropic.
    assert config.provider({"ANTHROPIC_API_KEY": "a", "OPENAI_API_KEY": "o"}) == "anthropic"
    # An explicit provider always overrides inference.
    assert config.provider({"LLM_PROVIDER": "openai", "ANTHROPIC_API_KEY": "a"}) == "openai"


def test_provider_rejects_unknown():
    with pytest.raises(MepError):
        config.provider({"LLM_PROVIDER": "gemini"})


def test_model_default_per_provider_and_override():
    assert config.model({}) == config.DEFAULT_MODELS["anthropic"]
    assert config.model({"LLM_PROVIDER": "openai"}) == config.DEFAULT_MODELS["openai"]
    assert config.model({"EXTRACTION_MODEL": "custom-x"}) == "custom-x"


def test_require_api_key_follows_provider():
    assert config.require_api_key({"ANTHROPIC_API_KEY": "a"}) == "a"
    assert config.require_api_key({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "o"}) == "o"
    with pytest.raises(MepError):  # openai selected but no openai key
        config.require_api_key({"LLM_PROVIDER": "openai", "ANTHROPIC_API_KEY": "a"})


def _data_with(servings, ingredients):
    return {"recipe": {"servings": servings}, "ingredients": ingredients}


def test_gather_lines_unknown_servings_scales_as_batches():
    data = _data_with(None, [{"quantity": "2", "unit": "cups", "name": "flour", "prep": None}])
    lines, note = cli._gather_lines(data, 3)
    assert lines == ["6 cups flour"]  # treated as 1 serving -> x3
    assert "1 serving = the full recipe" in note


def test_gather_lines_known_servings_scales_to_people():
    data = _data_with("4", [{"quantity": "2", "unit": "cups", "name": "flour", "prep": None}])
    lines, note = cli._gather_lines(data, 2)
    assert lines == ["1 cups flour"]  # 4 -> 2 servings = half
    assert "4 → 2 servings" in note


def test_set_servings_roundtrip():
    db.init_db()
    conn = db.connect()
    rid = _seed_recipe(conn, "servvid0001")
    assert db.get_recipe(conn, rid)["recipe"]["servings"] is None
    db.set_servings(conn, rid, "4-6")
    assert db.get_recipe(conn, rid)["recipe"]["servings"] == "4-6"


def test_normalize_macros_coerces():
    out = _normalize_macros(
        {"calories": "520", "protein_g": 32, "carbs_g": 41.5, "fat_g": -2, "servings": 4, "note": " est "}
    )
    assert out["macros"]["calories"] == 520.0
    assert out["macros"]["fat_g"] == 0.0  # clamped
    assert out["servings"] == 4.0
    assert out["note"] == "est"


def test_normalize_macros_all_missing_raises():
    with pytest.raises(MepError):
        _normalize_macros({"calories": None, "protein_g": "x"})


def test_macros_cache_roundtrip_and_lazy():
    db.init_db()
    conn = db.connect()
    rid = _seed_recipe(conn, "macrovid001")
    assert db.get_macros(conn, rid) is None  # lazy: nothing until requested
    est = {"macros": {"calories": 500.0}, "servings": 2.0, "note": None}
    db.save_macros(conn, rid, est)
    assert db.get_macros(conn, rid) == est


def test_increment_cook_count():
    db.init_db()
    conn = db.connect()
    rid = _seed_recipe(conn, "cookcount01")
    assert db.get_recipe(conn, rid)["recipe"]["times_cooked"] == 0
    assert db.increment_cook_count(conn, rid) == 1
    assert db.increment_cook_count(conn, rid) == 2
    assert db.get_recipe(conn, rid)["recipe"]["times_cooked"] == 2


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


def test_lane_for_task_picks_vessels_not_prep_tools():
    assert cook.lane_for_task({"equipment": ["knife", "cutting board"]}) is None
    assert cook.lane_for_task({"equipment": ["large pot", "stove"]}) == "large pot"
    assert cook.lane_for_task({"equipment": ["stove", "12-inch skillet"]}) == "12-inch skillet"
    assert cook.lane_for_task({"equipment": []}) is None


def test_plan_lanes_distinct_in_first_seen_order():
    tasks = [
        {"equipment": ["knife"]},
        {"equipment": ["large pot", "stove"]},
        {"equipment": ["oven"]},
        {"equipment": ["Large Pot"]},  # case-insensitive duplicate of lane 1
    ]
    assert cook.plan_lanes(tasks) == ["large pot", "oven"]


@pytest.mark.parametrize(
    "frac,expected",
    [(0, "░" * 10), (1, "▓" * 10), (0.5, "▓" * 5 + "░" * 5), (2, "▓" * 10), (-1, "░" * 10)],
)
def test_progress_bar(frac, expected):
    assert cook.progress_bar(frac) == expected


def test_tui_pilot_lanes_idle_until_started_then_count_down():
    pytest.importorskip("textual")  # the [tui] extra; skips cleanly when absent
    import asyncio
    import time
    from mep import tui

    CookApp = tui._app_class()
    recipe = {"dish_name": "Soup", "title": None}
    tasks = [
        {"instruction": "Dice onion", "mode": "active", "duration_minutes": 2,
         "equipment": ["knife"], "dish": None, "overlap_hint": None,
         "ingredients": [], "timer_label": None},
        {"instruction": "Simmer in pot", "mode": "passive", "duration_minutes": 20,
         "equipment": ["large pot"], "dish": None, "overlap_hint": None,
         "ingredients": [], "timer_label": "soup"},
    ]

    async def scenario():
        app = CookApp(recipe, tasks)
        async with app.run_test() as pilot:
            assert app.focus_i == 0 and app.lanes == ["large pot"]
            # Focused step is knife prep (no lane); the pot isn't started -> idle.
            assert "idle" in app._occupant("large pot", time.monotonic())
            await pilot.press("enter")  # active step -> done, advance to the pot step
            assert app.state[0] == "done" and app.focus_i == 1
            # Now focused on the passive pot step but NOT started -> still idle
            # (nothing is assumed to be cooking until you start it).
            assert "idle" in app._occupant("large pot", time.monotonic())
            await pilot.press("enter")  # start the timer in its lane
            assert app.state[1] == "running" and app.start[1] is not None
            assert "left" in app._occupant("large pot", time.monotonic())  # counting now

    asyncio.run(scenario())


def test_status_line():
    assert cook.status_line(40, 300, passive=True) == "remaining 4:20"
    assert cook.status_line(360, 300, passive=True) == "over by 1:00"
    assert cook.status_line(42, 0, passive=False) == "elapsed 0:42"
    # An active task ignores duration and reports elapsed.
    assert cook.status_line(70, 300, passive=False) == "elapsed 1:10"


# --- quantity scaling ---------------------------------------------------------


@pytest.mark.parametrize(
    "quantity,factor,expected",
    [
        ("200", 2, "400"),
        ("1 1/2", 2, "3"),
        ("3/4", 2, "1 1/2"),
        ("2", 0.5, "1"),
        ("3-4", 2, "6-8"),
        ("3 to 4", 2, "6 to 8"),
        ("1 (14 oz can)", 2, "2 (14 oz can)"),  # only leading amount scales
        ("a handful", 2, "a handful"),  # vague passes through
        ("to taste", 3, "to taste"),
        ("200", 1, "200"),  # factor 1 is a no-op
        (None, 2, None),
    ],
)
def test_scale_quantity(quantity, factor, expected):
    assert scale.scale_quantity(quantity, factor) == expected


@pytest.mark.parametrize(
    "servings,expected",
    [("4", 4), ("4-6", 4), ("serves 8", 8), ("a lot", None), (None, None)],
)
def test_parse_base_servings(servings, expected):
    assert scale.parse_base_servings(servings) == expected


@pytest.mark.parametrize(
    "cook_time,expected",
    [
        ("30 minutes", 30),
        ("30 min", 30),
        ("45", 45),                 # a bare number means minutes
        ("1 hour", 60),
        ("1 hr 30 min", 90),
        ("1h30m", 90),
        ("1:30", 90),               # clock form
        ("1.5 hours", 90),
        ("30-40 minutes", 40),      # a range takes the upper bound
        ("about 25 mins", 25),
        ("overnight", None),        # no number -> unknown
        ("a while", None),
        (None, None),
    ],
)
def test_parse_minutes(cook_time, expected):
    assert scale.parse_minutes(cook_time) == expected


def _seed_timed(conn, video_id, cook_time):
    return db.insert_recipe(
        conn, video_id=video_id, title="t", channel=None, url=None, raw_transcript=None,
        extracted={"dish_name": "d", "cook_time": cook_time,
                   "ingredients": [], "steps": ["go"], "tags": []},
    )


def test_discover_max_time_keeps_quick_excludes_slow_and_unknown():
    db.init_db()
    conn = db.connect()
    quick = _seed_timed(conn, "qk-disc", "15 minutes")
    slow = _seed_timed(conn, "sl-disc", "2 hours")
    unknown = _seed_timed(conn, "un-disc", None)
    ids = {r["id"] for r in db.discover(conn, max_time=30, count=50)}
    assert quick in ids
    assert slow not in ids and unknown not in ids


def test_list_max_time_keeps_quick_excludes_slow_and_unknown():
    db.init_db()
    conn = db.connect()
    quick = _seed_timed(conn, "qk-list", "20 min")
    slow = _seed_timed(conn, "sl-list", "1 hr 30 min")
    unknown = _seed_timed(conn, "un-list", "overnight")
    ids = {r["id"] for r in db.list_recipes(conn, max_time=30)}
    assert quick in ids
    assert slow not in ids and unknown not in ids


# --- ratings, notes, cook log, pantry, edit, backup ---------------------------


def test_set_rating_and_set_cook_time():
    db.init_db()
    conn = db.connect()
    rid = _seed_recipe(conn, "rate-time-01")
    db.set_rating(conn, rid, 4)
    db.set_cook_time(conn, rid, "25 minutes")
    r = db.get_recipe(conn, rid)["recipe"]
    assert r["rating"] == 4 and r["cook_time"] == "25 minutes"


def test_add_note_appends_dated_lines():
    db.init_db()
    conn = db.connect()
    rid = _seed_recipe(conn, "note-01")
    db.add_note(conn, rid, "too salty")
    db.add_note(conn, rid, "perfect now")
    notes = db.get_recipe(conn, rid)["recipe"]["notes"]
    assert "too salty" in notes and "perfect now" in notes
    assert notes.count("\n") == 1  # two dated lines
    import datetime
    assert datetime.date.today().isoformat() in notes


def test_discover_min_rating_excludes_low_and_unrated():
    db.init_db()
    conn = db.connect()
    hi = _seed_classified(conn, "rate-hi", "dinner", 6, ["x"])
    lo = _seed_classified(conn, "rate-lo", "dinner", 6, ["x"])
    unrated = _seed_classified(conn, "rate-none", "dinner", 6, ["x"])
    db.set_rating(conn, hi, 5)
    db.set_rating(conn, lo, 2)
    ids = {r["id"] for r in db.discover(conn, min_rating=4, count=50)}
    assert hi in ids
    assert lo not in ids and unrated not in ids


def test_increment_cook_count_logs_history_and_last_cooked():
    db.init_db()
    conn = db.connect()
    rid = _seed_recipe(conn, "cooklog-01")
    assert db.last_cooked(conn, rid) is None
    db.increment_cook_count(conn, rid)
    db.increment_cook_count(conn, rid)
    assert db.last_cooked(conn, rid) is not None
    mine = [h for h in db.cook_history(conn, limit=50) if h["recipe_id"] == rid]
    assert len(mine) == 2 and mine[0]["dish_name"] == "d"


def test_pantry_add_remove_list():
    db.init_db()
    conn = db.connect()
    assert db.pantry_add(conn, ["eggs", "milk", "eggs"]) == 2  # dupe ignored
    assert set(db.pantry_list(conn)) >= {"eggs", "milk"}
    assert db.pantry_remove(conn, ["eggs"]) == 1
    assert "eggs" not in db.pantry_list(conn)


def test_cook_now_ranks_by_fewest_missing():
    db.init_db()
    conn = db.connect()
    toast = db.insert_recipe(
        conn, video_id="cn-toast", title="t", channel=None, url=None, raw_transcript=None,
        extracted={"dish_name": "Egg Toast",
                   "ingredients": [{"name": "eggs"}, {"name": "bread"}], "steps": ["go"], "tags": []},
    )
    cake = db.insert_recipe(
        conn, video_id="cn-cake", title="t", channel=None, url=None, raw_transcript=None,
        extracted={"dish_name": "Fancy Cake",
                   "ingredients": [{"name": "flour"}, {"name": "saffron"}, {"name": "truffle"}],
                   "steps": ["go"], "tags": []},
    )
    db.pantry_add(conn, ["eggs", "bread", "flour"])
    ranked = db.cook_now(conn)
    ids = [r["id"] for r in ranked]
    assert ids.index(toast) < ids.index(cake)  # 0 missing before 2 missing
    assert next(r for r in ranked if r["id"] == toast)["missing"] == []
    assert set(next(r for r in ranked if r["id"] == cake)["missing"]) == {"saffron", "truffle"}


def test_export_import_roundtrip_preserves_metadata():
    db.init_db()
    conn = db.connect()
    rid = db.insert_recipe(
        conn, video_id="exp-1", title="T", channel="C", url="U", raw_transcript=None,
        extracted={"dish_name": "Soup", "cook_time": "30 min", "servings": "4", "difficulty": "easy",
                   "ingredients": [{"name": "broth", "quantity": "4", "unit": "cups", "prep": None}],
                   "steps": ["boil", "serve"], "tags": ["soup"]},
        source_type="web",
    )
    db.set_rating(conn, rid, 5)
    db.add_note(conn, rid, "great")
    db.save_classification(conn, rid, "dinner", 7)
    db.increment_cook_count(conn, rid)

    record = cli._to_export(db.get_recipe(conn, rid))
    record["video_id"] = "exp-1-copy"  # import as a new recipe
    new_id = db.import_recipe(conn, record)
    full = db.get_recipe(conn, new_id)
    r = full["recipe"]
    assert r["dish_name"] == "Soup" and r["rating"] == 5 and "great" in r["notes"]
    assert r["meal_type"] == "dinner" and r["health_score"] == 7
    assert r["times_cooked"] == 1 and r["source_type"] == "web"
    assert [i["name"] for i in full["ingredients"]] == ["broth"]
    assert [s["instruction"] for s in full["steps"]] == ["boil", "serve"]
    assert full["tags"] == ["soup"]
    # Re-importing the same record is a skip.
    assert db.import_recipe(conn, record) is None


def test_validate_editable_coerces_and_drops():
    out = cli._validate_editable({
        "dish_name": " Soup ", "cook_time": "", "servings": None, "difficulty": "easy",
        "ingredients": [
            {"name": " broth ", "quantity": "4", "unit": "cups"},
            {"name": ""},        # dropped: no name
            {"quantity": "1"},   # dropped: no name
        ],
        "steps": ["boil", "", 2],
        "tags": [" soup ", ""],
    })
    assert out["dish_name"] == "Soup"
    assert out["cook_time"] is None  # blank -> None
    assert out["ingredients"] == [{"name": "broth", "quantity": "4", "unit": "cups", "prep": None}]
    assert out["steps"] == ["boil", "2"]
    assert out["tags"] == ["soup"]


def test_validate_editable_rejects_non_dict():
    with pytest.raises(MepError):
        cli._validate_editable([1, 2, 3])


# --- plan normalization -------------------------------------------------------


def test_normalize_tasks_coerces_and_cleans():
    raw = [
        {
            "instruction": "  Chop onion  ",
            "duration_minutes": "5",
            "mode": "active",
            "ingredients": [" 1 onion ", "", 2],
            "equipment": ["knife"],
            "timer_label": "  ",
        },
        {
            "instruction": "Marinate",
            "duration_minutes": -3,  # clamped to 0
            "mode": "bogus",  # defaults to active
            "overlap_hint": "make sauce",
        },
        {"instruction": "   "},  # dropped (empty)
        "not a dict",  # dropped
    ]
    tasks = _normalize_tasks(raw)
    assert len(tasks) == 2
    assert tasks[0]["instruction"] == "Chop onion"
    assert tasks[0]["duration_minutes"] == 5.0
    assert tasks[0]["ingredients"] == ["1 onion", "2"]
    assert tasks[0]["timer_label"] is None
    assert tasks[1]["duration_minutes"] == 0.0
    assert tasks[1]["mode"] == "active"


def test_normalize_tasks_all_empty_raises():
    with pytest.raises(MepError):
        _normalize_tasks([{"instruction": ""}, "junk"])


# --- combined plan (cook a side + main together) ------------------------------


def test_normalize_tasks_carries_dish_label():
    tasks = _normalize_tasks(
        [
            {"instruction": "boil pasta", "dish": "Spaghetti", "duration_minutes": 10, "mode": "active"},
            {"instruction": "toast bread", "duration_minutes": 5, "mode": "active"},
        ]
    )
    assert tasks[0]["dish"] == "Spaghetti"
    assert tasks[1]["dish"] is None  # absent -> None, so single-recipe plans are unaffected


def test_format_combined_input_names_each_dish():
    a = {"recipe": {"dish_name": "Steak", "title": None}, "ingredients": [], "steps": []}
    b = {"recipe": {"dish_name": "Mashed Potatoes", "title": None}, "ingredients": [], "steps": []}
    text = plan._format_combined_input([a, b])
    assert "=== Steak ===" in text
    assert "=== Mashed Potatoes ===" in text
    assert "Cooking these dishes together: Steak, Mashed Potatoes." in text


def test_generate_combined_plan_returns_labeled_tasks(monkeypatch):
    monkeypatch.setattr(
        plan, "complete",
        lambda config, *, system, user, max_tokens: json.dumps(
            {"tasks": [
                {"dish": "Steak", "instruction": "sear", "duration_minutes": 6, "mode": "active"},
                {"dish": "Mash", "instruction": "boil potatoes", "duration_minutes": 15, "mode": "passive"},
            ]}
        ),
    )
    a = {"recipe": {"dish_name": "Steak", "title": None}, "ingredients": [], "steps": []}
    b = {"recipe": {"dish_name": "Mash", "title": None}, "ingredients": [], "steps": []}
    tasks = plan.generate_combined_plan([a, b], config={})
    assert [t["dish"] for t in tasks] == ["Steak", "Mash"]


def _seed_recipe_with_step(conn, video_id, name):
    return db.insert_recipe(
        conn, video_id=video_id, title="t", channel="c", url="u", raw_transcript="x",
        extracted={
            "dish_name": name,
            "ingredients": [{"name": "salt", "quantity": "1", "unit": "tsp"}],
            "steps": ["do the thing"],
            "tags": [],
        },
    )


def test_load_combined_validates_and_orders():
    db.init_db()
    conn = db.connect()
    main = _seed_recipe_with_step(conn, "combomain01", "Steak")
    side = _seed_recipe_with_step(conn, "comboside01", "Mash")
    recipes = cli._load_combined(conn, main, (side, main))  # dup main is dropped
    assert [r["recipe"]["dish_name"] for r in recipes] == ["Steak", "Mash"]


def test_load_combined_rejects_single_and_missing():
    db.init_db()
    conn = db.connect()
    main = _seed_recipe_with_step(conn, "combomain02", "Steak")
    with pytest.raises(MepError):  # nothing distinct to combine
        cli._load_combined(conn, main, (main,))
    with pytest.raises(MepError):  # unknown id
        cli._load_combined(conn, main, (999999,))


def test_combined_gather_prefixes_each_dish():
    db.init_db()
    conn = db.connect()
    main = _seed_recipe_with_step(conn, "combomain03", "Steak")
    side = _seed_recipe_with_step(conn, "comboside03", "Mash")
    recipes = cli._load_combined(conn, main, (side,))
    lines = cli._combined_gather(recipes)
    assert any(line.startswith("[Steak] ") for line in lines)
    assert any(line.startswith("[Mash] ") for line in lines)


# --- clarify cookware in steps ------------------------------------------------


def _recipe_data_with_steps(steps):
    return {
        "recipe": {"dish_name": "Soup", "title": None},
        "ingredients": [{"name": "broth", "quantity": "4", "unit": "cups"}],
        "steps": [{"step_number": i, "instruction": s} for i, s in enumerate(steps, 1)],
    }


def test_clarify_steps_rewrites_with_cookware(monkeypatch):
    monkeypatch.setattr(
        cookware, "complete",
        lambda config, *, system, user, max_tokens: json.dumps(
            {"steps": ["In a large pot, bring broth to a boil.", "Season to taste."]}
        ),
    )
    out = cookware.clarify_steps(_recipe_data_with_steps(["Boil broth.", "Season."]), config={})
    assert out[0].startswith("In a large pot")


def test_clarify_steps_keeps_originals_on_count_mismatch(monkeypatch):
    monkeypatch.setattr(
        cookware, "complete",
        lambda config, *, system, user, max_tokens: json.dumps({"steps": ["only one"]}),
    )
    original = ["Boil broth.", "Season."]
    out = cookware.clarify_steps(_recipe_data_with_steps(original), config={})
    assert out == original  # mismatch -> never drop a step


def test_replace_steps_renumbers_and_clears_plan():
    db.init_db()
    conn = db.connect()
    rid = _seed_recipe_with_step(conn, "clarify0001", "Soup")
    db.save_plan(conn, rid, [{"instruction": "a", "duration_minutes": 1, "mode": "active"}])
    db.replace_steps(conn, rid, ["In a large pot, simmer.", "", "Ladle into bowls."])
    steps = db.get_recipe(conn, rid)["steps"]
    assert [s["instruction"] for s in steps] == ["In a large pot, simmer.", "Ladle into bowls."]
    assert [s["step_number"] for s in steps] == [1, 2]  # re-numbered, blank dropped
    assert db.get_plan(conn, rid) == []  # stale cached plan cleared


def test_recipe_ids_with_steps_lists_only_those_with_steps():
    db.init_db()
    conn = db.connect()
    with_steps = _seed_recipe_with_step(conn, "hassteps001", "Soup")
    stub = db.insert_recipe(
        conn, video_id="nosteps001", title="t", channel=None, url=None,
        raw_transcript=None,
        extracted={"dish_name": None, "ingredients": [], "steps": [], "tags": []},
    )
    ids = db.recipe_ids_with_steps(conn)
    assert with_steps in ids
    assert stub not in ids


# --- enriched plan storage round-trip -----------------------------------------


def test_save_get_plan_preserves_enrichment():
    db.init_db()
    conn = db.connect()
    rid = _seed_recipe(conn, "planvid0003")
    tasks = [
        {
            "instruction": "roast",
            "duration_minutes": 30,
            "mode": "passive",
            "overlap_hint": "make salad",
            "ingredients": ["1 chicken"],
            "equipment": ["oven"],
            "timer_label": "chicken roast",
        }
    ]
    db.save_plan(conn, rid, tasks)
    got = db.get_plan(conn, rid)
    assert got[0]["ingredients"] == ["1 chicken"]
    assert got[0]["equipment"] == ["oven"]
    assert got[0]["timer_label"] == "chicken roast"


# --- cook helpers -------------------------------------------------------------


def test_estimate_wallclock_overlaps_passive():
    # 5m active, then a 120m passive wait that runs in the background while a
    # final 10m active step proceeds: wall-clock = max(active spine, passive tail).
    tasks = [
        {"mode": "active", "duration_minutes": 5},
        {"mode": "passive", "duration_minutes": 120},
        {"mode": "active", "duration_minutes": 10},
    ]
    # active spine = 15; passive tail finishes at 5 + 120 = 125.
    assert cook.estimate_wallclock_minutes(tasks) == 125


def test_estimate_wallclock_all_active_is_sum():
    tasks = [
        {"mode": "active", "duration_minutes": 5},
        {"mode": "active", "duration_minutes": 10},
    ]
    assert cook.estimate_wallclock_minutes(tasks) == 15


def test_all_equipment_dedupes_in_order():
    tasks = [
        {"equipment": ["skillet", "oven"]},
        {"equipment": ["oven", "tongs"]},
        {"equipment": []},
    ]
    assert cook.all_equipment(tasks) == ["skillet", "oven", "tongs"]


def test_preheat_cue_looks_ahead():
    tasks = [
        {"equipment": ["bowl"]},
        {"equipment": ["oven"]},
    ]
    cue = cook.preheat_cue(tasks, 0)
    assert cue is not None and "oven" in cue
    # No cue once you're already on the heat step.
    assert cook.preheat_cue(tasks, 1) is None


# --- adapt: input parsing -----------------------------------------------------


@pytest.mark.parametrize(
    "text,count,expected",
    [
        ("1,4", 5, [0, 3]),
        ("1, 4", 5, [0, 3]),
        ("", 5, []),
        ("2,2,2", 5, [1]),  # dedupes
        ("0,6,3", 5, [2]),  # out-of-range dropped
        ("x,3,y", 5, [2]),  # junk ignored
    ],
)
def test_parse_selection(text, count, expected):
    assert adapt.parse_selection(text, count) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("yogurt=sour cream", {"yogurt": "sour cream"}),
        ("a=b, c=d", {"a": "b", "c": "d"}),
        ("", {}),
        ("nope", {}),  # no '='
        ("a=, =b", {}),  # empty side skipped
        (" butter = oil ", {"butter": "oil"}),
    ],
)
def test_parse_subs(text, expected):
    assert adapt.parse_subs(text) == expected


# --- components: normalization + storage --------------------------------------


def test_normalize_components_coerces():
    raw = [
        {
            "name": "  Pita  ",
            "purpose": " the bread ",
            "ingredients": ["150g flour", "", 2],
            "make_steps": [1, "3", "x", 5.0],
        },
        {"name": "  "},  # dropped (no name)
        "junk",  # dropped
    ]
    comps = _normalize_components(raw)
    assert len(comps) == 1
    assert comps[0]["name"] == "Pita"
    assert comps[0]["purpose"] == "the bread"
    assert comps[0]["ingredients"] == ["150g flour", "2"]
    assert comps[0]["make_steps"] == [1, 3, 5]


def test_normalize_components_all_empty_raises():
    with pytest.raises(MepError):
        _normalize_components([{"name": ""}, "junk"])


def test_save_get_components_roundtrip():
    db.init_db()
    conn = db.connect()
    rid = _seed_recipe(conn, "compvid0001")
    comps = [
        {"name": "Pita", "purpose": "bread", "ingredients": ["flour"], "make_steps": [1, 2]},
        {"name": "Marinade", "purpose": "flavor", "ingredients": ["yogurt"], "make_steps": [3]},
    ]
    db.save_components(conn, rid, comps)
    got = db.get_components(conn, rid)
    assert [c["name"] for c in got] == ["Pita", "Marinade"]
    assert got[0]["ingredients"] == ["flour"]
    assert got[0]["make_steps"] == [1, 2]

    # Cascades on recipe delete.
    with conn:
        conn.execute("DELETE FROM recipes WHERE id = ?", (rid,))
    assert db.get_components(conn, rid) == []


# --- pairing graph ------------------------------------------------------------


def test_normalize_pairings_filters_invalid_ids():
    out = _normalize_pairings(
        {
            "generic": [{"name": " Garlic bread ", "why": " soaks sauce "}, {"name": ""}, "junk"],
            "matches": [{"id": 3, "why": "bright"}, {"id": 99, "why": "nope"},
                        {"id": "x"}, {"id": 3, "why": "dup"}],
        },
        valid_ids={3, 7},
    )
    assert out["generic"] == [{"name": "Garlic bread", "why": "soaks sauce"}]
    # id 99 not in valid set -> dropped; non-int dropped; id 3 deduped.
    assert out["matches"] == [{"id": 3, "why": "bright"}]


def test_normalize_pairings_empty_raises():
    with pytest.raises(MepError):
        _normalize_pairings({"generic": [], "matches": []}, valid_ids=set())


def test_pairing_edges_are_undirected_and_cascade():
    db.init_db()
    conn = db.connect()
    a = _seed_recipe(conn, "pairvid001")
    b = _seed_recipe(conn, "pairvid002")
    c = _seed_recipe(conn, "pairvid003")
    db.add_pairing_edge(conn, a, b, "go together")
    db.add_pairing_edge(conn, b, a, "duplicate, ignored")  # same edge, sorted
    db.add_pairing_edge(conn, a, c, "also good")
    # Symmetric: querying from either endpoint finds the partner.
    assert {e["id"] for e in db.get_pairing_edges(conn, a)} == {b, c}
    assert [e["id"] for e in db.get_pairing_edges(conn, b)] == [a]
    assert db.get_pairing_edges(conn, b)[0]["reason"] == "go together"
    # Deleting a recipe drops its edges (FK cascade).
    db.delete_recipe(conn, a)
    assert db.get_pairing_edges(conn, b) == []
    assert db.get_pairing_edges(conn, c) == []


def test_clear_pairings_resets_generic_and_edges():
    db.init_db()
    conn = db.connect()
    a = _seed_recipe(conn, "pairvid010")
    b = _seed_recipe(conn, "pairvid011")
    db.save_pairings(conn, a, [{"name": "wine", "why": "classic"}])
    db.add_pairing_edge(conn, a, b, "x")
    db.clear_pairings(conn, a)
    assert db.get_pairings(conn, a) is None
    assert db.get_pairing_edges(conn, a) == []


def test_pairing_candidates_excludes_self_and_stubs():
    db.init_db()
    conn = db.connect()
    a = _seed_recipe(conn, "pairvid020")
    b = _seed_recipe(conn, "pairvid021")
    stub = db.insert_recipe(
        conn, video_id="pairstub01", title="vlog", channel="c", url="u",
        raw_transcript=None, extracted={"dish_name": None, "ingredients": [], "steps": [], "tags": []},
    )
    cands = db.pairing_candidates(conn, exclude_id=a)
    ids = {c["id"] for c in cands}
    assert b in ids and a not in ids and stub not in ids  # no self, no stub


def test_pair_recipe_stores_generic_and_edges(monkeypatch):
    db.init_db()
    conn = db.connect()
    main = _seed_recipe(conn, "pairvid030")
    side = _seed_recipe(conn, "pairvid031")
    monkeypatch.setattr(
        ingest.pairing, "suggest_pairings",
        lambda data, cands, *, config: {
            "generic": [{"name": "crusty bread", "why": "for the sauce"}],
            "matches": [{"id": side, "why": "fresh contrast"}],
        },
    )
    assert ingest.pair_recipe(conn, {}, main) is True
    assert db.get_pairings(conn, main) == [{"name": "crusty bread", "why": "for the sauce"}]
    assert {e["id"] for e in db.get_pairing_edges(conn, main)} == {side}
    # The edge is mutual: the side now lists the main dish too.
    assert {e["id"] for e in db.get_pairing_edges(conn, side)} == {main}


def test_pair_recipe_skips_stub(monkeypatch):
    db.init_db()
    conn = db.connect()
    stub = db.insert_recipe(
        conn, video_id="pairstub02", title="vlog", channel="c", url="u",
        raw_transcript=None, extracted={"dish_name": None, "ingredients": [], "steps": [], "tags": []},
    )
    monkeypatch.setattr(
        ingest.pairing, "suggest_pairings",
        lambda *a, **k: pytest.fail("should not pair a stub"),
    )
    assert ingest.pair_recipe(conn, {}, stub) is False


# --- graceful fault handling --------------------------------------------------


def test_load_config_corrupt_file_raises_clean_error(tmp_path, monkeypatch):
    bad = tmp_path / "config.json"
    bad.write_text("{ not valid json,,, ")
    monkeypatch.setattr(config, "CONFIG_PATH", bad)
    with pytest.raises(MepError):  # not a raw JSONDecodeError
        config.load_config()


def test_load_config_missing_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "nope.json")
    assert config.load_config() == {}


def test_youtube_api_error_is_wrapped(monkeypatch):
    from googleapiclient.errors import HttpError
    from mep import youtube

    resp = type("Resp", (), {"status": 403, "reason": "quotaExceeded"})()
    err = HttpError(resp, b'{"error": {"message": "quota"}}')

    class _Raises:
        def channels(self):
            return self
        def list(self, **kw):
            return self
        def execute(self):
            raise err

    monkeypatch.setattr(youtube, "_client", lambda api_key: _Raises())
    with pytest.raises(MepError) as got:
        youtube.resolve_channel("badkey", "@someone")
    assert "YouTube Data API" in str(got.value)


# --- schema self-heal on connect ----------------------------------------------


def test_connect_migrates_an_older_database(tmp_path, monkeypatch):
    # A database created by an older version: the original columns, none of the
    # ones added later (times_cooked, macros_json, gaps_json, meal_type, ...).
    legacy = tmp_path / "old.db"
    raw = sqlite3.connect(legacy)
    raw.execute(
        "CREATE TABLE recipes (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " video_id TEXT UNIQUE NOT NULL, title TEXT, channel TEXT, url TEXT,"
        " dish_name TEXT, cook_time TEXT, servings TEXT, difficulty TEXT,"
        " raw_transcript TEXT, created_at TEXT)"
    )
    raw.execute("INSERT INTO recipes (video_id, dish_name) VALUES ('old1', 'Old Soup')")
    raw.commit()
    raw.close()

    monkeypatch.setattr(db, "DB_PATH", legacy)
    monkeypatch.setattr(db, "_schema_ready", False)

    # connect() must add the missing columns, so the new commands don't crash.
    conn = db.connect()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(recipes)")}
    assert {"meal_type", "health_score", "source_type", "gaps_json", "times_cooked"} <= cols
    assert db.recipe_ids_for_classify(conn) == [1]  # no longer raises "no such column"
    # Legacy rows are backfilled as YouTube.
    assert conn.execute("SELECT source_type FROM recipes WHERE id=1").fetchone()[0] == "youtube"


# --- web ingestion (JSON-LD parsing) ------------------------------------------


_JSONLD_PAGE = """<html><head><title>Best Cacio e Pepe Recipe</title>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Recipe","name":"Cacio e Pepe",
 "recipeIngredient":["200g spaghetti","100g pecorino","black pepper to taste"],
 "recipeInstructions":[{"@type":"HowToStep","text":"Boil pasta"},
                       {"@type":"HowToStep","text":"Toss with cheese"}],
 "totalTime":"PT20M","recipeYield":"2 servings","recipeCuisine":"Italian"}
</script></head><body><p>A long life story about my trip to Rome...</p>
<script>var ads = 1;</script></body></html>"""


def test_web_parses_jsonld_recipe():
    page = web.parse_page(_JSONLD_PAGE, "https://www.example.com/cacio/?utm_source=fb")
    assert page["title"] == "Best Cacio e Pepe Recipe"
    assert page["site"] == "example.com"
    assert len(page["recipes"]) == 1
    r = page["recipes"][0]
    assert r["dish_name"] == "Cacio e Pepe"
    # Ingredient lines are kept verbatim (no quantity parsing).
    assert r["ingredients"][0] == {"name": "200g spaghetti", "quantity": None, "unit": None, "prep": None}
    assert r["steps"] == ["Boil pasta", "Toss with cheese"]
    assert r["cook_time"] == "20 min"
    assert r["servings"] == "2 servings"
    assert "italian" in r["tags"]


def test_web_handles_graph_and_string_instructions():
    html = (
        '<script type="application/ld+json">{"@graph":['
        '{"@type":"WebPage"},'
        '{"@type":["Recipe"],"name":"Toast","recipeIngredient":["bread"],'
        '"recipeInstructions":"Step one\\nStep two"}]}</script>'
    )
    page = web.parse_page(html, "https://x.com/")
    assert len(page["recipes"]) == 1
    r = page["recipes"][0]
    assert r["dish_name"] == "Toast"
    assert r["steps"] == ["Step one", "Step two"]


def test_web_no_recipe_returns_empty_with_readable_text():
    html = "<html><body><p>Just a blog post about cats.</p><script>var x=1;</script></body></html>"
    page = web.parse_page(html, "https://x.com/")
    assert page["recipes"] == []
    assert "cats" in page["text"]
    assert "var x" not in page["text"]  # script content excluded from fallback text


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://Example.com/Recipe/?utm_source=fb&id=3#frag", "https://example.com/Recipe?id=3"),
        ("https://www.site.com/a/", "https://www.site.com/a"),
        ("http://site.com", "http://site.com/"),
    ],
)
def test_canonical_url(url, expected):
    assert web.canonical_url(url) == expected


def test_canonical_url_rejects_non_web():
    with pytest.raises(MepError):
        web.canonical_url("ftp://nope.com/x")


# --- text + source dispatch ---------------------------------------------------


def test_add_text_stores_and_is_idempotent(monkeypatch):
    db.init_db()
    conn = db.connect()
    monkeypatch.setattr(
        ingest, "extract_recipes",
        lambda t, *, title, config: [
            {"dish_name": "Soup", "ingredients": [{"name": "water"}], "steps": ["boil"], "tags": []}
        ],
    )
    monkeypatch.setattr(
        ingest, "classify_recipe", lambda d, *, config: {"meal_type": "dinner", "health_score": 5}
    )
    status, results = ingest.add_text(conn, {}, "some recipe text")
    assert status == "added" and len(results) == 1
    assert db.get_recipe(conn, results[0][0])["recipe"]["source_type"] == "text"
    # Same text hashes to the same id -> idempotent skip.
    status2, _ = ingest.add_text(conn, {}, "some recipe text")
    assert status2 == "skipped"


def test_add_text_no_recipe_raises(monkeypatch):
    db.init_db()
    conn = db.connect()
    monkeypatch.setattr(ingest, "extract_recipes", lambda t, *, title, config: [])
    with pytest.raises(MepError):
        ingest.add_text(conn, {}, "not a recipe at all")


def test_add_source_routes_by_kind(monkeypatch, tmp_path):
    conn = db.connect()
    monkeypatch.setattr(ingest, "add_video", lambda c, cfg, u: ("video", []))
    monkeypatch.setattr(ingest, "add_url", lambda c, cfg, u: ("web", []))
    monkeypatch.setattr(ingest, "add_text", lambda c, cfg, t, title=None: ("text", []))
    monkeypatch.setattr(ingest, "add_images", lambda c, cfg, paths: ("image", []))
    assert ingest.add_source(conn, {}, "https://youtu.be/dQw4w9WgXcQ")[0] == "video"
    assert ingest.add_source(conn, {}, "https://example.com/recipe")[0] == "web"
    f = tmp_path / "r.txt"
    f.write_text("paste me")
    assert ingest.add_source(conn, {}, str(f))[0] == "text"
    img = tmp_path / "card.png"
    img.write_bytes(b"fake png bytes")
    assert ingest.add_source(conn, {}, str(img))[0] == "image"  # routed to vision
    with pytest.raises(MepError):
        ingest.add_source(conn, {}, "not-a-url-or-file")


# --- image ingestion ----------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [("a.jpg", "image/jpeg"), ("a.JPEG", "image/jpeg"), ("a.png", "image/png"),
     ("a.webp", "image/webp"), ("a.gif", "image/gif")],
)
def test_image_media_type_accepts_supported(name, expected):
    assert ingest._image_media_type(name) == expected


@pytest.mark.parametrize("name", ["photo.heic", "doc.pdf", "notes.txt", "noext"])
def test_image_media_type_rejects_unsupported(name):
    with pytest.raises(MepError):
        ingest._image_media_type(name)


def _patch_vision(monkeypatch, recipes):
    monkeypatch.setattr(
        ingest, "extract_recipes_from_images", lambda imgs, *, title, config: recipes
    )
    monkeypatch.setattr(
        ingest, "classify_recipe", lambda d, *, config: {"meal_type": "lunch", "health_score": 5}
    )


def test_add_images_stores_and_is_idempotent(monkeypatch, tmp_path):
    db.init_db()
    conn = db.connect()
    _patch_vision(
        monkeypatch,
        [{"dish_name": "Card Soup", "ingredients": [{"name": "water"}], "steps": ["boil"], "tags": []}],
    )
    img = tmp_path / "card.png"
    img.write_bytes(b"\x89PNG fake image data")
    status, results = ingest.add_images(conn, {}, [str(img)])
    assert status == "added" and len(results) == 1
    assert db.get_recipe(conn, results[0][0])["recipe"]["source_type"] == "image"
    # Same bytes hash to the same id -> idempotent skip.
    assert ingest.add_images(conn, {}, [str(img)])[0] == "skipped"


def test_add_images_combines_multiple_into_one(monkeypatch, tmp_path):
    db.init_db()
    conn = db.connect()
    _patch_vision(
        monkeypatch,
        [{"dish_name": "Two Page Stew", "ingredients": [{"name": "beef"}], "steps": ["simmer"], "tags": []}],
    )
    p1 = tmp_path / "p1.jpg"
    p2 = tmp_path / "p2.jpg"
    p1.write_bytes(b"page one")
    p2.write_bytes(b"page two")
    status, results = ingest.add_images(conn, {}, [str(p1), str(p2)])
    assert status == "added" and len(results) == 1  # one recipe from two images


def test_add_images_rejects_oversized(monkeypatch, tmp_path):
    monkeypatch.setattr(ingest, "_MAX_IMAGE_BYTES", 10)
    db.init_db()
    conn = db.connect()
    big = tmp_path / "big.png"
    big.write_bytes(b"x" * 50)
    with pytest.raises(MepError):
        ingest.add_images(conn, {}, [str(big)])


def test_add_images_no_recipe_raises(monkeypatch, tmp_path):
    db.init_db()
    conn = db.connect()
    _patch_vision(monkeypatch, [])
    img = tmp_path / "blank.png"
    img.write_bytes(b"not a recipe")
    with pytest.raises(MepError):
        ingest.add_images(conn, {}, [str(img)])


def test_insert_recipe_records_source_type():
    db.init_db()
    conn = db.connect()
    rid = db.insert_recipe(
        conn, video_id="srctype01", title="t", channel="c", url="u", raw_transcript=None,
        extracted={"dish_name": "d", "ingredients": [], "steps": [], "tags": []},
        source_type="web",
    )
    assert db.get_recipe(conn, rid)["recipe"]["source_type"] == "web"
    # Default stays youtube for the existing callers.
    rid2 = _seed_recipe(conn, "srctype02")
    assert db.get_recipe(conn, rid2)["recipe"]["source_type"] == "youtube"


# --- multi-recipe extraction --------------------------------------------------


def test_parse_recipes_keeps_named_drops_rest():
    data = {
        "recipes": [
            {"dish_name": "Pasta", "steps": ["boil"]},
            {"dish_name": None},  # non-recipe signal -> dropped
            "junk",  # not a dict -> dropped
        ]
    }
    assert _parse_recipes(data) == [{"dish_name": "Pasta", "steps": ["boil"]}]


def test_parse_recipes_handles_missing_or_bad_shape():
    assert _parse_recipes({}) == []
    assert _parse_recipes({"recipes": "nope"}) == []
    assert _parse_recipes({"recipes": []}) == []


def test_ingest_one_stores_multiple_recipes(monkeypatch):
    db.init_db()
    conn = db.connect()
    monkeypatch.setattr(ingest, "fetch_transcript", lambda vid: "transcript text")
    monkeypatch.setattr(
        ingest, "extract_recipes",
        lambda t, *, title, config: [
            {"dish_name": "Pasta", "ingredients": [{"name": "noodles"}], "steps": ["boil"], "tags": []},
            {"dish_name": "Salad", "ingredients": [{"name": "lettuce"}], "steps": ["toss"], "tags": []},
        ],
    )
    monkeypatch.setattr(
        ingest, "classify_recipe",
        lambda data, *, config: {"meal_type": "dinner", "health_score": 6},
    )
    status, results = ingest.ingest_one(conn, {}, "multivid001", "Two Dishes", "Chef")
    assert status == "added"
    assert len(results) == 2
    # First recipe anchors the real video_id; the extra gets a '#2' suffix.
    assert db.video_exists(conn, "multivid001")
    assert db.video_exists(conn, "multivid001#2")
    dishes = {db.get_recipe(conn, rid)["recipe"]["dish_name"] for rid, *_ in results}
    assert dishes == {"Pasta", "Salad"}
    # Classified at ingest and stored on the row.
    assert results[0][2:] == ("dinner", 6)
    assert db.get_recipe(conn, results[0][0])["recipe"]["meal_type"] == "dinner"
    assert db.get_recipe(conn, results[0][0])["recipe"]["health_score"] == 6
    # Only the first row carries the (large) transcript.
    assert db.get_recipe(conn, results[0][0])["recipe"]["raw_transcript"] == "transcript text"
    assert db.get_recipe(conn, results[1][0])["recipe"]["raw_transcript"] is None


def test_ingest_one_non_recipe_stores_single_stub(monkeypatch):
    db.init_db()
    conn = db.connect()
    monkeypatch.setattr(ingest, "fetch_transcript", lambda vid: "just vlog talk")
    monkeypatch.setattr(ingest, "extract_recipes", lambda t, *, title, config: [])
    # A non-recipe stub has no dish_name, so classification is skipped entirely.
    monkeypatch.setattr(
        ingest, "classify_recipe",
        lambda data, *, config: pytest.fail("should not classify a non-recipe stub"),
    )
    status, results = ingest.ingest_one(conn, {}, "vlogvid0001", "My Day", "Chef")
    assert status == "added"
    assert len(results) == 1
    assert db.get_recipe(conn, results[0][0])["recipe"]["dish_name"] is None
    assert results[0][2:] == (None, None)


# --- gap check ----------------------------------------------------------------


def test_normalize_gaps_cleans_and_tolerates():
    assert _normalize_gaps({"gaps": [" missing temp ", "", 2]}) == ["missing temp", "2"]
    assert _normalize_gaps({}) == []
    assert _normalize_gaps({"gaps": "not a list"}) == []


def test_gaps_cache_distinguishes_unchecked_from_clean():
    db.init_db()
    conn = db.connect()
    rid = _seed_recipe(conn, "gapsvid0001")
    assert db.get_gaps(conn, rid) is None  # never checked
    db.save_gaps(conn, rid, [])
    assert db.get_gaps(conn, rid) == []  # checked, looked complete
    db.save_gaps(conn, rid, ["a step has no temperature"])
    assert db.get_gaps(conn, rid) == ["a step has no temperature"]


# --- classification + discovery -----------------------------------------------


def test_normalize_classification_coerces_and_validates():
    out = _normalize_classification({"meal_type": " Dinner ", "health_score": "7"})
    assert out == {"meal_type": "dinner", "health_score": 7}
    # 'sweets' is an accepted meal type; the old 'dessert' label is not.
    assert _normalize_classification({"meal_type": "Sweets", "health_score": 3})["meal_type"] == "sweets"
    assert _normalize_classification({"meal_type": "dessert", "health_score": 3})["meal_type"] is None
    # Out-of-range clamps; unknown meal type -> None; junk -> None.
    assert _normalize_classification({"meal_type": "brunch", "health_score": 99}) == {
        "meal_type": None, "health_score": 10,
    }
    assert _normalize_classification({"meal_type": 5, "health_score": "x"}) == {
        "meal_type": None, "health_score": None,
    }


def test_migrate_renames_dessert_to_sweets():
    db.init_db()
    conn = db.connect()
    rid = _seed_recipe(conn, "dessertmig01")
    db.save_classification(conn, rid, "dessert", 2)  # legacy label written directly
    db._migrate(conn)
    conn.commit()  # connect() commits after _migrate; mirror that here
    assert db.get_recipe(conn, rid)["recipe"]["meal_type"] == "sweets"


@pytest.mark.parametrize(
    "healthy,indulgent,mn,mx,expected",
    [
        (False, False, None, None, (None, None)),  # no filter
        (True, False, None, None, (7, None)),       # --healthy
        (False, True, None, None, (None, 4)),       # --indulgent
        (True, False, 5, 8, (5, 8)),                 # explicit overrides shortcut
    ],
)
def test_health_range(healthy, indulgent, mn, mx, expected):
    assert cli._health_range(healthy, indulgent, mn, mx) == expected


def test_recipe_ids_for_classify_tracks_unclassified():
    db.init_db()
    conn = db.connect()
    a = _seed_recipe(conn, "classvid001")
    b = _seed_recipe(conn, "classvid002")
    assert set(db.recipe_ids_for_classify(conn)) >= {a, b}
    db.save_classification(conn, a, "dinner", 6)
    pending = db.recipe_ids_for_classify(conn)
    assert a not in pending and b in pending
    assert a in db.recipe_ids_for_classify(conn, include_classified=True)


def _seed_classified(conn, video_id, meal_type, health, ingredients):
    rid = db.insert_recipe(
        conn, video_id=video_id, title="t", channel="c", url="u", raw_transcript=None,
        extracted={
            "dish_name": video_id, "tags": [], "steps": ["do"],
            "ingredients": [{"name": n, "quantity": None, "unit": None, "prep": None} for n in ingredients],
        },
    )
    db.save_classification(conn, rid, meal_type, health)
    return rid


def test_discover_filters_by_type_health_and_ingredient():
    db.init_db()
    conn = db.connect()
    bfast = _seed_classified(conn, "disc_bfast", "breakfast", 9, ["oats", "banana"])
    dinner = _seed_classified(conn, "disc_dinner", "dinner", 3, ["beef", "cheese"])
    salad = _seed_classified(conn, "disc_salad", "dinner", 8, ["lettuce", "chicken"])

    def ids(**kw):
        return {r["id"] for r in db.discover(conn, count=99, **kw)}

    # All three are reachable with no filter.
    assert {bfast, dinner, salad} <= ids()
    assert ids(meal_type="breakfast") & {bfast, dinner, salad} == {bfast}
    assert ids(min_health=7) & {bfast, dinner, salad} == {bfast, salad}  # healthy
    assert ids(max_health=4) & {bfast, dinner, salad} == {dinner}        # indulgent
    assert ids(meal_type="dinner", min_health=7) & {bfast, dinner, salad} == {salad}
    assert ids(ingredients=["chicken"]) & {bfast, dinner, salad} == {salad}
    # Must include *all* listed ingredients.
    assert ids(ingredients=["chicken", "beef"]) & {bfast, dinner, salad} == set()


def test_discover_count_limits_results():
    db.init_db()
    conn = db.connect()
    for i in range(5):
        _seed_classified(conn, f"disc_n{i}", "lunch", 5, ["rice"])
    assert len(db.discover(conn, meal_type="lunch", count=3)) == 3


# --- delete -------------------------------------------------------------------


def test_delete_recipe_removes_children_and_fts():
    db.init_db()
    conn = db.connect()
    rid = db.insert_recipe(
        conn,
        video_id="delvid00001",
        title="Doomed",
        channel="Chef",
        url="u",
        raw_transcript="x",
        extracted={
            "dish_name": "Garlic Soup",
            "ingredients": [{"name": "garlic", "quantity": "6", "unit": "cloves", "prep": None}],
            "steps": ["simmer"],
            "tags": ["soup"],
        },
    )
    db.save_plan(conn, rid, [{"instruction": "a", "duration_minutes": 1, "mode": "active"}])
    assert any(r["id"] == rid for r in db.search(conn, "garlic"))

    db.delete_recipe(conn, rid)

    assert db.get_recipe(conn, rid) is None
    assert db.get_plan(conn, rid) == []  # children cascaded
    assert not any(r["id"] == rid for r in db.search(conn, "garlic"))  # FTS gone


# --- export to markdown -------------------------------------------------------


def test_to_markdown_renders_card():
    data = {
        "recipe": {
            "dish_name": "Garlic Butter Pasta",
            "title": "Best Pasta",
            "channel": "Test Kitchen",
            "url": "https://youtu.be/x",
            "cook_time": "20 minutes",
            "servings": "2",
            "difficulty": "easy",
            "times_cooked": 3,
            "rating": None,
            "notes": None,
        },
        "ingredients": [
            {"name": "spaghetti", "quantity": "200", "unit": "g", "prep": None},
            {"name": "garlic", "quantity": "a handful", "unit": None, "prep": "minced"},
        ],
        "steps": [
            {"step_number": 1, "instruction": "boil the pasta"},
            {"step_number": 2, "instruction": "toss together"},
        ],
        "tags": ["italian", "pasta"],
    }
    md = cli._to_markdown(data)
    assert md.startswith("# Garlic Butter Pasta\n")
    assert "cooked 3x" in md
    assert "- 200 g spaghetti" in md
    assert "- a handful garlic, minced" in md  # verbatim + prep
    assert "1. boil the pasta" in md
    assert "Tags: italian, pasta" in md
    assert md.endswith("\n")


def test_to_markdown_untitled_no_recipe():
    data = {
        "recipe": {
            "dish_name": None, "title": None, "channel": None, "url": None,
            "cook_time": None, "servings": None, "difficulty": None, "times_cooked": 0,
            "rating": None, "notes": None,
        },
        "ingredients": [],
        "steps": [],
        "tags": [],
    }
    assert cli._to_markdown(data) == "# (untitled)\n"


# --- shopping list normalization ----------------------------------------------


def test_normalize_shopping_drops_empties():
    sections = shopping._normalize(
        {
            "sections": [
                {"aisle": " Produce ", "items": ["2 lemons", "", "6 cloves garlic"]},
                {"aisle": "Dairy", "items": []},  # dropped: no items
                {"items": ["salt"]},  # missing aisle -> Other
                "junk",  # dropped
            ]
        }
    )
    assert sections == [
        {"aisle": "Produce", "items": ["2 lemons", "6 cloves garlic"]},
        {"aisle": "Other", "items": ["salt"]},
    ]


def test_normalize_shopping_empty_raises():
    with pytest.raises(MepError):
        shopping._normalize({"sections": [{"aisle": "Produce", "items": []}]})


# --- adapt: save-copy + overwrite ---------------------------------------------


def test_next_adapted_video_id_is_unique():
    db.init_db()
    conn = db.connect()
    rid = _seed_recipe(conn, "basevideo01")
    first = db.next_adapted_video_id(conn, "basevideo01")
    assert first == "basevideo01~adapted"
    # Take it, then the next one steps to a fresh suffix.
    db.insert_recipe(
        conn,
        video_id=first,
        title="t (adapted)",
        channel="c",
        url="u",
        raw_transcript=None,
        extracted={"dish_name": "d", "ingredients": [], "steps": [], "tags": []},
    )
    assert db.next_adapted_video_id(conn, "basevideo01") == "basevideo01~adapted2"


def test_replace_recipe_content_swaps_and_clears_caches():
    db.init_db()
    conn = db.connect()
    rid = db.insert_recipe(
        conn,
        video_id="replacevid01",
        title="Original",
        channel="Chef",
        url="u",
        raw_transcript="x",
        extracted={
            "dish_name": "Shawarma",
            "ingredients": [{"name": "pita flour", "quantity": "150", "unit": "g", "prep": None}],
            "steps": ["make pita", "assemble"],
            "tags": ["wrap"],
        },
    )
    db.save_plan(conn, rid, [{"instruction": "a", "duration_minutes": 1, "mode": "active"}])
    db.save_components(conn, rid, [{"name": "Pita", "purpose": "", "ingredients": [], "make_steps": [1]}])
    db.save_gaps(conn, rid, ["no temperature given"])
    assert any(r["id"] == rid for r in db.search(conn, "pita"))

    db.replace_recipe_content(
        conn,
        rid,
        {
            "dish_name": "Shawarma",
            "cook_time": None,
            "servings": None,
            "difficulty": None,
            "ingredients": [{"name": "store-bought pita", "quantity": "1", "unit": None, "prep": None}],
            "steps": ["assemble"],
            "tags": ["wrap"],
        },
    )

    full = db.get_recipe(conn, rid)
    assert [s["instruction"] for s in full["steps"]] == ["assemble"]
    assert full["ingredients"][0]["name"] == "store-bought pita"
    # Stale caches cleared; FTS reflects the new ingredient, not the old one.
    assert db.get_plan(conn, rid) == []
    assert db.get_components(conn, rid) == []
    assert db.get_gaps(conn, rid) is None
    assert any(r["id"] == rid for r in db.search(conn, "store-bought"))
    assert not any(r["id"] == rid for r in db.search(conn, "flour"))
