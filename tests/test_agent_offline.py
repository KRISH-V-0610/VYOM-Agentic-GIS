"""
Offline tests for the VYOM agent layer — no database, no API key, no network.

- tools.list_events / tools.get_event_aoi read from config YAML only.
- The orchestrator loop is driven by ScriptedBackend (deterministic, no SDK).
- coerce_args / dispatch are pure-function tests.

Run with:  pytest tests/test_agent_offline.py -v
"""
import json
import pytest

from vyom.agent import tools
from vyom.agent.llm import ScriptedBackend
from vyom.agent.orchestrator import VyomAgent


# ── tools.list_events / get_event_aoi ────────────────────────────────────────

class TestListEvents:
    def test_returns_at_least_six_events(self):
        result = tools.list_events()
        assert result["count"] >= 6

    def test_count_matches_events_list(self):
        result = tools.list_events()
        assert result["count"] == len(result["events"])

    def test_known_event_keys_present(self):
        keys = {e["key"] for e in tools.list_events()["events"]}
        expected = {
            "kerala_periyar_2018",
            "assam_brahmaputra_2022",
            "bihar_kosi_ganga_2019",
            "wildfire_uttarakhand_2016",
            "landslide_sikkim_glof_2023",
            "drought_marathwada_2016",
        }
        assert expected.issubset(keys)

    def test_each_event_has_required_fields(self):
        for e in tools.list_events()["events"]:
            assert "key" in e
            assert "hazard_type" in e
            assert "bbox" in e
            assert "primary_index" in e


class TestGetEventAoi:
    def test_valid_key_kerala(self):
        result = tools.get_event_aoi("kerala_periyar_2018")
        assert "error" not in result
        assert result["event_key"] == "kerala_periyar_2018"
        assert result["primary_index"].lower() == "ndwi"

    def test_valid_key_wildfire(self):
        result = tools.get_event_aoi("wildfire_uttarakhand_2016")
        assert result["primary_index"].lower() == "nbr"

    def test_aoi_is_closed_polygon(self):
        result = tools.get_event_aoi("kerala_periyar_2018")
        aoi = result["aoi_geojson"]
        assert aoi["type"] == "Polygon"
        ring = aoi["coordinates"][0]
        assert len(ring) == 5
        assert ring[0] == ring[-1]

    def test_aoi_bbox_matches_polygon_extent(self):
        result = tools.get_event_aoi("kerala_periyar_2018")
        bbox = result["bbox"]
        ring = result["aoi_geojson"]["coordinates"][0]
        lons = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        assert min(lons) == bbox[0]
        assert min(lats) == bbox[1]
        assert max(lons) == bbox[2]
        assert max(lats) == bbox[3]

    def test_invalid_key_returns_error(self):
        result = tools.get_event_aoi("nonexistent_event_xyz")
        assert "error" in result
        assert "available_events" in result
        assert "kerala_periyar_2018" in result["available_events"]

    def test_event_date_present(self):
        result = tools.get_event_aoi("kerala_periyar_2018")
        assert result.get("event_date") is not None


# ── coerce_args ───────────────────────────────────────────────────────────────

class TestCoerceArgs:
    _POLY = {"type": "Polygon",
             "coordinates": [[[76, 9], [77, 9], [77, 10], [76, 10], [76, 9]]]}

    def test_json_string_decoded_to_dict(self):
        coerced = tools.coerce_args("check_coverage",
                                    {"aoi_geojson": json.dumps(self._POLY)})
        assert isinstance(coerced["aoi_geojson"], dict)
        assert coerced["aoi_geojson"]["type"] == "Polygon"

    def test_dict_passes_through_unchanged(self):
        coerced = tools.coerce_args("check_coverage", {"aoi_geojson": self._POLY})
        assert coerced["aoi_geojson"] is self._POLY

    def test_null_optional_args_dropped(self):
        coerced = tools.coerce_args("list_scenes",
                                    {"event_key": None, "sensor": None, "limit": 50})
        assert "event_key" not in coerced
        assert "sensor" not in coerced
        assert coerced["limit"] == 50

    def test_empty_string_optional_args_dropped(self):
        coerced = tools.coerce_args("list_scenes", {"event_key": "", "limit": 10})
        assert "event_key" not in coerced

    def test_invalid_json_string_raises_value_error(self):
        with pytest.raises(ValueError, match="not valid JSON"):
            tools.coerce_args("check_coverage", {"aoi_geojson": "not-json{"})

    def test_non_aoi_params_not_json_decoded(self):
        coerced = tools.coerce_args("list_scenes", {"event_key": "kerala_periyar_2018"})
        assert coerced["event_key"] == "kerala_periyar_2018"


# ── dispatch ──────────────────────────────────────────────────────────────────

class TestDispatch:
    def test_unknown_tool_returns_error_dict(self):
        result = tools.dispatch("totally_fake_tool", {})
        assert "error" in result
        assert "totally_fake_tool" in result["error"]
        assert "available" in result

    def test_list_events_via_dispatch(self):
        result = tools.dispatch("list_events", {})
        assert "events" in result
        assert result["count"] >= 6

    def test_get_event_aoi_via_dispatch(self):
        result = tools.dispatch("get_event_aoi", {"event_key": "assam_brahmaputra_2022"})
        assert "aoi_geojson" in result

    def test_dispatch_calls_coerce_args_on_json_string(self):
        # coerce_args runs before dispatch reaches the function — an invalid JSON
        # string surfaces as a ValueError, not an "Unknown tool" error.
        result = tools.dispatch("check_coverage", {"aoi_geojson": "not-json{"})
        assert "error" in result
        assert "not valid JSON" in result["error"]

    def test_exception_in_fn_returns_error_dict(self):
        # Calling get_event_aoi without the required arg should surface as an error.
        result = tools.dispatch("get_event_aoi", {})
        assert "error" in result


# ── declarations / registry sanity ────────────────────────────────────────────

class TestDeclarations:
    def test_all_registry_keys_have_declarations(self):
        declared = {d["name"] for d in tools.DECLARATIONS}
        for name in tools.REGISTRY:
            assert name in declared, f"{name} in REGISTRY but not in DECLARATIONS"

    def test_all_heavy_tools_are_in_registry(self):
        for name in tools.HEAVY_TOOLS:
            assert name in tools.REGISTRY

    def test_declarations_have_required_fields(self):
        for d in tools.DECLARATIONS:
            assert "name" in d
            assert "description" in d
            assert "parameters" in d


# ── VyomAgent with ScriptedBackend ────────────────────────────────────────────

class TestOrchestrator:
    def test_text_only_answer_on_first_step(self):
        backend = ScriptedBackend([
            {"text": "VYOM covers 6 registered events.", "function_calls": []},
        ])
        result = VyomAgent(backend=backend).run("What events exist?")
        assert result["answer"] == "VYOM covers 6 registered events."
        assert result["stopped"] == "answer"
        assert result["steps"] == 1
        assert result["tool_calls"] == []
        assert result["coverage_checked"] is False

    def test_single_tool_call_then_answer(self):
        backend = ScriptedBackend([
            {"function_calls": [{"name": "list_events", "args": {}}]},
            {"text": "Found 6 events.", "function_calls": []},
        ])
        result = VyomAgent(backend=backend).run("List events.")
        assert result["stopped"] == "answer"
        assert result["answer"] == "Found 6 events."
        assert len(result["tool_calls"]) == 1
        tc = result["tool_calls"][0]
        assert tc["name"] == "list_events"
        assert tc["step"] == 1
        assert "events" in tc["result"]

    def test_coverage_checked_flag_set(self):
        fake_aoi = json.dumps(
            {"type": "Polygon",
             "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]})
        backend = ScriptedBackend([
            {"function_calls": [
                {"name": "check_coverage", "args": {"aoi_geojson": fake_aoi}}]},
            {"text": "Coverage result received.", "function_calls": []},
        ])
        result = VyomAgent(backend=backend).run("Any data near 0,0?")
        assert result["coverage_checked"] is True

    def test_policy_blocks_data_tool_before_coverage(self):
        backend = ScriptedBackend([
            # Agent tries list_scenes before check_coverage
            {"function_calls": [
                {"name": "list_scenes", "args": {"event_key": "kerala_periyar_2018"}}]},
            {"text": "I should have called check_coverage first.", "function_calls": []},
        ])
        result = VyomAgent(backend=backend).run("Show Kerala scenes.")
        tc = result["tool_calls"][0]
        assert tc["name"] == "list_scenes"
        assert "error" in tc["result"]
        assert "Policy" in tc["result"]["error"]
        assert result["coverage_checked"] is False

    def test_pre_coverage_tools_exempt_from_policy(self):
        """list_events and get_event_aoi are allowed before check_coverage."""
        backend = ScriptedBackend([
            {"function_calls": [{"name": "list_events", "args": {}}]},
            {"function_calls": [
                {"name": "get_event_aoi",
                 "args": {"event_key": "kerala_periyar_2018"}}]},
            {"text": "AOI resolved, now I will check coverage.", "function_calls": []},
        ])
        result = VyomAgent(backend=backend).run("Find Kerala event.")
        for tc in result["tool_calls"]:
            assert "Policy" not in str(tc["result"])
        assert result["coverage_checked"] is False  # coverage not yet called

    def test_max_steps_stops_loop(self):
        backend = ScriptedBackend([
            {"function_calls": [{"name": "list_events", "args": {}}]},
            {"function_calls": [{"name": "list_events", "args": {}}]},
            {"function_calls": [{"name": "list_events", "args": {}}]},
        ])
        result = VyomAgent(backend=backend, max_steps=3).run("Flood data?")
        assert result["stopped"] == "max_steps"
        assert result["steps"] == 3
        assert len(result["tool_calls"]) == 3

    def test_heavy_tools_flagged_in_trace(self):
        """Tool calls to heavy tools have heavy=True; catalog tools have heavy=False."""
        fake_aoi = json.dumps(
            {"type": "Polygon",
             "coordinates": [[[76, 9], [77, 9], [77, 10], [76, 10], [76, 9]]]})
        backend = ScriptedBackend([
            # check_coverage first (will fail on DB but flag flips)
            {"function_calls": [
                {"name": "check_coverage", "args": {"aoi_geojson": fake_aoi}}]},
            # flood_extent (heavy, will error without real DB/file)
            {"function_calls": [
                {"name": "flood_extent", "args": {"scene_id": "fake_scene_id"}}]},
            {"text": "Analysis complete.", "function_calls": []},
        ])
        result = VyomAgent(backend=backend).run("Flood extent?")
        by_name = {tc["name"]: tc for tc in result["tool_calls"]}
        assert by_name["check_coverage"]["heavy"] is False
        assert by_name["flood_extent"]["heavy"] is True

    def test_multiple_tool_calls_in_one_step(self):
        """A single model turn may propose multiple tool calls simultaneously."""
        backend = ScriptedBackend([
            {"function_calls": [
                {"name": "list_events", "args": {}},
                {"name": "get_event_aoi",
                 "args": {"event_key": "kerala_periyar_2018"}},
            ]},
            {"text": "Got both.", "function_calls": []},
        ])
        result = VyomAgent(backend=backend).run("Events and Kerala AOI?")
        assert len(result["tool_calls"]) == 2
        names = {tc["name"] for tc in result["tool_calls"]}
        assert names == {"list_events", "get_event_aoi"}
        # Both from step 1
        assert all(tc["step"] == 1 for tc in result["tool_calls"])

    def test_result_dict_has_all_keys(self):
        backend = ScriptedBackend([
            {"text": "Done.", "function_calls": []},
        ])
        result = VyomAgent(backend=backend).run("Hi")
        for key in ("answer", "tool_calls", "coverage_checked", "steps",
                    "stopped", "history"):
            assert key in result

    def test_scripted_backend_callable_step(self):
        """ScriptedBackend accepts callable steps for history-aware responses."""
        def dynamic_step(history):
            user_text = history[0]["text"]
            return {"text": f"Echo: {user_text}", "function_calls": []}

        backend = ScriptedBackend([dynamic_step])
        result = VyomAgent(backend=backend).run("Hello world")
        assert result["answer"] == "Echo: Hello world"

    def test_scripted_backend_runs_out_raises(self):
        backend = ScriptedBackend([
            {"text": "Done.", "function_calls": []},
        ])
        agent = VyomAgent(backend=backend, max_steps=5)
        agent.run("First query")  # consumes the one step
        with pytest.raises(AssertionError, match="ran out"):
            agent.run("Second query — no steps left")
