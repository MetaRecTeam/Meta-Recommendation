# Environment variables

- for the primary data store + LangGraph runtime checkpointing
    - `DATABASE_URL` (**required**): PostgreSQL connection string — the primary store
      for users, conversations, feedback, *and* LangGraph checkpoints. Local example
      `postgresql://metarec:metarec@localhost:5432/metarec?sslmode=disable`; on
      Hugging Face Spaces use an external managed Postgres (e.g. Neon) with the plain
      `postgresql://USER:PASSWORD@HOST/DB?sslmode=require` form.
    - `METAREC_CHECKPOINTER_BACKEND`: defaults to `postgres`; use `memory` only for tests
    - `LANGGRAPH_STRICT_MSGPACK`: set to `true` in Compose and CI

- for tool-output compaction (used in `langgraph_metarec/tool_compaction.py`)
    - The raw Google Maps / Yelp / Xiaohongshu search results are size-bounded
      before they reach the summarizer LLM and the persisted `metadata.executions`
      blob. Structured metadata (opening hours, coordinates, links, price, ...) is
      preserved verbatim; only high-volume free text (e.g. Google Maps
      `user_reviews`) is capped. Tune without redeploying code via:
    - `METAREC_TOOL_MAX_ITEMS` (default `10`) — max candidates kept per tool
    - `METAREC_TOOL_LIST_CAP` (default `3`) — max items kept in bounded nested lists (e.g. reviews)
    - `METAREC_TOOL_TEXT_CAP` (default `240`) — max characters kept per bounded free-text string

- product ranking (WP4)
    - `METAREC_PRODUCT_RANKER` (accepts exactly `legacy|domain_v1`, default
      `legacy`; an unrecognised value falls back to `legacy`) — selects the
      `product` domain ranker: `legacy` = pre-WP4 generic order (rating,
      reviews, popularity), byte-identical to B0, no network/ranker access;
      `domain_v1` (M2) = constraint-aware — enforces the stated hard
      constraints (budget, brand, category, model) before ranking and drops or
      explains violations. Flag name and values per the WP4 proposal's runtime
      contract.

- for live task-progress streaming (`GET /api/status/{task_id}/stream`, used in `main.py`)
    - The frontend watches in-flight recommendation tasks over Server-Sent Events
      so progress/thinking-step updates arrive in real time instead of on a 1s
      poll; the server pushes a frame only when the task projection changes and
      stops on completion/error. `/api/status/{task_id}` stays as the polling
      fallback when SSE can't get through. Tune the server-side stream via:
    - `METAREC_SSE_POLL_INTERVAL` (default `0.4`) — seconds between server-side projection checks
    - `METAREC_SSE_NOT_FOUND_TIMEOUT` (default `10`) — seconds to wait for a task to appear before emitting a terminal error frame
    - `METAREC_SSE_MAX_DURATION` (default `300`) — hard cap (seconds) on a single stream's lifetime

- for in-conversation memory / context (used in `conversation_context.py`)
    - Each turn is given memory built server-side from the persisted messages: a
      verbatim window of recent turns (incl. recommendations + the user's feedback),
      a rolling compressed summary of older turns (fast model, off the reply path,
      persisted on the conversation's `metadata.context_summary` with a watermark),
      and a structured ledger (accumulated preferences + shown/disliked places).
      The task's preferences are persisted back to the conversation so a later
      "make it cheaper / somewhere closer" refines the prior request. Tune via:
    - `METAREC_CONTEXT_WINDOW_TURNS` (default `8`) — verbatim recent turns kept in the window
    - `METAREC_CONTEXT_SUMMARY_TRIGGER` (default `4`) — rolled-out turns required before re-summarizing

- for the restaurant agent planner/summarizer (used in `agent/`, selected by
  `client.create_agent_sync_client`)
    - Runs on the **OpenAI-compatible client** below (`LLM_BASE_URL` /
      `LLM_API_KEY` / `LLM_MODEL`) whenever an OpenAI-compatible API key is set.
    - `AGENT_PLANNING_MODEL` / `AGENT_SUMMARY_MODEL`: optional model overrides
      (both default to `LLM_MODEL`). The planning model must support OpenAI
      tool calling; the summary model needs reliable JSON output.
    - `AGENT_LLM_TIMEOUT_SECONDS` (default `120`) — request timeout for this
      client. The summarizer sends large compacted tool payloads, so it does
      not inherit the chat-path `LLM_TIMEOUT_SECONDS` (default 30 s).
    - Azure OpenAI is a **fallback** used only when no OpenAI-compatible key
      (`LLM_API_KEY` / `GROQ_API_KEY` / aliases) is configured:
      - `OPENAI_API_KEY` (Azure subscription key)
      - `AZURE_OPENAI_ENDPOINT`
      - `AZURE_OPENAI_API_VERSION`
      - `AZURE_AGENT_PLANNING_MODEL` (default `gpt-4.1`)
      - `AZURE_AGENT_SUMMARY_MODEL` (default `o4-mini`)
    
- for OpenAI-compatible LLM client (used in `client.py` / `llm_service.py`)
    - `LLM_BASE_URL` (canonical; defaults to `https://api.groq.com/openai/v1`)
    - `LLM_API_KEY` (canonical; falls back to `GROQ_API_KEY`)
    - `LLM_MODEL` (model name)
    - supported aliases for base URL: `OPENAI_COMPAT_BASE_URL`, `OPENAI_COMPATIBLE_BASE_URL`, `GROQ_BASE_URL`
    - supported aliases for API key: `OPENAI_COMPAT_API_KEY`, `OPENAI_COMPATIBLE_API_KEY`, `GROQ_API_KEY`
    - optional network knobs:
      - `LLM_TIMEOUT_SECONDS` (default `30`)
      - `LLM_SDK_MAX_RETRIES` (default `2`, OpenAI SDK network retries)
      - `LLM_TRUST_ENV` (default `true`; set `false` to ignore proxy-related environment variables)
      - `LLM_TEXT_MAX_TOKENS` (default `1024`, used for non-JSON confirmation/guidance messages)
    - optional cost pricing (for the admin **Token Consumption** card; token counts are
      always recorded, cost stays `$0` until priced):
      - `LLM_PRICE_INPUT_PER_1M` / `LLM_PRICE_OUTPUT_PER_1M` (USD per 1,000,000 prompt /
        completion tokens; default `0`)
      - `LLM_PRICE_TABLE_JSON` (optional per-model override, e.g.
        `{"gpt-4o": {"input": 2.5, "output": 10}}`; falls back to the globals above)
    - example for another OpenAI-style provider:
      ```powershell
      $env:LLM_BASE_URL="https://your-provider.example.com/openai/v1"
      $env:LLM_API_KEY="your_provider_key"
      $env:LLM_MODEL="your_provider_model"
      $env:LLM_TRUST_ENV="false"
      ```
    - `AGENT_PLANNING_MODEL` / `AGENT_SUMMARY_MODEL` also target this client
      (see the restaurant agent planner/summarizer section above)
    
- for serpapi api (used in `agent/agent_mcp/`)
    - `SERPAPI_KEY`

- for xiaohongshu (used in `agent/agent_mcp/agent_xiaohongshu.py`)
    - `TIKHUB_API_KEY`
    - `API_302_KEY`

- for multi-domain recommendation tools (used in `langgraph_metarec/tool_registry.py`)
    - `TMDB_API_ACCESS_TOKEN` — movie/TV search + discovery (cast/crew/genre). Use the v4 Read Access Token.
    - `HARDCOVER_API_KEY` — book keyword search.
    - `LASTFM_API_KEY` — popularity-ranked music discovery (`lastfm.track.discover`).
      **Optional**: the tool self-skips (reports `missing_credentials`) when unset.
    - MusicBrainz recording search/discover, Cover Art Archive, and OpenLibrary book
      discovery (author/publisher/subject) require **no** credentials.
    - Hotel domain: `gmap.hotel.search` reuses the existing `SERPAPI_KEY` (SerpAPI
      Google Maps engine — no extra credential); `osm.hotel.discover` (Nominatim
      geocoding + Overpass lodging lookup) requires **no** credentials.

- for itinerary leg ETAs / routing (used in `langgraph_metarec/eta.py`)
    - `ITINERARY_SOLVER` — solver adapter name. Defaults to `beam`; unsupported
      values fail explicitly. Future MILP adapters use the same serialized
      planning IR and service boundary.
    - `ONEMAP_EMAIL` / `ONEMAP_PASSWORD` — OneMap (SLA) account credentials for
      Singapore walk + public-transport leg routing with fares (free account at
      https://www.onemap.gov.sg/). **Optional**: without them legs fall back to
      Mapbox Directions, and without any provider to deterministic haversine
      estimates (legs are labeled with their `source`). The JWT is fetched and
      cached in-process (~3-day validity, refreshed early).
    - `MAPBOX_ACCESS_TOKEN` — backend-only Mapbox Directions token for routing
      outside Singapore and OneMap fallback. `VITE_MAPBOX_TOKEN` remains a
      compatibility fallback, but production should scope the two tokens separately.
    - Dynamic solving itself never calls a routing provider: it uses normalized
      duration/cost/availability evidence and deterministic haversine estimates.
      Routing APIs are called only for the chosen legs (cached by provider, mode,
      rounded coordinates, and public-transport service date/time bucket).

- for authentication / roles (used in `main.py`, `business_repositories.py`)
    - `METAREC_SESSION_COOKIE_NAME` (default `metarec_session`) — app session cookie
    - `METAREC_SESSION_COOKIE_SECURE` (default `false`) — set `true` behind HTTPS
    - `METAREC_SESSION_MAX_AGE_SECONDS` (default 30 days)
    - `METAREC_ADMIN_EMAILS` — comma-separated emails promoted to the `admin` role on
      startup (idempotent; the user must have registered first). Example:
      `METAREC_ADMIN_EMAILS="alice@example.com,bob@example.com"`
    - `SEED_ADMIN_EMAIL` / `SEED_ADMIN_PASSWORD` — if both set, the app **creates** this
      admin account on startup (and re-asserts the `admin` role on every boot). Unlike
      `METAREC_ADMIN_EMAILS`, the user need not register first, so this is the shell-free
      way to bootstrap an admin on Hugging Face Spaces. Password must be >= 8 chars;
      idempotent (an existing account's password is left untouched).

- for the debug arena (used in `internal/debug/router.py`)
    - `DEBUG_UI_ENABLED` (default `false`) — master kill-switch for the `/internal/debug` API
    - Access now requires an authenticated user with the **`admin`** role (the former
      `DEBUG_ADMIN_TOKEN` / `DEBUG_ADMIN_TOKEN_HASH` / `DEBUG_SESSION_*` shared-token auth has
      been removed). Bootstrap an admin in one of these ways:
        1. Fresh account (create + promote, idempotent). Set `SEED_ADMIN_EMAIL` /
           `SEED_ADMIN_PASSWORD` — the app seeds it **on startup** (no shell needed;
           works on HF Spaces). Equivalent one-off command:
           `python scripts/seed_admin_user.py admin@metarec.local Admin12345!`
           (locally in Docker: `docker compose exec backend python scripts/seed_admin_user.py`).
        2. Promote an already-registered user: `METAREC_ADMIN_EMAILS` allowlist
           (auto-promote on startup), or `python scripts/seed_admin.py admin@example.com`.
        3. direct DB: `UPDATE users SET role='admin' WHERE email='admin@example.com';`
    - `DEBUG_LLM_EXPLAIN_ENABLED` (default `true`)
    - rate-limit knobs: `DEBUG_LLM_GEN_RATE_LIMIT_COUNT` / `_WINDOW_SECONDS`,
      `DEBUG_LLM_EXPLAIN_RATE_LIMIT_COUNT` / `_WINDOW_SECONDS`, `DEBUG_EXEC_TIMEOUT_SECONDS`
