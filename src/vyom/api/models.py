"""Pydantic request/response schemas for the VYOM REST API."""

from typing import Any, Optional

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    query: str = Field(..., description="Natural-language disaster question.",
                       min_length=1, examples=["How did water area change in the "
                                                "Kerala 2018 floods?"])
    model: Optional[str] = Field(
        None,
        description=(
            "Google AI model to use for this request. Overrides the GEMINI_MODEL env var. "
            "Gemma examples: 'gemma-3-27b-it', 'gemma-3-12b-it', 'gemma-3-4b-it'. "
            "Gemini examples: 'gemini-2.0-flash', 'gemini-1.5-pro'. "
            "Leave null to use the server default."
        ),
        examples=["gemma-3-27b-it", "gemini-2.0-flash"],
    )
    max_steps: int = Field(12, ge=1, le=30,
                           description="Max model turns before the agent stops.")
    include_history: bool = Field(
        False, description="Include the full neutral conversation history in the response.")
    aoi_geojson: Optional[str] = Field(
        None,
        description=(
            "Optional user-drawn AOI as a JSON-encoded GeoJSON geometry (WGS84). When "
            "given, the agent is instructed to use this exact polygon for all spatial "
            "tools instead of resolving the event's default bounding box."
        ),
    )


class ToolCall(BaseModel):
    step: int
    name: str
    heavy: bool
    args: dict[str, Any]
    result: Any


class QueryResponse(BaseModel):
    answer: Optional[str]
    tool_calls: list[ToolCall]
    coverage_checked: bool
    steps: int
    stopped: str
    charts: Optional[list[str]] = None
    history: Optional[list[dict[str, Any]]] = None


class EventInfo(BaseModel):
    key: str
    hazard_type: Optional[str] = None
    display_name: Optional[str] = None
    bbox: Optional[list[float]] = None
    event_date: Optional[str] = None
    primary_index: Optional[str] = None


class EventsResponse(BaseModel):
    count: int
    events: list[EventInfo]


class HealthResponse(BaseModel):
    status: str
    db_ok: bool
    llm_provider: Optional[str] = None
    llm_key_configured: bool = False
    llm_model: Optional[str] = None
    detail: Optional[str] = None


class SceneAssetsResponse(BaseModel):
    scene_id: str
    sensor: Optional[str] = None
    satellite: Optional[str] = None
    gsd_m: Optional[float] = None
    acq_datetime: Optional[str] = None
    window_type: Optional[str] = None
    event_key: Optional[str] = None
    # [min_lon, min_lat, max_lon, max_lat] in WGS84.
    bbox_wgs84: Optional[list[float]] = None
    # asset key (e.g. "ndwi", "red") -> URL under the /ard static mount.
    assets: dict[str, str] = {}
