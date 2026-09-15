from __future__ import annotations

import asyncio
import hashlib
import inspect
import math
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set, TypedDict

from langgraph.graph import END, START, StateGraph

from langgraph_metarec.genres import detect_genres_in_text
from langgraph_metarec.product_constraints import apply_hard_constraints, resolve_constraints
from langgraph_metarec.tool_registry import DEFAULT_TOOL_REGISTRY, ToolRegistry


ProgressCallback = Callable[[Dict[str, Any]], Awaitable[None] | None]

# A gather reasoner observes the query, preferences and per-tool candidate counts
# so far and proposes the next {"tool", "parameters"} action — or None to stop.
# It is optional: when absent (or it errors) the loop uses a deterministic
# relaxation ladder, so no tool or LLM is ever assumed to be working.
GatherReasoner = Callable[[Dict[str, Any]], Awaitable[Optional[Dict[str, Any]]]]

# Stop gathering once we have this many unique candidates; bound the refinement
# loop so a sparse query can't spin. Well-specified queries usually clear the
# target on the seed pass and never invoke the reasoner (zero extra LLM cost).
GATHER_TARGET = 8
MAX_GATHER_ITERS = 3

# WP4. Selects the product ranker, per the proposal's exact contract:
# "legacy" (default) bypasses the ranker and reproduces frozen B0 fixtures;
# "domain_v1" enables the constraint-aware path. An invalid value falls back
# to "legacy" rather than raising, matching "rejects invalid startup values"
# by never silently running the new path on a typo.
_PRODUCT_RANKERS = {"legacy", "domain_v1"}


def _product_ranker() -> str:
    value = os.getenv("METAREC_PRODUCT_RANKER", "legacy").strip().lower()
    return value if value in _PRODUCT_RANKERS else "legacy"


class GenericRuntimeState(TypedDict, total=False):
    query: str
    domain: str
    preferences: Dict[str, Any]
    use_online_agent: bool
    tool_tags: List[str]
    selected_tools: List[str]
    skipped_tools: List[Dict[str, str]]
    executions: List[Dict[str, Any]]
    items: List[Dict[str, Any]]
    progress_events: List[Dict[str, Any]]
    metadata: Dict[str, Any]
    errors: List[str]
    gather_iterations: int
    candidate_count: int
    constraint_filter: Optional[Dict[str, Any]]


@dataclass
class GenericGraphResult:
    executions: List[Dict[str, Any]]
    items: List[Dict[str, Any]]
    progress_events: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)


@dataclass
class GenericGraphAdapters:
    tool_registry: ToolRegistry = field(default_factory=lambda: DEFAULT_TOOL_REGISTRY)
    # Optional LLM-backed reasoner for the ReAct gather loop; None -> deterministic.
    reasoner: Optional[GatherReasoner] = None


async def _emit(
    state: GenericRuntimeState,
    progress_callback: Optional[ProgressCallback],
    event: Dict[str, Any],
) -> None:
    events = list(state.get("progress_events", []))
    events.append(event)
    state["progress_events"] = events
    if progress_callback:
        result = progress_callback(event)
        if inspect.isawaitable(result):
            await result  # type: ignore[misc]


def _stable_id(*parts: Any) -> str:
    text = "|".join(str(part or "") for part in parts)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        value = value.replace(",", "").strip()
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _gps_coordinates(value: Any) -> Optional[Dict[str, float]]:
    if not isinstance(value, dict):
        return None
    latitude = _float_or_none(value.get("latitude"))
    longitude = _float_or_none(value.get("longitude"))
    if latitude is None or longitude is None:
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return {"latitude": latitude, "longitude": longitude}


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, list):
        return []
    result: List[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip())
        elif isinstance(item, dict):
            title = item.get("title") or item.get("name")
            if title:
                result.append(str(title))
    return result


def _item(
    *,
    domain: str,
    tool: str,
    raw: Dict[str, Any],
    title: Any,
    subtitle: Any = None,
    description: Any = None,
    image_url: Any = None,
    url: Any = None,
    rating: Any = None,
    reviews_count: Any = None,
    source: Any = None,
    tags: Optional[List[str]] = None,
    why: Any = None,
    item_id: Any = None,
    gps_coordinates: Any = None,
) -> Dict[str, Any]:
    title_text = str(title or "").strip()
    url_text = str(url or "").strip() or None
    item_id_text = str(item_id).strip() if item_id else ""
    return {
        "id": item_id_text or f"{domain}_{_stable_id(tool, title_text, url_text)}",
        "domain": domain,
        "title": title_text or "Untitled",
        "subtitle": str(subtitle).strip() if subtitle else None,
        "description": str(description).strip() if description else None,
        "image_url": str(image_url).strip() if image_url else None,
        "url": url_text,
        "rating": _float_or_none(rating),
        "reviews_count": _int_or_none(reviews_count),
        "source": str(source or tool),
        "tags": tags or [],
        "why": str(why).strip() if why else None,
        "gps_coordinates": _gps_coordinates(gps_coordinates),
        "raw": raw,
    }


def normalize_tool_items(tool: str, output: Any, domain: str) -> List[Dict[str, Any]]:
    if not isinstance(output, list):
        return []
    items: List[Dict[str, Any]] = []
    for raw_item in output:
        if not isinstance(raw_item, dict):
            continue
        if tool == "amazon.product.search":
            items.append(
                _item(
                    domain=domain,
                    tool=tool,
                    raw=raw_item,
                    title=raw_item.get("title"),
                    subtitle=raw_item.get("brand") or raw_item.get("price"),
                    image_url=raw_item.get("thumbnail"),
                    url=raw_item.get("link"),
                    rating=raw_item.get("rating"),
                    reviews_count=raw_item.get("reviews"),
                    source="Amazon",
                    tags=[tag for tag in [raw_item.get("brand"), raw_item.get("price")] if tag],
                    why="Matched the product search query.",
                    item_id=raw_item.get("product_id") or raw_item.get("asin"),
                )
            )
        elif tool == "hardcover.book.search":
            authors = _string_list(raw_item.get("authors"))
            tags = _string_list(raw_item.get("genres")) + _string_list(raw_item.get("moods"))
            items.append(
                _item(
                    domain=domain,
                    tool=tool,
                    raw=raw_item,
                    title=raw_item.get("title"),
                    subtitle=", ".join(authors) if authors else raw_item.get("release_date"),
                    description=raw_item.get("description"),
                    image_url=raw_item.get("image"),
                    url=raw_item.get("hardcover_link"),
                    reviews_count=raw_item.get("reviews_count") or raw_item.get("ratings_count"),
                    source="Hardcover",
                    tags=tags,
                    why="Matched the requested book theme or genre.",
                    item_id=raw_item.get("id") or raw_item.get("hardcover_id"),
                )
            )
        elif tool.startswith("musicbrainz.recording."):
            artists = _string_list(raw_item.get("artists"))
            items.append(
                _item(
                    domain=domain,
                    tool=tool,
                    raw=raw_item,
                    title=raw_item.get("title"),
                    subtitle=", ".join(artists) if artists else raw_item.get("date"),
                    image_url=raw_item.get("cover_art_url"),
                    url=raw_item.get("link"),
                    source="MusicBrainz",
                    tags=_string_list(raw_item.get("tags")),
                    why="Matched the requested artist or genre.",
                    item_id=raw_item.get("id") or raw_item.get("mbid"),
                )
            )
        elif tool.startswith("lastfm."):
            artists = _string_list(raw_item.get("artists"))
            items.append(
                _item(
                    domain=domain,
                    tool=tool,
                    raw=raw_item,
                    title=raw_item.get("title"),
                    subtitle=", ".join(artists) if artists else None,
                    image_url=raw_item.get("image"),
                    url=raw_item.get("link"),
                    reviews_count=raw_item.get("playcount") or raw_item.get("listeners"),
                    source="Last.fm",
                    why="Popular match for the requested artist or genre.",
                    item_id=raw_item.get("link"),
                )
            )
        elif tool == "gmap.hotel.search":
            url = (
                raw_item.get("link")
                or raw_item.get("website")
                or raw_item.get("booking_link")
                or raw_item.get("reviews_link")
                or raw_item.get("photos_link")
            )
            items.append(
                _item(
                    domain=domain,
                    tool=tool,
                    raw=raw_item,
                    title=raw_item.get("title"),
                    subtitle=raw_item.get("address"),
                    image_url=raw_item.get("thumbnail"),
                    url=url,
                    rating=raw_item.get("rating"),
                    reviews_count=raw_item.get("reviews"),
                    source="Google Maps",
                    tags=[tag for tag in [raw_item.get("type"), raw_item.get("price")] if tag],
                    why="Matched the hotel search on Google Maps.",
                    item_id=raw_item.get("place_id") or raw_item.get("data_id") or url,
                    gps_coordinates=raw_item.get("gps_coordinates"),
                )
            )
        elif tool == "osm.hotel.discover":
            stars = raw_item.get("stars")
            stars_tag = f"{stars:g}-star" if isinstance(stars, (int, float)) else None
            items.append(
                _item(
                    domain=domain,
                    tool=tool,
                    raw=raw_item,
                    title=raw_item.get("title"),
                    subtitle=raw_item.get("address") or raw_item.get("searched_location"),
                    url=raw_item.get("website") or raw_item.get("link"),
                    source="OpenStreetMap",
                    tags=[tag for tag in [raw_item.get("tourism"), stars_tag] if tag],
                    why="Located near the requested destination on OpenStreetMap.",
                    item_id=raw_item.get("link"),
                    gps_coordinates=raw_item.get("gps_coordinates"),
                )
            )
        elif tool == "gmap.attraction.search":
            url = (
                raw_item.get("link")
                or raw_item.get("website")
                or raw_item.get("reviews_link")
                or raw_item.get("photos_link")
            )
            items.append(
                _item(
                    domain=domain,
                    tool=tool,
                    raw=raw_item,
                    title=raw_item.get("title"),
                    subtitle=raw_item.get("address"),
                    image_url=raw_item.get("thumbnail"),
                    url=url,
                    rating=raw_item.get("rating"),
                    reviews_count=raw_item.get("reviews"),
                    source="Google Maps",
                    tags=[tag for tag in [raw_item.get("type"), raw_item.get("price")] if tag],
                    why="Matched the attraction search on Google Maps.",
                    item_id=raw_item.get("place_id") or raw_item.get("data_id") or url,
                    gps_coordinates=raw_item.get("gps_coordinates"),
                )
            )
        elif tool == "osm.attraction.discover":
            items.append(
                _item(
                    domain=domain,
                    tool=tool,
                    raw=raw_item,
                    title=raw_item.get("title"),
                    subtitle=raw_item.get("address") or raw_item.get("searched_location"),
                    description=raw_item.get("opening_hours"),
                    url=raw_item.get("website") or raw_item.get("link"),
                    source="OpenStreetMap",
                    tags=[tag for tag in [raw_item.get("osm_category") or raw_item.get("tourism")] if tag],
                    why="Located near the requested destination on OpenStreetMap.",
                    item_id=raw_item.get("link"),
                    gps_coordinates=raw_item.get("gps_coordinates"),
                )
            )
        elif tool == "openlibrary.book.discover":
            authors = _string_list(raw_item.get("authors"))
            subjects = _string_list(raw_item.get("subjects"))
            year = raw_item.get("first_publish_year")
            items.append(
                _item(
                    domain=domain,
                    tool=tool,
                    raw=raw_item,
                    title=raw_item.get("title"),
                    subtitle=", ".join(authors) if authors else (str(year) if year else None),
                    image_url=raw_item.get("image"),
                    url=raw_item.get("link"),
                    rating=raw_item.get("rating"),
                    reviews_count=raw_item.get("ratings_count"),
                    source="OpenLibrary",
                    tags=subjects[:5],
                    why="Matched the requested author, publisher, or subject.",
                    item_id=raw_item.get("key") or raw_item.get("link"),
                )
            )
        elif tool.startswith("tmdb."):
            media_type = raw_item.get("media_type") or ("tv" if ".tv." in tool else "movie")
            tmdb_id = raw_item.get("tmdb_id")
            items.append(
                _item(
                    domain=domain,
                    tool=tool,
                    raw=raw_item,
                    title=raw_item.get("title") or raw_item.get("name"),
                    subtitle=raw_item.get("release_date") or raw_item.get("original_language"),
                    description=raw_item.get("overview"),
                    image_url=raw_item.get("poster_url"),
                    url=f"https://www.themoviedb.org/{media_type}/{tmdb_id}" if tmdb_id else None,
                    rating=raw_item.get("vote_average"),
                    reviews_count=raw_item.get("vote_count"),
                    source="TMDB",
                    tags=_string_list(raw_item.get("genres")) + [str(media_type)],
                    why="Matched the entertainment search query.",
                    item_id=f"tmdb_{media_type}_{tmdb_id}" if tmdb_id else None,
                )
            )
    return items


def _item_score(item: Dict[str, Any]) -> tuple:
    raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
    return (
        item.get("rating") or 0,
        item.get("reviews_count") or 0,
        _float_or_none(raw.get("popularity")) or 0,
        item.get("title") or "",
    )


def _canonical_place_text(value: Any) -> str:
    text = str(value or "").strip().casefold()
    for source, target in (("avenue", "ave"), ("street", "st"), ("road", "rd"), ("boulevard", "blvd")):
        text = text.replace(source, target)
    return "".join(char for char in text if char.isalnum())


def _coordinates_distance_meters(left: Any, right: Any) -> Optional[float]:
    left_coords = _gps_coordinates(left)
    right_coords = _gps_coordinates(right)
    if left_coords is None or right_coords is None:
        return None
    lat1 = math.radians(left_coords["latitude"])
    lat2 = math.radians(right_coords["latitude"])
    delta_lat = lat2 - lat1
    delta_lon = math.radians(right_coords["longitude"] - left_coords["longitude"])
    haversine = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
    return 6_371_000 * 2 * math.asin(min(1.0, math.sqrt(haversine)))


def _same_recommendation(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    if left.get("domain") != right.get("domain"):
        return False
    if _canonical_place_text(left.get("title")) != _canonical_place_text(right.get("title")):
        return False

    left_url = str(left.get("url") or "").strip()
    right_url = str(right.get("url") or "").strip()
    if left_url and left_url == right_url:
        return True

    if left.get("domain") not in {"hotel", "attraction"}:
        if left_url or right_url:
            return False
        left_subtitle = _canonical_place_text(left.get("subtitle"))
        right_subtitle = _canonical_place_text(right.get("subtitle"))
        return bool(left_subtitle) and left_subtitle == right_subtitle

    distance = _coordinates_distance_meters(left.get("gps_coordinates"), right.get("gps_coordinates"))
    if distance is not None:
        return distance <= 300

    left_address = _canonical_place_text(left.get("subtitle"))
    right_address = _canonical_place_text(right.get("subtitle"))
    if not left_address or not right_address:
        return False
    return left_address == right_address or (
        min(len(left_address), len(right_address)) >= 8
        and (left_address in right_address or right_address in left_address)
    )


def _merge_recommendation_items(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    primary, secondary = (left, right) if _item_score(left) >= _item_score(right) else (right, left)
    merged = dict(primary)
    for key, value in secondary.items():
        if key in {"id", "domain", "title", "raw", "tags"}:
            continue
        if merged.get(key) in (None, "", [], {}):
            merged[key] = value
    merged["tags"] = list(dict.fromkeys([*(primary.get("tags") or []), *(secondary.get("tags") or [])]))
    return merged


def _rank_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduped: List[Dict[str, Any]] = []
    for item in items:
        for index, current in enumerate(deduped):
            if _same_recommendation(current, item):
                deduped[index] = _merge_recommendation_items(current, item)
                break
        else:
            deduped.append(item)
    return sorted(deduped, key=_item_score, reverse=True)


def _csv_tokens(*values: Any) -> List[str]:
    """Flatten lists/comma-separated strings into a de-duplicated, order-preserving
    token list. Used for both genre names and person/author names."""
    tokens: List[str] = []
    for value in values:
        if not value:
            continue
        if isinstance(value, (list, tuple, set)):
            tokens.extend(str(token).strip() for token in value if str(token).strip())
        else:
            tokens.extend(part.strip() for part in str(value).split(",") if part.strip())
    return list(dict.fromkeys(tokens))


def _tmdb_discover_params(tool: str, query: str, preferences: Dict[str, Any]) -> Dict[str, Any]:
    media_type = "tv" if ".tv." in tool else "movie"
    out: Dict[str, Any] = {}
    # Explicit preference genres win; otherwise infer them from the query so
    # discover fires for natural prompts like "a quiet sci-fi movie". Genre/person
    # *names* are passed through — the TMDB adapter resolves them to ids.
    include = _csv_tokens(preferences.get("with_genres"), preferences.get("genres")) or [
        str(name) for name in detect_genres_in_text(query, media_type)
    ]
    exclude = _csv_tokens(preferences.get("without_genres"), preferences.get("exclude_genres"))
    cast = _csv_tokens(preferences.get("with_cast"), preferences.get("actors"))
    crew = _csv_tokens(preferences.get("with_crew"), preferences.get("directors"))
    if include:
        out["with_genres"] = ",".join(include)
    if exclude:
        out["without_genres"] = ",".join(exclude)
    if cast:
        out["with_cast"] = ",".join(cast)
    if crew:
        out["with_crew"] = ",".join(crew)
    if preferences.get("min_rating") not in (None, ""):
        out["min_rating"] = preferences["min_rating"]
    if preferences.get("year") not in (None, ""):
        out["year"] = preferences["year"]
    return out


def _music_discover_params(preferences: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    artist = ", ".join(_csv_tokens(preferences.get("artist"), preferences.get("artists")))
    if artist:
        out["artist"] = artist
    genres = ",".join(_csv_tokens(preferences.get("genres")))
    if genres:
        out["genres"] = genres
    return out


def _hotel_search_query(query: str, preferences: Dict[str, Any]) -> str:
    """Compose the Google Maps hotel query: anchor on lodging and thread the
    structured stay filters (destination, star class, amenities, budget) into
    the text — the same enrichment pattern the product search uses."""
    lowered = query.lower()
    parts = [query]
    if not any(term in lowered for term in ("hotel", "hostel", "lodging", "guest house")):
        parts.append("hotels")
    stars = str(preferences.get("stars") or "").strip()
    tokens = _csv_tokens(
        f"{stars}-star" if stars and stars.lower() != "any" else None,
        preferences.get("amenities"),
        preferences.get("budget"),
    )
    parts.extend(token for token in tokens if token.lower() not in lowered)
    location = str(preferences.get("location") or "").strip()
    if location and location.lower() != "any" and location.lower() not in lowered:
        parts.append(f"in {location}")
    return " ".join(parts).strip()


def _hotel_discover_params(preferences: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    location = str(preferences.get("location") or "").strip()
    if location and location.lower() != "any":
        out["location"] = location
    stars = preferences.get("stars")
    if stars not in (None, "", "any"):
        out["stars"] = stars
    return out


def _attraction_search_query(query: str, preferences: Dict[str, Any]) -> str:
    """Compose the Google Maps attraction query: anchor on attractions and thread
    the structured filters (types, budget, destination) into the text — the same
    enrichment pattern the hotel search uses."""
    lowered = query.lower()
    parts = [query]
    if not any(
        term in lowered
        for term in ("attraction", "things to do", "sightseeing", "museum", "theme park", "landmark")
    ):
        parts.append("attractions")
    # Form values like "theme-park" read better as "theme park" in a text query.
    tokens = [
        token.replace("-", " ")
        for token in _csv_tokens(
            preferences.get("attraction_types"),
            preferences.get("interest_terms"),
            preferences.get("budget"),
        )
    ]
    parts.extend(token for token in tokens if token.lower() not in lowered)
    location = str(preferences.get("location") or "").strip()
    if location and location.lower() != "any" and location.lower() not in lowered:
        parts.append(f"in {location}")
    return " ".join(parts).strip()


def _attraction_discover_params(preferences: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    location = str(preferences.get("location") or "").strip()
    if location and location.lower() != "any":
        out["location"] = location
        region_hint = str(preferences.get("region_hint") or "").strip()
        if region_hint:
            out["region_hint"] = region_hint
    attraction_types = _csv_tokens(preferences.get("attraction_types"))
    if attraction_types:
        out["attraction_types"] = attraction_types
    return out


def _book_discover_params(preferences: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    author = ", ".join(_csv_tokens(preferences.get("author"), preferences.get("authors")))
    if author:
        out["author"] = author
    publisher = ", ".join(_csv_tokens(preferences.get("publisher"), preferences.get("publishers")))
    if publisher:
        out["publisher"] = publisher
    subject = ",".join(_csv_tokens(preferences.get("subject"), preferences.get("genres")))
    if subject:
        out["subject"] = subject
    return out


def _product_search_query(query: str, preferences: Dict[str, Any]) -> str:
    budget_range = preferences.get("budget_range")
    budget_text = ""
    if isinstance(budget_range, dict):
        currency = str(budget_range.get("currency") or "SGD").upper()
        if budget_range.get("max") not in (None, ""):
            budget_text = f"under {budget_range.get('max')} {currency}"
        elif budget_range.get("min") not in (None, ""):
            budget_text = f"over {budget_range.get('min')} {currency}"
    tokens = _csv_tokens(
        preferences.get("product"),
        preferences.get("model"),
        preferences.get("use_case"),
        preferences.get("category"),
        preferences.get("brand"),
        preferences.get("budget"),
        budget_text,
        preferences.get("tags"),
    )
    if not tokens:
        return query
    lowered = query.lower()
    extras = [token for token in tokens if token.lower() not in lowered]
    if not extras:
        return query
    return " ".join([query, *extras]).strip()


def _place_anchor_params(preferences: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in ("anchor_lat", "anchor_lng", "radius_meters", "date", "start_min", "end_min"):
        value = preferences.get(key)
        if value not in (None, ""):
            out[key] = value
    exclusions = preferences.get("exclusions") or preferences.get("exclude")
    if exclusions:
        out["exclusions"] = exclusions
    return out


def _parameters_for_tool(tool: str, query: str, preferences: Dict[str, Any]) -> Dict[str, Any]:
    """Map preferences into a single tool's call params. Each discover builder
    yields its structured filters only when present; the adapter then contributes
    nothing when no filter resolved (so an over-broad call adds no noise)."""
    params: Dict[str, Any] = {"max_results": 10}
    preferences = preferences or {}
    place_tools = {
        "gmap.hotel.search", "gmap.attraction.search",
        "osm.hotel.discover", "osm.attraction.discover",
    }
    if tool in place_tools:
        params.update(_place_anchor_params(preferences))
    if tool.endswith(".search"):
        if tool == "amazon.product.search":
            params["query"] = _product_search_query(query, preferences)
        elif tool == "gmap.hotel.search":
            params["query"] = _hotel_search_query(query, preferences)
        elif tool == "gmap.attraction.search":
            params["query"] = _attraction_search_query(query, preferences)
            # Structured destination + region hint let the adapter geocode and
            # bias the map search instead of trusting an ambiguous text token.
            location = str(preferences.get("location") or "").strip()
            if location and location.lower() != "any":
                params["location"] = location
            region_hint = str(preferences.get("region_hint") or "").strip()
            if region_hint:
                params["region_hint"] = region_hint
        else:
            params["query"] = query
        return params
    if tool.startswith("tmdb.") and tool.endswith(".discover"):
        params.update(_tmdb_discover_params(tool, query, preferences))
    elif tool in ("musicbrainz.recording.discover", "lastfm.track.discover"):
        params.update(_music_discover_params(preferences))
    elif tool == "openlibrary.book.discover":
        params.update(_book_discover_params(preferences))
    elif tool == "osm.hotel.discover":
        params.update(_hotel_discover_params(preferences))
    elif tool == "osm.attraction.discover":
        params.update(_attraction_discover_params(preferences))
    return params


# Narrowest-first relaxation ladder per discover tool. The deterministic fallback
# (used when no reasoner is injected, or it errors/declines) drops these keys in
# order, keeping each tool's most important filter (movie genre, music artist,
# book author) until last.
_RELAX_ORDER: Dict[str, List[str]] = {
    "tmdb.movie.discover": ["year", "min_rating", "without_genres", "with_cast", "with_crew"],
    "tmdb.tv.discover": ["year", "min_rating", "without_genres", "with_cast", "with_crew"],
    "musicbrainz.recording.discover": ["genres"],
    "lastfm.track.discover": ["genres"],
    "openlibrary.book.discover": ["publisher", "subject"],
    "osm.hotel.discover": ["stars"],
    "osm.attraction.discover": ["attraction_types"],
}

# What still counts as a usable structured filter per discover tool, so the
# fallback never re-dispatches a call that would just return an empty list.
_DISCOVER_FILTER_KEYS: Dict[str, Set[str]] = {
    "tmdb.movie.discover": {"with_genres", "with_cast", "with_crew"},
    "tmdb.tv.discover": {"with_genres", "with_cast", "with_crew"},
    "musicbrainz.recording.discover": {"artist", "genres"},
    "lastfm.track.discover": {"artist", "genres"},
    "openlibrary.book.discover": {"author", "publisher", "subject", "title"},
    "osm.hotel.discover": {"location"},
    "osm.attraction.discover": {"location"},
}


def _execution_item_count(execution: Dict[str, Any]) -> int:
    output = execution.get("output")
    return len(output) if isinstance(output, list) else 0


def _unique_items(executions: List[Dict[str, Any]], domain: str) -> List[Dict[str, Any]]:
    """Normalized + de-duplicated candidates gathered so far — the loop's stop
    signal (reuses the same normalize/rank the result stage uses)."""
    items: List[Dict[str, Any]] = []
    for execution in executions:
        if execution.get("success"):
            items.extend(normalize_tool_items(str(execution.get("tool")), execution.get("output"), domain))
    return _rank_items(items)


def _discover_has_filter(tool: str, params: Dict[str, Any]) -> bool:
    keys = _DISCOVER_FILTER_KEYS.get(tool)
    return bool(keys) and any(params.get(key) for key in keys)


def _relaxation_actions(observations: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Build the deterministic relaxation ladder from the seed observations: for
    each discover tool, drop its droppable keys narrowest-first, keeping only the
    steps that still carry a usable filter."""
    actions: List[Dict[str, Any]] = []
    for observation in observations:
        tool = observation.get("tool")
        relax_keys = _RELAX_ORDER.get(tool)
        if not relax_keys:
            continue
        params = dict(observation.get("parameters") or {})
        for key in relax_keys:
            if key in params:
                params = {k: v for k, v in params.items() if k != key}
                if _discover_has_filter(tool, params):
                    actions.append({"tool": tool, "parameters": dict(params)})
    return actions


def _valid_action(action: Any, active_names: List[str]) -> bool:
    return isinstance(action, dict) and action.get("tool") in active_names


async def _safe_propose(reasoner: GatherReasoner, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Call the reasoner defensively — any failure yields None so the loop falls
    back to the deterministic ladder. Never assume the LLM is working."""
    try:
        action = await reasoner(context)
    except Exception:
        return None
    return action if isinstance(action, dict) else None


def build_generic_domain_graph(
    *,
    adapters: Optional[GenericGraphAdapters] = None,
    progress_callback: Optional[ProgressCallback] = None,
):
    adapters = adapters or GenericGraphAdapters()
    registry = adapters.tool_registry
    reasoner = adapters.reasoner

    async def candidate_gather(state: GenericRuntimeState) -> GenericRuntimeState:
        await _emit(
            state,
            progress_callback,
            {
                "stage": "candidate_gather",
                "stage_number": 1,
                "status": "started",
                "progress": 15,
                "message": f"Gathering {state.get('domain')} candidates...",
            },
        )
        domain = state.get("domain", "unknown")
        tags = state.get("tool_tags") or []
        query = state.get("query", "")
        preferences = state.get("preferences", {})
        all_specs = registry.resolve(domain=domain, tags=tags, active_only=False)
        active_specs = registry.resolve(domain=domain, tags=tags, active_only=True)
        active_names = [spec.name for spec in active_specs]
        state["selected_tools"] = active_names
        state["skipped_tools"] = [
            {"name": spec.name, "status": spec.status}
            for spec in all_specs
            if spec.name not in active_names
        ]
        executions: List[Dict[str, Any]] = []
        observations: List[Dict[str, Any]] = []
        quota_tracker: Dict[str, int] = {}
        total = max(len(active_specs), 1)

        async def _run(tool_name: str, params: Dict[str, Any], progress: int) -> None:
            await _emit(
                state,
                progress_callback,
                {
                    "stage": "candidate_gather",
                    "stage_number": 1,
                    "status": "in_progress",
                    "progress": progress,
                    "message": f"Executing: {tool_name}",
                    "tool": tool_name,
                    "query": params.get("query", ""),
                },
            )
            execution = await asyncio.to_thread(registry.dispatch, tool_name, params, quota_tracker=quota_tracker)
            executions.append(execution)
            observations.append(
                {"tool": tool_name, "parameters": params, "count": _execution_item_count(execution)}
            )

        # Seed pass: every active tool for the domain, with the new richer params.
        for idx, spec in enumerate(active_specs, start=1):
            params = _parameters_for_tool(spec.name, query, preferences)
            await _run(spec.name, params, 20 + int((idx / total) * 40))

        # ReAct refinement: only when the seed pass is thin. A reasoner (when
        # injected) proposes the next action from the observed candidate counts;
        # otherwise — or whenever it errors/declines — a deterministic relaxation
        # ladder widens over-constrained discover calls. Bounded by MAX_GATHER_ITERS.
        unique = _unique_items(executions, domain)
        relaxation_queue = _relaxation_actions(observations)
        iterations = 0
        while active_specs and len(unique) < GATHER_TARGET and iterations < MAX_GATHER_ITERS:
            iterations += 1
            action: Optional[Dict[str, Any]] = None
            if reasoner is not None:
                action = await _safe_propose(
                    reasoner,
                    {
                        "query": query,
                        "domain": domain,
                        "preferences": preferences,
                        "observations": observations,
                        "tools": active_names,
                        "target": GATHER_TARGET,
                        "found": len(unique),
                    },
                )
            if not _valid_action(action, active_names):
                action = relaxation_queue.pop(0) if relaxation_queue else None
            if not _valid_action(action, active_names):
                break
            params = {"max_results": 10, **(action.get("parameters") or {})}
            await _run(action["tool"], params, 60 + int((iterations / MAX_GATHER_ITERS) * 8))
            unique = _unique_items(executions, domain)

        state["executions"] = executions
        state["gather_iterations"] = iterations
        state["candidate_count"] = len(unique)
        await _emit(
            state,
            progress_callback,
            {
                "stage": "candidate_gather",
                "stage_number": 1,
                "status": "completed",
                "progress": 70,
                "message": f"{domain.title()} candidate gathering completed",
                "tools": active_names,
                "gather_iterations": iterations,
                "skipped_tools": state.get("skipped_tools", []),
            },
        )
        return state

    async def normalize_and_rank(state: GenericRuntimeState) -> GenericRuntimeState:
        await _emit(
            state,
            progress_callback,
            {
                "stage": "normalize_and_rank",
                "stage_number": 2,
                "status": "started",
                "progress": 80,
                "message": "Normalizing and ranking candidates...",
            },
        )
        domain = state.get("domain", "unknown")
        items: List[Dict[str, Any]] = []
        errors = list(state.get("errors", []))
        for execution in state.get("executions", []):
            if not execution.get("success"):
                if execution.get("error"):
                    errors.append(str(execution["error"]))
                continue
            items.extend(normalize_tool_items(str(execution.get("tool")), execution.get("output"), domain))

        ranked = _rank_items(items)
        # WP4: the constraint-aware path (M2/domain_v1) is gated by METAREC_PRODUCT_RANKER.
        # Default "legacy" leaves ordering byte-identical to the pre-WP4 baseline
        # (acceptance criterion 1: feature-off == B0, no network needed).
        if domain == "product" and _product_ranker() != "legacy":
            constraints = resolve_constraints(state.get("query", ""), state.get("preferences", {}))
            outcome = apply_hard_constraints(ranked, constraints)
            state["constraint_filter"] = outcome.to_metadata()
            ranked = outcome.kept
            if outcome.exhausted:
                errors.append(
                    "No products matched the stated hard constraints "
                    "(e.g. every candidate was over budget)."
                )
        state["items"] = ranked[:10]
        state["errors"] = errors
        await _emit(
            state,
            progress_callback,
            {
                "stage": "normalize_and_rank",
                "stage_number": 2,
                "status": "completed",
                "progress": 92,
                "message": "Candidate ranking completed",
                "item_count": len(state["items"]),
            },
        )
        return state

    async def recommendation_result(state: GenericRuntimeState) -> GenericRuntimeState:
        state["metadata"] = {
            "domain": state.get("domain"),
            "graph": "generic_domain_graph",
            "tool_tags": state.get("tool_tags", []),
            "selected_tools": state.get("selected_tools", []),
            "skipped_tools": state.get("skipped_tools", []),
            "executions": state.get("executions", []),
            "items_count": len(state.get("items", [])),
            "gather_iterations": state.get("gather_iterations", 0),
            "candidate_count": state.get("candidate_count", 0),
            "constraint_filter": state.get("constraint_filter"),
        }
        await _emit(
            state,
            progress_callback,
            {
                "stage": "recommendation_result",
                "stage_number": 3,
                "status": "completed",
                "progress": 100,
                "message": "Recommendations ready!",
            },
        )
        return state

    graph = StateGraph(GenericRuntimeState)
    graph.add_node("candidate_gather", candidate_gather)
    graph.add_node("normalize_and_rank", normalize_and_rank)
    graph.add_node("recommendation_result", recommendation_result)
    graph.add_edge(START, "candidate_gather")
    graph.add_edge("candidate_gather", "normalize_and_rank")
    graph.add_edge("normalize_and_rank", "recommendation_result")
    graph.add_edge("recommendation_result", END)
    return graph.compile()


async def run_generic_domain_graph(
    *,
    query: str,
    domain: str,
    preferences: Optional[Dict[str, Any]] = None,
    use_online_agent: bool = False,
    tool_tags: Optional[List[str]] = None,
    adapters: Optional[GenericGraphAdapters] = None,
    progress_callback: Optional[ProgressCallback] = None,
) -> GenericGraphResult:
    graph = build_generic_domain_graph(adapters=adapters, progress_callback=progress_callback)
    final_state = await graph.ainvoke(
        {
            "query": query,
            "domain": domain,
            "preferences": preferences or {},
            "use_online_agent": use_online_agent,
            "tool_tags": tool_tags or [],
            "selected_tools": [],
            "skipped_tools": [],
            "executions": [],
            "items": [],
            "progress_events": [],
            "metadata": {},
            "errors": [],
        }
    )
    return GenericGraphResult(
        executions=final_state.get("executions", []),
        items=final_state.get("items", []),
        progress_events=final_state.get("progress_events", []),
        metadata=final_state.get("metadata", {}),
        errors=final_state.get("errors", []),
    )
