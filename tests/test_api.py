"""
FastAPI endpoint tests using HTTPX TestClient.

- GET  /health  — always returns 200 (status: ok|degraded)
- GET  /events  — reads YAML, no DB needed
- GET  /scenes  — hits DB (marked integration); skipped without live DB
- POST /query   — VyomAgent dependency overridden with ScriptedBackend
- GET  /exports — file-not-found and path-traversal guards

Run offline tests only:  pytest tests/test_api.py -m "not integration" -v
Run all (needs live DB): pytest tests/test_api.py -v
"""
import json
import pytest
from fastapi.testclient import TestClient

from vyom.api.app import app, get_agent_factory
from vyom.agent.llm import ScriptedBackend
from vyom.agent.orchestrator import VyomAgent


# ── helpers ───────────────────────────────────────────────────────────────────

def _scripted_agent(*steps):
    """Return a VyomAgent with the given scripted steps."""
    return VyomAgent(backend=ScriptedBackend(list(steps)))


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def client_with_agent(client):
    """Fixture that yields (client, override-setter) for /query tests.

    The test passes a zero-arg callable returning a (scripted) VyomAgent; we adapt it
    to the factory signature ``(max_steps, model) -> agent`` that /query expects.
    """
    def _set(agent_zero_arg):
        app.dependency_overrides[get_agent_factory] = (
            lambda: (lambda max_steps=12, model=None: agent_zero_arg()))

    yield client, _set
    app.dependency_overrides.clear()


# ── GET /health ───────────────────────────────────────────────────────────────

class TestHealth:
    def test_always_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_response_schema(self, client):
        body = client.get("/health").json()
        assert "status" in body
        assert body["status"] in ("ok", "degraded")
        assert "db_ok" in body
        assert isinstance(body["db_ok"], bool)
        assert "llm_key_configured" in body
        assert isinstance(body["llm_key_configured"], bool)
        assert "llm_provider" in body

    def test_detail_present_when_db_down(self, client):
        body = client.get("/health").json()
        if not body["db_ok"]:
            # detail should explain why (not just null)
            assert body.get("detail") is not None


# ── GET /events ───────────────────────────────────────────────────────────────

class TestEvents:
    def test_returns_200(self, client):
        assert client.get("/events").status_code == 200

    def test_count_is_at_least_six(self, client):
        assert client.get("/events").json()["count"] >= 6

    def test_known_event_keys_present(self, client):
        keys = {e["key"] for e in client.get("/events").json()["events"]}
        assert "kerala_periyar_2018" in keys
        assert "wildfire_uttarakhand_2016" in keys

    def test_event_schema(self, client):
        for e in client.get("/events").json()["events"]:
            assert "key" in e
            assert "hazard_type" in e
            # bbox is a list of 4 floats
            if e.get("bbox"):
                assert len(e["bbox"]) == 4

    def test_count_matches_list_length(self, client):
        body = client.get("/events").json()
        assert body["count"] == len(body["events"])


# ── GET /scenes ───────────────────────────────────────────────────────────────

class TestScenes:
    @pytest.mark.integration
    def test_returns_200(self, client):
        resp = client.get("/scenes", params={"limit": 5})
        assert resp.status_code == 200
        assert "scenes" in resp.json()

    @pytest.mark.integration
    def test_event_filter(self, client):
        resp = client.get("/scenes",
                          params={"event_key": "kerala_periyar_2018", "limit": 10})
        assert resp.status_code == 200
        for s in resp.json().get("scenes", []):
            assert s["event_key"] == "kerala_periyar_2018"

    def test_invalid_limit_rejected(self, client):
        resp = client.get("/scenes", params={"limit": 0})
        assert resp.status_code == 422

    def test_limit_too_large_rejected(self, client):
        resp = client.get("/scenes", params={"limit": 9999})
        assert resp.status_code == 422


# ── POST /query ───────────────────────────────────────────────────────────────

class TestQuery:
    def test_simple_answer_no_tools(self, client_with_agent):
        client, set_agent = client_with_agent
        set_agent(lambda: _scripted_agent(
            {"text": "VYOM covers 6 events.", "function_calls": []}
        ))
        resp = client.post("/query", json={"query": "What events does VYOM have?"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["answer"] == "VYOM covers 6 events."
        assert body["stopped"] == "answer"
        assert body["tool_calls"] == []
        assert body["coverage_checked"] is False

    def test_tool_call_trace_in_response(self, client_with_agent):
        client, set_agent = client_with_agent
        set_agent(lambda: _scripted_agent(
            {"function_calls": [{"name": "list_events", "args": {}}]},
            {"text": "Found events.", "function_calls": []},
        ))
        body = client.post("/query", json={"query": "List events."}).json()
        assert len(body["tool_calls"]) == 1
        tc = body["tool_calls"][0]
        assert tc["name"] == "list_events"
        assert tc["step"] == 1
        assert isinstance(tc["heavy"], bool)
        assert "result" in tc

    def test_history_excluded_by_default(self, client_with_agent):
        client, set_agent = client_with_agent
        set_agent(lambda: _scripted_agent(
            {"text": "Done.", "function_calls": []}
        ))
        body = client.post("/query", json={"query": "Any events?"}).json()
        assert body.get("history") is None

    def test_history_included_when_requested(self, client_with_agent):
        client, set_agent = client_with_agent
        set_agent(lambda: _scripted_agent(
            {"text": "Done.", "function_calls": []}
        ))
        body = client.post("/query",
                           json={"query": "Any events?", "include_history": True}).json()
        assert body["history"] is not None
        assert isinstance(body["history"], list)
        assert len(body["history"]) >= 2  # user turn + model turn

    def test_custom_max_steps_respected(self, client_with_agent):
        client, set_agent = client_with_agent
        set_agent(lambda: _scripted_agent(
            {"function_calls": [{"name": "list_events", "args": {}}]},
            {"text": "Done.", "function_calls": []},
        ))
        body = client.post("/query",
                           json={"query": "Flood data?", "max_steps": 5}).json()
        assert body["steps"] <= 5

    def test_empty_query_rejected(self, client_with_agent):
        # get_agent must be overridden so dependency resolution doesn't 503 before
        # Pydantic validates the body and returns 422.
        client, set_agent = client_with_agent
        set_agent(lambda: _scripted_agent({"text": "x", "function_calls": []}))
        assert client.post("/query", json={"query": ""}).status_code == 422

    def test_max_steps_zero_rejected(self, client_with_agent):
        client, set_agent = client_with_agent
        set_agent(lambda: _scripted_agent({"text": "x", "function_calls": []}))
        assert client.post("/query",
                           json={"query": "Test", "max_steps": 0}).status_code == 422

    def test_max_steps_over_limit_rejected(self, client_with_agent):
        client, set_agent = client_with_agent
        set_agent(lambda: _scripted_agent({"text": "x", "function_calls": []}))
        assert client.post("/query",
                           json={"query": "Test", "max_steps": 31}).status_code == 422

    def test_missing_query_field_rejected(self, client_with_agent):
        client, set_agent = client_with_agent
        set_agent(lambda: _scripted_agent({"text": "x", "function_calls": []}))
        assert client.post("/query", json={"max_steps": 5}).status_code == 422

    def test_policy_error_visible_in_tool_calls(self, client_with_agent):
        """Policy error surfaced when agent skips check_coverage."""
        client, set_agent = client_with_agent
        set_agent(lambda: _scripted_agent(
            {"function_calls": [
                {"name": "list_scenes", "args": {"event_key": "kerala_periyar_2018"}}]},
            {"text": "Should have called check_coverage.", "function_calls": []},
        ))
        body = client.post("/query", json={"query": "Kerala scenes."}).json()
        tc = body["tool_calls"][0]
        assert "error" in tc["result"]
        assert "Policy" in tc["result"]["error"]


# ── GET /exports/{filename} ───────────────────────────────────────────────────

class TestExports:
    def test_missing_file_returns_404(self, client):
        resp = client.get("/exports/no_such_file.png")
        assert resp.status_code == 404

    def test_path_traversal_blocked(self, client):
        # The guard in app.py rejects filenames that contain path separators
        resp = client.get("/exports/../some_file.png")
        # FastAPI may normalise the URL differently, but the guard must fire
        assert resp.status_code in (400, 404)

    def test_path_traversal_with_subdir_blocked(self, client):
        resp = client.get("/exports/subdir%2Ffile.png")
        assert resp.status_code in (400, 404)
