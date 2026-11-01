from __future__ import annotations

import concurrent.futures
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from langgraph_metarec.genres import music_genre_tags, resolve_genre_ids, split_genres
from langgraph_metarec.tool_compaction import compact_tool_output


ToolAdapter = Callable[[Dict[str, Any]], Any]
DEFAULT_TOOL_TIMEOUT_SECONDS = 12.0
DEFAULT_TOOL_QUOTA_PER_RUN = 5


class ToolDispatchEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    parameters: Dict[str, Any] = Field(default_factory=dict)


def normalize_tag(tag: str) -> str:
    value = str(tag or "").strip().lower()
    if not value:
        return value
    return value if value.startswith("#") else f"#{value}"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    domain: str
    tags: Set[str]
    input_schema: Dict[str, Any]
    output_schema: Dict[str, Any]
    adapter: ToolAdapter
    online_only: bool = False
    status: str = "active"
    description: Optional[str] = None
    timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS
    quota_per_run: int = DEFAULT_TOOL_QUOTA_PER_RUN

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("Tool timeout_seconds must be greater than 0")
        if self.quota_per_run <= 0:
            raise ValueError("Tool quota_per_run must be greater than 0")
        object.__setattr__(self, "domain", self.domain.lower())
        object.__setattr__(self, "tags", {normalize_tag(tag) for tag in self.tags})

    def matches(
        self,
        *,
        domain: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
        include_online_only: bool = True,
        active_only: bool = True,
    ) -> bool:
        if domain and self.domain != domain.lower():
            return False
        if active_only and self.status != "active":
            return False
        if self.online_only and not include_online_only:
            return False
        required_tags = {normalize_tag(tag) for tag in (tags or []) if tag}
        return required_tags.issubset(self.tags)


@dataclass
class ToolRegistry:
    _tools: Dict[str, ToolSpec] = field(default_factory=dict)

    def register(self, spec: ToolSpec) -> None:
        if not spec.name:
            raise ValueError("Tool name is required")
        if spec.name in self._tools:
            raise ValueError(f"Tool already registered: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(f"Unknown tool: {name}") from exc

    def names(self) -> List[str]:
        return sorted(self._tools)

    def resolve(
        self,
        *,
        domain: Optional[str] = None,
        tags: Optional[Iterable[str]] = None,
        include_online_only: bool = True,
        active_only: bool = True,
    ) -> List[ToolSpec]:
        return sorted(
            [
                spec
                for spec in self._tools.values()
                if spec.matches(
                    domain=domain,
                    tags=tags,
                    include_online_only=include_online_only,
                    active_only=active_only,
                )
            ],
            key=lambda spec: spec.name,
        )

    def dispatch(
        self,
        name: str,
        parameters: Optional[Dict[str, Any]] = None,
        *,
        quota_tracker: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        spec = self.get(name)
        try:
            envelope = ToolDispatchEnvelope(tool=name, parameters=parameters or {})
        except ValidationError as exc:
            return {
                "tool": name,
                "input": parameters or {},
                "success": False,
                "error": f"Tool dispatch envelope validation failed: {exc.errors()}",
            }
        if spec.status != "active":
            return {
                "tool": name,
                "input": envelope.parameters,
                "success": False,
                "error": f"Tool is not active: {spec.status}",
            }

        input_errors = validate_json_schema(spec.input_schema, envelope.parameters, "input")
        if input_errors:
            return {
                "tool": name,
                "input": envelope.parameters,
                "success": False,
                "error": "Tool input schema validation failed: " + "; ".join(input_errors),
            }

        quota_used = None
        if quota_tracker is not None:
            quota_used = int(quota_tracker.get(name, 0))
            if quota_used >= spec.quota_per_run:
                return {
                    "tool": name,
                    "input": envelope.parameters,
                    "success": False,
                    "error": f"Tool quota exceeded: {quota_used}/{spec.quota_per_run} calls used",
                    "metadata": {
                        "quota_per_run": spec.quota_per_run,
                        "quota_used": quota_used,
                        "timeout_seconds": spec.timeout_seconds,
                    },
                }
            quota_used += 1
            quota_tracker[name] = quota_used

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"tool-{name}")
        try:
            future = executor.submit(spec.adapter, envelope.parameters)
            output = future.result(timeout=spec.timeout_seconds)
            output_errors = validate_json_schema(spec.output_schema, output, "output")
            if output_errors:
                return {
                    "tool": name,
                    "input": envelope.parameters,
                    "output": output,
                    "success": False,
                    "error": "Tool output schema validation failed: " + "; ".join(output_errors),
                    "metadata": {
                        "quota_per_run": spec.quota_per_run,
                        "quota_used": quota_used,
                        "timeout_seconds": spec.timeout_seconds,
                    },
                }
            return {
                "tool": name,
                "input": envelope.parameters,
                "output": output,
                "success": output is not None,
                "metadata": {
                    "quota_per_run": spec.quota_per_run,
                    "quota_used": quota_used,
                    "timeout_seconds": spec.timeout_seconds,
                },
            }
        except concurrent.futures.TimeoutError:
            future.cancel()
            return {
                "tool": name,
                "input": envelope.parameters,
                "success": False,
                "error": f"Tool timed out after {spec.timeout_seconds:.2f}s",
                "metadata": {
                    "quota_per_run": spec.quota_per_run,
                    "quota_used": quota_used,
                    "timeout_seconds": spec.timeout_seconds,
                },
            }
        except Exception as exc:
            return {
                "tool": name,
                "input": envelope.parameters,
                "success": False,
                "error": str(exc),
                "metadata": {
                    "quota_per_run": spec.quota_per_run,
                    "quota_used": quota_used,
                    "timeout_seconds": spec.timeout_seconds,
                },
            }
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


def validate_json_schema(schema: Dict[str, Any], value: Any, path: str = "value") -> List[str]:
    if not schema:
        return []

    errors: List[str] = []
    expected_type = schema.get("type")
    if expected_type is not None and not _matches_json_type(value, expected_type):
        errors.append(f"{path}: expected {_type_name(expected_type)}, got {type(value).__name__}")
        return errors

    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: expected one of {schema['enum']!r}")

    if isinstance(value, dict):
        required = schema.get("required") or []
        for field_name in required:
            if field_name not in value:
                errors.append(f"{path}.{field_name}: missing required field")

        properties = schema.get("properties") or {}
        for field_name, field_schema in properties.items():
            if field_name in value:
                errors.extend(validate_json_schema(field_schema, value[field_name], f"{path}.{field_name}"))

        additional = schema.get("additionalProperties", True)
        if additional is False:
            extra_fields = sorted(set(value) - set(properties))
            for field_name in extra_fields:
                errors.append(f"{path}.{field_name}: additional property is not allowed")
        elif isinstance(additional, dict):
            for field_name in sorted(set(value) - set(properties)):
                errors.extend(validate_json_schema(additional, value[field_name], f"{path}.{field_name}"))

    if isinstance(value, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for idx, item in enumerate(value):
                errors.extend(validate_json_schema(item_schema, item, f"{path}[{idx}]"))

    return errors


def _matches_json_type(value: Any, expected_type: Any) -> bool:
    if isinstance(expected_type, list):
        return any(_matches_json_type(value, item) for item in expected_type)
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True


def _type_name(expected_type: Any) -> str:
    if isinstance(expected_type, list):
        return " or ".join(str(item) for item in expected_type)
    return str(expected_type)


def _gmap_search_adapter(parameters: Dict[str, Any]) -> Any:
    from agent.agent_mcp.agent_google_map import search_google_maps

    return compact_tool_output(
        "gmap.search",
        search_google_maps(
            query=parameters.get("query", ""),
            latitude=_float_param(parameters.get("anchor_lat")),
            longitude=_float_param(parameters.get("anchor_lng")),
            map_height=_bounded_place_radius(parameters.get("radius_meters"), 10000),
            max_results=int(parameters.get("max_results", 10)),
        ),
    )


def _xhs_search_adapter(parameters: Dict[str, Any]) -> Any:
    from agent.agent_mcp.agent_xiaohongshu import search_notes_by_keyword

    return compact_tool_output(
        "xhs.search",
        search_notes_by_keyword(
            keyword=parameters.get("query", ""),
            max_results=int(parameters.get("max_results", 10)),
        ),
    )


def _yelp_search_adapter(parameters: Dict[str, Any]) -> Any:
    from agent.agent_mcp.agent_yelp import search_yelp_organic_results

    return compact_tool_output(
        "yelp.search",
        search_yelp_organic_results(
            query=parameters.get("query", ""),
            location=parameters.get("location", "Singapore"),
            max_results=int(parameters.get("max_results", 10)),
        ),
    )


def _gmap_source_matcher_adapter(parameters: Dict[str, Any]) -> Any:
    return {
        "candidates": parameters.get("candidates", []),
        "reference_results": parameters.get("reference_results", []),
        "matches": [],
        "status": "passthrough",
    }


def _gmap_hotel_search_adapter(parameters: Dict[str, Any]) -> Any:
    from agent.agent_mcp.agent_google_map import search_google_maps

    return compact_tool_output(
        "gmap.hotel.search",
        search_google_maps(
            query=parameters.get("query", ""),
            latitude=_float_param(parameters.get("anchor_lat")),
            longitude=_float_param(parameters.get("anchor_lng")),
            map_height=_bounded_place_radius(parameters.get("radius_meters"), 10000),
            max_results=int(parameters.get("max_results", 10)),
        ),
    )


def _gmap_attraction_search_adapter(parameters: Dict[str, Any]) -> Any:
    from agent.agent_mcp.agent_google_map import search_google_maps

    kwargs: Dict[str, Any] = {
        "query": parameters.get("query", ""),
        "max_results": int(parameters.get("max_results", 10)),
    }
    anchor_lat = _float_param(parameters.get("anchor_lat"))
    anchor_lng = _float_param(parameters.get("anchor_lng"))
    if anchor_lat is not None and anchor_lng is not None:
        kwargs["latitude"] = anchor_lat
        kwargs["longitude"] = anchor_lng
        kwargs["map_height"] = _bounded_place_radius(parameters.get("radius_meters"), 10000)
    # Geocode the structured destination (keyless Nominatim, cached) and bias
    # the SerpAPI map search around it — without an ``ll`` anchor, ambiguous
    # tokens like "NTU" drift to whichever region Google guesses.
    location = str(parameters.get("location") or "").strip()
    if "latitude" not in kwargs and location and location.lower() != "any":
        center = _osm_geocode(location, region_hint=parameters.get("region_hint"))
        if center is not None:
            kwargs["latitude"] = center["lat"]
            kwargs["longitude"] = center["lon"]
    return compact_tool_output("gmap.attraction.search", search_google_maps(**kwargs))


# OpenStreetMap lodging discovery needs no credential: Nominatim geocodes the
# destination, then Overpass finds lodging around the resolved place. Each call
# carries a tight timeout so the worst-case pair stays below the 12s dispatch
# backstop, and both requests set the User-Agent the Nominatim usage policy
# requires.
_OSM_HTTP_TIMEOUT = httpx.Timeout(5.0, connect=3.0)
_OSM_USER_AGENT = "MetaRec/0.1 (multi-source recommendation research)"
_OSM_LODGING_TYPES = ("hotel", "guest_house", "hostel", "motel", "apartment", "chalet", "resort")
_OSM_ATTRACTION_TYPES = ("attraction", "museum", "gallery", "theme_park", "zoo", "aquarium", "viewpoint", "artwork")
_OSM_ATTRACTION_SELECTORS: Dict[str, Tuple[str, ...]] = {
    "tourism": _OSM_ATTRACTION_TYPES,
    "historic": ("castle", "fort", "monument", "memorial", "ruins", "archaeological_site"),
    "leisure": ("park", "garden", "nature_reserve", "water_park"),
    "natural": ("beach", "peak", "waterfall", "cave_entrance"),
    "man_made": ("tower", "lighthouse"),
}
_OSM_OPTIONAL_ATTRACTION_SELECTORS: Dict[str, Tuple[str, ...]] = {
    "amenity": ("university", "college"),
}
_OSM_ALLOWED_ATTRACTION_KEYS = tuple(_OSM_ATTRACTION_SELECTORS) + tuple(
    _OSM_OPTIONAL_ATTRACTION_SELECTORS
)
# Form/extractor ``attraction_types`` values -> curated OSM key/value selectors.
# Only values mapped here ever reach Overpass; raw user text never does.
_ATTRACTION_TYPE_OSM: Dict[str, Dict[str, Tuple[str, ...]]] = {
    "museum": {"tourism": ("museum",)},
    "gallery": {"tourism": ("gallery",)},
    "theme-park": {"tourism": ("theme_park",), "leisure": ("water_park",)},
    "zoo-aquarium": {"tourism": ("zoo", "aquarium")},
    "landmark": {
        "tourism": ("attraction", "artwork"),
        "historic": _OSM_ATTRACTION_SELECTORS["historic"],
        "man_made": _OSM_ATTRACTION_SELECTORS["man_made"],
    },
    "viewpoint": {"tourism": ("viewpoint",)},
    "park-nature": {
        "leisure": ("park", "garden", "nature_reserve"),
        "natural": ("peak", "waterfall", "cave_entrance"),
    },
    "historic-site": {"historic": _OSM_ATTRACTION_SELECTORS["historic"]},
    "beach": {"natural": ("beach",)},
    "university-campus": {"amenity": ("university", "college")},
}
_OSM_DEFAULT_SEARCH_RADIUS_METERS = 5000
_OSM_MIN_SEARCH_RADIUS_METERS = 2500
_OSM_MAX_SEARCH_RADIUS_METERS = 50000


# Geocode results are cached for the process lifetime (TMDB-cache style,
# failures never cached): itinerary slots and the gmap map-bias lookup repeat
# the same destination many times per request.
_GEOCODE_CACHE: Dict[Tuple[str, str], Dict[str, Any]] = {}
_GEOCODE_CACHE_MAX = 256


def _osm_geocode(location: str, region_hint: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Resolve a destination via Nominatim; None when unresolvable.

    ``region_hint`` (typically the user's profile region) is a soft preference
    among the geocoder's candidates: an ambiguous token such as "NTU" resolves
    to the candidate mentioning the hint (Singapore's Nanyang Technological
    University) instead of the globally top-ranked one, while a destination
    that matches nothing containing the hint keeps its best candidate — an
    explicit "Kyoto" is never dragged toward the user's home region."""
    hint = str(region_hint or "").strip().casefold()
    cache_key = (str(location or "").strip().casefold(), hint)
    cached = _GEOCODE_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)
    results = _http_get_json(
        "https://nominatim.openstreetmap.org/search",
        params={"q": location, "format": "jsonv2", "limit": 3, "addressdetails": 1},
        headers={"User-Agent": _OSM_USER_AGENT},
        timeout=_OSM_HTTP_TIMEOUT,
    )
    if not isinstance(results, list) or not results:
        return None
    try:
        chosen = results[0]
        if hint and len(results) > 1:
            chosen = next(
                (
                    result
                    for result in results
                    if hint in str(result.get("display_name") or "").casefold()
                ),
                results[0],
            )
        bbox = chosen.get("boundingbox")
        resolved = {
            "lat": float(chosen["lat"]),
            "lon": float(chosen["lon"]),
            "display_name": str(chosen.get("display_name") or location),
            "class": str(chosen.get("class") or ""),
            "type": str(chosen.get("type") or ""),
            "boundingbox": bbox if isinstance(bbox, list) else None,
            "ambiguous": len(results) > 1,
        }
    except (KeyError, TypeError, ValueError):
        return None
    if len(_GEOCODE_CACHE) >= _GEOCODE_CACHE_MAX:
        _GEOCODE_CACHE.clear()
    _GEOCODE_CACHE[cache_key] = dict(resolved)
    return resolved


def geocode_anchor_candidates(
    anchor_query: str,
    destination: str = "",
    *,
    max_results: int = 4,
) -> List[Dict[str, Any]]:
    """Resolve a named itinerary anchor directly with keyless Nominatim.

    Unlike lodging discovery, this lookup searches the supplied place name or
    address itself. Destination text is only context for geocoding; the caller
    still performs deterministic uniqueness checks.
    """
    anchor_query = str(anchor_query or "").strip()
    destination = str(destination or "").strip()
    if not anchor_query:
        return []
    combined = anchor_query
    if destination and destination.casefold() not in anchor_query.casefold():
        combined = f"{anchor_query}, {destination}"
    limit = max(1, min(int(max_results), 6))
    queries = [combined]
    if combined != anchor_query:
        queries.append(anchor_query)
    results: Any = None
    for query in queries:
        results = _http_get_json(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": query,
                "format": "jsonv2",
                "limit": limit,
                "addressdetails": 1,
                "namedetails": 1,
            },
            headers={"User-Agent": _OSM_USER_AGENT},
            timeout=_OSM_HTTP_TIMEOUT,
        )
        if isinstance(results, list) and results:
            break
    if not isinstance(results, list):
        return []
    candidates: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for result in results[:limit]:
        if not isinstance(result, dict):
            continue
        try:
            latitude = float(result["lat"])
            longitude = float(result["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        display_name = str(result.get("display_name") or "").strip()
        namedetails = result.get("namedetails") if isinstance(result.get("namedetails"), dict) else {}
        title = str(
            result.get("name")
            or namedetails.get("name")
            or (display_name.split(",", 1)[0] if display_name else anchor_query)
        ).strip()
        place_id = str(
            result.get("place_id")
            or f"{result.get('osm_type') or 'place'}:{result.get('osm_id') or title}"
        )
        if not title or place_id in seen:
            continue
        seen.add(place_id)
        candidates.append({
            "id": f"nominatim:{place_id}",
            "domain": "hotel",
            "title": title,
            "subtitle": display_name or None,
            "source": "Nominatim",
            "tags": [
                value for value in (
                    str(result.get("class") or "").strip(),
                    str(result.get("type") or "").strip(),
                ) if value
            ],
            "gps_coordinates": {"latitude": latitude, "longitude": longitude},
            "raw": {
                "class": result.get("class"),
                "type": result.get("type"),
                "display_name": display_name,
            },
        })
    return candidates


def _meters_from_bbox(center: Dict[str, Any]) -> Optional[int]:
    bbox = center.get("boundingbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        return None
    try:
        south, north, west, east = [float(value) for value in bbox]
        lat = float(center["lat"])
    except (KeyError, TypeError, ValueError):
        return None
    lat_span = abs(north - south) * 111_000
    lon_span = abs(east - west) * 111_000 * max(0.1, math.cos(math.radians(lat)))
    span = max(lat_span, lon_span)
    if span <= 0:
        return None
    radius = int(span * 0.65)
    return max(_OSM_MIN_SEARCH_RADIUS_METERS, min(radius, _OSM_MAX_SEARCH_RADIUS_METERS))


def _osm_dynamic_radius(center: Dict[str, Any]) -> int:
    bbox_radius = _meters_from_bbox(center)
    if bbox_radius is not None:
        return bbox_radius
    place_type = str(center.get("type") or "").lower()
    if place_type in {"country", "state", "province", "region"}:
        return _OSM_MAX_SEARCH_RADIUS_METERS
    if place_type in {"city", "municipality"}:
        return 25000
    if place_type in {"town", "borough"}:
        return 15000
    if place_type in {"suburb", "neighbourhood", "quarter", "village", "island"}:
        return 7000
    return _OSM_DEFAULT_SEARCH_RADIUS_METERS


def _bounded_place_radius(value: Any, default: int) -> int:
    try:
        radius = int(value)
    except (TypeError, ValueError):
        radius = int(default)
    return max(250, min(radius, _OSM_MAX_SEARCH_RADIUS_METERS))


def _explicit_place_center(parameters: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    latitude = _float_param(parameters.get("anchor_lat"))
    longitude = _float_param(parameters.get("anchor_lng"))
    if latitude is None or longitude is None:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return {"lat": latitude, "lon": longitude, "type": "explicit_anchor"}


def _osm_tourism_elements(lat: float, lon: float, type_regex: str, fetch_count: int, radius_meters: int) -> List[Dict[str, Any]]:
    """Fetch named tourism elements (lodging, attractions, ...) around a point
    from the Overpass API. ``out center`` gives ways/relations a representative
    coordinate."""
    overpass_query = (
        f'[out:json][timeout:5];'
        f'nwr["tourism"~"^({type_regex})$"]["name"]'
        f'(around:{radius_meters},{lat:.7f},{lon:.7f});'
        f'out tags center {fetch_count};'
    )
    with httpx.Client(timeout=_OSM_HTTP_TIMEOUT) as client:
        response = client.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": overpass_query},
            headers={"User-Agent": _OSM_USER_AGENT},
        )
        response.raise_for_status()
        data = response.json()
    elements = data.get("elements")
    return elements if isinstance(elements, list) else []


def _osm_attraction_elements(
    lat: float,
    lon: float,
    selectors: Dict[str, Tuple[str, ...]],
    fetch_count: int,
    radius_meters: int,
) -> List[Dict[str, Any]]:
    clauses = "".join(
        f'nwr["{key}"~"^({"|".join(values)})$"]["name"]'
        f'(around:{radius_meters},{lat:.7f},{lon:.7f});'
        for key, values in selectors.items()
        if key in _OSM_ALLOWED_ATTRACTION_KEYS and values
    )
    if not clauses:
        return []
    overpass_query = f'[out:json][timeout:5];({clauses});out tags center {fetch_count};'
    with httpx.Client(timeout=_OSM_HTTP_TIMEOUT) as client:
        response = client.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": overpass_query},
            headers={"User-Agent": _OSM_USER_AGENT},
        )
        response.raise_for_status()
        data = response.json()
    elements = data.get("elements")
    return elements if isinstance(elements, list) else []


def _osm_stars_value(tags: Dict[str, Any]) -> Optional[float]:
    # OSM star values include "superior" suffixes, e.g. "4S".
    raw = str(tags.get("stars") or "").strip().rstrip("sS")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def _osm_address(tags: Dict[str, Any]) -> Optional[str]:
    parts = [tags.get("addr:housenumber"), tags.get("addr:street"), tags.get("addr:city")]
    rendered = " ".join(str(part) for part in parts if part)
    return rendered or None


def _osm_hotel_discover_adapter(parameters: Dict[str, Any]) -> Any:
    tool = "osm.hotel.discover"
    location = str(parameters.get("location") or "").strip()
    # Contribute nothing without a destination (mirrors the TMDB/OpenLibrary
    # discover gate) — an un-anchored lodging dump is noise, not candidates.
    center = _explicit_place_center(parameters)
    if center is None and (not location or location.lower() == "any"):
        return compact_tool_output(tool, [])
    if center is None:
        center = _osm_geocode(location, region_hint=parameters.get("region_hint"))
    if center is None:
        return compact_tool_output(tool, [])
    limit = _max_results(parameters, default=10, ceiling=25)
    exact_stars = _float_param(parameters.get("stars"))
    # Over-fetch so a stars filter still fills the page (stars is a sparse tag).
    radius_meters = _bounded_place_radius(
        parameters.get("radius_meters"), _osm_dynamic_radius(center)
    )
    elements = _osm_tourism_elements(
        center["lat"], center["lon"], "|".join(_OSM_LODGING_TYPES), max(limit * 3, 30), radius_meters
    )
    items: List[Dict[str, Any]] = []
    for element in elements:
        tags = element.get("tags") or {}
        name = str(tags.get("name") or "").strip()
        if not name:
            continue
        stars = _osm_stars_value(tags)
        if exact_stars is not None and stars != exact_stars:
            continue
        center_point = element.get("center") or {}
        lat = element.get("lat", center_point.get("lat"))
        lon = element.get("lon", center_point.get("lon"))
        element_type = element.get("type")
        element_id = element.get("id")
        items.append(
            {
                "title": name,
                "tourism": tags.get("tourism"),
                "stars": stars,
                "address": _osm_address(tags),
                "website": tags.get("website") or tags.get("contact:website"),
                "phone": tags.get("phone") or tags.get("contact:phone"),
                "gps_coordinates": (
                    {"latitude": lat, "longitude": lon} if lat is not None and lon is not None else {}
                ),
                "link": (
                    f"https://www.openstreetmap.org/{element_type}/{element_id}"
                    if element_type and element_id
                    else None
                ),
                "searched_location": location,
                "resolved_location": center.get("display_name"),
                "search_radius_meters": radius_meters,
                "source": "openstreetmap",
            }
        )
        if len(items) >= limit:
            break
    return compact_tool_output(tool, items)


def _attraction_selectors(parameters: Dict[str, Any]) -> Dict[str, Tuple[str, ...]]:
    """Curated OSM selectors for requested attraction types."""
    raw = parameters.get("attraction_types")
    if isinstance(raw, str):
        raw = [part for part in raw.split(",")]
    selected: Dict[str, List[str]] = {}
    if isinstance(raw, list):
        for token in raw:
            mapping = _ATTRACTION_TYPE_OSM.get(str(token).strip().lower(), {})
            for key, values in mapping.items():
                bucket = selected.setdefault(key, [])
                for value in values:
                    if value not in bucket:
                        bucket.append(value)
    if not selected:
        return dict(_OSM_ATTRACTION_SELECTORS)
    return {key: tuple(values) for key, values in selected.items()}


def _osm_attraction_discover_adapter(parameters: Dict[str, Any]) -> Any:
    tool = "osm.attraction.discover"
    location = str(parameters.get("location") or "").strip()
    # Contribute nothing without a destination (same gate as osm.hotel.discover).
    center = _explicit_place_center(parameters)
    if center is None and (not location or location.lower() == "any"):
        return compact_tool_output(tool, [])
    if center is None:
        center = _osm_geocode(location, region_hint=parameters.get("region_hint"))
    if center is None:
        return compact_tool_output(tool, [])
    limit = _max_results(parameters, default=10, ceiling=25)
    radius_meters = _bounded_place_radius(
        parameters.get("radius_meters"), _osm_dynamic_radius(center)
    )
    # Type filtering happens inside the Overpass regex, so only a small headroom
    # over-fetch is needed (nameless elements are skipped below).
    elements = _osm_attraction_elements(
        center["lat"], center["lon"], _attraction_selectors(parameters), max(limit * 2, 20), radius_meters
    )
    items: List[Dict[str, Any]] = []
    for element in elements:
        tags = element.get("tags") or {}
        name = str(tags.get("name") or "").strip()
        if not name:
            continue
        center_point = element.get("center") or {}
        lat = element.get("lat", center_point.get("lat"))
        lon = element.get("lon", center_point.get("lon"))
        element_type = element.get("type")
        element_id = element.get("id")
        osm_tag = next((key for key in _OSM_ALLOWED_ATTRACTION_KEYS if tags.get(key)), None)
        osm_category = tags.get(osm_tag) if osm_tag else None
        items.append(
            {
                "title": name,
                "tourism": tags.get("tourism"),
                "amenity": tags.get("amenity"),
                "osm_tag": osm_tag,
                "osm_category": osm_category,
                "address": _osm_address(tags),
                "website": tags.get("website") or tags.get("contact:website"),
                "phone": tags.get("phone") or tags.get("contact:phone"),
                "opening_hours": tags.get("opening_hours"),
                "fee": tags.get("fee"),
                "gps_coordinates": (
                    {"latitude": lat, "longitude": lon} if lat is not None and lon is not None else {}
                ),
                "link": (
                    f"https://www.openstreetmap.org/{element_type}/{element_id}"
                    if element_type and element_id
                    else None
                ),
                "searched_location": location,
                "resolved_location": center.get("display_name"),
                "search_radius_meters": radius_meters,
                "source": "openstreetmap",
            }
        )
        if len(items) >= limit:
            break
    return compact_tool_output(tool, items)


def _credential_status(*env_names: str) -> str:
    missing = [name for name in env_names if not os.getenv(name)]
    return "missing_credentials:" + ",".join(missing) if missing else "active"


def _max_results(parameters: Dict[str, Any], default: int = 10, ceiling: int = 25) -> int:
    try:
        value = int(parameters.get("max_results", default))
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, ceiling))


def _name_list(value: Any) -> List[str]:
    """Split a names value (list, or a comma/semicolon/&-separated string) into
    clean, de-duplicated names. Used for actor/director/artist/author/publisher
    fields that may arrive either as an LLM-emitted list or free-text form input."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        tokens = [str(item) for item in value]
    else:
        tokens = re.split(r"[,;&]", str(value))
    names = [token.strip() for token in tokens if token and token.strip()]
    return list(dict.fromkeys(names))


def _float_param(value: Any) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _int_param(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


# Outbound provider HTTP timeout, sized BELOW the per-tool dispatch backstop
# (DEFAULT_TOOL_TIMEOUT_SECONDS) so a slow or unreachable provider raises here
# first — letting the worker thread finish and freeing it — instead of being
# abandoned by the dispatch timeout (which would leak a hung thread). The tight
# connect timeout fails fast on dead/unreachable hosts. Never assume a provider
# is up.
PROVIDER_HTTP_TIMEOUT = httpx.Timeout(8.0, connect=4.0)


def _http_get_json(
    url: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: Any = PROVIDER_HTTP_TIMEOUT,
    follow_redirects: bool = True,
) -> Any:  # providers return objects OR lists (e.g. Nominatim)
    with httpx.Client(timeout=timeout, follow_redirects=follow_redirects) as client:
        response = client.get(url, params=params, headers=headers)
        response.raise_for_status()
        return response.json()


def _amazon_product_search_adapter(parameters: Dict[str, Any]) -> Any:
    api_key = os.getenv("SERPAPI_KEY")
    if not api_key:
        return None
    params = {
        "api_key": api_key,
        "engine": "amazon",
        "k": parameters.get("query", ""),
    }
    data = _http_get_json(os.getenv("SERPAPI_URL", "https://serpapi.com/search.json"), params=params)
    items = []
    for item in (data.get("organic_results") or [])[:_max_results(parameters)]:
        items.append(
            {
                "title": item.get("title"),
                "brand": item.get("brand"),
                "link": item.get("link_clean") or item.get("link"),
                "rating": item.get("rating"),
                "reviews": item.get("reviews"),
                "price": item.get("price"),
                "thumbnail": item.get("thumbnail"),
                "source": "amazon",
                # WP4: kept for ASIN-first identity. Only the constraint-aware path uses it, so
                # the legacy ids (title hash) and output do not change.
                "asin": item.get("asin"),
            }
        )
    return compact_tool_output("amazon.product.search", items)


def _hardcover_book_search_adapter(parameters: Dict[str, Any]) -> Any:
    api_key = os.getenv("HARDCOVER_API_KEY")
    if not api_key:
        return None
    query = str(parameters.get("query") or "")
    max_results = _max_results(parameters, ceiling=20)
    graphql_query = """
        query Query {
            search(
                query: "%s",
                query_type: "Book",
                per_page: %d,
                fields: "genres",
                weights: "1",
                sort: "users_read_count:desc",
                typos: "2",
                page: 1
            ) {
                results
            }
        }
    """ % (query.replace('"', '\\"'), max_results)
    with httpx.Client(
        base_url="https://api.hardcover.app/v1/graphql",
        timeout=PROVIDER_HTTP_TIMEOUT,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    ) as client:
        response = client.post("/", json={"query": graphql_query})
        response.raise_for_status()
        data = response.json()
    hits = (((data.get("data") or {}).get("search") or {}).get("results") or {}).get("hits") or []
    items = []
    for hit in hits[:max_results]:
        doc = hit.get("document") or {}
        image = doc.get("image") if isinstance(doc.get("image"), dict) else {}
        authors = []
        for contribution in doc.get("contributions") or []:
            author = contribution.get("author") if isinstance(contribution, dict) else None
            if isinstance(author, dict) and author.get("name"):
                authors.append(author["name"])
        slug = doc.get("slug")
        items.append(
            {
                "title": doc.get("title"),
                "release_date": doc.get("release_date"),
                "ratings_count": doc.get("ratings_count"),
                "reviews_count": doc.get("reviews_count"),
                "description": doc.get("description"),
                "image": image.get("url"),
                "genres": doc.get("genres") or [],
                "moods": doc.get("moods") or [],
                "tags": doc.get("tags") or [],
                "authors": authors,
                "hardcover_link": f"https://hardcover.app/books/{slug}" if slug else None,
                "source": "hardcover",
            }
        )
    return compact_tool_output("hardcover.book.search", items)


# OpenLibrary needs no credential and supports field-restricted search, so it is
# the structured-discovery complement to Hardcover's keyword search: it turns
# author/publisher/subject into real query filters instead of one text blob.
_OPENLIBRARY_FIELDS = (
    "key,title,author_name,publisher,first_publish_year,cover_i,subject,"
    "ratings_average,ratings_count"
)


def _openlibrary_book_discover_adapter(parameters: Dict[str, Any]) -> Any:
    tool = "openlibrary.book.discover"
    authors = _name_list(parameters.get("author") or parameters.get("authors"))
    publishers = _name_list(parameters.get("publisher") or parameters.get("publishers"))
    subjects = split_genres(parameters.get("subject")) or split_genres(parameters.get("genres"))
    title = str(parameters.get("title") or "").strip()
    # Contribute nothing when no structured filter resolved (mirrors the TMDB
    # discover gate) rather than returning a generic popularity list.
    if not (authors or publishers or subjects or title):
        return compact_tool_output(tool, [])
    limit = _max_results(parameters, default=10, ceiling=25)
    query_params: Dict[str, Any] = {"fields": _OPENLIBRARY_FIELDS, "limit": limit}
    if authors:
        query_params["author"] = " ".join(authors[:2])
    if publishers:
        query_params["publisher"] = " ".join(publishers[:2])
    if subjects:
        query_params["subject"] = " ".join(subjects[:3])
    if title:
        query_params["title"] = title
    data = _http_get_json(
        "https://openlibrary.org/search.json",
        params=query_params,
        headers={"User-Agent": "MetaRec/0.1 (multi-source recommendation research)"},
    )
    items = []
    for doc in (data.get("docs") or [])[:limit]:
        if not isinstance(doc, dict):
            continue
        cover_i = doc.get("cover_i")
        key = doc.get("key")
        items.append(
            {
                "title": doc.get("title"),
                "authors": doc.get("author_name") or [],
                "publishers": doc.get("publisher") or [],
                "first_publish_year": doc.get("first_publish_year"),
                "subjects": (doc.get("subject") or [])[:8],
                "rating": doc.get("ratings_average"),
                "ratings_count": doc.get("ratings_count"),
                "image": f"https://covers.openlibrary.org/b/id/{cover_i}-M.jpg" if cover_i else None,
                "link": f"https://openlibrary.org{key}" if key else None,
                "source": "openlibrary",
            }
        )
    return compact_tool_output(tool, items)


# Cover-art enrichment costs one extra HTTP round-trip per recording. Bound the
# count (and use a short per-call timeout) so a batch of slow Cover Art Archive
# lookups can't blow the tool's overall timeout budget — the rest of the items
# simply ship without a cover image.
MUSICBRAINZ_COVER_ART_LIMIT = 5
_COVER_ART_TIMEOUT_SECONDS = 2.0


def _musicbrainz_search(query: str, max_results: int) -> List[Dict[str, Any]]:
    """Run a MusicBrainz recording search (Lucene ``query``) and normalize the
    recordings, with bounded cover-art enrichment. Shared by the plain keyword
    search and the structured discover adapters."""
    data = _http_get_json(
        "https://musicbrainz.org/ws/2/recording",
        params={"query": query, "limit": max_results},
        headers={
            "Accept": "application/json",
            "User-Agent": "MetaRec/0.1 (multi-source recommendation research)",
        },
    )
    items: List[Dict[str, Any]] = []
    enrich_budget = MUSICBRAINZ_COVER_ART_LIMIT
    cover_client = httpx.Client(timeout=_COVER_ART_TIMEOUT_SECONDS, follow_redirects=False)
    try:
        for item in (data.get("recordings") or [])[:max_results]:
            artists = [
                artist.get("name")
                for artist in (item.get("artist-credit") or [])
                if isinstance(artist, dict) and artist.get("name")
            ]
            tags = [
                tag.get("name")
                for tag in (item.get("tags") or [])
                if isinstance(tag, dict) and tag.get("name")
            ]
            release = (item.get("releases") or [{}])[0] or {}
            cover_art_url = None
            release_id = release.get("id")
            if release_id and enrich_budget > 0:
                enrich_budget -= 1
                try:
                    response = cover_client.get(f"https://coverartarchive.org/release/{release_id}/front")
                    if response.status_code in {301, 302, 307, 308}:
                        cover_art_url = response.headers.get("Location")
                except Exception:
                    cover_art_url = None
            mbid = item.get("id")
            items.append(
                {
                    "title": item.get("title"),
                    "date": item.get("first-release-date"),
                    "link": f"https://musicbrainz.org/recording/{mbid}" if mbid else None,
                    "artists": artists,
                    "tags": tags,
                    "cover_art_url": cover_art_url,
                    "source": "musicbrainz",
                }
            )
    finally:
        cover_client.close()
    return items


def _musicbrainz_recording_search_adapter(parameters: Dict[str, Any]) -> Any:
    query = str(parameters.get("query") or "")
    items = _musicbrainz_search(query, _max_results(parameters, default=10, ceiling=25))
    return compact_tool_output("musicbrainz.recording.search", items)


def _musicbrainz_lucene_query(parameters: Dict[str, Any]) -> str:
    """Build a structured MusicBrainz Lucene query from artist + genre(tag)
    preferences. Genres are crowd-sourced *tags* in MusicBrainz, so no id
    resolution is needed. Returns ``""`` when no structured filter is present."""
    clauses: List[str] = []
    artists = _name_list(parameters.get("artist") or parameters.get("artists"))
    if artists:
        artist_clause = " OR ".join(f'artist:"{name}"' for name in artists)
        clauses.append(f"({artist_clause})" if len(artists) > 1 else artist_clause)
    tags = music_genre_tags(parameters.get("genres"))
    if tags:
        tag_clause = " OR ".join(f'tag:"{tag}"' for tag in tags)
        clauses.append(f"({tag_clause})" if len(tags) > 1 else tag_clause)
    return " AND ".join(clauses)


def _musicbrainz_recording_discover_adapter(parameters: Dict[str, Any]) -> Any:
    tool = "musicbrainz.recording.discover"
    lucene = _musicbrainz_lucene_query(parameters)
    if not lucene:
        return compact_tool_output(tool, [])
    items = _musicbrainz_search(lucene, _max_results(parameters, default=10, ceiling=25))
    return compact_tool_output(tool, items)


# Last.fm complements MusicBrainz with the popularity signal MusicBrainz lacks:
# top tracks for a named artist, or top tracks for a genre/tag. Credential-gated
# so it self-skips (returns None -> dispatch marks it inactive/failed) when the
# key is absent.
def _lastfm_get(method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    api_key = os.getenv("LASTFM_API_KEY")
    query = {"method": method, "api_key": api_key, "format": "json", **params}
    return _http_get_json("https://ws.audioscrobbler.com/2.0/", params=query)


def _lastfm_normalize_tracks(tracks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for track in tracks:
        if not isinstance(track, dict):
            continue
        artist = track.get("artist")
        artist_name = artist.get("name") if isinstance(artist, dict) else artist
        image_url = None
        for image in reversed(track.get("image") or []):
            if isinstance(image, dict) and image.get("#text"):
                image_url = image.get("#text")
                break
        items.append(
            {
                "title": track.get("name"),
                "artists": [artist_name] if artist_name else [],
                "link": track.get("url"),
                "listeners": _int_param(track.get("listeners")),
                "playcount": _int_param(track.get("playcount")),
                "image": image_url,
                "source": "lastfm",
            }
        )
    return items


def _lastfm_discover_adapter(parameters: Dict[str, Any]) -> Any:
    tool = "lastfm.track.discover"
    if not os.getenv("LASTFM_API_KEY"):
        return None
    max_results = _max_results(parameters, default=10, ceiling=50)
    artists = _name_list(parameters.get("artist") or parameters.get("artists"))
    tags = music_genre_tags(parameters.get("genres"))
    tracks: List[Dict[str, Any]] = []
    try:
        if artists:
            for artist in artists[:2]:
                data = _lastfm_get("artist.gettoptracks", {"artist": artist, "limit": max_results})
                tracks.extend(((data.get("toptracks") or {}).get("track")) or [])
        elif tags:
            data = _lastfm_get("tag.gettoptracks", {"tag": tags[0], "limit": max_results})
            tracks.extend(((data.get("tracks") or {}).get("track")) or [])
        else:
            return compact_tool_output(tool, [])
    except Exception:
        return compact_tool_output(tool, [])
    return compact_tool_output(tool, _lastfm_normalize_tracks(tracks[:max_results]))


def _tmdb_headers() -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {os.getenv('TMDB_API_ACCESS_TOKEN')}",
        "accept": "application/json",
    }


def _tmdb_get(path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    with httpx.Client(base_url="https://api.themoviedb.org", timeout=PROVIDER_HTTP_TIMEOUT, headers=_tmdb_headers()) as client:
        response = client.get(path, params=params)
        response.raise_for_status()
        return response.json()


# TMDB configuration / genre / language tables are effectively static. Cache the
# first successful fetch (process-lifetime) so each search makes one call instead
# of four — fewer round-trips means a smaller window for a slow provider to stall
# a task. Failures are not cached, so a transient outage retries next time.
_TMDB_STATIC_CACHE: Dict[str, Any] = {}


def _tmdb_configuration() -> Dict[str, Any]:
    if "config" in _TMDB_STATIC_CACHE:
        return _TMDB_STATIC_CACHE["config"]
    try:
        config = _tmdb_get("/3/configuration")
    except Exception:
        return {}
    _TMDB_STATIC_CACHE["config"] = config
    return config


def _tmdb_genres(media_type: str) -> Dict[int, str]:
    cache_key = f"genres:{media_type}"
    if cache_key in _TMDB_STATIC_CACHE:
        return _TMDB_STATIC_CACHE[cache_key]
    try:
        data = _tmdb_get(f"/3/genre/{media_type}/list", {"language": "en"})
        genres = {
            int(item["id"]): item["name"]
            for item in data.get("genres") or []
            if isinstance(item, dict) and item.get("id") is not None and item.get("name")
        }
    except Exception:
        return {}
    _TMDB_STATIC_CACHE[cache_key] = genres
    return genres


def _tmdb_languages() -> Dict[str, str]:
    if "languages" in _TMDB_STATIC_CACHE:
        return _TMDB_STATIC_CACHE["languages"]
    try:
        data = _tmdb_get("/3/configuration/languages")
        languages = {
            item.get("iso_639_1"): item.get("english_name")
            for item in data
            if isinstance(item, dict) and item.get("iso_639_1")
        }
    except Exception:
        return {}
    _TMDB_STATIC_CACHE["languages"] = languages
    return languages


# Resolved person ids are stable, so cache per (role, name): a repeated cast or
# crew filter then costs one /search/person call at most. Failures cache as None
# so an unresolved name simply drops its filter instead of retrying every time.
_TMDB_PERSON_CACHE: Dict[str, Optional[int]] = {}
_TMDB_PERSON_DEPARTMENT = {"actor": "Acting", "director": "Directing"}


def _tmdb_resolve_person(name: str, role: str) -> Optional[int]:
    """Resolve a person name to a TMDB id via /search/person, preferring the
    department matching the role (actor->Acting, director->Directing) and falling
    back to the most popular match. Returns ``None`` when unresolved."""
    cache_key = f"{role}:{name.strip().lower()}"
    if cache_key in _TMDB_PERSON_CACHE:
        return _TMDB_PERSON_CACHE[cache_key]
    person_id: Optional[int] = None
    try:
        data = _tmdb_get("/3/search/person", {"query": name, "language": "en"})
        results = [r for r in (data.get("results") or []) if isinstance(r, dict) and r.get("id")]
        if results:
            department = _TMDB_PERSON_DEPARTMENT.get(role)
            preferred = [r for r in results if r.get("known_for_department") == department]
            best = max(preferred or results, key=lambda r: r.get("popularity") or 0)
            person_id = int(best["id"])
    except Exception:
        person_id = None
    _TMDB_PERSON_CACHE[cache_key] = person_id
    return person_id


def _tmdb_resolve_people(values: Any, role: str) -> List[int]:
    ids: List[int] = []
    for name in _name_list(values):
        person_id = _tmdb_resolve_person(name, role)
        if person_id is not None:
            ids.append(person_id)
    return list(dict.fromkeys(ids))


def _tmdb_normalize_results(results: List[Dict[str, Any]], *, media_type: str) -> List[Dict[str, Any]]:
    config = _tmdb_configuration()
    image_base = ((config.get("images") or {}).get("secure_base_url") or "https://image.tmdb.org/t/p/")
    genres = _tmdb_genres("tv" if media_type == "tv" else "movie")
    languages = _tmdb_languages()
    normalized = []
    for item in results:
        poster_path = item.get("poster_path")
        normalized.append(
            {
                "title": item.get("title") or item.get("name"),
                "name": item.get("name"),
                "release_date": item.get("release_date") or item.get("first_air_date"),
                "overview": item.get("overview"),
                "vote_count": item.get("vote_count"),
                "popularity": item.get("popularity"),
                "vote_average": item.get("vote_average"),
                "original_language": languages.get(item.get("original_language"), item.get("original_language")),
                "poster_url": f"{image_base}original{poster_path}" if poster_path else None,
                "genres": [genres[genre_id] for genre_id in item.get("genre_ids") or [] if genre_id in genres],
                "tmdb_id": item.get("id"),
                "media_type": media_type,
                "source": "tmdb",
            }
        )
    return normalized


def _tmdb_movie_search_adapter(parameters: Dict[str, Any]) -> Any:
    data = _tmdb_get("/3/search/movie", {"query": parameters.get("query", ""), "language": "en"})
    return compact_tool_output(
        "tmdb.movie.search",
        _tmdb_normalize_results((data.get("results") or [])[:_max_results(parameters)], media_type="movie"),
    )


def _tmdb_discover(parameters: Dict[str, Any], *, media_type: str) -> Any:
    tool = f"tmdb.{media_type}.discover"
    with_ids = resolve_genre_ids(parameters.get("with_genres"), media_type)
    without_ids = resolve_genre_ids(parameters.get("without_genres"), media_type)
    cast_ids = _tmdb_resolve_people(parameters.get("with_cast"), "actor")
    # NOTE: TMDB discover can't filter crew by job, so a "director" filter is an
    # approximation — with_crew matches any crew credit for that person.
    crew_ids = _tmdb_resolve_people(parameters.get("with_crew"), "director")
    # Discover with no resolvable structured filter only returns a generic
    # popularity list; contribute nothing rather than add off-target noise.
    if not (with_ids or without_ids or cast_ids or crew_ids):
        return compact_tool_output(tool, [])
    params: Dict[str, Any] = {
        "language": "en",
        "sort_by": "popularity.desc",
        # Quality floor so discover doesn't surface obscure zero-vote entries.
        "vote_count.gte": 50,
    }
    if with_ids:
        params["with_genres"] = ",".join(str(genre_id) for genre_id in with_ids)
    if without_ids:
        params["without_genres"] = ",".join(str(genre_id) for genre_id in without_ids)
    if cast_ids:
        params["with_cast"] = ",".join(str(person_id) for person_id in cast_ids)
    if crew_ids:
        params["with_crew"] = ",".join(str(person_id) for person_id in crew_ids)
    min_rating = _float_param(parameters.get("min_rating"))
    if min_rating is not None:
        params["vote_average.gte"] = min_rating
    year = _int_param(parameters.get("year"))
    if year is not None:
        params["first_air_date_year" if media_type == "tv" else "primary_release_year"] = year
    path = "/3/discover/tv" if media_type == "tv" else "/3/discover/movie"
    data = _tmdb_get(path, params)
    return compact_tool_output(
        tool,
        _tmdb_normalize_results((data.get("results") or [])[:_max_results(parameters)], media_type=media_type),
    )


def _tmdb_movie_discover_adapter(parameters: Dict[str, Any]) -> Any:
    return _tmdb_discover(parameters, media_type="movie")


def _tmdb_tv_search_adapter(parameters: Dict[str, Any]) -> Any:
    data = _tmdb_get("/3/search/tv", {"query": parameters.get("query", ""), "language": "en"})
    return compact_tool_output(
        "tmdb.tv.search",
        _tmdb_normalize_results((data.get("results") or [])[:_max_results(parameters)], media_type="tv"),
    )


def _tmdb_tv_discover_adapter(parameters: Dict[str, Any]) -> Any:
    return _tmdb_discover(parameters, media_type="tv")


def build_default_tool_registry() -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(
        ToolSpec(
            name="gmap.search",
            domain="restaurant",
            tags={"#place", "#restaurant", "#review", "#map"},
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"}, "max_results": {"type": "integer"},
                    "anchor_lat": {"type": "number"}, "anchor_lng": {"type": "number"},
                    "radius_meters": {"type": "integer"},
                },
            },
            output_schema={"type": "array", "items": {"type": "object"}},
            adapter=_gmap_search_adapter,
            description="Search Google Maps for restaurant/place candidates.",
        )
    )
    registry.register(
        ToolSpec(
            name="xhs.search",
            domain="restaurant",
            tags={"#place", "#restaurant", "#review", "#social"},
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
            },
            output_schema={"type": "array", "items": {"type": "object"}},
            adapter=_xhs_search_adapter,
            description="Search Xiaohongshu notes for social/review evidence.",
        )
    )
    registry.register(
        ToolSpec(
            name="yelp.search",
            domain="restaurant",
            tags={"#place", "#restaurant", "#review"},
            input_schema={
                "type": "object",
                "required": ["query", "location"],
                "properties": {
                    "query": {"type": "string"},
                    "location": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
            },
            output_schema={"type": "array", "items": {"type": "object"}},
            adapter=_yelp_search_adapter,
            description="Search Yelp for restaurant review candidates.",
        )
    )
    registry.register(
        ToolSpec(
            name="gmap.source_matcher",
            domain="restaurant",
            tags={"#place", "#restaurant", "#map", "#source_match"},
            input_schema={
                "type": "object",
                "properties": {
                    "candidates": {"type": "array", "items": {"type": "object"}},
                    "reference_results": {"type": "array", "items": {"type": "object"}},
                },
            },
            output_schema={"type": "object"},
            adapter=_gmap_source_matcher_adapter,
            description="Match restaurant candidates against Google Maps source records.",
        )
    )
    registry.register(
        ToolSpec(
            name="gmap.hotel.search",
            domain="hotel",
            tags={"#place", "#hotel", "#review", "#map"},
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"}, "max_results": {"type": "integer"},
                    "anchor_lat": {"type": "number"}, "anchor_lng": {"type": "number"},
                    "radius_meters": {"type": "integer"},
                    "date": {"type": "string"}, "start_min": {"type": "integer"},
                    "end_min": {"type": "integer"}, "exclusions": {"type": "array"},
                },
            },
            output_schema={"type": "array", "items": {"type": "object"}},
            adapter=_gmap_hotel_search_adapter,
            status=_credential_status("SERPAPI_KEY"),
            description="Search Google Maps for hotel/lodging candidates.",
        )
    )
    registry.register(
        ToolSpec(
            name="osm.hotel.discover",
            domain="hotel",
            tags={"#place", "#hotel", "#map"},
            input_schema={
                "type": "object",
                "properties": {
                    "location": {"type": "string"},
                    "stars": {"type": ["string", "number"]},
                    "max_results": {"type": "integer"},
                    "anchor_lat": {"type": "number"}, "anchor_lng": {"type": "number"},
                    "radius_meters": {"type": "integer"},
                    "date": {"type": "string"}, "start_min": {"type": "integer"},
                    "end_min": {"type": "integer"}, "exclusions": {"type": "array"},
                },
            },
            output_schema={"type": "array", "items": {"type": "object"}},
            adapter=_osm_hotel_discover_adapter,
            description="Discover lodging around a destination via OpenStreetMap (Nominatim + Overpass); no credential required.",
        )
    )
    registry.register(
        ToolSpec(
            name="gmap.attraction.search",
            domain="attraction",
            tags={"#place", "#attraction", "#review", "#map"},
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string"},
                    "location": {"type": "string"},
                    "region_hint": {"type": "string"},
                    "max_results": {"type": "integer"},
                    "anchor_lat": {"type": "number"}, "anchor_lng": {"type": "number"},
                    "radius_meters": {"type": "integer"},
                    "date": {"type": "string"}, "start_min": {"type": "integer"},
                    "end_min": {"type": "integer"}, "exclusions": {"type": "array"},
                },
            },
            output_schema={"type": "array", "items": {"type": "object"}},
            adapter=_gmap_attraction_search_adapter,
            status=_credential_status("SERPAPI_KEY"),
            description="Search Google Maps for tourist attraction and activity candidates.",
        )
    )
    registry.register(
        ToolSpec(
            name="osm.attraction.discover",
            domain="attraction",
            tags={"#place", "#attraction", "#map"},
            input_schema={
                "type": "object",
                "properties": {
                    "location": {"type": "string"},
                    "region_hint": {"type": "string"},
                    "attraction_types": {"type": ["array", "string"]},
                    "max_results": {"type": "integer"},
                    "anchor_lat": {"type": "number"}, "anchor_lng": {"type": "number"},
                    "radius_meters": {"type": "integer"},
                    "date": {"type": "string"}, "start_min": {"type": "integer"},
                    "end_min": {"type": "integer"}, "exclusions": {"type": "array"},
                },
            },
            output_schema={"type": "array", "items": {"type": "object"}},
            adapter=_osm_attraction_discover_adapter,
            description="Discover tourist attractions around a destination via OpenStreetMap (Nominatim + Overpass); no credential required.",
        )
    )
    registry.register(
        ToolSpec(
            name="amazon.product.search",
            domain="product",
            tags={"#thing", "#shopping", "#product"},
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
            },
            output_schema={"type": "array", "items": {"type": "object"}},
            adapter=_amazon_product_search_adapter,
            status=_credential_status("SERPAPI_KEY"),
            description="Search Amazon products via SerpAPI.",
        )
    )
    registry.register(
        ToolSpec(
            name="hardcover.book.search",
            domain="book",
            tags={"#thing", "#book"},
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
            },
            output_schema={"type": "array", "items": {"type": "object"}},
            adapter=_hardcover_book_search_adapter,
            status=_credential_status("HARDCOVER_API_KEY"),
            description="Search books by genre or keyword via Hardcover.",
        )
    )
    registry.register(
        ToolSpec(
            name="openlibrary.book.discover",
            domain="book",
            tags={"#thing", "#book"},
            input_schema={
                "type": "object",
                "properties": {
                    "author": {"type": "string"},
                    "publisher": {"type": "string"},
                    "subject": {"type": "string"},
                    "genres": {"type": "string"},
                    "title": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
            },
            output_schema={"type": "array", "items": {"type": "object"}},
            adapter=_openlibrary_book_discover_adapter,
            description="Discover books by author/publisher/subject via OpenLibrary (no key).",
        )
    )
    registry.register(
        ToolSpec(
            name="musicbrainz.recording.search",
            domain="music",
            tags={"#thing", "#music"},
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
            },
            output_schema={"type": "array", "items": {"type": "object"}},
            adapter=_musicbrainz_recording_search_adapter,
            # Search + bounded cover-art enrichment needs more headroom than the
            # default single-call budget.
            timeout_seconds=20.0,
            description="Search recordings via MusicBrainz with optional cover-art enrichment.",
        )
    )
    registry.register(
        ToolSpec(
            name="musicbrainz.recording.discover",
            domain="music",
            tags={"#thing", "#music"},
            input_schema={
                "type": "object",
                "properties": {
                    "artist": {"type": "string"},
                    "genres": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
            },
            output_schema={"type": "array", "items": {"type": "object"}},
            adapter=_musicbrainz_recording_discover_adapter,
            # Structured search + bounded cover-art enrichment, same as keyword search.
            timeout_seconds=20.0,
            description="Discover recordings by artist + genre tag via MusicBrainz.",
        )
    )
    registry.register(
        ToolSpec(
            name="lastfm.track.discover",
            domain="music",
            tags={"#thing", "#music"},
            input_schema={
                "type": "object",
                "properties": {
                    "artist": {"type": "string"},
                    "genres": {"type": "string"},
                    "max_results": {"type": "integer"},
                },
            },
            output_schema={"type": "array", "items": {"type": "object"}},
            adapter=_lastfm_discover_adapter,
            status=_credential_status("LASTFM_API_KEY"),
            description="Discover popularity-ranked tracks by artist or genre via Last.fm.",
        )
    )
    registry.register(
        ToolSpec(
            name="tmdb.movie.search",
            domain="movie",
            tags={"#thing", "#movie"},
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
            },
            output_schema={"type": "array", "items": {"type": "object"}},
            adapter=_tmdb_movie_search_adapter,
            status=_credential_status("TMDB_API_ACCESS_TOKEN"),
            description="Search movies by title via TMDB.",
        )
    )
    registry.register(
        ToolSpec(
            name="tmdb.movie.discover",
            domain="movie",
            tags={"#thing", "#movie"},
            input_schema={
                "type": "object",
                "properties": {
                    "with_genres": {"type": "string"},
                    "without_genres": {"type": "string"},
                    "with_cast": {"type": "string"},
                    "with_crew": {"type": "string"},
                    "min_rating": {"type": ["number", "string"]},
                    "year": {"type": ["integer", "string"]},
                    "max_results": {"type": "integer"},
                },
            },
            output_schema={"type": "array", "items": {"type": "object"}},
            adapter=_tmdb_movie_discover_adapter,
            status=_credential_status("TMDB_API_ACCESS_TOKEN"),
            description="Discover movies by TMDB genre/cast/crew/rating filters.",
        )
    )
    registry.register(
        ToolSpec(
            name="tmdb.tv.search",
            domain="movie",
            tags={"#thing", "#movie", "#tv"},
            input_schema={
                "type": "object",
                "required": ["query"],
                "properties": {"query": {"type": "string"}, "max_results": {"type": "integer"}},
            },
            output_schema={"type": "array", "items": {"type": "object"}},
            adapter=_tmdb_tv_search_adapter,
            status=_credential_status("TMDB_API_ACCESS_TOKEN"),
            description="Search TV series by title via TMDB.",
        )
    )
    registry.register(
        ToolSpec(
            name="tmdb.tv.discover",
            domain="movie",
            tags={"#thing", "#movie", "#tv"},
            input_schema={
                "type": "object",
                "properties": {
                    "with_genres": {"type": "string"},
                    "without_genres": {"type": "string"},
                    "with_cast": {"type": "string"},
                    "with_crew": {"type": "string"},
                    "min_rating": {"type": ["number", "string"]},
                    "year": {"type": ["integer", "string"]},
                    "max_results": {"type": "integer"},
                },
            },
            output_schema={"type": "array", "items": {"type": "object"}},
            adapter=_tmdb_tv_discover_adapter,
            status=_credential_status("TMDB_API_ACCESS_TOKEN"),
            description="Discover TV series by TMDB genre/cast/crew/rating filters.",
        )
    )

    return registry


DEFAULT_TOOL_REGISTRY = build_default_tool_registry()
