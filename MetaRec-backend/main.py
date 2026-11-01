"""
MetaRec FastAPI Application
提供HTTP API接口，调用核心服务层
"""
from dotenv import load_dotenv, find_dotenv
dotenv_path = find_dotenv()
load_dotenv(dotenv_path)

from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from typing import List, Optional, Dict, Any, Awaitable, Callable
from datetime import datetime
from client import (
    LLM_API_KEY,
    LLM_BASE_URL,
    create_agent_sync_client,
    create_async_client,
    describe_openai_compatible_config,
    get_openai_compatible_transport_config,
)
from llm_compat import create_chat_completion_async
import os
import asyncio
import json
import time
import logging
import sys
import socket
from urllib.parse import urlparse

import httpx


# 配置日志系统 - 确保实时输出到控制台
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout)  # 输出到标准输出（控制台）
    ],
    force=True  # 强制重新配置，覆盖之前的配置
)

# 设置 uvicorn 的日志级别
logging.getLogger("uvicorn").setLevel(logging.INFO)
logging.getLogger("uvicorn.access").setLevel(logging.INFO)

logger = logging.getLogger("metarec.api")

# 导入核心服务
from service import ItineraryConflictError, MetaRecService
from internal.debug.router import create_debug_router
from internal.admin.router import create_admin_router
from internal.feedback.router import create_feedback_router
from internal.item_interactions.router import create_item_interaction_router
from business_models import AuthSessionPayload, UserRole
from business_repositories import auth_repository, conversation_repository, profile_repository
from profile_model import apply_profile_memory_from_preferences, assemble_domains, normalize_profile
from preference_specs import build_domain_form


def _admin_allowlist_emails() -> List[str]:
    raw = os.getenv("METAREC_ADMIN_EMAILS", "")
    return [part.strip() for part in raw.split(",") if part.strip()]


async def _promote_admins_from_allowlist() -> None:
    """Promote registered users in METAREC_ADMIN_EMAILS to ADMIN on startup.
    Idempotent and non-fatal — a user must already exist (register first) to be
    promoted; unknown emails are simply skipped."""
    if not os.getenv("DATABASE_URL"):
        return
    emails = _admin_allowlist_emails()
    if not emails:
        return
    try:
        promoted = await auth_repository.promote_admins(emails)
        logging.getLogger(__name__).info(
            "[startup] Admin allowlist processed: %d/%d promoted", promoted, len(emails)
        )
    except Exception as exc:  # pragma: no cover - startup best-effort
        logging.getLogger(__name__).warning("[startup] Admin promotion failed: %s", exc)


async def _seed_admin_user_from_env() -> None:
    """Create-and-promote a full admin account on startup from
    SEED_ADMIN_EMAIL / SEED_ADMIN_PASSWORD.

    Unlike METAREC_ADMIN_EMAILS (which only *promotes* an already-registered user),
    this creates the account if it is missing — the only shell-free way to bootstrap
    an admin on hosts without exec access (e.g. Hugging Face Spaces). Idempotent
    (re-asserts the role on every boot, password left untouched if the user exists)
    and non-fatal."""
    if not os.getenv("DATABASE_URL"):
        return
    email = (os.getenv("SEED_ADMIN_EMAIL") or "").strip()
    password = os.getenv("SEED_ADMIN_PASSWORD") or ""
    if not email or not password:
        return
    if len(password) < 8:
        logging.getLogger(__name__).warning(
            "[startup] SEED_ADMIN_PASSWORD must be >= 8 chars; skipping admin seed"
        )
        return
    try:
        try:
            await auth_repository.register(email=email, password=password, display_name="Admin")
            created = True
        except ValueError as exc:
            if "already registered" not in str(exc):
                raise
            created = False
        await auth_repository.set_role_by_email(email, UserRole.ADMIN)
        logging.getLogger(__name__).info(
            "[startup] Admin seed %s: %s", "created" if created else "existed (role ensured)", email
        )
    except Exception as exc:  # pragma: no cover - startup best-effort
        logging.getLogger(__name__).warning("[startup] Admin seed failed: %s", exc)


@asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    # WP4: METAREC_PRODUCT_RANKER accepts exactly legacy|domain_v1; anything else stops startup
    # instead of silently running the wrong product ranker.
    from langgraph_metarec.product_ranker_live import validate_product_ranker_setting

    validate_product_ranker_setting()
    await _seed_admin_user_from_env()
    await _promote_admins_from_allowlist()
    yield
    # Close the LangGraph checkpointer's Postgres connection on shutdown.
    try:
        await metarec_service.runtime_checkpointer.aclose()
    except Exception:  # pragma: no cover - shutdown best-effort
        logging.getLogger(__name__).debug("Checkpointer close failed on shutdown", exc_info=True)


app = FastAPI(title="MetaRec API", version="1.0.0", lifespan=_lifespan)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ],
    allow_origin_regex=r"https://.*\.hf\.space",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# create OpenAI clients
async_client = create_async_client()
llm_model = os.getenv('LLM_MODEL')
logging.getLogger("metarec.llm").info(
    "OpenAI-compatible LLM config: %s",
    describe_openai_compatible_config(llm_model),
)

sync_client, summary_model, planning_model = create_agent_sync_client()
logging.getLogger("metarec.llm").info(
    "Agent planner/summarizer provider: %s (planning=%s, summary=%s)",
    "azure" if type(sync_client).__name__ == "AzureOpenAI" else "openai-compatible",
    planning_model,
    summary_model,
)

# ==================== 创建服务实例 ====================
# 这是全局服务实例，可以被所有路由使用
metarec_service = MetaRecService(async_client, sync_client, summary_model, planning_model, llm_model)

# ==================== Auth helpers ====================
AUTH_COOKIE_NAME = os.getenv("METAREC_SESSION_COOKIE_NAME", auth_repository.cookie_name)
AUTH_COOKIE_SECURE = os.getenv("METAREC_SESSION_COOKIE_SECURE", "false").lower() in {"1", "true", "yes", "on"}
AUTH_SESSION_MAX_AGE_SECONDS = int(os.getenv("METAREC_SESSION_MAX_AGE_SECONDS", str(30 * 24 * 60 * 60)))


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=token,
        max_age=AUTH_SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=AUTH_COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=AUTH_COOKIE_NAME, path="/")


async def get_optional_auth_session(request: Request) -> Optional[AuthSessionPayload]:
    return await auth_repository.session_from_token(request.cookies.get(AUTH_COOKIE_NAME))


async def require_auth_session(request: Request) -> AuthSessionPayload:
    session = await get_optional_auth_session(request)
    if session is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return session


async def resolve_request_user_id(request: Request, provided_user_id: Optional[str] = None) -> str:
    session = await require_auth_session(request)
    if provided_user_id and provided_user_id != "default" and provided_user_id != session.user.id:
        raise HTTPException(status_code=403, detail="user_id does not match authenticated session")
    return session.user.id


async def require_path_user(request: Request, user_id: str) -> AuthSessionPayload:
    session = await require_auth_session(request)
    if session.user.id != user_id:
        raise HTTPException(status_code=403, detail="user_id does not match authenticated session")
    return session


async def require_admin_session(request: Request) -> AuthSessionPayload:
    """Require an authenticated user with the ADMIN role. Reused by the debug
    arena to gate privileged tooling on a real user's role."""
    session = await require_auth_session(request)
    if session.user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="Admin role required")
    return session


# 挂载内部 debug 路由（可用性由 DEBUG_UI_ENABLED 控制；访问需要 ADMIN 角色）
app.include_router(create_debug_router(lambda: metarec_service, require_admin_session))

# 挂载管理后台路由（仅按 ADMIN 角色控制，不受 DEBUG_UI_ENABLED 影响）
app.include_router(create_admin_router(require_admin_session))

# 挂载用户反馈路由（需登录会话；游客在端点内被拒绝）
app.include_router(create_feedback_router(require_auth_session))

# 挂载 item 级交互路由（Save / Not interested / Played…；需登录会话；游客在端点内被拒绝）
app.include_router(create_item_interaction_router(require_auth_session))


def _merge_meaningful_preferences(existing: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(existing or {})
    default_preferences = metarec_service.get_default_preferences()

    for key, value in (incoming or {}).items():
        if value is None:
            continue
        existing_value = merged.get(key)
        default_value = default_preferences.get(key)

        if isinstance(value, list):
            meaningful = [item for item in value if item not in (None, "", "any")]
            if meaningful or key not in merged:
                merged[key] = value
            continue

        if isinstance(value, dict):
            meaningful_dict = {k: v for k, v in value.items() if v is not None and v != ""}
            if not meaningful_dict:
                continue
            if value == default_value and existing_value not in (None, {}, default_value):
                continue
            merged[key] = {**(existing_value if isinstance(existing_value, dict) else {}), **meaningful_dict}
            continue

        if value != "any" or key not in merged:
            merged[key] = value

    return merged


_PERSISTABLE_RESTAURANT_PREFERENCE_KEYS = {
    "restaurant_types",
    "flavor_profiles",
    "dining_purpose",
    "budget_range",
    "location",
}


def _restaurant_preference_subset(preferences: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in (preferences or {}).items()
        if key in _PERSISTABLE_RESTAURANT_PREFERENCE_KEYS
    }


async def _persist_profile_preferences_from_result(
    user_id: str,
    preferences: Optional[Dict[str, Any]],
    *,
    update_persona_memory: bool = False,
) -> None:
    if not isinstance(preferences, dict) or not preferences:
        return
    profile = await profile_repository.get_user_profile(user_id)
    domains = assemble_domains(profile)
    domain = str(preferences.get("domain") or "restaurant").lower()
    request_scoped_keys = {"food_intent", "domain", "query"}
    if domain in {"restaurant", "multi_domain", "unknown"}:
        restaurant_preferences = _restaurant_preference_subset(preferences)
        if not restaurant_preferences:
            return
        domains["restaurant"] = _merge_meaningful_preferences(
            domains.get("restaurant", {}),
            restaurant_preferences,
        )
    else:
        domain_preferences = {
            key: value
            for key, value in preferences.items()
            if key not in request_scoped_keys and value not in (None, "", [], {})
        }
        if not domain_preferences:
            return
        domains[domain] = _merge_meaningful_preferences(domains.get(domain, {}), domain_preferences)

    restaurant_slice = domains.pop("restaurant", {})
    metadata = profile.setdefault("metadata", {})
    metadata["preferences"] = {}
    metadata["domains"] = domains
    profile["dining_habits"] = restaurant_slice
    if update_persona_memory:
        profile = apply_profile_memory_from_preferences(
            profile,
            preferences,
            source="confirmed_recommendation",
            evidence=str(preferences.get("query") or "")[:240],
        )
    await profile_repository.save_user_profile(user_id, profile)

# ==================== 静态文件服务配置 ====================
FRONTEND_DIST = (Path(__file__).parent.parent / 'frontend-dist').resolve()

# 启动时检查静态文件目录
def check_frontend_dist():
    """检查前端静态文件目录是否存在"""
    if os.path.exists(FRONTEND_DIST):
        print(f"[INFO] Frontend dist directory found: {FRONTEND_DIST}")
        index_path = os.path.join(FRONTEND_DIST, "index.html")
        if os.path.exists(index_path):
            print(f"[INFO] Frontend index.html found: {index_path}")
        else:
            print(f"[WARN] index.html not found in {FRONTEND_DIST}")
        # 列出目录内容
        try:
            files = os.listdir(FRONTEND_DIST)
            print(f"[INFO] Frontend dist contents: {files[:10]}...")  # 只显示前10个
        except Exception as e:
            print(f"[WARN] Error listing frontend dist: {e}")
    else:
        print(f"[WARN] Frontend dist directory not found: {FRONTEND_DIST}")

# 在应用启动时检查
check_frontend_dist()


# ==================== API数据模型 ====================
# 这些模型用于API请求和响应，与服务层的模型分离

class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AuthUserAPI(StrictBaseModel):
    id: str
    kind: str
    role: str
    email: Optional[str] = None
    display_name: Optional[str] = None
    status: str


class AuthSessionAPI(StrictBaseModel):
    id: str
    user_id: str
    anonymous_device_id: Optional[str] = None
    status: str
    expires_at: str


class AuthResponseAPI(StrictBaseModel):
    user: AuthUserAPI
    session: AuthSessionAPI


class GuestLoginRequestAPI(StrictBaseModel):
    device_id: str


class RegisterRequestAPI(StrictBaseModel):
    email: str
    password: str
    display_name: Optional[str] = None


class LoginRequestAPI(StrictBaseModel):
    email: str
    password: str


class ProcessMessageAPI(StrictBaseModel):
    role: str
    content: str


class ProcessRequestAPI(StrictBaseModel):
    query: str
    user_id: str = "default"
    conversation_history: Optional[List[ProcessMessageAPI]] = None
    conversation_id: Optional[str] = None
    use_online_agent: bool = False
    source_message_id: Optional[str] = None
    parent_message_id: Optional[str] = None
    replay_from_message_id: Optional[str] = None
    branch_id: Optional[str] = None
    time_travel_mode: Optional[str] = None
    domain_lock: Optional[str] = None
    itinerary_mode: Optional[bool] = None
    hitl_state: Optional[Dict[str, Any]] = Field(
        default=None,
        json_schema_extra={"additionalProperties": True},
    )


class HealthResponseAPI(StrictBaseModel):
    status: str
    timestamp: str


class ApiInfoResponseAPI(StrictBaseModel):
    message: str
    version: str


class FrontendConfigResponseAPI(StrictBaseModel):
    mapboxToken: str


class RestaurantAPI(StrictBaseModel):
    id: str
    name: str
    address: Optional[str] = None
    area: Optional[str] = None
    cuisine: Optional[str] = None
    type: Optional[str] = None
    location: Optional[str] = None
    rating: Optional[float] = None
    reviews_count: Optional[int] = None
    price: Optional[str] = None
    price_per_person_sgd: Optional[str] = None
    distance_or_walk_time: Optional[str] = None
    open_hours_note: Optional[str] = None
    highlights: Optional[List[str]] = None
    flavor_match: Optional[List[str]] = None
    purpose_match: Optional[List[str]] = None
    why: Optional[str] = None
    reason: Optional[str] = None
    reference: Optional[str] = None
    sources: Optional[Dict[str, str]] = None
    phone: Optional[str] = None
    gps_coordinates: Optional[Dict[str, float]] = None


class RecommendationItemAPI(StrictBaseModel):
    id: str
    domain: str
    title: str
    subtitle: Optional[str] = None
    description: Optional[str] = None
    image_url: Optional[str] = None
    url: Optional[str] = None
    rating: Optional[float] = None
    reviews_count: Optional[int] = None
    source: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    why: Optional[str] = None
    gps_coordinates: Optional[Dict[str, float]] = None
    # NOTE: the internal RecommendationItem carries a ``raw`` upstream payload for
    # persistence/debug, but it is deliberately *not* exposed here — see
    # ``_client_safe_item`` and ``_persist_recommendation_result``.


class ThinkingStepAPI(StrictBaseModel):
    step: str
    description: str
    status: str
    details: Optional[str] = None


class ConfirmationQuickActionAPI(StrictBaseModel):
    id: str
    label: str
    value: str
    preference_patch: Dict[str, Any] = Field(
        default_factory=dict,
        json_schema_extra={"additionalProperties": True},
    )
    message: Optional[str] = None
    clear_preference_keys: Optional[List[str]] = None


class ConfirmationRequestAPI(StrictBaseModel):
    message: str
    preferences: Dict[str, Any] = Field(
        default_factory=dict,
        json_schema_extra={"additionalProperties": True},
    )
    needs_confirmation: bool = True
    # Server-generated, request-time preference form for the resolved domain.
    preference_form: Optional[Dict[str, Any]] = Field(
        default=None,
        json_schema_extra={"additionalProperties": True},
    )
    quick_actions: Optional[List[ConfirmationQuickActionAPI]] = None


class RecommendationResponseAPI(StrictBaseModel):
    restaurants: List[RestaurantAPI]
    items: List[RecommendationItemAPI] = Field(default_factory=list)
    thinking_steps: Optional[List[ThinkingStepAPI]] = None
    confirmation_request: Optional[ConfirmationRequestAPI] = None
    llm_reply: Optional[str] = None  # 后端 LLM 的回复（用于普通对话）
    intent: Optional[str] = None  # 意图类型
    task_id: Optional[str] = None
    result_id: Optional[str] = None
    domain: Optional[str] = None
    time_travel: Optional[Dict[str, Any]] = Field(
        default=None,
        json_schema_extra={"additionalProperties": True},
    )
    hitl_state: Optional[Dict[str, Any]] = Field(
        default=None,
        json_schema_extra={"additionalProperties": True},
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        json_schema_extra={"additionalProperties": True},
    )
    preferences: Optional[Dict[str, Any]] = Field(
        default=None,
        json_schema_extra={"additionalProperties": True},
    )  # 提取的偏好设置（当 intent 为 "query" 时）


class TaskStatusAPI(StrictBaseModel):
    task_id: str
    status: str  # "processing", "completed", "error", "cancelled"
    progress: int  # 0-100
    message: str
    result: Optional[RecommendationResponseAPI] = None
    error: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        json_schema_extra={"additionalProperties": True},
    )


class BudgetRangeInputAPI(StrictBaseModel):
    min: Optional[int] = 20
    max: Optional[int] = 60
    currency: str = "SGD"
    per: str = "person"


class UpdatePreferencesRequestAPI(StrictBaseModel):
    user_id: str = "default"
    restaurantTypes: List[str] = ["any"]
    flavorProfiles: List[str] = ["any"]
    diningPurpose: str = "any"
    budgetRange: BudgetRangeInputAPI = BudgetRangeInputAPI()
    location: str = "any"


class PreferencesResponseAPI(StrictBaseModel):
    preferences: Dict[str, Any] = Field(
        default_factory=dict,
        json_schema_extra={"additionalProperties": True},
    )


class UpdatePreferencesResponseAPI(StrictBaseModel):
    message: str
    preferences: Dict[str, Any] = Field(
        default_factory=dict,
        json_schema_extra={"additionalProperties": True},
    )


class UserPreferencesResponseAPI(StrictBaseModel):
    user_id: str
    preferences: Dict[str, Any] = Field(
        default_factory=dict,
        json_schema_extra={"additionalProperties": True},
    )


class GenericSuccessResponseAPI(StrictBaseModel):
    success: bool
    message: str


def _mask_debug_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if len(value) <= 10:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def _debug_exception(exc: Exception) -> Dict[str, Any]:
    cause = getattr(exc, "__cause__", None)
    context = getattr(exc, "__context__", None)
    data: Dict[str, Any] = {
        "type": type(exc).__name__,
        "repr": repr(exc),
    }
    status_code = getattr(exc, "status_code", None)
    if status_code is not None:
        data["status_code"] = status_code
    body = getattr(exc, "body", None)
    if body is not None:
        data["body"] = repr(body)[:500]
    if cause is not None:
        data["cause"] = {
            "type": type(cause).__name__,
            "repr": repr(cause),
        }
    if context is not None and context is not cause:
        data["context"] = {
            "type": type(context).__name__,
            "repr": repr(context),
        }
    return data


async def _debug_httpx_get(url: str, headers: Dict[str, str], trust_env: bool) -> Dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=trust_env) as client:
            response = await client.get(url, headers=headers)
        return {
            "ok": True,
            "status_code": response.status_code,
            "body_prefix": response.text[:160],
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": _debug_exception(exc),
        }


async def _debug_sdk_models() -> Dict[str, Any]:
    try:
        response = await async_client.models.list()
        return {
            "ok": True,
            "model_count": len(getattr(response, "data", []) or []),
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": _debug_exception(exc),
        }


async def _debug_sdk_chat() -> Dict[str, Any]:
    model = llm_model or os.getenv("LLM_MODEL")
    if not model:
        return {
            "ok": False,
            "error": {"type": "ConfigError", "repr": "LLM_MODEL is not configured"},
        }
    try:
        response = await create_chat_completion_async(
            async_client,
            model=model,
            messages=[{"role": "user", "content": "Reply with only: ok"}],
            temperature=0,
            max_tokens=16,
        )
        content = response.choices[0].message.content if response.choices else ""
        return {
            "ok": True,
            "content_prefix": (content or "")[:80],
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": _debug_exception(exc),
        }


# ==================== API路由 ====================


def _auth_response(payload: AuthSessionPayload) -> Dict[str, Any]:
    return {
        "user": {
            "id": payload.user.id,
            "kind": payload.user.kind,
            "role": payload.user.role.value,
            "email": payload.user.email,
            "display_name": payload.user.display_name,
            "status": payload.user.status,
        },
        "session": {
            "id": payload.session.id,
            "user_id": payload.session.user_id,
            "anonymous_device_id": payload.session.anonymous_device_id,
            "status": payload.session.status,
            "expires_at": payload.session.expires_at.isoformat(),
        },
    }


@app.post("/api/auth/guest", response_model=AuthResponseAPI)
async def guest_login(payload: GuestLoginRequestAPI, request: Request, response: Response):
    try:
        auth = await auth_repository.get_or_create_guest(
            device_id=payload.device_id,
            user_agent=request.headers.get("user-agent"),
        )
        _set_session_cookie(response, auth.token)
        return _auth_response(auth)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/auth/register", response_model=AuthResponseAPI)
async def register(payload: RegisterRequestAPI, request: Request, response: Response):
    try:
        existing_auth = await get_optional_auth_session(request)
        existing_guest_user_id = (
            existing_auth.user.id
            if existing_auth is not None and existing_auth.user.kind == "guest"
            else None
        )
        auth = await auth_repository.register(
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
            existing_guest_user_id=existing_guest_user_id,
        )
        _set_session_cookie(response, auth.token)
        return _auth_response(auth)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/auth/login", response_model=AuthResponseAPI)
async def login(payload: LoginRequestAPI, response: Response):
    try:
        auth = await auth_repository.login(email=payload.email, password=payload.password)
        _set_session_cookie(response, auth.token)
        return _auth_response(auth)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc))


@app.post("/api/auth/logout", response_model=GenericSuccessResponseAPI)
async def logout(request: Request, response: Response):
    await auth_repository.revoke_token(request.cookies.get(AUTH_COOKIE_NAME))
    _clear_session_cookie(response)
    return {"success": True, "message": "Logged out"}


@app.get("/api/auth/session", response_model=AuthResponseAPI)
async def auth_session(auth: AuthSessionPayload = Depends(require_auth_session)):
    return _auth_response(auth)


@app.get("/api", response_model=ApiInfoResponseAPI)
async def api_root():
    """
    返回API信息
    
    Returns:
        API基本信息
    """
    return {"message": "MetaRec API is running!", "version": "1.0.0"}


@app.get("/health", response_model=HealthResponseAPI)
async def health_check():
    """
    健康检查
    
    Returns:
        服务健康状态
    """
    return {"status": "healthy", "timestamp": datetime.now().isoformat()}


@app.get("/api/debug/llm-connection")
async def debug_llm_connection(_auth: AuthSessionPayload = Depends(require_admin_session)):
    """
    Diagnose LLM connectivity from inside the running backend process.

    Admin-only: it discloses LLM transport config and issues a live probe request,
    so it must not be reachable unauthenticated. The response is still redacted and
    does not expose API keys.
    """
    transport = get_openai_compatible_transport_config()
    parsed = urlparse(LLM_BASE_URL)
    host = parsed.hostname or ""
    dns_result: Dict[str, Any]
    try:
        addresses = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
        dns_result = {
            "ok": True,
            "addresses": sorted({item[4][0] for item in addresses})[:8],
        }
    except Exception as exc:
        dns_result = {
            "ok": False,
            "error": _debug_exception(exc),
        }

    proxy_env_names = [
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
    ]
    env_snapshot = {
        name: _mask_debug_value(os.getenv(name))
        for name in proxy_env_names
        if os.getenv(name) is not None
    }

    headers = {"Authorization": f"Bearer {LLM_API_KEY}"} if LLM_API_KEY else {}
    models_url = f"{LLM_BASE_URL.rstrip('/')}/models"

    return {
        "config": {
            "base_url": LLM_BASE_URL,
            "models_url": models_url,
            "chat_url_expected": f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
            "model": llm_model or os.getenv("LLM_MODEL"),
            "api_key_configured": bool(LLM_API_KEY),
            "transport": transport,
        },
        "env": env_snapshot,
        "dns": dns_result,
        "httpx_models_trust_env_true": await _debug_httpx_get(models_url, headers, True),
        "httpx_models_trust_env_false": await _debug_httpx_get(models_url, headers, False),
        "sdk_models_current_client": await _debug_sdk_models(),
        "sdk_chat_current_client": await _debug_sdk_chat(),
    }


@app.get("/api/config", response_model=FrontendConfigResponseAPI)
async def get_config():
    """
    获取前端配置信息（包括 Mapbox access token）

    Returns:
        配置信息
    """
    mapbox_token = os.getenv("VITE_MAPBOX_TOKEN", "")
    return {
        "mapboxToken": mapbox_token
    }


@app.post("/api/process", response_model=RecommendationResponseAPI)
async def process_user_request(query_data: ProcessRequestAPI, request: Request):
    """
    处理用户请求的统一接口
    融合了 LLM 意图识别、偏好提取、确认流程
    
    这个接口会自动处理：
    - 使用 LLM 进行意图识别和生成回复
    - 如果是推荐餐厅请求：触发推荐流程
    - 如果是普通对话：返回 LLM 的回复
    
    Args:
        query_data: {"query": "用户查询", "user_id": "用户ID（可选）", "conversation_history": "对话历史（可选）"}
        
    Returns:
        根据处理结果返回不同的响应：
        - 如果是 LLM 回复：返回 llm_reply 字段
        - 如果是确认请求：返回确认请求对象
        - 如果是任务创建：返回任务ID
        - 如果是修改请求：返回修改提示
    """
    try:
        query = query_data.query
        user_id = await resolve_request_user_id(request, query_data.user_id)
        conversation_history = query_data.conversation_history
        if conversation_history is not None:
            conversation_history = [msg.model_dump() for msg in conversation_history]
        conversation_id = query_data.conversation_id
        use_online_agent = query_data.use_online_agent
        replay_from_message_id = query_data.replay_from_message_id
        branch_id = query_data.branch_id
        time_travel_mode = query_data.time_travel_mode
        domain_lock = query_data.domain_lock
        hitl_state = query_data.hitl_state
        
        # 添加日志，确认参数接收
        print(f"[API] Received request - use_online_agent: {use_online_agent} (type: {type(use_online_agent)})")

        if (
            conversation_id
            and replay_from_message_id
            and (time_travel_mode is None or time_travel_mode == "linear_regenerate")
        ):
            try:
                await conversation_repository.mark_messages_superseded_after(
                    user_id,
                    conversation_id,
                    replay_from_message_id,
                    branch_id,
                )
            except Exception as e:
                print(f"Warning: Failed to mark superseded messages: {e}")
        
        # 调用异步处理函数（使用 LLM 进行意图识别）
        result = await metarec_service.handle_user_request_async(
            query,
            user_id,
            conversation_history,
            conversation_id,
            use_online_agent,
            message_id=query_data.source_message_id,
            branch_id=branch_id,
            timeline_cursor=replay_from_message_id or query_data.parent_message_id,
            domain_lock=domain_lock,
            itinerary_mode=bool(query_data.itinerary_mode),
            hitl_state=hitl_state,
        )

        time_travel_payload = None
        if replay_from_message_id or branch_id or time_travel_mode:
            time_travel_payload = {
                "mode": time_travel_mode or "linear_regenerate",
                "replay_from_message_id": replay_from_message_id,
                "branch_id": branch_id,
                "source_message_id": query_data.source_message_id,
                "parent_message_id": query_data.parent_message_id,
            }
        
        # 如果响应包含 preferences 且有 conversation_id，更新 conversation 的 preferences
        # （food_intent 为请求级，不写入会话基线，避免上一句的菜品粘连到下一次请求）
        if result.get("preferences") and conversation_id:
            try:
                result_preferences = result["preferences"]
                persistable_preferences = _restaurant_preference_subset(result_preferences)
                if persistable_preferences:
                    await conversation_repository.update_conversation_preferences(user_id, conversation_id, persistable_preferences)
            except Exception as e:
                print(f"Warning: Failed to update conversation preferences: {e}")
        if result.get("preferences"):
            try:
                await _persist_profile_preferences_from_result(
                    user_id,
                    result["preferences"],
                    update_persona_memory=result.get("type") == "task_created",
                )
            except Exception as e:
                print(f"Warning: Failed to update profile preferences: {e}")
        
        # 根据处理结果类型返回不同的响应
        if result["type"] == "llm_reply":
            # LLM 的普通对话回复
            # 如果是confirm no的情况（intent为confirmation_no或chat且有preferences），确保返回preferences
            intent = result.get("intent", "chat")
            preferences = result.get("preferences")
            return RecommendationResponseAPI(
                restaurants=[],
                thinking_steps=None,
                confirmation_request=None,
                llm_reply=result.get("llm_reply", ""),
                intent=intent,
                domain=result.get("domain"),
                time_travel=time_travel_payload,
                hitl_state=result.get("hitl_state"),
                metadata=client_safe_metadata(result.get("metadata")),
                preferences=preferences
            )

        elif result["type"] == "task_created":
            # 任务已创建，返回任务ID和thinking step
            task_id = result["task_id"]
            metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
            metadata = client_safe_metadata({**metadata, "task_id": task_id})
            return RecommendationResponseAPI(
                restaurants=[],
                thinking_steps=[ThinkingStepAPI(
                    step="start_processing",
                    description="Starting recommendation process...",
                    status="thinking",
                    details=f"Task ID: {task_id}"
                )],
                confirmation_request=None,
                task_id=task_id,
                domain=result.get("domain"),
                time_travel=time_travel_payload,
                hitl_state=result.get("hitl_state"),
                metadata=metadata,
                preferences=result.get("preferences")
            )
        
        elif result["type"] == "confirmation":
            # 需要确认，返回确认请求
            confirmation = result["confirmation_request"]
            # 确保返回intent信息（如果有）
            intent = result.get("intent")
            # 安全地转换 confirmation 对象，确保 preferences 中的列表被正确处理
            confirmation_dict = confirmation.dict()
            # 确保 preferences 中的列表被正确复制（避免引用问题）
            if "preferences" in confirmation_dict:
                preferences = confirmation_dict["preferences"]
                if isinstance(preferences, dict):
                    # 深拷贝 preferences 字典，确保列表被正确复制
                    import copy
                    confirmation_dict["preferences"] = copy.deepcopy(preferences)
            return RecommendationResponseAPI(
                restaurants=[],
                thinking_steps=None,
                confirmation_request=ConfirmationRequestAPI(**confirmation_dict),
                intent=intent,
                domain=result.get("domain"),
                time_travel=time_travel_payload,
                hitl_state=result.get("hitl_state"),
                metadata=client_safe_metadata(result.get("metadata")),
                preferences=result.get("preferences")
            )

        else:  # modify_request
            # 需要修改，返回修改提示
            return RecommendationResponseAPI(
                restaurants=[],
                thinking_steps=None,
                confirmation_request=ConfirmationRequestAPI(
                    message=result["message"],
                    preferences=result.get("preferences", {}),
                    needs_confirmation=True
                ),
                domain=result.get("domain"),
                time_travel=time_travel_payload,
                hitl_state=result.get("hitl_state"),
                metadata=client_safe_metadata(result.get("metadata")),
                preferences=result.get("preferences")
            )
    
    except HTTPException:
        raise
    except Exception:
        logger.exception("process_user_request failed")
        raise HTTPException(status_code=500, detail="Error processing request")


# Server-side-only diagnostic keys carried in result/task metadata: raw third-party
# tool outputs, the LLM tool-plan, and internal tool names. The frontend never reads
# them; stripping at the API boundary keeps them out of responses AND out of the
# conversation message metadata the client persists from a result. The durable result
# store keeps full detail for admin/debug.
_INTERNAL_METADATA_KEYS = (
    "executions",
    "plan_calls",
    "selected_tools",
    "skipped_tools",
    "progress_events",
    "product_ranker",  # WP4 D4: scores, components and reason codes stay server-side
)


def client_safe_metadata(metadata: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return a copy of result/task metadata with server-only diagnostic blobs
    removed, recursing into the nested task-projection ``result_metadata``."""
    if not isinstance(metadata, dict):
        return metadata
    cleaned = {key: value for key, value in metadata.items() if key not in _INTERNAL_METADATA_KEYS}
    nested = cleaned.get("result_metadata")
    if isinstance(nested, dict):
        cleaned["result_metadata"] = client_safe_metadata(nested)
    return cleaned


def _client_safe_item(item: Any) -> Dict[str, Any]:
    """Project a recommendation item to its client-facing fields, dropping the
    ``raw`` upstream provider payload. ``raw`` is retained server-side in the
    recommendation_results store for persistence/debug but must never ship to
    the client (it can carry unbounded, unsanitized third-party data)."""
    if hasattr(item, "model_dump"):
        data = item.model_dump()
    elif hasattr(item, "dict"):
        data = item.dict()
    else:
        try:
            data = dict(item)
        except (TypeError, ValueError):
            data = {}
    if not isinstance(data, dict):
        data = {}
    data.pop("raw", None)
    return data


def _build_task_status_api(task_status: Dict[str, Any], task_id: str) -> TaskStatusAPI:
    """Project a raw task-status dict (in-memory or persisted) into the public
    TaskStatusAPI shape. Shared by the polling endpoint and the SSE stream so both
    serve byte-identical frames."""
    result_api = None
    if task_status.get("result"):
        result = task_status["result"]
        if hasattr(result, "model_dump"):
            result_data = result.model_dump()
        elif hasattr(result, "dict"):
            result_data = result.dict()
        else:
            result_data = result if isinstance(result, dict) else {}
        restaurants_data = result_data.get("restaurants", [])
        items_data = result_data.get("items", [])
        thinking_steps_data = result_data.get("thinking_steps")
        metadata = result_data.get("metadata") if isinstance(result_data.get("metadata"), dict) else {}
        metadata = client_safe_metadata(metadata) or {}
        status_metadata = task_status.get("metadata") if isinstance(task_status.get("metadata"), dict) else {}
        result_task_id = (
            result_data.get("task_id")
            or metadata.get("task_id")
            or status_metadata.get("task_id")
            or task_status.get("task_id")
            or task_id
        )
        result_id = result_data.get("result_id") or metadata.get("result_id") or status_metadata.get("result_id")
        if result_task_id and not metadata.get("task_id"):
            metadata["task_id"] = result_task_id
        if result_id and not metadata.get("result_id"):
            metadata["result_id"] = result_id
        result_api = RecommendationResponseAPI(
            restaurants=[
                RestaurantAPI(**(r.dict() if hasattr(r, "dict") else r))
                for r in restaurants_data
            ],
            items=[
                RecommendationItemAPI(**_client_safe_item(item))
                for item in items_data
            ],
            thinking_steps=[
                ThinkingStepAPI(**(s.dict() if hasattr(s, "dict") else s))
                for s in thinking_steps_data
            ] if thinking_steps_data else None,
            confirmation_request=None,
            task_id=result_task_id,
            result_id=result_id,
            domain=metadata.get("domain"),
            metadata=metadata or None,
            preferences=metadata.get("preferences"),
        )

    return TaskStatusAPI(
        task_id=task_status.get("task_id", task_id),
        status=task_status.get("status", "unknown"),
        progress=task_status.get("progress", 0),
        message=task_status.get("message", ""),
        result=result_api,
        error=task_status.get("error"),
        metadata=client_safe_metadata(task_status.get("metadata")) if isinstance(task_status.get("metadata"), dict) else None,
    )


async def sse_task_status_frames(
    fetch_status: Callable[[], Awaitable[Optional[Dict[str, Any]]]],
    task_id: str,
    *,
    is_disconnected: Optional[Callable[[], Awaitable[bool]]] = None,
    interval: float = 0.4,
    not_found_timeout: float = 10.0,
    max_duration: float = 300.0,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
):
    """Yield Server-Sent-Events frames for a task's status until it settles.

    `fetch_status` returns the current public status dict (or None if the task
    isn't visible yet). A frame is emitted only when the serialized status
    changes, so an idle task produces no traffic. The generator stops after the
    task reaches a terminal state, the client disconnects, the task never appears
    within `not_found_timeout`, or `max_duration` elapses (safety cap). The clock
    and sleep are injectable so the logic is unit-testable without real delays."""
    start = now()
    last_serialized: Optional[str] = None
    # Open the stream immediately so the client's connection settles fast.
    yield ": connected\n\n"
    while True:
        if is_disconnected is not None and await is_disconnected():
            return
        if now() - start > max_duration:
            return
        status = await fetch_status()
        if status is None:
            if now() - start > not_found_timeout:
                # Surface as a terminal error frame (not an SSE `error` event) so
                # the client treats it as settled and stops instead of reconnecting.
                yield "data: " + json.dumps(
                    {
                        "task_id": task_id,
                        "status": "error",
                        "progress": 0,
                        "message": "Task not found",
                        "error": "Task not found",
                        "result": None,
                        "metadata": None,
                    }
                ) + "\n\n"
                return
            await sleep(interval)
            continue
        serialized = json.dumps(status, default=str, sort_keys=True)
        if serialized != last_serialized:
            last_serialized = serialized
            yield "data: " + json.dumps(status, default=str) + "\n\n"
        if status.get("status") in {"completed", "error", "cancelled"}:
            return
        await sleep(interval)


@app.get("/api/status/{task_id}", response_model=TaskStatusAPI)
async def get_task_status(
    request: Request,
    task_id: str,
    user_id: Optional[str] = None,
    conversation_id: Optional[str] = None
):
    """
    获取任务状态
    前端通过轮询此接口获取任务进度和最终结果（SSE 不可用时的回退路径）

    Args:
        task_id: 任务ID
        user_id: 用户ID（可选，提供后更精确查找）
        conversation_id: 会话ID（可选，提供后更精确查找）

    Returns:
        任务状态信息，包括：
        - status: "processing" | "completed" | "error" | "cancelled"
        - progress: 0-100的进度值
        - message: 当前状态消息
        - result: 推荐结果（任务完成时）
        - error: 错误信息（任务失败时）
    """
    if not user_id or not conversation_id:
        raise HTTPException(
            status_code=400,
            detail="user_id and conversation_id are required for scoped task status",
        )
    user_id = await resolve_request_user_id(request, user_id)

    task_status = await metarec_service.get_task_status_async(task_id, user_id, conversation_id)

    if not task_status:
        raise HTTPException(status_code=404, detail="Task not found")

    return _build_task_status_api(task_status, task_id)


@app.get(
    "/api/status/{task_id}/stream",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def stream_task_status(
    request: Request,
    task_id: str,
    user_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
):
    """Stream task progress as Server-Sent Events.

    Replaces client-side 1s polling: the browser opens a single EventSource and
    the server pushes a status frame whenever the task projection changes, ending
    on completion/error. Reads the same projection the polling endpoint serves
    (no graph changes), so /api/status remains a working fallback when SSE can't
    get through (proxies, missing EventSource)."""
    if not user_id or not conversation_id:
        raise HTTPException(
            status_code=400,
            detail="user_id and conversation_id are required for scoped task status",
        )
    resolved_user_id = await resolve_request_user_id(request, user_id)

    interval = float(os.getenv("METAREC_SSE_POLL_INTERVAL", "0.4"))
    not_found_timeout = float(os.getenv("METAREC_SSE_NOT_FOUND_TIMEOUT", "10"))
    max_duration = float(os.getenv("METAREC_SSE_MAX_DURATION", "300"))

    async def fetch_status() -> Optional[Dict[str, Any]]:
        task_status = await metarec_service.get_task_status_async(
            task_id, resolved_user_id, conversation_id
        )
        if not task_status:
            return None
        return _build_task_status_api(task_status, task_id).model_dump(mode="json")

    return StreamingResponse(
        sse_task_status_frames(
            fetch_status,
            task_id,
            is_disconnected=request.is_disconnected,
            interval=interval,
            not_found_timeout=not_found_timeout,
            max_duration=max_duration,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Disable proxy/nginx response buffering so frames flush immediately.
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/tasks/{task_id}/result")
async def get_task_result(
    request: Request,
    task_id: str,
    user_id: Optional[str] = None,
    conversation_id: Optional[str] = None,
):
    """Resolve the durable recommendation persisted for a Task ID.

    Reads from recommendation_results (the canonical, queryable source of truth),
    scoped to the owning user/conversation — used by the conversation side card and
    the /Debug testing arena to fetch a result by Task ID without re-deriving it
    from conversation messages.
    """
    if not user_id or not conversation_id:
        raise HTTPException(
            status_code=400,
            detail="user_id and conversation_id are required for scoped task result lookup",
        )
    user_id = await resolve_request_user_id(request, user_id)

    repository = getattr(metarec_service, "result_repository", None)
    if repository is None:
        raise HTTPException(status_code=503, detail="Result store is not available")

    payload = await repository.load_by_task(user_id, conversation_id, task_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="No stored result for this task")
    if isinstance(payload, dict):
        payload = _client_safe_result_payload(payload)
        # Legacy rows nested a full duplicate of the result under ``result``;
        # sanitize it the same way (new rows store one canonical copy only).
        inner_result = payload.get("result")
        if isinstance(inner_result, dict):
            payload["result"] = _client_safe_result_payload(inner_result)
    return payload


def _client_safe_result_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Client-facing projection of a stored recommendation payload: internal
    diagnostic metadata removed and every item's ``raw`` provider blob stripped
    (same rules the task-status endpoints apply via ``_client_safe_item``)."""
    cleaned = dict(payload)
    if isinstance(cleaned.get("metadata"), dict):
        cleaned["metadata"] = client_safe_metadata(cleaned["metadata"])
    if isinstance(cleaned.get("items"), list):
        cleaned["items"] = [_client_safe_item(item) for item in cleaned["items"]]
    return cleaned


class ItineraryRefineRequestAPI(StrictBaseModel):
    user_id: Optional[str] = None
    conversation_id: str
    slot_index: Optional[int] = None
    selected_item_id: Optional[str] = None
    prompt: Optional[str] = None
    expected_revision: Optional[int] = None
    accept_uncertainties: bool = False


@app.post("/api/itinerary/{task_id}/refine")
async def refine_itinerary_endpoint(task_id: str, body: ItineraryRefineRequestAPI, request: Request):
    """Refine one slot of a completed itinerary: swap in one of the slot's
    alternates (``selected_item_id``) or re-gather the slot from a free-text
    ``prompt`` — exactly one of the two. Neighbors stay fixed; only the
    adjacent legs' ETAs are recomputed. Returns the updated, client-safe
    result payload (same shape as GET /api/tasks/{task_id}/result)."""
    user_id = await resolve_request_user_id(request, body.user_id)
    try:
        payload = await metarec_service.refine_itinerary_slot(
            task_id=task_id,
            user_id=user_id,
            conversation_id=body.conversation_id,
            slot_index=body.slot_index,
            selected_item_id=body.selected_item_id,
            prompt=body.prompt,
            expected_revision=body.expected_revision,
            accept_uncertainties=body.accept_uncertainties,
        )
    except ItineraryConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except HTTPException:
        raise
    except Exception:
        logger.exception("refine_itinerary failed")
        raise HTTPException(status_code=500, detail="Error refining itinerary")
    return _client_safe_result_payload(payload)


@app.post("/api/update-preferences", response_model=UpdatePreferencesResponseAPI)
async def update_preferences_endpoint(preferences_data: UpdatePreferencesRequestAPI, request: Request):
    """
    更新用户偏好设置
    
    Args:
        preferences_data: 包含用户偏好的字典，格式：
        {
            "user_id": "用户ID（可选，默认'default'）",
            "restaurantTypes": ["casual", "fine-dining"],
            "flavorProfiles": ["spicy", "savory"],
            "diningPurpose": "friends",
            "budgetRange": {"min": 20, "max": 60, "currency": "SGD", "per": "person"},
            "location": "Chinatown"
        }
        
    Returns:
        更新后的偏好设置
    """
    try:
        user_id = await resolve_request_user_id(request, preferences_data.user_id)
        
        # 验证和标准化偏好数据
        processed_preferences = {
            "restaurant_types": preferences_data.restaurantTypes,
            "flavor_profiles": preferences_data.flavorProfiles,
            "dining_purpose": preferences_data.diningPurpose,
            "budget_range": preferences_data.budgetRange.model_dump(),
            "location": preferences_data.location
        }
        
        profile = await profile_repository.get_user_profile(user_id)
        domains = assemble_domains(profile)
        domains["restaurant"] = _merge_meaningful_preferences(domains.get("restaurant", {}), processed_preferences)
        restaurant_slice = domains.pop("restaurant", {})
        metadata = profile.setdefault("metadata", {})
        metadata["preferences"] = {}
        metadata["domains"] = domains
        profile["dining_habits"] = restaurant_slice
        await profile_repository.save_user_profile(user_id, profile)
        updated_prefs = processed_preferences
        
        return {
            "message": "Preferences updated successfully",
            "preferences": updated_prefs
        }
    
    except HTTPException:
        raise
    except Exception:
        logger.exception("update_preferences failed")
        raise HTTPException(status_code=500, detail="Error updating preferences")


@app.get("/api/user-preferences/{user_id}", response_model=UserPreferencesResponseAPI)
async def get_user_preferences_endpoint(user_id: str, request: Request):
    """
    获取用户当前的偏好设置
    
    Args:
        user_id: 用户ID
        
    Returns:
        用户偏好设置，包括：
        - user_id: 用户ID
        - preferences: 偏好设置字典
    """
    try:
        await require_path_user(request, user_id)
        profile = await profile_repository.get_user_profile(user_id)
        domains = assemble_domains(profile)
        preferences = _merge_meaningful_preferences(
            metarec_service.get_default_preferences(),
            domains.get("restaurant", {}),
        )
        return {
            "user_id": user_id,
            "preferences": preferences
        }
    except HTTPException:
        raise
    except Exception:
        logger.exception("get_user_preferences failed")
        raise HTTPException(status_code=500, detail="Error getting user preferences")


# ==================== 三层用户画像 API ====================

class UserProfileAPI(StrictBaseModel):
    """Three-layer user profile: generic core (demographics + cross-domain
    constraints), an NL taste persona, and per-domain structured slices."""
    user_id: str
    demographics: Dict[str, Any] = Field(default_factory=dict)
    constraints: Dict[str, Any] = Field(default_factory=dict)
    taste_persona: str = ""
    domains: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


class UserProfileUpdateAPI(StrictBaseModel):
    demographics: Dict[str, Any] = Field(default_factory=dict)
    constraints: Dict[str, Any] = Field(default_factory=dict)
    taste_persona: str = ""
    domains: Dict[str, Dict[str, Any]] = Field(default_factory=dict)


def _profile_to_api(user_id: str, profile: Dict[str, Any]) -> UserProfileAPI:
    normalized = normalize_profile(profile)
    return UserProfileAPI(
        user_id=user_id,
        demographics=normalized.get("demographics", {}),
        constraints=normalized.get("constraints", {}),
        taste_persona=normalized.get("taste_persona", ""),
        domains=normalized.get("domains", {}),
    )


@app.get("/api/user-profile/{user_id}", response_model=UserProfileAPI)
async def get_user_profile_endpoint(user_id: str, request: Request):
    """Return the three-layer profile for editing/fusion."""
    try:
        await require_path_user(request, user_id)
        profile = await profile_repository.get_user_profile(user_id)
        return _profile_to_api(user_id, profile)
    except HTTPException:
        raise
    except Exception:
        logger.exception("get_user_profile failed")
        raise HTTPException(status_code=500, detail="Error getting user profile")


@app.put("/api/user-profile/{user_id}", response_model=UserProfileAPI)
async def update_user_profile_endpoint(user_id: str, payload: UserProfileUpdateAPI, request: Request):
    """Persist the three-layer profile. The restaurant slice maps onto the legacy
    ``dining_habits`` column; other domains and the persona/constraints live in
    ``metadata`` — keeping existing restaurant data backward compatible."""
    try:
        await require_path_user(request, user_id)
        domains = dict(payload.domains or {})
        restaurant_slice = domains.pop("restaurant", {}) or {}
        physical = {
            "user_id": user_id,
            "demographics": payload.demographics or {},
            "dining_habits": restaurant_slice,
            "metadata": {
                "taste_persona": payload.taste_persona or "",
                "constraints": payload.constraints or {},
                "domains": domains,
                "preferences": {},
            },
        }
        await profile_repository.save_user_profile(user_id, physical)
        refreshed = await profile_repository.get_user_profile(user_id)
        return _profile_to_api(user_id, refreshed)
    except HTTPException:
        raise
    except Exception:
        logger.exception("update_user_profile failed")
        raise HTTPException(status_code=500, detail="Error updating user profile")


# ==================== 请求时偏好表单生成 API ====================

class PreferenceFieldAPI(StrictBaseModel):
    key: str
    label: str
    type: str
    options: List[str] = Field(default_factory=list)
    required: bool = False
    required_when: Optional[Dict[str, Any]] = None
    placeholder: str = ""
    value: Optional[Any] = None
    min: Optional[float] = None
    max: Optional[float] = None


class DomainPreferenceFormAPI(StrictBaseModel):
    domain: str
    fields: List[PreferenceFieldAPI] = Field(default_factory=list)
    missing_required: List[str] = Field(default_factory=list)
    complete: bool = True


@app.get("/api/domains/{domain}/preference-form", response_model=DomainPreferenceFormAPI)
async def get_domain_preference_form(domain: str, request: Request):
    """Generate the (server-driven) preference form for a domain at request time.
    The frontend renders it generically; adding a domain's form is a data change."""
    try:
        await resolve_request_user_id(request, None)
        return build_domain_form(domain)
    except HTTPException:
        raise
    except Exception:
        logger.exception("get_domain_preference_form failed")
        raise HTTPException(status_code=500, detail="Error building preference form")


# ==================== 对话历史API ====================

class ConversationSummary(StrictBaseModel):
    """对话摘要（用于列表）"""
    id: str
    title: str
    model: str
    last_message: str
    timestamp: str
    updated_at: str
    message_count: int


class MessageData(StrictBaseModel):
    """消息数据"""
    id: Optional[str] = None
    role: str
    content: str
    timestamp: Optional[str] = None
    branch_id: Optional[str] = None
    parent_message_id: Optional[str] = None
    fork_from_message_id: Optional[str] = None
    revision_of_message_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        json_schema_extra={"additionalProperties": True},
    )


class BranchData(StrictBaseModel):
    """Conversation branch metadata."""
    id: str
    parent_branch_id: Optional[str] = None
    fork_from_message_id: Optional[str] = None
    root_message_id: Optional[str] = None
    head_message_id: Optional[str] = None
    title: Optional[str] = None
    created_at: str
    updated_at: str


class ConversationData(StrictBaseModel):
    """完整对话数据"""
    id: str
    user_id: str
    title: str
    model: str
    last_message: str
    timestamp: str
    updated_at: str
    active_branch_id: Optional[str] = "branch-main"
    branch_selection_state: Dict[str, str] = Field(default_factory=dict)
    branches: Dict[str, BranchData] = Field(default_factory=dict)
    messages: List[MessageData]
    preferences: Dict[str, Any] = Field(
        default_factory=dict,
        json_schema_extra={"additionalProperties": True},
    )
    # Internal conversation memory (e.g. the rolling context summary). Accepted so
    # the loaded conversation validates, but excluded from the API response — it is
    # server-side state, not part of the client contract.
    metadata: Dict[str, Any] = Field(default_factory=dict, exclude=True)


class CreateConversationRequest(StrictBaseModel):
    """创建对话请求"""
    title: Optional[str] = None
    model: str = "Auto"


class UpdateConversationRequest(StrictBaseModel):
    """更新对话请求"""
    title: Optional[str] = None
    model: Optional[str] = None


class AddMessageRequest(StrictBaseModel):
    """添加消息请求"""
    role: str
    content: str
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        json_schema_extra={"additionalProperties": True},
    )


class SetActiveBranchRequest(StrictBaseModel):
    branch_id: str
    source_message_id: Optional[str] = None


@app.get("/api/conversations/{user_id}", response_model=List[ConversationSummary])
async def get_all_conversations(
    user_id: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    """
    获取用户的所有对话列表
    
    Args:
        user_id: 用户ID
        
    Returns:
        对话摘要列表
    """
    try:
        await require_path_user(request, user_id)
        conversations = await conversation_repository.get_all_conversations(user_id, limit=limit, offset=offset)
        return conversations
    except HTTPException:
        raise
    except Exception:
        logger.exception("get_all_conversations failed")
        raise HTTPException(status_code=500, detail="Error getting conversations")


@app.get("/api/conversations/{user_id}/{conversation_id}", response_model=ConversationData)
async def get_conversation(user_id: str, conversation_id: str, request: Request):
    """
    获取单个对话的完整信息（包含所有消息）
    
    Args:
        user_id: 用户ID
        conversation_id: 对话ID
        
    Returns:
        完整的对话数据
    """
    try:
        await require_path_user(request, user_id)
        conversation = await conversation_repository.get_full_conversation(user_id, conversation_id)
        
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        
        return conversation
    except HTTPException:
        raise
    except Exception:
        logger.exception("get_conversation failed")
        raise HTTPException(status_code=500, detail="Error getting conversation")


@app.post("/api/conversations/{user_id}", response_model=ConversationData)
async def create_conversation(user_id: str, request_data: CreateConversationRequest, request: Request):
    """
    创建新对话
    
    Args:
        user_id: 用户ID
        request: 创建对话请求
        
    Returns:
        创建的对话数据
    """
    try:
        await require_path_user(request, user_id)
        conversation = await conversation_repository.create_conversation(
            user_id=user_id,
            title=request_data.title,
            model=request_data.model
        )
        return conversation
    except HTTPException:
        raise
    except Exception:
        logger.exception("create_conversation failed")
        raise HTTPException(status_code=500, detail="Error creating conversation")


@app.put("/api/conversations/{user_id}/{conversation_id}", response_model=ConversationData)
async def update_conversation(
    user_id: str,
    conversation_id: str,
    request_data: UpdateConversationRequest,
    request: Request,
):
    """
    更新对话信息（如标题、模型等）
    
    Args:
        user_id: 用户ID
        conversation_id: 对话ID
        request: 更新请求
        
    Returns:
        更新后的对话数据
    """
    try:
        await require_path_user(request, user_id)
        updates = {}
        
        if request_data.title is not None:
            updates["title"] = request_data.title
        if request_data.model is not None:
            updates["model"] = request_data.model
        
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")
        
        success = await conversation_repository.update_conversation(user_id, conversation_id, updates)
        
        if not success:
            raise HTTPException(status_code=404, detail="Conversation not found")
        
        conversation = await conversation_repository.get_full_conversation(user_id, conversation_id)
        return conversation
    except HTTPException:
        raise
    except Exception:
        logger.exception("update_conversation failed")
        raise HTTPException(status_code=500, detail="Error updating conversation")


@app.post("/api/conversations/{user_id}/{conversation_id}/messages", response_model=GenericSuccessResponseAPI)
async def add_message(
    user_id: str,
    conversation_id: str,
    request_data: AddMessageRequest,
    request: Request,
):
    """
    向对话添加消息
    
    Args:
        user_id: 用户ID
        conversation_id: 对话ID
        request: 添加消息请求
        
    Returns:
        成功状态
    """
    try:
        await require_path_user(request, user_id)
        if request_data.role not in ["user", "assistant"]:
            raise HTTPException(status_code=400, detail="Role must be 'user' or 'assistant'")
        
        success = await conversation_repository.add_message(
            user_id=user_id,
            conversation_id=conversation_id,
            role=request_data.role,
            content=request_data.content,
            metadata=request_data.metadata
        )
        
        if not success:
            raise HTTPException(status_code=404, detail="Conversation not found")
        
        return {"success": True, "message": "Message added successfully"}
    except HTTPException:
        raise
    except Exception:
        logger.exception("add_message failed")
        raise HTTPException(status_code=500, detail="Error adding message")


@app.put("/api/conversations/{user_id}/{conversation_id}/active-branch", response_model=ConversationData)
async def set_active_branch(
    user_id: str,
    conversation_id: str,
    request_data: SetActiveBranchRequest,
    request: Request,
):
    """
    Switch the active visible branch for a conversation.
    """
    try:
        await require_path_user(request, user_id)
        success = await conversation_repository.set_active_branch(
            user_id,
            conversation_id,
            request_data.branch_id,
            request_data.source_message_id,
        )
        if not success:
            raise HTTPException(status_code=404, detail="Conversation or branch not found")
        conversation = await conversation_repository.get_full_conversation(user_id, conversation_id)
        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return conversation
    except HTTPException:
        raise
    except Exception:
        logger.exception("set_active_branch failed")
        raise HTTPException(status_code=500, detail="Error setting active branch")


@app.delete("/api/conversations/{user_id}/{conversation_id}", response_model=GenericSuccessResponseAPI)
async def delete_conversation(user_id: str, conversation_id: str, request: Request):
    """
    删除对话
    
    Args:
        user_id: 用户ID
        conversation_id: 对话ID
        
    Returns:
        成功状态
    """
    try:
        await require_path_user(request, user_id)
        success = await conversation_repository.delete_conversation(user_id, conversation_id)
        
        if not success:
            raise HTTPException(status_code=404, detail="Conversation not found")

        try:
            cancel_summary = await metarec_service.cancel_conversation_tasks_async(user_id, conversation_id)
        except Exception:
            logger.exception("delete_conversation task cancellation failed")
            cancel_summary = {"cancelled": 0}
        
        cancelled = int(cancel_summary.get("cancelled") or 0)
        if cancelled:
            message = f"Conversation deleted successfully; cancelled {cancelled} running recommendation task(s)."
        else:
            message = "Conversation deleted successfully"
        return {"success": True, "message": message}
    except HTTPException:
        raise
    except Exception:
        logger.exception("delete_conversation failed")
        raise HTTPException(status_code=500, detail="Error deleting conversation")


@app.get("/api/conversations/{user_id}/{conversation_id}/preferences", response_model=PreferencesResponseAPI)
async def get_conversation_preferences(user_id: str, conversation_id: str, request: Request):
    """
    获取对话的偏好设置（直接读取持久化仓库）

    Args:
        user_id: 用户ID
        conversation_id: 对话ID

    Returns:
        偏好设置字典
    """
    try:
        await require_path_user(request, user_id)
        preferences = await conversation_repository.get_conversation_preferences(user_id, conversation_id)
        
        if preferences is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        
        return {"preferences": preferences}
    except HTTPException:
        raise
    except Exception:
        logger.exception("get_conversation_preferences failed")
        raise HTTPException(status_code=500, detail="Error getting conversation preferences")


@app.put("/api/conversations/{user_id}/{conversation_id}/preferences", response_model=PreferencesResponseAPI)
async def update_conversation_preferences(
    user_id: str,
    conversation_id: str,
    preferences_data: Dict[str, object],
    request: Request,
):
    """
    更新对话的偏好设置（写入持久化仓库后回读返回）

    Args:
        user_id: 用户ID
        conversation_id: 对话ID
        preferences_data: 偏好设置字典

    Returns:
        更新后的偏好设置
    """
    try:
        await require_path_user(request, user_id)
        success = await conversation_repository.update_conversation_preferences(user_id, conversation_id, preferences_data)
        
        if not success:
            raise HTTPException(status_code=404, detail="Conversation not found")
        
        updated_preferences = await conversation_repository.get_conversation_preferences(user_id, conversation_id)
        if updated_preferences is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        return {"preferences": updated_preferences}
    except HTTPException:
        raise
    except Exception:
        logger.exception("update_conversation_preferences failed")
        raise HTTPException(status_code=500, detail="Error updating conversation preferences")


# ==================== 静态文件服务（在所有 API 路由之后）====================

# 挂载静态资源目录
if os.path.exists(FRONTEND_DIST):
    assets_dir = os.path.join(FRONTEND_DIST, "assets")
    if os.path.exists(assets_dir):
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")


@app.get("/", include_in_schema=False)
async def serve_root():
    """服务根路径的前端应用"""
    index_path = os.path.join(FRONTEND_DIST, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "MetaRec API", "docs": "/docs"}


@app.get("/{full_path:path}", include_in_schema=False)
async def serve_spa(full_path: str):
    """SPA fallback - 所有未匹配的路由返回 index.html"""
    # 检查是否是静态文件
    file_path = FRONTEND_DIST.joinpath(full_path).resolve()
    
    # 1. Prevent escaping FRONTEND_DIST directory using path traversal i.e. '../' which would otherwise allow user to access arbitrary files on the filesystem
    # TODO: consider logging this to track malicious users?
    if not file_path.is_relative_to(FRONTEND_DIST):
        raise HTTPException(status_code=403, detail="Forbidden")
    
    # 2. check that the requested file exists
    if file_path.is_file(): # checks for existence of file and that the file is a regular file
        return FileResponse(file_path)
    
    # 3. fallback to index page
    index_path = os.path.join(FRONTEND_DIST, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    
    # 如果没有前端文件，返回 404
    raise HTTPException(status_code=404, detail="Not found")


# ==================== 启动配置 ====================

if __name__ == "__main__":
    import uvicorn
    # 使用环境变量PORT，默认8000（本地开发）
    # Hugging Face Spaces 可以设置 PORT=7860
    port = int(os.getenv("PORT", 8000))
    print(f"🚀 Starting MetaRec API server on http://0.0.0.0:{port}")
    print(f"📖 API docs available at http://localhost:{port}/docs")
    print(f"🌐 Frontend should be available at http://localhost:{port}/")
    print(f"📝 Logging level: INFO - All print() messages will be displayed")
    
    # 配置 uvicorn 日志，确保实时输出
    log_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
        },
        "root": {
            "level": "INFO",
            "handlers": ["default"],
        },
        "loggers": {
            "uvicorn": {"level": "INFO"},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"level": "INFO"},
        },
    }
    
    uvicorn.run(
        app, 
        host="0.0.0.0", 
        port=port,
        log_config=log_config,
        log_level="info"
    )
