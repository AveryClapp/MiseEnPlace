"""Offline unit tests: no network, no API keys.

Covers the pure logic: video-id parsing, defensive JSON parsing, and a full
SQLite round-trip including FTS search. Set MEP_HOME to a tmp dir before
importing mep modules that read config paths.
"""

import os
import tempfile

import pytest

os.environ["MEP_HOME"] = tempfile.mkdtemp()

from mep import adapt, cli, config, cook, db, scale  # noqa: E402
from mep.components import _normalize_components  # noqa: E402
from mep.errors import MepError  # noqa: E402
from mep.extract import _parse_json  # noqa: E402
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
    assert any(r["id"] == rid for r in db.search(conn, "store-bought"))
    assert not any(r["id"] == rid for r in db.search(conn, "flour"))
