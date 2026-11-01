"""
MetaRec 核心服务类
提供餐厅推荐的核心业务逻辑，可以被其他模块直接调用
"""
from typing import Callable, List, Dict, Any, Optional, Sequence, Tuple, Union
import asyncio
import copy
import inspect
import logging
import re
import json
import os
import time
from contextlib import asynccontextmanager
from difflib import SequenceMatcher
from datetime import datetime
from weakref import WeakValueDictionary
from pydantic import BaseModel, Field
from openai import AsyncOpenAI, AsyncAzureOpenAI, OpenAI, AzureOpenAI

from business_models import new_uuid
import llm_usage

# 导入 LLM 服务
from llm_service import analyze_user_message, generate_confirmation_message, generate_confirmation_payload, LLMResponse, detect_language

# 偏好合并（profile/会话 基线 与 新提取偏好 的 meaningful 合并）
from langgraph_metarec.nodes.preferences import merge_preferences
# 显式菜系/菜品意图（混合推荐：命名了具体食物时按其收窄）
from langgraph_metarec.nodes.food_intent import (
    empty_food_intent,
    extract_food_intent_keywords,
    food_intent_terms,
    is_food_intent_strict,
    is_meaningful_food_intent,
    relax_food_intent,
    restaurant_matches_food_intent,
)


TERMINAL_TASK_STATUSES = {"completed", "error", "cancelled"}


class ItineraryConflictError(RuntimeError):
    """The client attempted to refine an itinerary revision that is no longer current."""


# ==================== 数据模型 ====================

class BudgetRange(BaseModel):
    min: Optional[int] = None
    max: Optional[int] = None
    currency: str = "SGD"
    per: str = "person"


def _loads_llm_json(text: str) -> Any:
    """Parse the first complete JSON object from an LLM response."""
    from langgraph_metarec.json_parsing import loads_first_json_object

    return loads_first_json_object(text)


class Restaurant(BaseModel):
    id: str
    name: str
    address: Optional[str] = None
    area: Optional[str] = None
    cuisine: Optional[str] = None
    type: Optional[str] = None  # casual, fine-dining, etc.
    location: Optional[str] = None
    rating: Optional[float] = None
    reviews_count: Optional[int] = None
    price: Optional[str] = None  # price range in SGD
    price_per_person_sgd: Optional[str] = None  # e.g., "20-30", "28.80"
    distance_or_walk_time: Optional[str] = None
    open_hours_note: Optional[str] = None
    highlights: Optional[List[str]] = None
    flavor_match: Optional[List[str]] = None
    purpose_match: Optional[List[str]] = None
    why: Optional[str] = None  # reason for recommendation
    reason: Optional[str] = None  # alias for why
    reference: Optional[str] = None
    sources: Optional[Dict[str, str]] = None  # e.g., {"xiaohongshu": "id", "google_maps": "id"}
    phone: Optional[str] = None
    gps_coordinates: Optional[Dict[str, float]] = None  # {"latitude": 1.29, "longitude": 103.85}


class RecommendationItem(BaseModel):
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
    raw: Dict[str, Any] = Field(default_factory=dict)


class ThinkingStep(BaseModel):
    step: str
    description: str
    status: str  # "thinking", "completed", "error"
    details: Optional[str] = None


class RecommendationResult(BaseModel):
    """推荐结果"""
    restaurants: List[Restaurant]
    items: List[RecommendationItem] = Field(default_factory=list)
    thinking_steps: Optional[List[ThinkingStep]] = None
    confidence_score: Optional[float] = None  # 推荐置信度 0-1
    metadata: Optional[Dict[str, Any]] = None  # 额外的元数据


class ConfirmationRequest(BaseModel):
    """确认请求"""
    message: str
    preferences: Dict[str, Any]
    needs_confirmation: bool = True
    # Optional server-generated preference form for the resolved domain (request-time).
    preference_form: Optional[Dict[str, Any]] = None
    # Optional single-click choices that confirm and patch one missing preference.
    quick_actions: Optional[List[Dict[str, Any]]] = None


# ==================== 核心服务类 ====================

class MetaRecService:
    """
    MetaRec 核心推荐服务
    
    这个类封装了所有的推荐逻辑，可以被其他模块直接调用：
    - 用户意图分析
    - 偏好提取
    - 确认流程
    - 思考过程模拟
    - 餐厅推荐
    """
    
    def __init__(
            self, 
            async_client: Union[AsyncOpenAI, AsyncAzureOpenAI],
            sync_client: Union[OpenAI, AzureOpenAI],
            summary_model: str,
            planning_model: str,
            llm_model: str,
            restaurant_data: Optional[List[Dict]] = None,
        ):
        """
        初始化服务
        
        Args:
            async_client: async openai client
            sync_client: sync openai client
            summary_model: model name for summary task
            planning_model: model name for planning task
            llm_model: model name for other task

            restaurant_data: 餐厅数据列表，如果为None则使用默认样例数据
        """
        # 餐厅数据库
        self.restaurant_data = restaurant_data or self._get_default_restaurants()
        
        # Session 上下文存储（按 user_id:session_id 分隔）
        # 每个 session 包含：preferences（用户偏好）、context（确认流程上下文）、tasks（异步任务）
        # 格式: {f"{user_id}:{session_id}": {"preferences": {...}, "context": {...}, "tasks": {...}}}
        self.session_contexts: Dict[str, Dict[str, Any]] = {}
        self._running_tasks: Dict[str, asyncio.Task[Any]] = {}
        self._running_task_scopes: Dict[str, Tuple[str, Optional[str]]] = {}
        self._itinerary_refine_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        # Fire-and-forget maintenance work (e.g. the rolling-summary update) keeps a
        # strong reference here so the event loop cannot GC a pending task mid-flight.
        self._background_tasks: set = set()

        try:
            from business_repositories import profile_repository, task_repository, result_repository

            self.profile_repository = profile_repository
            self.task_repository = task_repository
            self.result_repository = result_repository
        except Exception:
            self.profile_repository = None
            self.task_repository = None
            self.result_repository = None
        from langgraph_metarec.checkpointing import RuntimeCheckpointer

        self.runtime_checkpointer = RuntimeCheckpointer()
        
        self.async_client = async_client
        self.sync_client = sync_client
        
        self.summary_model = summary_model
        self.planning_model = planning_model
        self.llm_model = llm_model
        try:
            self.llm_max_format_retries = max(0, min(int(os.getenv("LLM_MAX_FORMAT_RETRIES", "2")), 50))
        except ValueError:
            self.llm_max_format_retries = 2
    
    def _get_session_key(self, user_id: str, session_id: Optional[str] = None) -> str:
        """
        生成 session 键
        
        Args:
            user_id: 用户ID
            session_id: 会话ID（可选，如果为None则使用"default"）
            
        Returns:
            session键，格式为 "{user_id}:{session_id}"
        """
        if session_id is None:
            session_id = "default"
        return f"{user_id}:{session_id}"
    
    def _get_session_context(self, user_id: str, session_id: Optional[str] = None) -> Dict[str, Any]:
        """
        获取或创建 session 上下文
        
        Args:
            user_id: 用户ID
            session_id: 会话ID（可选）
            
        Returns:
            session上下文字典
        """
        key = self._get_session_key(user_id, session_id)
        if key not in self.session_contexts:
            self.session_contexts[key] = {
                "preferences": self.get_default_preferences(),
                "context": {},
                "tasks": {}
            }
        return self.session_contexts[key]

    @staticmethod
    def _extract_profile_preferences(user_profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Restaurant runtime preferences stored on the unified profile.

        The old preferences panel wrote flat restaurant preferences to
        ``metadata.preferences``. The unified profile stores them in the
        restaurant domain slice (physically ``dining_habits`` for backwards
        compatibility). Read both, with the domain slice taking precedence.
        """
        if not isinstance(user_profile, dict):
            return {}
        try:
            from profile_model import assemble_domains

            restaurant = assemble_domains(user_profile).get("restaurant", {})
        except Exception:
            restaurant = {}
        preferences: Dict[str, Any] = {}
        for key in ("restaurant_types", "flavor_profiles", "dining_purpose", "budget_range", "location"):
            value = restaurant.get(key)
            if value not in (None, "", [], {}):
                preferences[key] = value
        if "budget_range" not in preferences:
            budget = MetaRecService._parse_budget_text(restaurant.get("typical_budget"))
            if budget:
                preferences["budget_range"] = budget
        return preferences

    @staticmethod
    def _parse_budget_text(value: Any) -> Optional[Dict[str, Any]]:
        """Parse a free-form profile budget (e.g. ``"5-10"``, ``"5-10 SGD"``,
        ``"$8"``, ``8``, or a ``{min,max}`` dict) into a budget_range dict.
        Returns None when nothing usable is found."""
        if isinstance(value, dict):
            minimum, maximum = value.get("min"), value.get("max")
            if minimum is None and maximum is None:
                return None
            return {"min": minimum, "max": maximum, "currency": value.get("currency", "SGD"), "per": value.get("per", "person")}
        if isinstance(value, (int, float)):
            amount = int(value)
            return {"min": amount, "max": amount, "currency": "SGD", "per": "person"}
        if not isinstance(value, str) or not value.strip():
            return None
        numbers = [int(n) for n in re.findall(r"\d+", value)]
        if not numbers:
            return None
        if len(numbers) >= 2:
            low, high = sorted(numbers[:2])
            return {"min": low, "max": high, "currency": "SGD", "per": "person"}
        return {"min": numbers[0], "max": numbers[0], "currency": "SGD", "per": "person"}

    @staticmethod
    def _profile_field_preferences(user_profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Derive recommendation preferences from the editable profile fields
        (``dining_habits.typical_budget`` -> budget_range,
        ``demographics.location`` -> location) so a user's profile actually
        seeds the recommendation flow instead of being ignored."""
        if not isinstance(user_profile, dict):
            return {}
        derived: Dict[str, Any] = {}
        dining_habits = user_profile.get("dining_habits")
        if isinstance(dining_habits, dict):
            budget = MetaRecService._parse_budget_text(dining_habits.get("typical_budget"))
            if budget:
                derived["budget_range"] = budget
        demographics = user_profile.get("demographics")
        if isinstance(demographics, dict):
            location = demographics.get("location")
            if isinstance(location, str) and location.strip():
                derived["location"] = location.strip()
        return derived

    @staticmethod
    def _select_runtime_preferences(
        default_preferences: Dict[str, Any],
        user_profile: Optional[Dict[str, Any]],
        conversation_preferences: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Build the complete runtime preference baseline by layering, lowest to
        highest priority: defaults -> profile field-derived -> explicit profile
        preferences -> conversation-specific preferences. Each layer only
        overrides fields it meaningfully specifies (see ``merge_preferences``).

        ``food_intent`` is intentionally **request-scoped**: it is stripped from
        every persisted layer so a previous query's "Pho" never sticks to the
        next request. It is re-extracted per query and only survives the
        confirm/refine loop of a single recommendation (via the pending state)."""
        baseline = dict(default_preferences)
        baseline = merge_preferences(baseline, MetaRecService._strip_food_intent(MetaRecService._profile_field_preferences(user_profile)))
        baseline = merge_preferences(baseline, MetaRecService._strip_food_intent(MetaRecService._extract_profile_preferences(user_profile)))
        if isinstance(conversation_preferences, dict) and conversation_preferences:
            baseline = merge_preferences(baseline, MetaRecService._strip_food_intent(conversation_preferences))
        baseline["food_intent"] = empty_food_intent()
        return baseline

    @staticmethod
    def _strip_food_intent(preferences: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Drop the request-scoped ``food_intent`` key from a (persisted) prefs dict."""
        if not isinstance(preferences, dict):
            return {}
        return {k: v for k, v in preferences.items() if k != "food_intent"}
    
    @staticmethod
    def _normalize_profile_updates(updates: Dict[str, Any]) -> Dict[str, Any]:
        """
        规范化 profile 更新，确保：
        1. 只更新 profile_example.json 中定义的字段
        2. 所有值都转换为字符串（数组用逗号分隔，null 转为空字符串）
        3. 未定义的字段内容合并到 description 中
        
        Args:
            updates: 原始更新字典
            
        Returns:
            规范化后的更新字典
        """
        normalized = {}
        
        # 定义合法的字段（与 profile_example.json 一致）
        valid_demographics_fields = {
            "age_range", "gender", "occupation", "location", "nationality"
        }
        
        valid_dining_habits_fields = {
            "typical_budget", "dietary_restrictions",
            "spice_tolerance", "description"
        }
        
        def _to_string(value: Any) -> str:
            """将值转换为字符串"""
            if value is None:
                return ""
            if isinstance(value, list):
                # 数组转换为逗号分隔的字符串
                return ", ".join(str(item) for item in value if item)
            if isinstance(value, dict):
                # 字典转换为字符串描述
                return str(value)
            return str(value) if value else ""
        
        for key, value in updates.items():
            if key == "demographics" and isinstance(value, dict):
                # 处理 demographics
                normalized_demographics = {}
                description_parts = []
                
                for field, field_value in value.items():
                    if field in valid_demographics_fields:
                        # 转换为字符串
                        normalized_demographics[field] = _to_string(field_value)
                    else:
                        # 未定义的字段，添加到 description
                        description_parts.append(f"{field}: {field_value}")
                
                if normalized_demographics:
                    normalized["demographics"] = normalized_demographics
                
                # 如果有未定义字段，需要添加到 dining_habits.description
                # 注意：description 应该是一个完整的描述，不是增量追加
                if description_parts:
                    if "dining_habits" not in normalized:
                        normalized["dining_habits"] = {}
                    # 直接设置 description，不追加
                    normalized["dining_habits"]["description"] = "demographics: " + "; ".join(description_parts)
                    
            elif key == "dining_habits" and isinstance(value, dict):
                # 处理 dining_habits
                normalized_dining_habits = {}
                description_parts = []
                has_explicit_description = False
                
                for field, field_value in value.items():
                    if field == "description":
                        # LLM 明确提供了 description，使用它（完整描述，覆盖旧内容）
                        has_explicit_description = True
                        normalized_dining_habits["description"] = _to_string(field_value)
                    elif field in valid_dining_habits_fields:
                        # 合法字段，转换为字符串
                        normalized_dining_habits[field] = _to_string(field_value)
                    else:
                        # 未定义的字段，添加到 description_parts（但只有在没有明确 description 时才使用）
                        description_parts.append(f"{field}: {field_value}")
                
                # 如果有未定义字段且没有明确的 description，才创建 description
                # 注意：如果 LLM 明确提供了 description，我们使用它，不追加未定义字段
                if description_parts and not has_explicit_description:
                    # 直接设置 description，不追加
                    normalized_dining_habits["description"] = "; ".join(description_parts)
                
                if normalized_dining_habits:
                    normalized["dining_habits"] = normalized_dining_habits
            elif key == "inferred_info":
                # inferred_info 不再使用，将其内容添加到 description
                # 注意：description 应该是一个完整的描述，不是增量追加
                if "dining_habits" not in normalized:
                    normalized["dining_habits"] = {}
                normalized["dining_habits"]["description"] = f"inferred_info: {value}"
            else:
                # 其他未定义的顶级字段，忽略或添加到 description
                pass
        
        return normalized

    @staticmethod
    def _merge_profile_updates(current_profile: Dict[str, Any], profile_updates: Dict[str, Any]) -> Dict[str, Any]:
        merged = {
            **current_profile,
            "demographics": dict(current_profile.get("demographics") or {}),
            "dining_habits": dict(current_profile.get("dining_habits") or {}),
            "metadata": dict(current_profile.get("metadata") or {}),
        }
        for section in ("demographics", "dining_habits"):
            updates = profile_updates.get(section)
            if not isinstance(updates, dict):
                continue
            target = merged.setdefault(section, {})
            for key, value in updates.items():
                if value is None:
                    continue
                if isinstance(value, str) and value == "" and target.get(key):
                    continue
                target[key] = value
        return merged
    
    @staticmethod
    def _clean_sources_dict(sources: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
        """
        清理 sources 字典，移除所有值为 None 或非字符串的键
        
        Args:
            sources: 原始 sources 字典
            
        Returns:
            清理后的 sources 字典，如果为空则返回 None
        """
        if not sources:
            return None
        cleaned = {k: v for k, v in sources.items() if v is not None and isinstance(v, str)}
        return cleaned if cleaned else None
    
    @staticmethod
    def _extract_restaurants_from_execution_data(data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        从真实执行数据中提取餐厅信息
        
        Args:
            data: 包含 executions 和 summary 的数据字典
            
        Returns:
            餐厅列表
        """
        restaurants = []
        
        # 从 summary.recommendations 中提取推荐餐厅
        # 处理不同的 summary 格式
        summary = data.get("summary")
        recommendations = None
        
        import logging
        logger = logging.getLogger(__name__)
        logger.info("_extract_restaurants_from_execution_data: summary type=%s", type(summary))
        
        if summary:
            # 如果 summary 是字典且直接包含 recommendations
            if isinstance(summary, dict) and "recommendations" in summary:
                recommendations = summary["recommendations"]
                logger.info("Found recommendations directly in summary dict: %d items", len(recommendations) if recommendations else 0)
            # 如果 summary 是字符串，尝试解析
            elif isinstance(summary, str):
                try:
                    parsed = _loads_llm_json(summary)
                    logger.info("Parsed summary string, type: %s, keys: %s", type(parsed), list(parsed.keys()) if isinstance(parsed, dict) else "N/A")
                    if isinstance(parsed, dict) and "recommendations" in parsed:
                        recommendations = parsed["recommendations"]
                        logger.info("Found recommendations in parsed string: %d items", len(recommendations) if recommendations else 0)
                except Exception as e:
                    logger.exception("Failed to parse summary string: %s", str(e))
            # 如果 summary 有 raw 字段，尝试解析
            elif isinstance(summary, dict) and "raw" in summary:
                raw_content = summary["raw"]
                logger.info("Summary has raw field, type: %s", type(raw_content))
                if isinstance(raw_content, str):
                    try:
                        parsed = _loads_llm_json(raw_content)
                        logger.info("Parsed raw string, type: %s, keys: %s", type(parsed), list(parsed.keys()) if isinstance(parsed, dict) else "N/A")
                        if isinstance(parsed, dict) and "recommendations" in parsed:
                            recommendations = parsed["recommendations"]
                            logger.info("Found recommendations in parsed raw: %d items", len(recommendations) if recommendations else 0)
                    except Exception as e:
                        logger.exception("Failed to parse raw string: %s", str(e))
                elif isinstance(raw_content, dict) and "recommendations" in raw_content:
                    recommendations = raw_content["recommendations"]
                    logger.info("Found recommendations in raw dict: %d items", len(recommendations) if recommendations else 0)
            else:
                logger.warning("Summary format not recognized, type: %s", type(summary))
        else:
            logger.warning("Summary is None or empty")
        
        if recommendations:
            logger.info("Processing %d recommendations", len(recommendations))
            for idx, rec in enumerate(recommendations):
                restaurant = {
                    "id": f"rec_{idx}_{rec.get('name', '').replace(' ', '_')}",
                    "name": rec.get("name", ""),
                    "address": rec.get("address"),
                    "area": rec.get("area"),
                    "cuisine": rec.get("cuisine"),
                    "type": rec.get("type"),
                    "location": rec.get("area"),  # 使用 area 作为 location
                    "rating": rec.get("rating"),
                    "reviews_count": rec.get("reviews_count"),
                    "price": None,  # 从 price_per_person_sgd 推断
                    "price_per_person_sgd": rec.get("price_per_person_sgd"),
                    "distance_or_walk_time": rec.get("distance_or_walk_time"),
                    "open_hours_note": rec.get("open_hours_note"),
                    "flavor_match": rec.get("flavor_match", []),
                    "purpose_match": rec.get("purpose_match", []),
                    "why": rec.get("why"),
                    "reason": rec.get("why"),  # alias
                    "sources": MetaRecService._clean_sources_dict(rec.get("sources")),
                    "phone": None,
                    "gps_coordinates": None
                }
                
                restaurants.append(restaurant)
        
        # 从 executions 中的 gmap.search 结果中提取额外信息，并做高鲁棒性融合
        if "executions" in data:
            def normalize_text(text: Optional[str]) -> str:
                if not text or not isinstance(text, str):
                    return ""
                text = text.lower().strip()
                text = re.sub(r"[^\w\s]", " ", text)
                text = re.sub(r"\s+", " ", text).strip()
                return text

            def normalize_name(name: Optional[str]) -> str:
                normalized = normalize_text(name)
                if not normalized:
                    return ""
                # 去除常见泛化后缀，避免“店名包含”误匹配
                stopwords = {"restaurant", "restoran", "eatery", "cafe", "sg", "singapore"}
                tokens = [t for t in normalized.split() if t not in stopwords]
                normalized = " ".join(tokens)
                normalized = normalized.replace("餐厅", "").replace("饭店", "").replace("餐馆", "").strip()
                return re.sub(r"\s+", " ", normalized)

            def address_tokens(address: Optional[str]) -> set:
                normalized = normalize_text(address)
                if not normalized:
                    return set()
                blacklist = {"street", "road", "avenue", "lane", "singapore", "sg", "st", "rd", "ave", "ln"}
                return {t for t in normalized.split() if len(t) > 1 and t not in blacklist}

            def name_similarity(a: Optional[str], b: Optional[str]) -> float:
                na = normalize_name(a)
                nb = normalize_name(b)
                if not na or not nb:
                    return 0.0
                if na == nb:
                    return 1.0
                ratio = SequenceMatcher(None, na, nb).ratio()
                ta = set(na.split())
                tb = set(nb.split())
                jaccard = (len(ta & tb) / len(ta | tb)) if (ta and tb) else 0.0
                return max(ratio, 0.7 * ratio + 0.3 * jaccard)

            def choose_best_gmap_match(restaurant: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
                if not candidates:
                    return None
                r_name = restaurant.get("name")
                r_addr = restaurant.get("address")
                r_addr_tokens = address_tokens(r_addr)
                scored: List[Tuple[float, float, float, Dict[str, Any]]] = []

                for c in candidates:
                    c_name = c.get("title")
                    c_addr = c.get("address")
                    n_sim = name_similarity(r_name, c_name)
                    c_addr_tokens = address_tokens(c_addr)
                    a_sim = 0.0
                    if r_addr_tokens and c_addr_tokens:
                        denom = min(len(r_addr_tokens), len(c_addr_tokens))
                        if denom > 0:
                            a_sim = len(r_addr_tokens & c_addr_tokens) / denom
                    score = 0.8 * n_sim + 0.2 * a_sim
                    scored.append((score, n_sim, a_sim, c))

                scored.sort(key=lambda x: x[0], reverse=True)
                best_score, best_name_sim, best_addr_sim, best = scored[0]

                # 门槛1：名称要足够接近；仅靠包含关系不再允许直接合并
                if best_name_sim < 0.84:
                    return None

                # 门槛2：若名称仅中等相似，则地址也要有重叠
                if best_name_sim < 0.90 and best_addr_sim < 0.40:
                    return None

                # 门槛3：避免多候选接近时误合并
                if len(scored) > 1:
                    second_score = scored[1][0]
                    if (best_score - second_score) < 0.03 and best_name_sim < 0.95:
                        return None

                return best

            gmap_restaurants: List[Dict[str, Any]] = []
            for execution in data["executions"]:
                if execution.get("tool") == "gmap.search" and execution.get("success") and execution.get("output"):
                    for gmap_item in execution["output"]:
                        name = gmap_item.get("title", "")
                        if name:
                            gmap_restaurants.append(gmap_item)

            # 合并 gmap 数据到推荐餐厅（通过稳健匹配）
            for restaurant in restaurants:
                matched = choose_best_gmap_match(restaurant, gmap_restaurants)
                if not matched:
                    continue

                if not restaurant.get("rating") and matched.get("rating"):
                    restaurant["rating"] = matched["rating"]
                if not restaurant.get("reviews_count") and matched.get("reviews"):
                    restaurant["reviews_count"] = matched["reviews"]
                if not restaurant.get("price") and matched.get("price"):
                    restaurant["price"] = matched["price"]
                if not restaurant.get("phone") and matched.get("phone"):
                    restaurant["phone"] = matched["phone"]
                if not restaurant.get("address") and matched.get("address"):
                    restaurant["address"] = matched["address"]
                if not restaurant.get("gps_coordinates") and matched.get("gps_coordinates"):
                    restaurant["gps_coordinates"] = matched["gps_coordinates"]
                if not restaurant.get("open_hours_note") and matched.get("open_state"):
                    restaurant["open_hours_note"] = matched["open_state"]
        
        return restaurants
    
    @staticmethod
    def _get_default_restaurants() -> List[Dict]:
        """获取默认餐厅数据，优先从 demo_restaurant.json 加载"""
        # 尝试从 demo_restaurant.json 加载真实数据
        demo_file = os.path.join(os.path.dirname(__file__), "demo_restaurant.json")
        if os.path.exists(demo_file):
            try:
                with open(demo_file, 'r', encoding='utf-8') as f:
                    demo_data = json.load(f)
                    restaurants = MetaRecService._extract_restaurants_from_execution_data(demo_data)
                    if restaurants:
                        return restaurants
            except Exception as e:
                print(f"Warning: Failed to load demo_restaurant.json: {e}")
        
        # 如果加载失败，返回默认数据
        return [
            {
                "id": "default_1",
                "name": "四川饭店满庭芳",
                "address": "72 Pagoda St, Singapore 059231",
                "area": "Chinatown",
                "cuisine": "Sichuan",
                "type": "casual",
                "price_per_person_sgd": "20-30",
                "rating": None,
                "reviews_count": None,
                "distance_or_walk_time": "3 min walk from Chinatown MRT",
                "open_hours_note": "11 AM–10 PM daily",
                "flavor_match": ["Spicy"],
                "purpose_match": ["Friends", "Group-friendly"],
                "why": "人均约20新币，招牌辣子鸡与水煮肉片均为重辣口味，地理位置便利，深受川菜控好评。",
                "sources": {"xiaohongshu": "623d9ddf000000000102f1ce"}
            }
        ]
    
    # ==================== 偏好管理 ====================
    
    def get_default_preferences(self) -> Dict[str, Any]:
        """获取默认偏好设置"""
        return {
            "restaurant_types": ["any"],
            "flavor_profiles": ["any"],
            "dining_purpose": "any",
            "budget_range": {
                "min": 20,
                "max": 60,
                "currency": "SGD",
                "per": "person"
            },
            "location": "any",
            # 显式菜系/菜品意图，默认空 = 未指定（走原口味驱动逻辑）
            "food_intent": empty_food_intent(),
        }
    
    def get_user_preferences(self, user_id: str = "default", session_id: Optional[str] = None) -> Dict[str, Any]:
        """
        获取用户的偏好设置
        
        Args:
            user_id: 用户ID
            session_id: 会话ID（可选）
            
        Returns:
            用户偏好字典
        """
        session_ctx = self._get_session_context(user_id, session_id)
        return session_ctx["preferences"].copy()
    
    def update_user_preferences(self, user_id: str, preferences: Dict[str, Any], session_id: Optional[str] = None) -> Dict[str, Any]:
        """
        更新用户的偏好设置
        
        Args:
            user_id: 用户ID
            preferences: 要更新的偏好
            session_id: 会话ID（可选）
            
        Returns:
            更新后的完整偏好
        """
        session_ctx = self._get_session_context(user_id, session_id)
        
        # 合并更新偏好，只更新提供的字段
        if "restaurant_types" in preferences:
            session_ctx["preferences"]["restaurant_types"] = preferences["restaurant_types"]
        if "flavor_profiles" in preferences:
            session_ctx["preferences"]["flavor_profiles"] = preferences["flavor_profiles"]
        if "dining_purpose" in preferences:
            session_ctx["preferences"]["dining_purpose"] = preferences["dining_purpose"]
        if "budget_range" in preferences:
            session_ctx["preferences"]["budget_range"] = preferences["budget_range"]
        if "location" in preferences:
            session_ctx["preferences"]["location"] = preferences["location"]
        
        return session_ctx["preferences"].copy()
    
    # ==================== 意图分析 ====================
    
    def analyze_user_intent(self, query: str) -> Dict[str, Any]:
        """
        分析用户意图，判断是确认、拒绝还是新请求
        
        Args:
            query: 用户输入的查询
            
        Returns:
            意图分析结果，包含type和相关信息
        """
        query_lower = query.lower().strip()
        
        # 检查是否是确认响应
        yes_patterns = [
            r'\b(yes|yeah|yep|yup|correct|right|that\'s right|that\'s correct|sounds good|perfect|ok|okay|sure|exactly|precisely)\b',
            r'\b(是的|对|正确|没错|好的|可以|行|没问题|完全正确|就是这样)\b'
        ]
        
        no_patterns = [
            r'\b(no|nope|not right|incorrect|wrong|not correct|that\'s not right|that\'s wrong|not what I want|not quite|almost|close but|not exactly)\b',
            r'\b(不|不对|错误|不是|不是这样|不是这个|不对的|不是我要的|差不多|接近但不是|不完全对)\b'
        ]
        
        # 检查是否包含确认关键词
        is_yes = any(re.search(pattern, query_lower) for pattern in yes_patterns)
        is_no = any(re.search(pattern, query_lower) for pattern in no_patterns)
        
        # 检查是否包含修改/更新关键词
        modify_patterns = [
            r'\b(change|modify|update|different|instead|rather|actually|but|however|although|though)\b',
            r'\b(改变|修改|更新|不同|而是|实际上|但是|不过|虽然|但是)\b'
        ]
        
        is_modify = any(re.search(pattern, query_lower) for pattern in modify_patterns)
        
        # 检查是否包含新的餐厅查询关键词
        new_query_patterns = [
            r'\b(restaurant|food|dining|eat|meal|dinner|lunch|breakfast|cuisine|taste|flavor|spicy|sweet|sour|savory)\b',
            r'\b(餐厅|食物|用餐|吃饭|餐|晚餐|午餐|早餐|菜系|味道|口味|辣|甜|酸|咸|香)\b'
        ]
        
        is_new_query = any(re.search(pattern, query_lower) for pattern in new_query_patterns)
        
        # 判断意图类型
        if is_yes and not is_no:
            return {
                "type": "confirmation_yes",
                "original_query": query,
                "confidence": 0.9
            }
        elif is_no or is_modify:
            return {
                "type": "confirmation_no",
                "original_query": query,
                "confidence": 0.8
            }
        elif is_new_query:
            return {
                "type": "new_query",
                "original_query": query,
                "confidence": 0.85
            }
        else:
            # 默认认为是新查询
            return {
                "type": "new_query",
                "original_query": query,
                "confidence": 0.6
            }
    
    # ==================== 偏好提取 ====================
    
    def extract_preferences_from_query(
        self,
        query: str,
        user_id: str = "default",
        session_id: Optional[str] = None,
        *,
        persist: bool = True,
        base_preferences: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        从用户查询中智能提取偏好设置
        
        Args:
            query: 用户查询
            user_id: 用户ID
            session_id: 会话ID（可选）
            
        Returns:
            提取的偏好设置
        """
        query_lower = query.lower()
        
        # 获取用户存储的偏好作为基础。Graph/default path can pass
        # repository-loaded preferences and disable process-local writes.
        stored_prefs = base_preferences or self.get_user_preferences(user_id, session_id)
        
        # 初始化为空，用于检测用户是否指定了新值
        preferences = {
            "restaurant_types": [],
            "flavor_profiles": [],
            "dining_purpose": None,
            "budget_range": {"min": None, "max": None, "currency": "SGD", "per": "person"},
            "location": None
        }
        
        # 提取餐厅类型
        type_keywords = {
            "casual": ["casual", "relaxed", "informal", "everyday"],
            "fine-dining": ["fine dining", "fancy", "elegant", "upscale", "romantic", "special occasion"],
            "fast-casual": ["fast casual", "quick", "grab and go"],
            "street-food": ["street food", "hawker", "food court", "local"],
            "buffet": ["buffet", "all you can eat", "unlimited"],
            "cafe": ["cafe", "coffee", "brunch", "light meal"]
        }
        
        for type_key, keywords in type_keywords.items():
            if any(keyword in query_lower for keyword in keywords):
                preferences["restaurant_types"].append(type_key)
        
        # 提取口味偏好
        flavor_keywords = {
            "spicy": ["spicy", "hot", "chili", "sichuan", "thai", "indian", "korean"],
            "savory": ["savory", "umami", "meaty", "rich"],
            "sweet": ["sweet", "dessert", "cake", "chocolate"],
            "sour": ["sour", "tangy", "citrus", "vinegar"],
            "mild": ["mild", "gentle", "subtle", "light"]
        }
        
        for flavor_key, keywords in flavor_keywords.items():
            if any(keyword in query_lower for keyword in keywords):
                preferences["flavor_profiles"].append(flavor_key)
        
        # 提取用餐目的
        purpose_keywords = {
            "date-night": ["date", "romantic", "anniversary", "valentine", "couple"],
            "family": ["family", "kids", "children", "parents"],
            "business": ["business", "meeting", "client", "professional"],
            "solo": ["solo", "alone", "myself", "personal"],
            "friends": ["friends", "group", "party", "celebration"],
            "celebration": ["celebration", "birthday", "graduation", "promotion"]
        }
        
        for purpose_key, keywords in purpose_keywords.items():
            if any(keyword in query_lower for keyword in keywords):
                preferences["dining_purpose"] = purpose_key
                break
        
        # 提取预算信息
        budget_patterns = [
            r'(\$+)\s*(\d+)',  # $50, $$100
            r'(\d+)\s*to\s*(\d+)',  # 50 to 100
            r'under\s*(\d+)',  # under 50
            r'around\s*(\d+)',  # around 50
            r'budget\s*(\d+)',  # budget 50
        ]
        
        for pattern in budget_patterns:
            match = re.search(pattern, query_lower)
            if match:
                if 'to' in pattern:
                    preferences["budget_range"]["min"] = int(match.group(1))
                    preferences["budget_range"]["max"] = int(match.group(2))
                else:
                    amount = int(match.group(1)) if match.group(1).isdigit() else int(match.group(2))
                    if 'under' in pattern:
                        preferences["budget_range"]["max"] = amount
                    else:
                        preferences["budget_range"]["min"] = amount
                        preferences["budget_range"]["max"] = amount + 20
                break
        
        # 提取位置信息
        singapore_areas = [
            "orchard", "marina bay", "chinatown", "bugis", "tanjong pagar",
            "clarke quay", "little india", "holland village", "tiong bahru",
            "katong", "joo chiat", "downtown", "cbd", "central"
        ]
        
        for area in singapore_areas:
            if area in query_lower:
                preferences["location"] = area.title()
                break
        
        # 合并用户存储的偏好：如果query中没有指定，则使用存储的值
        if not preferences["restaurant_types"]:
            preferences["restaurant_types"] = stored_prefs["restaurant_types"]
        
        if not preferences["flavor_profiles"]:
            preferences["flavor_profiles"] = stored_prefs["flavor_profiles"]
        
        if preferences["dining_purpose"] is None:
            preferences["dining_purpose"] = stored_prefs["dining_purpose"]
        
        if not preferences["budget_range"]["min"] and not preferences["budget_range"]["max"]:
            preferences["budget_range"] = stored_prefs["budget_range"]
        
        if preferences["location"] is None:
            preferences["location"] = stored_prefs["location"]

        # 显式菜系/菜品意图：始终从当前 query 重新抽取（请求级，不继承存储值）
        preferences["food_intent"] = extract_food_intent_keywords(query)

        if persist:
            self.update_user_preferences(user_id, preferences, session_id)

        return preferences
    
    # ==================== 确认流程 ====================
    
    def generate_confirmation_prompt(self, query: str, preferences: Dict[str, Any], domain: str = "recommendation") -> str:
        """Generic, KeyError-safe confirmation template. Used as the fallback when
        the natural LLM confirmation message fails; works for any domain."""
        preferences = preferences or {}
        parts: List[str] = []

        food_intent = preferences.get("food_intent")
        if is_meaningful_food_intent(food_intent):
            cuisines = [str(c).title() for c in (food_intent.get("cuisines") or [])]
            dishes = [str(d).title() for d in (food_intent.get("dishes") or [])]
            if cuisines:
                parts.append(f"• Cuisine: {', '.join(cuisines)}")
            if dishes:
                parts.append(f"• Dish: {', '.join(dishes)}")

        budget = preferences.get("budget_range")
        if isinstance(budget, dict) and (budget.get("min") or budget.get("max")):
            lo, hi = budget.get("min"), budget.get("max")
            sep = "-" if lo and hi else ""
            parts.append(f"• Budget: {lo or ''}{sep}{hi or ''}")

        skip = {"food_intent", "budget_range", "domain", "query", "confidence"}
        for key, value in preferences.items():
            if key in skip:
                continue
            if isinstance(value, (list, tuple, set)):
                items = [str(v) for v in value if v and str(v).lower() != "any"]
                if items:
                    parts.append(f"• {key.replace('_', ' ').title()}: {', '.join(items)}")
            elif isinstance(value, dict):
                continue
            elif value and str(value).lower() != "any":
                parts.append(f"• {key.replace('_', ' ').title()}: {value}")

        body = "\n".join(parts) if parts else "• (no specific preferences yet)"
        return f"Based on your request '{query}', I'll look for a {domain} recommendation with:\n\n{body}\n\nIs that correct?"

    @staticmethod
    def _extract_tool_outputs(executions: List[Dict[str, Any]]) -> Tuple[Any, Any, Any]:
        """从 executions 中提取 gmap/xhs/yelp 输出"""
        gmap_results = None
        xhs_results = None
        yelp_results = None
        for item in executions or []:
            if item.get("tool") == "gmap.search":
                gmap_results = item.get("output")
            if item.get("tool") == "xhs.search":
                xhs_results = item.get("output")
            if item.get("tool") == "yelp.search":
                yelp_results = item.get("output")
        return gmap_results, xhs_results, yelp_results

    @staticmethod
    def _parse_summary_payload(summary_content: Any) -> Dict[str, Any]:
        """统一解析 summary 内容为字典结构"""
        import logging
        logger = logging.getLogger(__name__)

        result: Dict[str, Any] = {"summary": None}
        if not summary_content:
            logger.warning("summary_content is None or empty")
            return result

        logger.info("summary_content type: %s, length: %d", type(summary_content), len(str(summary_content)))
        try:
            if isinstance(summary_content, str):
                parsed_summary = _loads_llm_json(summary_content)
                logger.info("Parsed summary_content from string, type: %s", type(parsed_summary))
            else:
                parsed_summary = summary_content
                logger.info("summary_content is not string, type: %s", type(parsed_summary))

            if isinstance(parsed_summary, dict):
                logger.info("Parsed summary keys: %s", list(parsed_summary.keys()))
                result["summary"] = parsed_summary
            else:
                logger.warning("Parsed summary is not dict, type: %s", type(parsed_summary))
                result["summary"] = {"raw": parsed_summary}
        except Exception as e:
            logger.exception("Failed to parse summary_content: %s", str(e))
            logger.info("summary_content sample: %s", str(summary_content)[:200] if summary_content else "None")
            result["summary"] = {"raw": summary_content}
        return result

    def _detect_query_cuisine_intents(self, query: str) -> List[str]:
        """从 query 中提取菜系意图"""
        query_lower = (query or "").lower()
        cuisine_map = {
            "sichuan": ["sichuan", "chuan", "川菜", "麻辣"],
            "chinese": ["chinese", "cantonese", "dim sum", "hunan", "粤菜", "中餐"],
            "japanese": ["japanese", "sushi", "ramen", "yakitori", "寿司", "拉面"],
            "korean": ["korean", "kimchi", "韩餐", "韩式"],
            "thai": ["thai", "tom yum", "thailand", "泰餐"],
            "indian": ["indian", "biryani", "tandoor", "印度菜"],
            "italian": ["italian", "pasta", "pizza", "意大利菜"],
            "western": ["western", "steak", "burger", "西餐"]
        }
        detected = []
        for cuisine, keywords in cuisine_map.items():
            if any(k in query_lower for k in keywords):
                detected.append(cuisine)
        return detected

    @staticmethod
    def _parse_restaurant_price_range(restaurant: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
        """解析餐厅价格范围"""
        price_per_person = restaurant.get("price_per_person_sgd")
        if price_per_person:
            price_str = str(price_per_person)
            nums = re.findall(r"\d+(?:\.\d+)?", price_str)
            if len(nums) >= 2:
                return float(nums[0]), float(nums[1])
            if len(nums) == 1:
                val = float(nums[0])
                if "+" in price_str:
                    return val, val * 1.3
                if "up to" in price_str.lower() or "≤" in price_str:
                    return 0.0, val
                return val, val

        price_symbol = restaurant.get("price")
        symbol_map = {"$": (10.0, 25.0), "$$": (20.0, 50.0), "$$$": (45.0, 90.0), "$$$$": (80.0, 180.0)}
        if price_symbol in symbol_map:
            return symbol_map[price_symbol]

        return None, None

    @staticmethod
    def _restaurant_text_blob(restaurant: Dict[str, Any]) -> str:
        """餐厅可检索文本（名称/菜系/区域/简介等），用于一致性与意图匹配。"""
        return " ".join(filter(None, [
            str(restaurant.get("name", "")),
            str(restaurant.get("cuisine", "")),
            str(restaurant.get("type", "")),
            str(restaurant.get("area", "")),
            str(restaurant.get("address", "")),
            str(restaurant.get("location", "")),
            str(restaurant.get("why", "")),
            " ".join([str(x) for x in (restaurant.get("flavor_match") or [])]),
            " ".join([str(x) for x in (restaurant.get("purpose_match") or [])]),
        ])).lower()

    @staticmethod
    def _restaurant_coordinates(restaurant: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        """从 gps_coordinates/coordinates 解析 (lat, lng)，解析不出返回 None。"""
        raw = restaurant.get("gps_coordinates") or restaurant.get("coordinates")
        lat = lng = None
        if isinstance(raw, dict):
            lat = raw.get("latitude", raw.get("lat"))
            lng = raw.get("longitude", raw.get("lng", raw.get("lon")))
        elif isinstance(raw, str) and "," in raw:
            parts = raw.split(",")
            if len(parts) >= 2:
                try:
                    lat, lng = float(parts[0].strip()), float(parts[1].strip())
                except ValueError:
                    return None
        if lat is None or lng is None:
            return None
        try:
            return float(lat), float(lng)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
        from math import radians, sin, cos, asin, sqrt
        lat1, lng1 = a
        lat2, lng2 = b
        dlat = radians(lat2 - lat1)
        dlng = radians(lng2 - lng1)
        h = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
        return 2 * 6371.0 * asin(min(1.0, sqrt(h)))

    @classmethod
    def _geo_reference(cls, restaurants: List[Dict[str, Any]]) -> Optional[Tuple[float, float]]:
        """从候选自身坐标取中位数作为"本地簇"参考点（相对，不假设任何具体国家）。

        中位数对少数离群点稳健：只要真正靠近用户地点的结果不是绝对少数，参考点即落在
        本地簇上，远在他国的离群项（如同名地名解析到的美国餐厅）便会被判为过远。
        """
        coords = [c for r in restaurants if (c := cls._restaurant_coordinates(r)) is not None]
        # 至少 3 个坐标才能得到稳健的中位数参考（2 个时中位数会退化为逐维最大值）。
        if len(coords) < 3:
            return None
        lats = sorted(c[0] for c in coords)
        lngs = sorted(c[1] for c in coords)
        mid = len(coords) // 2
        return (lats[mid], lngs[mid])

    @classmethod
    def _is_far_from_reference(
        cls,
        restaurant: Dict[str, Any],
        reference: Optional[Tuple[float, float]],
        max_km: float = 100.0,
    ) -> bool:
        """候选坐标距参考点是否过远（>max_km）。无参考点或无坐标时不判远（不误杀）。"""
        if reference is None:
            return False
        coord = cls._restaurant_coordinates(restaurant)
        if coord is None:
            return False
        return cls._haversine_km(coord, reference) > max_km

    @classmethod
    def _drop_far_results(
        cls,
        restaurants: List[Dict[str, Any]],
        preferences: Dict[str, Any],
        max_km: float = 100.0,
    ) -> List[Dict[str, Any]]:
        """提供了 location 时，按坐标剔除明显远离本地簇的离群结果。"""
        location = preferences.get("location")
        if not location or location == "any":
            return restaurants
        reference = cls._geo_reference(restaurants)
        if reference is None:
            return restaurants
        return [r for r in restaurants if not cls._is_far_from_reference(r, reference, max_km)]

    @classmethod
    def _spatially_scope_candidates(
        cls,
        candidates: List[Any],
        anchor: Tuple[Optional[float], Optional[float]],
        radius_meters: Optional[int],
        *,
        coord_of: Optional[Callable[[Any], Optional[Tuple[float, float]]]] = None,
        min_keep: int = 3,
        tolerance: float = 1.25,
    ) -> List[Any]:
        """Keep candidates within (a tolerance multiple of) the request radius of the
        anchor. The restaurant graph ranks by quality, not proximity, and its
        yelp/xhs tools are geo-blind, so even an anchored fetch can surface results
        clear across the city; this is the single choke point that scopes them
        spatially before they reach the solver. Candidates *inside* the radius win
        outright — far ones are dropped even when higher-rated. Only when nothing
        lands inside the radius do we fall back to the nearest ``min_keep`` (plus any
        coordinate-less rows we cannot judge), so a meal window is never starved."""
        if not candidates or anchor[0] is None or anchor[1] is None:
            return candidates
        get_coord = coord_of or cls._restaurant_coordinates
        reference = (float(anchor[0]), float(anchor[1]))
        radius_km = max(0.5, (radius_meters or 5000) / 1000.0) * tolerance
        scored: List[Tuple[float, Any]] = []
        unknown: List[Any] = []
        for candidate in candidates:
            coord = get_coord(candidate)
            if coord is None:
                unknown.append(candidate)
                continue
            scored.append((cls._haversine_km(reference, coord), candidate))
        if not scored:
            return candidates  # nothing locatable to judge: don't over-prune
        scored.sort(key=lambda row: row[0])
        within = [candidate for distance, candidate in scored if distance <= radius_km]
        if within:
            return within
        nearest = [candidate for _distance, candidate in scored[:min_keep]]
        return nearest + unknown

    def _consistency_issues_for_restaurant(
        self,
        restaurant: Dict[str, Any],
        preferences: Dict[str, Any],
        query: str
    ) -> List[str]:
        """检查单条推荐与偏好一致性，返回问题标签"""
        issues: List[str] = []

        text_blob = self._restaurant_text_blob(restaurant)

        # 预算一致性（硬约束）
        budget = preferences.get("budget_range", {}) or {}
        budget_min = budget.get("min")
        budget_max = budget.get("max")
        rest_min, rest_max = self._parse_restaurant_price_range(restaurant)
        if budget_max is not None and rest_min is not None and rest_min > budget_max * 1.15:
            issues.append("budget_too_high")
        if budget_min is not None and rest_max is not None and rest_max < budget_min * 0.75:
            issues.append("budget_too_low")

        # 位置一致性（软约束）
        pref_location = preferences.get("location")
        if pref_location and pref_location != "any":
            pref_location_lower = str(pref_location).lower()
            location_text = " ".join(filter(None, [
                str(restaurant.get("area", "")),
                str(restaurant.get("address", "")),
                str(restaurant.get("location", "")),
            ])).lower()
            if location_text and pref_location_lower not in location_text:
                issues.append("location_mismatch")

        # 餐厅类型一致性（软约束）
        pref_types = preferences.get("restaurant_types", []) or []
        if pref_types and pref_types != ["any"] and restaurant.get("type"):
            type_text = str(restaurant.get("type", "")).lower()
            type_map = {
                "casual": ["casual"],
                "fine-dining": ["fine", "fine dining"],
                "fast-casual": ["fast", "quick", "casual"],
                "street-food": ["street", "hawker"],
                "buffet": ["buffet"],
                "cafe": ["cafe", "coffee"]
            }
            if not any(any(k in type_text for k in type_map.get(t, [t])) for t in pref_types):
                issues.append("type_mismatch")

        # 口味一致性（软约束）
        pref_flavors = preferences.get("flavor_profiles", []) or []
        if pref_flavors and pref_flavors != ["any"]:
            flavor_map = {
                "spicy": ["spicy", "辣", "麻辣", "sichuan", "chili"],
                "savory": ["savory", "umami", "鲜", "咸香"],
                "sweet": ["sweet", "甜"],
                "sour": ["sour", "酸"],
                "mild": ["mild", "light"]
            }
            expected_keywords = []
            for f in pref_flavors:
                expected_keywords.extend(flavor_map.get(f, [str(f)]))
            if expected_keywords and text_blob and not any(k in text_blob for k in expected_keywords):
                issues.append("flavor_mismatch")

        # 显式菜系/菜品意图一致性。
        # 命中结构化 food_intent 时以它为准（其硬/软由 confidence 在
        # _apply_preference_consistency_check 中据 is_food_intent_strict 决定）；
        # 未命中时回退到从原始 query 粗提菜系的旧软约束。
        food_intent = preferences.get("food_intent")
        if is_meaningful_food_intent(food_intent):
            if not restaurant_matches_food_intent(text_blob, food_intent):
                issues.append("food_intent_mismatch")
        else:
            query_cuisines = self._detect_query_cuisine_intents(query)
            if query_cuisines:
                cuisine_map = {
                    "sichuan": ["sichuan", "川菜", "麻辣"],
                    "chinese": ["chinese", "cantonese", "dim sum", "中餐"],
                    "japanese": ["japanese", "sushi", "ramen", "日料"],
                    "korean": ["korean", "韩"],
                    "thai": ["thai", "泰"],
                    "indian": ["indian", "印度"],
                    "italian": ["italian", "pizza", "pasta", "意大利"],
                    "western": ["western", "steak", "burger", "西餐"]
                }
                expected = []
                for c in query_cuisines:
                    expected.extend(cuisine_map.get(c, [c]))
                if text_blob and expected and not any(k.lower() in text_blob for k in expected):
                    issues.append("cuisine_mismatch")

        return issues

    def _apply_preference_consistency_check(
        self,
        restaurants: List[Dict[str, Any]],
        preferences: Dict[str, Any],
        query: str
    ) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
        """二次一致性校验：过滤明显错配结果"""
        rejection_stats: Dict[str, int] = {}
        kept: List[Dict[str, Any]] = []

        # 预算与"距指定地点过远"永远是硬约束；显式菜系/菜品仅在 confidence 达标（strict）
        # 时升级为硬约束，否则按软约束处理（混合推荐：信心高才硬收窄）。
        hard_tags = {"budget_too_high", "budget_too_low", "location_too_far"}
        if is_food_intent_strict(preferences.get("food_intent")):
            hard_tags = hard_tags | {"food_intent_mismatch"}

        # 提供了 location 时，按坐标建立"本地簇"参考点，剔除明显远离的离群结果
        # （如同名地名被解析到他国）。无 location 或坐标不足时不启用，避免误杀。
        location = preferences.get("location")
        geo_reference = (
            self._geo_reference(restaurants)
            if location and location != "any"
            else None
        )

        for r in restaurants:
            issues = self._consistency_issues_for_restaurant(r, preferences, query)
            if geo_reference is not None and self._is_far_from_reference(r, geo_reference):
                issues = issues + ["location_too_far"]
            hard_issue = any(i in hard_tags for i in issues)
            soft_issue_count = len([i for i in issues if i not in hard_tags])

            # 硬约束直接拒绝；软约束出现2个及以上也拒绝
            if hard_issue or soft_issue_count >= 2:
                for issue in issues:
                    rejection_stats[issue] = rejection_stats.get(issue, 0) + 1
                continue
            kept.append(r)

        return kept, rejection_stats

    def _select_empty_fallback(
        self,
        restaurants: List[Dict[str, Any]],
        preferences: Dict[str, Any],
        query: str,
        rejection_stats: Dict[str, int],
    ) -> List[Dict[str, Any]]:
        """二次校验把候选清空后的兜底策略。

        - 非显式菜系/菜品（或软意图）：沿用原行为，回退到评分最高的若干家。
        - 显式（strict）菜系/菜品：受控放宽一档（去掉具体菜品、保留菜系）；仍无匹配
          时返回空（由上层给出说明），绝不拿无关餐厅顶替（"问 Pho 给 burger" 问题）。
        """
        def top_rated(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            return sorted(
                items,
                key=lambda r: ((r.get("rating") or 0), (r.get("reviews_count") or 0)),
                reverse=True,
            )[:5]

        # 兜底也必须遵守"就近"：先剔除远离指定地点的离群结果。
        restaurants = self._drop_far_results(restaurants, preferences)

        food_intent = preferences.get("food_intent")
        if not is_food_intent_strict(food_intent):
            return top_rated(restaurants)

        relaxed = relax_food_intent(food_intent)
        if relaxed:
            matches = [
                r for r in restaurants
                if restaurant_matches_food_intent(self._restaurant_text_blob(r), relaxed)
            ]
            if matches:
                return top_rated(matches)
        return []

    @staticmethod
    def _build_refine_instruction(
        query: str,
        preferences: Dict[str, Any],
        rejection_stats: Dict[str, int]
    ) -> str:
        """构造 refine 指令，指导二次聚合"""
        if rejection_stats:
            reasons = ", ".join([f"{k}:{v}" for k, v in sorted(rejection_stats.items(), key=lambda x: x[1], reverse=True)])
        else:
            reasons = "no_explicit_reason"

        # 显式菜系/菜品意图时，明确告诉模型必须命中的主体，避免再次跑偏
        food_intent = preferences.get("food_intent")
        food_directive = ""
        if is_food_intent_strict(food_intent):
            terms = ", ".join(food_intent_terms(food_intent))
            food_directive = (
                f"\nThe user explicitly wants: {terms}. Every recommendation MUST be this "
                "cuisine/dish; do not substitute other cuisines."
            )

        return (
            "Refine recommendations because post-check removed all candidates.\n"
            f"Original query: {query}\n"
            f"Preferences: {json.dumps(preferences, ensure_ascii=False)}\n"
            f"Top mismatch reasons: {reasons}"
            f"{food_directive}\n"
            "Please regenerate recommendations strictly aligned with location/budget/cuisine/flavor intent. "
            "If data is missing, explain uncertainty but avoid obviously mismatched restaurants."
        )

    async def _agentic_refine_summary_once(
        self,
        query: str,
        preferences: Dict[str, Any],
        executions: List[Dict[str, Any]],
        previous_summary: Any,
        rejection_stats: Dict[str, int]
    ) -> Tuple[List[Dict[str, Any]], Any]:
        """基于失败原因做一次 agentic refine 重试"""
        import logging
        logger = logging.getLogger(__name__)

        try:
            from agent.agent_summary import summarize_recommendations
        except Exception as e:
            logger.exception("Failed to import summarize_recommendations: %s", str(e))
            return [], previous_summary

        gmap_results, xhs_results, yelp_results = self._extract_tool_outputs(executions)
        refine_instruction = self._build_refine_instruction(query, preferences, rejection_stats)
        refine_input = {
            "query": query,
            "preferences": preferences,
            "previous_summary": previous_summary,
            "refine_instruction": refine_instruction
        }

        try:
            refine_resp = await asyncio.to_thread(
                summarize_recommendations,
                self.sync_client,
                refine_input,
                gmap_results,
                xhs_results,
                yelp_results,
                self.summary_model
            )
            refine_content = refine_resp.choices[0].message.content if refine_resp and refine_resp.choices else None
            execution_data = {
                "executions": executions,
                **self._parse_summary_payload(refine_content)
            }
            refined_restaurants = self._extract_restaurants_from_execution_data(execution_data)
            return refined_restaurants, refine_content
        except Exception as e:
            logger.exception("Agentic refine retry failed: %s", str(e))
            return [], previous_summary

    @staticmethod
    def _build_widen_instruction(query: str, terms: str, location: str) -> str:
        """构造"扩大地点、保持菜系"的重汇总指令。"""
        loc = location if location and location != "any" else "the requested area"
        return (
            "The user asked for a specific cuisine/dish that has no match at the exact "
            f"location. Original query: {query}\n"
            f"Required cuisine — keep this, do NOT substitute other cuisines: {terms}\n"
            f"Originally searched location: {loc}\n"
            "Broaden the search to NEARBY areas across Singapore and surface the closest "
            "spots of this SAME cuisine, even if a bit further from the exact location. "
            "Do NOT include other cuisines. If there is genuinely nothing of this cuisine "
            "anywhere in the provided data, return no recommendations."
        )

    async def _widen_food_intent_search(
        self,
        query: str,
        preferences: Dict[str, Any],
        executions: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """显式菜系/菜品在指定地点无匹配时的"同菜系、放宽地点"兜底。

        复用已抓取的 executions（通常已包含邻近候选，只是被汇总器按精确地点过滤掉了），
        以"放宽地点、保持菜系"的指令重新汇总一次，再按菜系过滤。绝不跨菜系顶替。
        非 strict 意图直接返回空（不触发任何额外 LLM 调用）。
        """
        import logging
        logger = logging.getLogger(__name__)

        food_intent = preferences.get("food_intent")
        if not is_food_intent_strict(food_intent):
            return []
        relaxed = relax_food_intent(food_intent)  # 去掉具体菜品，只保留菜系
        if not relaxed:
            return []

        gmap_results, xhs_results, yelp_results = self._extract_tool_outputs(executions)
        if not any([gmap_results, xhs_results, yelp_results]):
            return []

        try:
            from agent.agent_summary import summarize_recommendations
        except Exception as e:
            logger.exception("Failed to import summarize_recommendations for widen: %s", str(e))
            return []

        location = str(preferences.get("location") or "any")
        terms = ", ".join(food_intent_terms(relaxed))
        widen_input = {
            "query": query,
            "preferences": {**preferences, "food_intent": relaxed},
            "widen_instruction": self._build_widen_instruction(query, terms, location),
        }

        try:
            resp = await asyncio.to_thread(
                summarize_recommendations,
                self.sync_client,
                widen_input,
                gmap_results,
                xhs_results,
                yelp_results,
                self.summary_model,
            )
            content = resp.choices[0].message.content if resp and resp.choices else None
            execution_data = {
                "executions": executions,
                **self._parse_summary_payload(content),
            }
            candidates = self._extract_restaurants_from_execution_data(execution_data)
        except Exception as e:
            logger.exception("Widen re-summarization failed: %s", str(e))
            return []

        # 放宽地点也要"就近"：先剔除远离指定地点的离群结果，再按同菜系过滤。
        candidates = self._drop_far_results(candidates, preferences)

        # 只保留同菜系命中，按评分取前若干；绝不跨菜系顶替。
        matches = [
            r for r in candidates
            if restaurant_matches_food_intent(self._restaurant_text_blob(r), relaxed)
        ]
        matches.sort(
            key=lambda r: ((r.get("rating") or 0), (r.get("reviews_count") or 0)),
            reverse=True,
        )
        return matches[:5]

    # ==================== 异步任务处理 ====================

    def _cancelled_task_status(
        self,
        task_id: str,
        user_id: str,
        session_id: Optional[str],
        current: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        payload = dict(current or {})
        metadata = dict(payload.get("metadata") or {})
        metadata.update(
            {
                "cancellation_reason": "conversation_deleted",
                "cancelled_at": datetime.now().isoformat(),
            }
        )
        payload.update(
            {
                "task_id": payload.get("task_id") or task_id,
                "status": "cancelled",
                "progress": int(payload.get("progress") or 0),
                "message": "Conversation deleted; recommendation task cancelled.",
                "result": payload.get("result"),
                "error": None,
                "user_id": payload.get("user_id") or user_id,
                "conversation_id": payload.get("conversation_id") or session_id or "default",
                "metadata": metadata,
            }
        )
        return payload

    async def _load_task_status_projection(
        self,
        user_id: str,
        session_id: Optional[str],
        task_id: str,
    ) -> Optional[Dict[str, Any]]:
        if self.task_repository is not None:
            try:
                return await self.task_repository.load(user_id, session_id, task_id)
            except ValueError:
                return None
        session_ctx = self.session_contexts.get(self._get_session_key(user_id, session_id))
        return (session_ctx or {}).get("tasks", {}).get(task_id)

    async def cancel_conversation_tasks_async(
        self,
        user_id: str,
        session_id: Optional[str],
    ) -> Dict[str, Any]:
        """Cancel active recommendation work for a deleted conversation.

        Completed tasks are intentionally left untouched so a delete request racing
        with a just-finished recommendation does not turn that successful run into
        a failure or cancellation.
        """
        if not session_id:
            return {"cancelled": 0, "completed": 0, "skipped": 0, "cancelled_task_ids": []}

        cancelled_task_ids: set[str] = set()
        completed_task_ids: set[str] = set()
        skipped_task_ids: set[str] = set()

        if self.task_repository is not None:
            try:
                summary = await self.task_repository.cancel_active_for_conversation(
                    user_id,
                    session_id,
                    message="Conversation deleted; recommendation task cancelled.",
                )
                cancelled_task_ids.update(summary.get("cancelled_task_ids", []))
                completed_task_ids.update(summary.get("completed_task_ids", []))
                skipped_task_ids.update(summary.get("skipped_task_ids", []))
            except ValueError:
                pass

        session_ctx = self.session_contexts.get(self._get_session_key(user_id, session_id))
        if session_ctx is not None:
            for task_id, status in list(session_ctx.get("tasks", {}).items()):
                current_status = status.get("status") if isinstance(status, dict) else None
                if current_status == "completed":
                    completed_task_ids.add(task_id)
                    continue
                if current_status in TERMINAL_TASK_STATUSES:
                    skipped_task_ids.add(task_id)
                    continue
                cancelled = self._cancelled_task_status(task_id, user_id, session_id, status)
                session_ctx["tasks"][task_id] = cancelled
                cancelled_task_ids.add(task_id)

        for task_id, task in list(self._running_tasks.items()):
            if self._running_task_scopes.get(task_id) != (user_id, session_id):
                continue
            if task.done():
                skipped_task_ids.add(task_id)
                continue
            task.cancel()

        return {
            "cancelled": len(cancelled_task_ids),
            "completed": len(completed_task_ids),
            "skipped": len(skipped_task_ids),
            "cancelled_task_ids": sorted(cancelled_task_ids),
        }

    @asynccontextmanager
    async def _usage_scope(
        self,
        *,
        user_id: Optional[str],
        conversation_id: Optional[str],
        task_id: Optional[str] = None,
    ):
        """Capture every LLM call made within this scope and, on exit, flush the
        accumulated token usage to the usage log. A fresh ledger is installed so a
        background task started inside another scope does not double-count."""
        ledger = llm_usage.UsageLedger()
        token = llm_usage.push_ledger(ledger)
        try:
            yield ledger
        finally:
            llm_usage.reset_ledger(token)
            await self._flush_usage_ledger(
                ledger, user_id=user_id, conversation_id=conversation_id, task_id=task_id
            )

    async def _flush_usage_ledger(
        self,
        ledger: "llm_usage.UsageLedger",
        *,
        user_id: Optional[str],
        conversation_id: Optional[str],
        task_id: Optional[str],
    ) -> None:
        """Persist a scope's usage events. Best-effort: analytics must never break
        request handling, and it no-ops cleanly when storage is unavailable."""
        if not ledger.events:
            return
        try:
            from business_repositories import usage_repository

            if usage_repository is None:
                return
            await usage_repository.record_events(
                user_id=user_id,
                conversation_id=conversation_id if conversation_id and conversation_id != "default" else None,
                task_id=task_id,
                events=ledger.events,
            )
        except Exception:
            logging.getLogger(__name__).debug("Failed to flush LLM usage ledger", exc_info=True)

    async def _run_scoped_task(
        self,
        task_id: str,
        query: str,
        preferences: Dict[str, Any],
        user_id: str,
        session_id: Optional[str],
        use_online_agent: bool,
        tool_tags: Optional[List[str]],
        branch_id: Optional[str],
        route: Optional[Dict[str, Any]] = None,
    ):
        """Background-task entrypoint that scopes LLM usage to this task (so the
        ranking / gather-reasoner calls are attributed and flushed)."""
        async with self._usage_scope(user_id=user_id, conversation_id=session_id, task_id=task_id):
            return await self._run_task_graph_compatible(
                task_id,
                query,
                preferences,
                user_id,
                session_id,
                use_online_agent,
                tool_tags,
                branch_id,
                route,
            )

    def _run_task_graph_compatible(
        self,
        task_id: str,
        query: str,
        preferences: Dict[str, Any],
        user_id: str,
        session_id: Optional[str],
        use_online_agent: bool,
        tool_tags: Optional[List[str]],
        branch_id: Optional[str],
        route: Optional[Dict[str, Any]] = None,
    ):
        task_runner = self.run_recommendation_task_graph
        parameters = inspect.signature(task_runner).parameters
        kwargs = {
            "task_id": task_id,
            "query": query,
            "preferences": preferences,
            "user_id": user_id,
            "session_id": session_id,
            "use_online_agent": use_online_agent,
            "tool_tags": tool_tags,
        }
        if "branch_id" in parameters:
            kwargs["branch_id"] = branch_id
        if "route" in parameters:
            kwargs["route"] = route
        return task_runner(**kwargs)

    async def _execute_restaurant_domain_task(
        self,
        *,
        query: str,
        preferences: Dict[str, Any],
        user_id: str,
        use_online_agent: bool,
        tool_tags: Optional[List[str]],
        progress_callback,
        conversation_context: Optional[str] = None,
    ) -> RecommendationResult:
        from langgraph_metarec.graphs.restaurant_graph import (
            RestaurantGraphAdapters,
            run_restaurant_graph,
        )

        food_intent = preferences.get("food_intent")
        strict_intent = is_food_intent_strict(food_intent)
        intent_terms = food_intent_terms(food_intent) if is_meaningful_food_intent(food_intent) else []
        searched_location = str(preferences.get("location") or "any")

        user_input = self._preferences_to_agent_input(query, preferences)
        if conversation_context:
            user_input = f"{user_input}\n\n[Conversation context]\n{conversation_context}"
        print(f"[Service] task graph - use_online_agent: {use_online_agent} (type: {type(use_online_agent)})")

        try:
            graph_result = await run_restaurant_graph(
                client=self.sync_client,
                summary_model=self.summary_model,
                planning_model=self.planning_model,
                query=query,
                preferences=preferences,
                user_input=user_input,
                use_online_agent=use_online_agent,
                tool_tags=tool_tags,
                adapters=RestaurantGraphAdapters(
                    summary_parser=self._parse_summary_payload,
                    restaurant_extractor=self._extract_restaurants_from_execution_data,
                    consistency_checker=self._apply_preference_consistency_check,
                    refine_once=self._agentic_refine_summary_once,
                    empty_fallback=self._select_empty_fallback,
                    widen_once=self._widen_food_intent_search,
                ),
                progress_callback=progress_callback,
            )
        except Exception as exc:
            # 绝不把"无候选/管线异常"变成硬失败：降级为带说明的空结果，让前端给出
            # 可操作提示（扩大范围/换个菜系），而不是 "Recommendation failed" 空白卡片。
            import logging
            logging.getLogger(__name__).exception(
                "Restaurant domain task degraded to an explained empty result: %s", str(exc)
            )
            return RecommendationResult(
                restaurants=[],
                thinking_steps=[
                    ThinkingStep(
                        step="recommendation_result",
                        description="Finalizing recommendations...",
                        status="completed",
                        details="Returned an explained empty result instead of failing",
                    )
                ],
                confidence_score=0.5,
                metadata={
                    "query": query,
                    "user_id": user_id,
                    "timestamp": datetime.now().isoformat(),
                    "preferences": preferences,
                    "degraded": True,
                    "error_summary": str(exc),
                    "food_intent_no_match": strict_intent,
                    "food_intent_widened": False,
                    "food_intent_terms": intent_terms if strict_intent else [],
                    "searched_location": searched_location,
                },
            )

        plan_calls = graph_result.plan_calls
        executions = graph_result.executions
        restaurants = graph_result.restaurants
        checked_restaurants = graph_result.checked_restaurants
        rejection_stats = graph_result.rejection_stats
        refine_used = graph_result.refine_used
        food_intent_widened = graph_result.food_intent_widened

        # 显式菜系/菜品收窄后确无匹配（含零候选）：标注出来，供前端给出有用的说明而非空白。
        food_intent_no_match = bool(not checked_restaurants and strict_intent)

        thinking_steps = [
            ThinkingStep(
                step="candidate_gather",
                description="Gathering restaurant candidates...",
                status="completed",
                details=f"Selected {len(plan_calls)} tools",
            ),
            ThinkingStep(
                step="rerank_and_summarize",
                description="Ranking and summarizing restaurants...",
                status="completed",
                details=f"Executed {len(executions)} tools",
            ),
            ThinkingStep(
                step="validation_and_calibration",
                description="Validating recommendation quality...",
                status="completed",
                details="Recommendations generated",
            ),
        ]

        return RecommendationResult(
            restaurants=[Restaurant(**restaurant) for restaurant in checked_restaurants],
            items=[],
            thinking_steps=thinking_steps,
            confidence_score=0.9 if checked_restaurants else 0.5,
            metadata={
                "query": query,
                "user_id": user_id,
                "timestamp": datetime.now().isoformat(),
                "preferences": preferences,
                "plan_calls": plan_calls,
                "executions": executions,
                "graph": graph_result.metadata.get("graph", "restaurant_graph"),
                "domain": graph_result.metadata.get("domain", "restaurant"),
                "selected_tools": graph_result.metadata.get("selected_tools", []),
                "skipped_tools": graph_result.metadata.get("skipped_tools", []),
                "progress_events": graph_result.progress_events,
                "consistency_check": {
                    "raw_count": len(restaurants),
                    "final_count": len(checked_restaurants),
                    "rejection_stats": rejection_stats,
                    "refine_used": refine_used,
                },
                "food_intent_no_match": food_intent_no_match,
                "food_intent_widened": food_intent_widened,
                "food_intent_terms": intent_terms,
                "searched_location": searched_location,
            },
        )

    async def _execute_generic_domain_task(
        self,
        *,
        query: str,
        preferences: Dict[str, Any],
        user_id: str,
        domain: str,
        use_online_agent: bool,
        tool_tags: Optional[List[str]],
        progress_callback,
    ) -> RecommendationResult:
        from langgraph_metarec.graphs.generic_graph import (
            GenericGraphAdapters,
            run_generic_domain_graph,
        )
        from llm_service import propose_gather_action

        async def _gather_reasoner(context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
            # LLM-backed ReAct step; defensive inside propose_gather_action so any
            # failure returns None and the graph uses its deterministic ladder.
            return await propose_gather_action(
                self.async_client,
                query=context.get("query", ""),
                domain=context.get("domain", domain),
                preferences=context.get("preferences", {}),
                observations=context.get("observations", []),
                tools=context.get("tools", []),
                found=context.get("found", 0),
                target=context.get("target", 0),
                model=self.llm_model,
            )

        async def _ranker_history():
            # WP4 D4: the signed-in user's product interactions, for the constraint-aware ranker.
            # The graph calls this only when METAREC_PRODUCT_RANKER=domain_v1. A guest, or any
            # user without rows, is simply a cold start.
            from business_models import ensure_uuid
            from business_repositories import item_interaction_repository
            from langgraph_metarec.product_ranker_live import history_from_records

            try:
                uid = ensure_uuid(user_id)
            except Exception:
                return [], []
            records = await item_interaction_repository.list_for_user(uid, domain="product")
            return history_from_records(records)

        try:
            graph_result = await run_generic_domain_graph(
                query=query,
                domain=domain,
                preferences=preferences,
                use_online_agent=use_online_agent,
                tool_tags=tool_tags,
                progress_callback=progress_callback,
                adapters=GenericGraphAdapters(
                    reasoner=_gather_reasoner,
                    ranker_history=_ranker_history if domain == "product" else None,
                ),
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception(
                "Generic domain task degraded to an explained empty result: %s", str(exc)
            )
            return RecommendationResult(
                restaurants=[],
                items=[],
                thinking_steps=[
                    ThinkingStep(
                        step="recommendation_result",
                        description="Finalizing recommendations...",
                        status="completed",
                        details="Returned an explained empty result instead of failing",
                    )
                ],
                confidence_score=0.4,
                metadata={
                    "query": query,
                    "user_id": user_id,
                    "timestamp": datetime.now().isoformat(),
                    "preferences": preferences,
                    "graph": "generic_domain_graph",
                    "domain": domain,
                    "degraded": True,
                    "error_summary": str(exc),
                    "items_count": 0,
                },
            )

        thinking_steps = [
            ThinkingStep(
                step="candidate_gather",
                description=f"Gathering {domain} candidates...",
                status="completed",
                details=f"Executed {len(graph_result.executions)} tools",
            ),
            ThinkingStep(
                step="normalize_and_rank",
                description="Normalizing and ranking candidates...",
                status="completed",
                details=f"Prepared {len(graph_result.items)} items",
            ),
        ]

        result_metadata = {
            "query": query,
            "user_id": user_id,
            "timestamp": datetime.now().isoformat(),
            "preferences": preferences,
            "graph": graph_result.metadata.get("graph", "generic_domain_graph"),
            "domain": graph_result.metadata.get("domain", domain),
            "selected_tools": graph_result.metadata.get("selected_tools", []),
            "skipped_tools": graph_result.metadata.get("skipped_tools", []),
            "progress_events": graph_result.progress_events,
            "executions": graph_result.executions,
            "items_count": len(graph_result.items),
            "errors": graph_result.errors,
        }
        if graph_result.metadata.get("product_ranker"):
            # WP4 D4: scores, components and reason codes. Server-side experiment data: the API
            # strips this key before a client sees the result (main._INTERNAL_METADATA_KEYS).
            result_metadata["product_ranker"] = graph_result.metadata["product_ranker"]
        return RecommendationResult(
            restaurants=[],
            items=[RecommendationItem(**item) for item in graph_result.items],
            thinking_steps=thinking_steps,
            confidence_score=0.85 if graph_result.items else 0.45,
            metadata=result_metadata,
        )

    async def run_recommendation_task_graph(
        self,
        task_id: str,
        query: str,
        preferences: Dict[str, Any],
        user_id: str = "default",
        session_id: Optional[str] = None,
        use_online_agent: bool = False,
        tool_tags: Optional[List[str]] = None,
        branch_id: Optional[str] = None,
        route: Optional[Dict[str, Any]] = None,
    ) -> None:
        from langgraph_metarec.graphs.task_graph import TaskGraphAdapters, run_task_graph
        from langgraph_metarec.state import DomainGraphResult

        async def write_projection(status: Dict[str, Any]) -> None:
            existing_status = await self._load_task_status_projection(user_id, session_id, task_id)
            existing_lifecycle = existing_status.get("status") if isinstance(existing_status, dict) else None
            next_lifecycle = status.get("status")
            if existing_lifecycle == "cancelled" and next_lifecycle != "completed":
                raise asyncio.CancelledError("Recommendation task cancelled")
            if existing_lifecycle in {"completed", "error"} and next_lifecycle != existing_lifecycle:
                return
            if self.task_repository is not None:
                # On completion, persist the recommendation as the canonical, queryable
                # record (recommendation_results) before the task projection. This is the
                # durable source of truth that the conversation message / feedback rows
                # reference; the task projection stays as transient lifecycle state.
                if status.get("status") == "completed" and status.get("result"):
                    result_id = await self._persist_recommendation_result(
                        user_id, session_id, task_id, branch_id, status
                    )
                    if result_id:
                        metadata = status.setdefault("metadata", {})
                        metadata["result_id"] = result_id
                await self.task_repository.save(user_id, session_id, task_id, status)
            else:
                session_ctx = self._get_session_context(user_id, session_id)
                session_ctx["tasks"][task_id] = status

        async def run_domain(progress_callback) -> Dict[str, Any]:
            # Give the recommender the same in-conversation memory: which places were
            # already shown / disliked, so it doesn't repeat them and stays on-thread.
            recommender_context = ""
            try:
                from business_repositories import conversation_repository
                from conversation_context import build_conversation_context

                if session_id:
                    conversation = await conversation_repository.get_full_conversation(user_id, session_id)
                    recommender_context = build_conversation_context(
                        conversation,
                        active_branch_id=branch_id,
                        current_query=query,
                    ).to_recommender_block()
            except Exception:
                import logging
                logging.getLogger(__name__).debug("Recommender context unavailable", exc_info=True)

            # Load the user profile once; each domain task fuses in only the slice
            # it needs (see execute_domain_task).
            user_profile: Dict[str, Any] = {}
            try:
                from business_repositories import profile_repository

                user_profile = await profile_repository.get_user_profile(user_id) or {}
            except Exception:
                import logging
                logging.getLogger(__name__).debug("User profile unavailable for fusion", exc_info=True)

            active_route = route or {
                "domain": "restaurant",
                "execution_domain": "restaurant",
                "mode": "single_domain",
                "status": "ready",
                "tool_tags": tool_tags or ["#place", "#restaurant"],
                "domain_tasks": [
                    {
                        "domain": "restaurant",
                        "status": "ready",
                        "tool_tags": tool_tags or ["#place", "#restaurant"],
                    }
                ],
            }

            async def execute_domain_task(domain_task: Dict[str, Any]) -> RecommendationResult:
                from profile_model import (
                    assemble_domains,
                    build_recommender_profile_block,
                    enrich_hotel_location_preferences,
                )

                task_domain = str(domain_task.get("domain") or active_route.get("execution_domain") or "restaurant")
                task_tool_tags = domain_task.get("tool_tags") or active_route.get("tool_tags") or tool_tags
                task_query = str(domain_task.get("query") or query)
                slot_preferences = domain_task.get("slot_preferences") if isinstance(domain_task.get("slot_preferences"), dict) else {}
                request_preferences = {**(preferences or {}), **slot_preferences, "domain": task_domain}

                async def scoped_progress_callback(event: Dict[str, Any]) -> None:
                    payload = dict(event)
                    if (
                        active_route.get("mode") == "itinerary"
                        and payload.get("stage") == "recommendation_result"
                    ):
                        payload["message"] = "Information gathered for itinerary planning."
                    await progress_callback(payload)

                # Fuse ONLY this domain's profile info: the NL block (demographics +
                # persona + constraints + this domain's slice) into the recommender
                # context, and this domain's structured slice into preferences so it
                # drives tool params. Explicit request preferences win over profile.
                profile_block = build_recommender_profile_block(user_profile, task_domain)
                combined_context = "\n\n".join(part for part in (recommender_context, profile_block) if part)
                domain_slice = assemble_domains(user_profile).get(task_domain, {})

                if task_domain == "restaurant":
                    restaurant_keys = {"restaurant_types", "flavor_profiles", "dining_purpose", "budget_range", "location"}
                    if any(key in request_preferences for key in restaurant_keys):
                        restaurant_preferences = merge_preferences(
                            self._select_runtime_preferences(self.get_default_preferences(), user_profile, {}),
                            request_preferences,
                        )
                    else:
                        restaurant_preferences = self.extract_preferences_from_query(
                            task_query,
                            user_id=user_id,
                            session_id=session_id,
                            persist=False,
                            base_preferences=self._select_runtime_preferences(self.get_default_preferences(), user_profile, {}),
                        )
                    # merge_preferences / query extraction only carry recommender
                    # fields, so they drop the itinerary geo anchor. Re-attach it
                    # here so restaurant retrieval is spatially scoped (gmap.search
                    # honors anchor_lat/lng/radius_meters) instead of a city-wide
                    # text search that lands far from the day's attractions.
                    for anchor_key in ("anchor_lat", "anchor_lng", "radius_meters"):
                        if anchor_key in request_preferences:
                            restaurant_preferences[anchor_key] = request_preferences[anchor_key]
                    return await self._execute_restaurant_domain_task(
                        query=task_query,
                        preferences=restaurant_preferences,
                        user_id=user_id,
                        use_online_agent=use_online_agent,
                        tool_tags=task_tool_tags,
                        progress_callback=scoped_progress_callback,
                        conversation_context=combined_context,
                    )

                # Explicit request preferences win over profile slice defaults.
                fused_preferences = {**domain_slice, **request_preferences}
                if task_domain == "hotel":
                    fused_preferences = enrich_hotel_location_preferences(fused_preferences, user_profile)
                elif task_domain == "attraction":
                    # Soft geo disambiguation: the user's home region steers the
                    # geocoder/map bias ("NTU" -> Singapore, not Taiwan) without
                    # ever rewriting the requested destination.
                    from profile_model import place_region_hint

                    hint = place_region_hint(user_profile)
                    if hint and not str(fused_preferences.get("region_hint") or "").strip():
                        fused_preferences["region_hint"] = hint
                # The generic graph has no LLM stage to consume NL context, so the
                # functional fusion there is the structured slice merged into
                # preferences above (e.g. movie genres -> discover with_genres).
                return await self._execute_generic_domain_task(
                    query=task_query,
                    preferences=fused_preferences,
                    user_id=user_id,
                    domain=task_domain,
                    use_online_agent=use_online_agent,
                    tool_tags=task_tool_tags,
                    progress_callback=scoped_progress_callback,
                )

            if active_route.get("mode") == "multi_domain":
                ready_tasks = [
                    task for task in active_route.get("domain_tasks", [])
                    if isinstance(task, dict) and task.get("status") == "ready"
                ]
                skipped_tasks = [
                    task for task in active_route.get("domain_tasks", [])
                    if isinstance(task, dict) and task.get("status") != "ready"
                ]
                domain_results: List[Dict[str, Any]] = []
                restaurants: List[Restaurant] = []
                items: List[RecommendationItem] = []
                thinking_steps: List[ThinkingStep] = []
                for domain_task in ready_tasks:
                    domain_result = await execute_domain_task(domain_task)
                    restaurants.extend(domain_result.restaurants)
                    items.extend(domain_result.items)
                    if domain_result.thinking_steps:
                        thinking_steps.extend(domain_result.thinking_steps)
                    domain_results.append(
                        {
                            "domain": domain_task.get("domain"),
                            "status": "completed",
                            "restaurants_count": len(domain_result.restaurants),
                            "items_count": len(domain_result.items),
                            "metadata": domain_result.metadata or {},
                        }
                    )
                for domain_task in skipped_tasks:
                    domain_results.append(
                        {
                            "domain": domain_task.get("domain"),
                            "status": domain_task.get("status", "skipped"),
                            "restaurants_count": 0,
                            "items_count": 0,
                            "metadata": {"tool_tags": domain_task.get("tool_tags", [])},
                        }
                    )
                result = RecommendationResult(
                    restaurants=restaurants,
                    items=items,
                    thinking_steps=thinking_steps or [
                        ThinkingStep(
                            step="recommendation_result",
                            description="Finalizing recommendations...",
                            status="completed",
                            details="Multi-domain recommendations completed",
                        )
                    ],
                    confidence_score=0.85 if (restaurants or items) else 0.45,
                    metadata={
                        "query": query,
                        "user_id": user_id,
                        "timestamp": datetime.now().isoformat(),
                        "preferences": preferences,
                        "graph": "multi_domain_graph",
                        "domain": "multi_domain",
                        "routing": active_route,
                        "domain_results": domain_results,
                        "items_count": len(items),
                        "restaurants_count": len(restaurants),
                    },
                )
            elif active_route.get("mode") == "itinerary":
                itinerary_started_at = time.perf_counter()
                from langgraph_metarec import eta
                from langgraph_metarec.itinerary_candidates import (
                    apply_duration_enrichment,
                    apply_containment_enrichment,
                    apply_role_enrichment,
                    build_itinerary_gather_query,
                    containment_enrichment_input,
                    duration_enrichment_input,
                    normalize_candidates,
                    role_enrichment_input,
                )
                from langgraph_metarec.itinerary_contracts import (
                    PlanningProblem,
                    planning_request_from_dict,
                    planning_request_from_preferences,
                )
                from langgraph_metarec.itinerary_runtime import (
                    apply_transport_cost,
                    build_itinerary_block,
                    build_travel_matrix,
                    exceeds_time_window,
                    finalize_dynamic_metadata,
                    resolve_itinerary_legs,
                )
                from langgraph_metarec.itinerary_lodging import build_lodging_scenarios
                from langgraph_metarec.itinerary_policy import PACE_MAX_IDLE_GAP
                from langgraph_metarec.itinerary_retrieval import (
                    AdaptiveEvaluation,
                    AdaptiveItineraryPlanner,
                    RetrievalBudget,
                    RetrievalRequest,
                    domains_needing_retrieval,
                    evaluation_failure_codes,
                )
                from langgraph_metarec.itinerary_solver import MEAL_WINDOWS, build_solver
                from langgraph_metarec.itinerary_snapshot import build_planning_snapshot
                from langgraph_metarec.itinerary_sanity import (
                    REPAIRABLE_CODES,
                    apply_sanity_report,
                    validate_itinerary_block,
                )
                from llm_service import (
                    classify_itinerary_candidate_roles,
                    classify_itinerary_containment,
                    enrich_itinerary_durations,
                )

                async def emit_slot_progress(event: Dict[str, Any]) -> None:
                    if progress_callback is None:
                        return
                    maybe = progress_callback(event)
                    if hasattr(maybe, "__await__"):
                        await maybe

                request_payload = (active_route.get("metadata") or {}).get("planning_request")
                if isinstance(request_payload, dict):
                    planning_request = planning_request_from_dict(request_payload)
                else:
                    planning_request, planning_errors = planning_request_from_preferences(preferences or {})
                    if planning_request is None or planning_errors:
                        raise ValueError(f"Itinerary task is missing confirmed planning constraints: {planning_errors}")
                try:
                    provider_call_limit = max(1, min(24, int(os.getenv("ITINERARY_MAX_PROVIDER_CALLS", "8"))))
                except ValueError:
                    provider_call_limit = 8
                try:
                    retrieval_round_limit = max(1, min(3, int(os.getenv("ITINERARY_MAX_RETRIEVAL_ROUNDS", "2"))))
                except ValueError:
                    retrieval_round_limit = 2
                snapshot_revision = 0
                latest_planning_snapshot: Optional[Dict[str, Any]] = None
                previous_snapshot_candidate_ids: set[str] = set()

                async def emit_planning_snapshot(
                    phase: str,
                    *,
                    pool: Sequence[Any] = (),
                    itinerary_block: Optional[Dict[str, Any]] = None,
                    round_index: Optional[int] = None,
                    provider_calls: int = 0,
                    progress: int = 0,
                ) -> None:
                    nonlocal snapshot_revision, latest_planning_snapshot, previous_snapshot_candidate_ids
                    snapshot_revision += 1
                    current_ids = {str(candidate.id) for candidate in pool}
                    retired = sorted(previous_snapshot_candidate_ids - current_ids)
                    if current_ids:
                        previous_snapshot_candidate_ids = current_ids
                    latest_planning_snapshot = build_planning_snapshot(
                        revision=snapshot_revision,
                        phase=phase,
                        request=planning_request,
                        candidates=pool,
                        block=itinerary_block,
                        round_index=round_index,
                        retired_ids=retired,
                        provider_calls=provider_calls,
                        provider_call_limit=provider_call_limit,
                    )
                    await emit_slot_progress({
                        "stage": "itinerary_planning",
                        "message": f"Itinerary planning: {phase.replace('_', ' ')}",
                        "progress": progress,
                        "planning_snapshot": latest_planning_snapshot,
                    })

                await emit_planning_snapshot("constraints_ready", progress=5)
                gather_tasks = [
                    task for task in active_route.get("domain_tasks", [])
                    if isinstance(task, dict) and task.get("status") == "ready"
                ]
                seen_domains: set[str] = set()
                gather_tasks = [
                    task for task in gather_tasks
                    if not (str(task.get("domain")) in seen_domains or seen_domains.add(str(task.get("domain"))))
                ]
                restaurants: List[Restaurant] = []
                items: List[RecommendationItem] = []
                thinking_steps: List[ThinkingStep] = []
                raw_candidates: List[Dict[str, Any]] = []
                solver = build_solver(os.getenv("ITINERARY_SOLVER", "beam"))
                solver_runtime_ms = 0.0

                def run_solver(problem: PlanningProblem) -> Any:
                    nonlocal solver_runtime_ms
                    started_at = time.perf_counter()
                    try:
                        return solver.solve(problem)
                    finally:
                        solver_runtime_ms += (time.perf_counter() - started_at) * 1000

                async def build_candidate_pool(
                    raw_pool: List[Dict[str, Any]],
                ) -> Tuple[List[Any], Dict[str, Any]]:
                    diagnostics: Dict[str, Any] = {}
                    pool = normalize_candidates(raw_pool, planning_request, diagnostics=diagnostics)
                    role_rows = role_enrichment_input(pool)
                    role_payload = None
                    if role_rows:
                        role_payload = await classify_itinerary_candidate_roles(
                            self.async_client, candidates=role_rows, model=self.llm_model
                        )
                    pool = apply_role_enrichment(pool, role_payload, diagnostics=diagnostics)
                    containment_rows = containment_enrichment_input(pool)
                    containment_payload = None
                    if containment_rows:
                        containment_payload = await classify_itinerary_containment(
                            self.async_client, candidates=containment_rows, model=self.llm_model
                        )
                    pool = apply_containment_enrichment(pool, containment_payload, diagnostics=diagnostics)
                    enrichment_rows = duration_enrichment_input(pool)
                    if enrichment_rows:
                        enrichment = await enrich_itinerary_durations(
                            self.async_client, candidates=enrichment_rows, model=self.llm_model
                        )
                        pool = apply_duration_enrichment(pool, enrichment)
                    return pool, diagnostics

                async def solve_and_validate(
                    pool: List[Any],
                    diagnostics: Dict[str, Any],
                    *,
                    resolve_eta: bool = True,
                ) -> Tuple[Any, Dict[str, Any]]:
                    lodging_scenarios = build_lodging_scenarios(pool, planning_request)
                    activity_pool = [candidate for candidate in pool if candidate.role != "lodging"]
                    travel_matrix = build_travel_matrix(
                        activity_pool,
                        planning_request,
                        lodging_scenarios,
                    )
                    solved = run_solver(PlanningProblem(
                        planning_request,
                        tuple(activity_pool),
                        travel_matrix,
                        tuple(lodging_scenarios),
                    ))
                    itinerary_block = build_itinerary_block(planning_request, solved, activity_pool)
                    eta_repair_count = 0
                    while resolve_eta and itinerary_block.get("legs") and eta_repair_count <= 2:
                        itinerary_block = await resolve_itinerary_legs(itinerary_block, eta.resolve_leg)
                        if not exceeds_time_window(itinerary_block, planning_request):
                            break
                        eta_repair_count += 1
                        if eta_repair_count > 2:
                            break
                        for leg in itinerary_block.get("legs") or []:
                            def matrix_node(side: str) -> str:
                                anchor_name = leg.get(f"{side}_anchor")
                                if anchor_name == "lodging" and solved.lodging:
                                    return f"lodging:{solved.lodging.get('candidate_id')}"
                                if anchor_name in {"start", "end"}:
                                    return f"anchor:{anchor_name}"
                                return str(leg.get(f"{side}_id"))

                            from_id, to_id = matrix_node("from"), matrix_node("to")
                            if from_id in travel_matrix and to_id in travel_matrix[from_id]:
                                travel_matrix[from_id][to_id] = int(
                                    leg.get("duration_min") or travel_matrix[from_id][to_id]
                                )
                        solved = run_solver(PlanningProblem(
                            planning_request,
                            tuple(activity_pool),
                            travel_matrix,
                            tuple(lodging_scenarios),
                        ))
                        itinerary_block = build_itinerary_block(planning_request, solved, activity_pool)
                    itinerary_block.setdefault("solver", {})["repair_count"] = eta_repair_count
                    itinerary_block["solver"]["candidate_count"] = len(pool)
                    itinerary_block["solver"]["candidate_diagnostics"] = diagnostics
                    # Ownership contract (order matters): finalize_dynamic_metadata
                    # produces the authoritative totals + validation after the ETA
                    # schedule; apply_transport_cost and apply_sanity_report are
                    # pure overlays that only add/downgrade, never recompute totals.
                    finalize_dynamic_metadata(itinerary_block, planning_request, solved)
                    apply_transport_cost(itinerary_block)
                    apply_sanity_report(
                        itinerary_block,
                        validate_itinerary_block(itinerary_block, planning_request),
                    )
                    return solved, itinerary_block

                task_by_domain = {
                    str(task.get("domain") or "attraction"): task for task in gather_tasks
                }
                retrieval_diagnostics: Dict[str, Any] = {}
                retrieval_fetch_count = 0

                def request_anchor() -> Tuple[Optional[float], Optional[float]]:
                    for key in ("start", "lodging"):
                        anchor = planning_request.anchors.get(key)
                        try:
                            return float(anchor.latitude), float(anchor.longitude)
                        except (AttributeError, TypeError, ValueError):
                            continue
                    try:
                        return float(planning_request.location.latitude), float(planning_request.location.longitude)
                    except (TypeError, ValueError):
                        return None, None

                def retrieval_radius(*, adaptive: bool) -> int:
                    pace = str(planning_request.soft_preferences.get("pace") or "balanced")
                    dense_tokens = {"singapore", "sentosa", "downtown", "city", "central"}
                    location_text = (
                        planning_request.location.resolved_name or planning_request.location.query
                    ).casefold()
                    dense = any(token in location_text for token in dense_tokens)
                    base = {"relaxed": 2200, "balanced": 3000, "packed": 4000}.get(pace, 3000)
                    if not dense:
                        base = int(base * 1.75)
                    return min(15000, int(base * (1.6 if adaptive else 1.0)))

                days_by_index = {day.day_index: day for day in planning_request.days}

                def day_for_index(index: Optional[int]) -> Any:
                    """Resolve the DayConstraint a retrieval belongs to. Multi-day
                    trips carry per-day dates (and, in future, per-day windows), so
                    an adaptive fetch anchored on a day-2 stop must gather against
                    day 2 — not day 0."""
                    if index is None:
                        return planning_request.days[0]
                    return days_by_index.get(int(index), planning_request.days[0])

                def make_retrieval_request(
                    domain: str,
                    *,
                    anchor: Tuple[Optional[float], Optional[float]],
                    adaptive: bool,
                    reason: str,
                    exclusions: Sequence[str] = (),
                    radius_override: Optional[int] = None,
                    day: Optional[Any] = None,
                ) -> RetrievalRequest:
                    day = day if day is not None else planning_request.days[0]
                    radius = int(radius_override) if radius_override else retrieval_radius(adaptive=adaptive)
                    effective_exclusions = {
                        str(value) for value in (
                            *(planning_request.hard_constraints.get("exclude") or ()),
                            *exclusions,
                        ) if str(value).strip()
                    }
                    signature = json.dumps(
                        {
                            "reason": reason,
                            "location": planning_request.location.resolved_name or planning_request.location.query,
                            "date": day.date,
                            "pace": planning_request.soft_preferences.get("pace"),
                            "style": planning_request.soft_preferences.get("style"),
                            "radius": radius,
                            "exclusions": sorted(effective_exclusions),
                        },
                        sort_keys=True,
                        ensure_ascii=True,
                    )
                    return RetrievalRequest(
                        task_id=task_id,
                        tool=f"domain_graph:{domain}",
                        domain=domain,
                        role={"restaurant": "food", "hotel": "lodging"}.get(domain, "experience"),
                        anchor_lat=anchor[0],
                        anchor_lng=anchor[1],
                        radius_meters=radius,
                        constraint_signature=signature,
                        day_index=day.day_index,
                        start_min=day.start_min,
                        end_min=day.end_min,
                        exclusions=tuple(sorted(effective_exclusions)),
                    )

                MEAL_ADJACENT_RADIUS_METERS = 1200

                def meal_adjacent_anchors(
                    evaluation: AdaptiveEvaluation,
                ) -> List[Tuple[str, Tuple[float, float], int]]:
                    """Anchor each required/suggested meal at the attraction the
                    traveller is actually near during that meal window, so restaurant
                    retrieval clusters with the sightseeing instead of the trip's
                    start/hotel. Returns (meal_label, (lat, lng), day_index) triples,
                    de-duplicated by attraction so two meals at one stop share a single
                    fetch; day_index scopes the re-fetch to that stop's day."""
                    meals: List[Tuple[int, str]] = []
                    for value in (
                        *(planning_request.hard_constraints.get("meal_obligations") or ()),
                        *(planning_request.soft_preferences.get("suggested_meals") or ()),
                    ):
                        name = str(value.get("meal") if isinstance(value, dict) else value).strip().lower()
                        try:
                            day_index = int(value.get("day_index") or 0) if isinstance(value, dict) else 0
                        except (TypeError, ValueError):
                            day_index = 0
                        if name in MEAL_WINDOWS and day_index in days_by_index:
                            meals.append((day_index, name))
                    if not meals:
                        return []
                    payload = evaluation.payload if isinstance(evaluation.payload, dict) else {}
                    scheduled: List[Tuple[int, Tuple[float, float], int]] = []
                    for slot in payload.get("slots") or []:
                        if str(slot.get("domain")) != "attraction":
                            continue
                        chosen = slot.get("chosen") if isinstance(slot.get("chosen"), dict) else {}
                        try:
                            latlng = (float(chosen["lat"]), float(chosen["lng"]))
                            hour, minute = (
                                int(part)
                                for part in str(slot.get("time") or slot.get("preferred_time")).split(":", 1)
                            )
                        except (KeyError, TypeError, ValueError):
                            continue
                        scheduled.append((hour * 60 + minute, latlng, int(slot.get("day_index") or 0)))
                    if not scheduled:
                        return []
                    anchors: Dict[Tuple[int, float, float], Tuple[Tuple[float, float], int, List[str]]] = {}
                    for day_index, meal in dict.fromkeys(meals):
                        window_start, window_end = MEAL_WINDOWS[meal]
                        center = (window_start + window_end) // 2
                        day_schedule = [row for row in scheduled if row[2] == day_index]
                        if not day_schedule:
                            continue
                        _minute, latlng, _scheduled_day = min(
                            day_schedule, key=lambda row: abs(row[0] - center)
                        )
                        bucket = (day_index, round(latlng[0], 3), round(latlng[1], 3))
                        if bucket not in anchors:
                            anchors[bucket] = (latlng, day_index, [])
                        anchors[bucket][2].append(meal)
                    return [
                        (",".join(meal_names), latlng, day_index)
                        for latlng, day_index, meal_names in anchors.values()
                    ]

                MEAL_ADJACENT_MAX_KM = 1.5

                def restaurant_far_from_experiences(evaluation: AdaptiveEvaluation) -> bool:
                    """True when a meal window has no chosen restaurant near the
                    attraction the traveller is at during it — the exact 'attractions
                    here, restaurants far away' trap. Drives a tight meal-adjacent
                    restaurant re-fetch even when a distant restaurant already
                    satisfies the meal."""
                    meal_anchors = meal_adjacent_anchors(evaluation)
                    if not meal_anchors:
                        return False
                    payload = evaluation.payload if isinstance(evaluation.payload, dict) else {}
                    restaurant_points: Dict[int, List[Tuple[float, float]]] = {}
                    for slot in payload.get("slots") or []:
                        if str(slot.get("domain")) != "restaurant":
                            continue
                        chosen = slot.get("chosen") if isinstance(slot.get("chosen"), dict) else {}
                        try:
                            day_index = int(slot.get("day_index") or 0)
                            restaurant_points.setdefault(day_index, []).append(
                                (float(chosen["lat"]), float(chosen["lng"]))
                            )
                        except (KeyError, TypeError, ValueError):
                            continue
                    for _label, anchor_point, day_index in meal_anchors:
                        nearest = min(
                            (
                                eta.haversine_km(anchor_point, point)
                                for point in restaurant_points.get(day_index, ())
                            ),
                            default=None,
                        )
                        if nearest is None or nearest > MEAL_ADJACENT_MAX_KM:
                            return True
                    return False

                async def fetch_retrieval(request: RetrievalRequest) -> Sequence[Any]:
                    nonlocal retrieval_fetch_count
                    await emit_slot_progress({
                        "stage": f"gather_{request.domain}",
                        "message": f"Finding {request.domain} options near the current route...",
                        "progress": min(75, 10 + 12 * (1 + len(retrieval_diagnostics))),
                        "anchor": (
                            {"lat": request.anchor_lat, "lng": request.anchor_lng}
                            if request.anchor_lat is not None and request.anchor_lng is not None else None
                        ),
                        "radius_meters": request.radius_meters,
                    })
                    task = task_by_domain.get(request.domain, {
                        "domain": request.domain,
                        "status": "ready",
                        "tool_tags": ["#place", f"#{request.domain}"],
                    })
                    query_text = build_itinerary_gather_query(planning_request, request.domain)
                    slot_preferences: Dict[str, Any] = {
                        "location": planning_request.location.resolved_name or planning_request.location.query,
                        "radius_meters": request.radius_meters,
                        "date": planning_request.days[request.day_index].date,
                        "start_min": request.start_min,
                        "end_min": request.end_min,
                        "exclusions": list(request.exclusions),
                        "attraction_types": list(
                            planning_request.soft_preferences.get("attraction_types") or []
                        ),
                        "interest_terms": list(
                            planning_request.soft_preferences.get("interest_terms") or []
                        ),
                    }
                    if request.anchor_lat is not None and request.anchor_lng is not None:
                        slot_preferences.update({
                            "anchor_lat": request.anchor_lat,
                            "anchor_lng": request.anchor_lng,
                        })
                    domain_result = await execute_domain_task({
                        **task,
                        "query": query_text,
                        "slot_preferences": slot_preferences,
                    })
                    domain_restaurants = domain_result.restaurants[:12]
                    domain_items = domain_result.items[:12]
                    # Spatial gate (anchored restaurant fetches only): gmap.search is
                    # anchored, but yelp/xhs are geo-blind and the graph's summarizer
                    # ranks by quality, not proximity — so a meal-adjacent fetch can
                    # still surface restaurants across the city. Scope them to the
                    # request radius here so the solver only ever sees near options,
                    # regardless of which provider produced each candidate.
                    if (
                        request.domain == "restaurant"
                        and request.anchor_lat is not None
                        and request.anchor_lng is not None
                    ):
                        domain_restaurants = self._spatially_scope_candidates(
                            domain_restaurants,
                            (request.anchor_lat, request.anchor_lng),
                            request.radius_meters,
                            coord_of=lambda rec: self._restaurant_coordinates(rec.model_dump()),
                        )
                    restaurants.extend(domain_restaurants)
                    items.extend(domain_items)
                    batch = [rec.model_dump() for rec in domain_restaurants]
                    batch.extend(item.model_dump() for item in domain_items)
                    raw_candidates.extend(batch)
                    if domain_result.thinking_steps:
                        thinking_steps.extend(domain_result.thinking_steps)
                    pool, diagnostics = await build_candidate_pool(batch)
                    retrieval_fetch_count += 1
                    retrieval_diagnostics[request.constraint_signature] = diagnostics
                    try:
                        retrieval_reason = str(json.loads(request.constraint_signature).get("reason") or "seed")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        retrieval_reason = "seed"
                    adaptive_round = retrieval_reason != "seed"
                    await emit_planning_snapshot(
                        "adaptive_retrieval" if adaptive_round else "seed_retrieval",
                        pool=pool,
                        round_index=2 if adaptive_round else 1,
                        provider_calls=retrieval_fetch_count,
                        progress=65 if adaptive_round else 35,
                    )
                    return pool

                async def evaluate_retrieval(pool: Sequence[Any]) -> AdaptiveEvaluation:
                    solved, preview = await solve_and_validate(
                        list(pool), {"retrievals": retrieval_diagnostics}, resolve_eta=False
                    )
                    components = dict((solved.diagnostics or {}).get("objective_components") or {})
                    await emit_planning_snapshot(
                        "provisional_solve",
                        pool=pool,
                        itinerary_block=preview,
                        provider_calls=retrieval_fetch_count,
                        progress=78,
                    )
                    return AdaptiveEvaluation(
                        status=solved.status,
                        selected_ids=tuple(
                            str(activity.get("candidate_id")) for activity in solved.activities
                        ),
                        utility=float((solved.diagnostics or {}).get("utility") or 0.0),
                        diagnostics={
                            **components,
                            "uncertainty_count": len(solved.uncertainties),
                            "unsatisfied_constraints": list(solved.unsatisfied_constraints),
                            "sanity_warnings": list(
                                (preview.get("sanity") or {}).get("warnings") or []
                            ),
                            # Violations, not just warnings: meal_obligation is
                            # raised as a violation, and it is the only code that
                            # triggers a restaurant re-fetch in derive_retrievals.
                            # Passing warnings alone left that trigger unreachable.
                            "sanity_violations": list(
                                (preview.get("sanity") or {}).get("violations") or []
                            ),
                        },
                        payload=preview,
                    )

                def derive_retrievals(
                    evaluation: AdaptiveEvaluation,
                    pool: Sequence[Any],
                ) -> Sequence[RetrievalRequest]:
                    codes = evaluation_failure_codes(evaluation.diagnostics)
                    needs: set[str] = set(
                        domains_needing_retrieval(
                            codes,
                            task_by_domain,
                            infeasible=evaluation.status == "infeasible",
                        )
                    )
                    # Restaurants seeded around the trip start/hotel can land far from
                    # the day's attractions; pull a tight meal-adjacent set when they do.
                    if "restaurant" in task_by_domain and restaurant_far_from_experiences(evaluation):
                        needs.add("restaurant")
                    pace = str(planning_request.soft_preferences.get("pace") or "balanced")
                    gap_limit = PACE_MAX_IDLE_GAP.get(pace, PACE_MAX_IDLE_GAP["balanced"])
                    if int(evaluation.diagnostics.get("unallocated_min") or 0) > gap_limit:
                        if "attraction" in task_by_domain:
                            needs.add("attraction")
                    if int(evaluation.diagnostics.get("max_idle_gap_min") or 0) > gap_limit:
                        if "attraction" in task_by_domain:
                            needs.add("attraction")
                    # Whole-trip sum: travel_wait_min spans every day on the
                    # multi-day path, so comparing it against a single day's
                    # window over-triggered re-fetches on every multi-day trip.
                    window_min = max(
                        1,
                        sum(day.end_min - day.start_min for day in planning_request.days),
                    )
                    if int(evaluation.diagnostics.get("travel_wait_min") or 0) > int(window_min * 0.35):
                        needs.update(domain for domain in ("attraction", "restaurant") if domain in task_by_domain)
                    if not needs:
                        return ()
                    by_id = {candidate.id: candidate for candidate in pool}
                    # Slots are built in the same order as selected_ids (both walk the
                    # solver's activities), so this pairs each chosen stop with the day
                    # it was scheduled on — letting an adaptive fetch inherit its day.
                    payload = evaluation.payload if isinstance(evaluation.payload, dict) else {}
                    day_by_selected = {
                        sid: int(slot.get("day_index") or 0)
                        for sid, slot in zip(evaluation.selected_ids, payload.get("slots") or [])
                    }
                    endpoint_id = evaluation.selected_ids[-1] if evaluation.selected_ids else None
                    endpoint = by_id.get(endpoint_id) if endpoint_id else None
                    if endpoint is not None:
                        anchor = (float(endpoint.latitude), float(endpoint.longitude))
                    else:
                        anchor = request_anchor()
                    endpoint_day = day_for_index(day_by_selected.get(endpoint_id))
                    reason = "frontier_gap:" + ",".join(sorted(codes or {"underfilled"}))
                    requests: List[RetrievalRequest] = []
                    for domain in sorted(needs):
                        if domain == "hotel":
                            continue
                        if domain == "restaurant":
                            meal_anchors = meal_adjacent_anchors(evaluation)
                            if meal_anchors:
                                requests.extend(
                                    make_retrieval_request(
                                        "restaurant",
                                        anchor=latlng,
                                        adaptive=True,
                                        reason=f"meal_adjacent:{meal_label}",
                                        exclusions=evaluation.selected_ids,
                                        radius_override=MEAL_ADJACENT_RADIUS_METERS,
                                        day=day_for_index(day_index),
                                    )
                                    for meal_label, latlng, day_index in meal_anchors
                                )
                                continue
                        requests.append(
                            make_retrieval_request(
                                domain,
                                anchor=anchor,
                                adaptive=True,
                                reason=reason,
                                exclusions=evaluation.selected_ids,
                                day=endpoint_day,
                            )
                        )
                    return tuple(requests)

                seed_anchor = request_anchor()
                seed_requests = tuple(
                    make_retrieval_request(
                        domain,
                        anchor=seed_anchor,
                        adaptive=False,
                        reason="seed",
                    )
                    for domain in task_by_domain
                )
                adaptive_planner = AdaptiveItineraryPlanner(
                    task_id,
                    fetch=fetch_retrieval,
                    evaluate=evaluate_retrieval,
                    derive_requests=derive_retrievals,
                    budget=RetrievalBudget(
                        max_provider_calls=provider_call_limit,
                        max_rounds=retrieval_round_limit,
                    ),
                )
                adaptive_result = await adaptive_planner.run(seed_requests)
                candidates = list(adaptive_result.candidates)
                candidate_diagnostics = {"retrievals": retrieval_diagnostics}
                await emit_slot_progress(
                    {"stage": "solve_itinerary", "message": "Solving the itinerary...", "progress": 82}
                )
                solver_result, block = await solve_and_validate(candidates, candidate_diagnostics)
                await emit_planning_snapshot(
                    "real_eta",
                    pool=candidates,
                    itinerary_block=block,
                    provider_calls=adaptive_result.provider_calls,
                    progress=88,
                )
                block["retrieval"] = {
                    "schema_version": "itinerary-retrieval/v1",
                    "rounds": list(adaptive_result.rounds),
                    "provider_calls": adaptive_result.provider_calls,
                    "provider_call_limit": provider_call_limit,
                    "round_limit": retrieval_round_limit,
                    "stop_reason": adaptive_result.stop_reason,
                    "task_scoped": True,
                }

                # Hard constraint failures live on the solver result, not the sanity
                # report: sanity only inspects the assembled block, so when the solver
                # aborts on an unresolved must-visit it sees an empty plan and reports
                # the downstream meal gaps instead. Fold the solver's own repairable
                # codes in, or must_visit_unavailable never triggers a repair at all.
                solver_failures = [
                    item for item in (solver_result.unsatisfied_constraints or ())
                    if isinstance(item, dict)
                ]
                unresolved_must_visit = [
                    str(item.get("value") or "").strip()
                    for item in solver_failures
                    if item.get("code") == "must_visit_unavailable"
                    and str(item.get("value") or "").strip()
                ]
                repair_metadata: Dict[str, Any] = {
                    "attempt_count": 0,
                    "initial_codes": sorted({
                        *((block.get("sanity") or {}).get("repairable_codes") or []),
                        *(
                            str(item.get("code") or "") for item in solver_failures
                            if str(item.get("code") or "") in REPAIRABLE_CODES
                        ),
                    }),
                    "candidate_count_before": len(candidates),
                    "provider_calls": adaptive_result.provider_calls,
                    "unresolved_must_visit": unresolved_must_visit,
                }
                repairable_codes = repair_metadata["initial_codes"]
                if (
                    repairable_codes
                    and repair_metadata["provider_calls"] < provider_call_limit
                ):
                    from langgraph_metarec.itinerary_repair import parse_repair_directive
                    from llm_service import propose_itinerary_repair

                    repair_started = time.perf_counter()
                    repair_metadata["attempt_count"] = 1
                    raw_directive = await propose_itinerary_repair(
                        self.async_client,
                        report=block.get("sanity") or {},
                        candidate_diagnostics=candidate_diagnostics,
                        style=str(planning_request.soft_preferences.get("style") or "sightseeing"),
                        immutable_constraints=planning_request.to_dict(),
                        unresolved_must_visit=unresolved_must_visit,
                        model=self.llm_model,
                    )
                    directive = parse_repair_directive(raw_directive)
                    repair_metadata["directive_accepted"] = directive is not None
                    if directive is not None:
                        repair_metadata["directive"] = directive.to_dict()
                        affected = set(directive.domain_queries)
                        raw_candidates = [
                            candidate for candidate in raw_candidates
                            if str(candidate.get("domain") or "") not in affected
                        ]
                        for domain in sorted(affected):
                            if repair_metadata["provider_calls"] >= provider_call_limit:
                                break
                            task = next(
                                (item for item in gather_tasks if str(item.get("domain")) == domain),
                                {
                                    "domain": domain,
                                    "status": "ready",
                                    "tool_tags": ["#place", f"#{domain}"],
                                },
                            )
                            domain_result = await execute_domain_task({
                                **task,
                                "query": directive.domain_queries[domain],
                                "slot_preferences": {
                                    "required_roles": list(directive.required_roles),
                                    "excluded_types": list(directive.excluded_types),
                                    "provider_hints": list(directive.provider_hints.get(domain, ())),
                                },
                            })
                            repair_metadata["provider_calls"] += 1
                            domain_restaurants = domain_result.restaurants[:12]
                            domain_items = domain_result.items[:12]
                            restaurants.extend(domain_restaurants)
                            items.extend(domain_items)
                            raw_candidates.extend(item.model_dump() for item in domain_restaurants)
                            raw_candidates.extend(item.model_dump() for item in domain_items)
                        candidates, candidate_diagnostics = await build_candidate_pool(raw_candidates)
                        solver_result, block = await solve_and_validate(candidates, candidate_diagnostics)
                        await emit_planning_snapshot(
                            "repair",
                            pool=candidates,
                            itinerary_block=block,
                            provider_calls=repair_metadata["provider_calls"],
                            progress=94,
                        )
                    repair_metadata["latency_ms"] = round((time.perf_counter() - repair_started) * 1000, 2)
                    repair_metadata["candidate_count_after"] = len(candidates)
                    repair_metadata["success"] = (
                        solver_result.status != "infeasible"
                        and (block.get("sanity") or {}).get("status") == "valid"
                    )
                    repair_metadata["remaining_warnings"] = list(
                        (block.get("sanity") or {}).get("warnings") or []
                    )[:8]
                    if (block.get("sanity") or {}).get("status") == "invalid":
                        block["planning_status"] = "needs_refinement"
                        block["suppress_normal_presentation"] = True
                        block["refinement"] = {
                            "reasons": list((block.get("sanity") or {}).get("violations") or [])[:8],
                            "suggested_fields": ["style", "pace", "attraction_types"],
                        }
                hard_failure_reasons = [
                    *(solver_result.unsatisfied_constraints or ()),
                    *((block.get("sanity") or {}).get("violations") or ()),
                ]
                if solver_result.status == "infeasible" or hard_failure_reasons:
                    hard_failure_codes = {
                        str(reason.get("code") or "")
                        for reason in hard_failure_reasons if isinstance(reason, dict)
                    }
                    suggested_fields = (
                        ["style", "pace", "attraction_types"]
                        if hard_failure_codes <= {
                            "", "no_feasible_route", "no_feasible_multi_day_route",
                            "must_visit_unavailable",
                        }
                        else [
                            "location", "date", "daily_start_time",
                            "daily_end_time", "budget_amount",
                        ]
                    )
                    block["planning_status"] = "needs_refinement"
                    block["suppress_normal_presentation"] = True
                    block["refinement"] = {
                        "reasons": list(hard_failure_reasons)[:8]
                        or [{"code": "no_feasible_route"}],
                        "suggested_fields": suggested_fields,
                    }
                repair_metadata["added_provider_calls"] = max(
                    0,
                    int(repair_metadata.get("provider_calls") or 0)
                    - adaptive_result.provider_calls,
                )
                block["repair"] = repair_metadata
                block.setdefault("solver", {})["runtime_ms"] = round(solver_runtime_ms, 3)
                block["runtime_ms"] = round((time.perf_counter() - itinerary_started_at) * 1000, 3)
                from langgraph_metarec.itinerary_evaluation import evaluate_itinerary
                block["evaluation"] = evaluate_itinerary(block)
                await emit_planning_snapshot(
                    "finalization",
                    pool=candidates,
                    itinerary_block=block,
                    provider_calls=repair_metadata["provider_calls"],
                    progress=98,
                )
                has_stops = any(slot.get("chosen") for slot in block.get("slots", []))
                planning_status = block.get("planning_status")
                selected_lodging_id = str((block.get("lodging") or {}).get("candidate_id") or "")
                if selected_lodging_id:
                    items = [
                        item for item in items
                        if item.domain != "hotel" or item.id == selected_lodging_id
                    ]
                result = RecommendationResult(
                    restaurants=restaurants,
                    items=items,
                    thinking_steps=[
                        step.model_copy(update={
                            "description": "Information gathered for itinerary planning."
                        })
                        if step.description == "Recommendations ready!" else step
                        for step in thinking_steps
                    ]
                    or [
                        ThinkingStep(
                            step="recommendation_result",
                            description="Finalizing recommendations...",
                            status="completed",
                            details="Itinerary composed",
                        )
                    ],
                    confidence_score=0.85 if planning_status == "feasible" else (0.6 if has_stops else 0.35),
                    metadata={
                        "query": query,
                        "user_id": user_id,
                        "timestamp": datetime.now().isoformat(),
                        "preferences": preferences,
                        "graph": "itinerary_graph",
                        "domain": "itinerary",
                        "routing": active_route,
                        "itinerary": block,
                        "itinerary_revision": block.get("revision", 1),
                        "planning_snapshot": latest_planning_snapshot,
                        "items_count": len(items),
                        "restaurants_count": len(restaurants),
                    },
                )
            else:
                domain_task = {
                    "domain": active_route.get("execution_domain") or active_route.get("domain") or "restaurant",
                    "status": active_route.get("status", "ready"),
                    "tool_tags": active_route.get("tool_tags") or tool_tags,
                }
                result = await execute_domain_task(domain_task)
                if result.metadata is not None:
                    result.metadata["routing"] = active_route
            result_payload = result.model_dump()
            metadata = result.metadata or {}
            return {
                "result_object": result,
                "result_payload": result_payload,
                "domain_graph_result": DomainGraphResult(
                    domain=metadata.get("domain", "restaurant"),
                    status="completed",
                    result=result_payload,
                    metadata=metadata,
                ),
                "metadata": metadata,
            }

        await run_task_graph(
            adapters=TaskGraphAdapters(
                run_domain_graph=run_domain,
                write_projection=write_projection,
            ),
            user_id=user_id,
            conversation_id=session_id,
            branch_id=branch_id,
            task_id=task_id,
            query=query,
            checkpointer=await self.runtime_checkpointer.aget(),
        )
    
    async def process_recommendation_task(
        self,
        task_id: str,
        query: str,
        preferences: Dict[str, Any],
        user_id: str = "default",
        session_id: Optional[str] = None,
        use_online_agent: bool = False,
        tool_tags: Optional[List[str]] = None,
        route: Optional[Dict[str, Any]] = None,
    ):
        """
        后台处理推荐任务（使用 agent 执行器）
        
        Args:
            task_id: 任务ID
            query: 用户查询
            preferences: 偏好设置
            user_id: 用户ID
            use_online_agent: 是否使用在线 agent（True=在线，False=离线）
        """
        return await self.run_recommendation_task_graph(
            task_id,
            query,
            preferences,
            user_id,
            session_id,
            use_online_agent,
            tool_tags,
            route=route,
        )

    def _preferences_to_agent_input(self, query: str, preferences: Dict[str, Any]) -> str:
        """
        将 preferences 转换为 agent 需要的输入格式
        
        Args:
            query: 原始查询
            preferences: 偏好设置
            
        Returns:
            agent 输入字符串（JSON 格式）
        """
        # 构建结构化输入
        input_dict = {}
        
        # 餐厅类型
        restaurant_types = preferences.get("restaurant_types", ["any"])
        if restaurant_types and restaurant_types != ["any"]:
            type_mapping = {
                "casual": "Casual Dining",
                "fine-dining": "Fine Dining",
                "fast-casual": "Fast Casual",
                "street-food": "Street Food",
                "buffet": "Buffet",
                "cafe": "Cafe"
            }
            input_dict["Restaurant Type"] = ", ".join([
                type_mapping.get(t, t.title()) for t in restaurant_types
            ])
        else:
            input_dict["Restaurant Type"] = "Restaurant"
        
        # 口味偏好
        flavor_profiles = preferences.get("flavor_profiles", ["any"])
        if flavor_profiles and flavor_profiles != ["any"]:
            flavor_mapping = {
                "spicy": "Spicy",
                "savory": "Savory",
                "sweet": "Sweet",
                "sour": "Sour",
                "mild": "Mild"
            }
            input_dict["Flavor Profile"] = ", ".join([
                flavor_mapping.get(f, f.title()) for f in flavor_profiles
            ])
        else:
            input_dict["Flavor Profile"] = "Any"
        
        # 用餐目的
        dining_purpose = preferences.get("dining_purpose", "any")
        if dining_purpose != "any":
            purpose_mapping = {
                "date-night": "Date Night",
                "family": "Family",
                "business": "Business",
                "solo": "Solo",
                "friends": "Friends",
                "celebration": "Celebration"
            }
            input_dict["Dining Purpose"] = purpose_mapping.get(dining_purpose, dining_purpose.title())
        else:
            input_dict["Dining Purpose"] = "Any"
        
        # 预算范围
        budget_range = preferences.get("budget_range", {})
        if budget_range:
            min_budget = budget_range.get("min")
            max_budget = budget_range.get("max")
            if min_budget and max_budget:
                input_dict["Budget Range (per person)"] = f"{min_budget} to {max_budget} (SGD)"
            elif min_budget:
                input_dict["Budget Range (per person)"] = f"{min_budget}+ (SGD)"
            elif max_budget:
                input_dict["Budget Range (per person)"] = f"up to {max_budget} (SGD)"
        
        # 位置
        location = preferences.get("location", "any")
        if location and location != "any":
            input_dict["Location (Singapore)"] = location
        else:
            input_dict["Location (Singapore)"] = "Singapore"
        
        # 显式菜系/菜品意图是主收窄条件：作为首要检索主体喂给规划器/汇总器。
        # 仅当用户未命名具体食物时，才回退到从原始查询里粗提菜系。
        food_intent = preferences.get("food_intent")
        if is_meaningful_food_intent(food_intent):
            cuisines = [str(c).title() for c in (food_intent.get("cuisines") or [])]
            dishes = [str(d).title() for d in (food_intent.get("dishes") or [])]
            if cuisines:
                input_dict["Cuisine"] = ", ".join(cuisines)
            if dishes:
                input_dict["Dish"] = ", ".join(dishes)
            # 组合一个明确的检索主体（菜品优先，其次菜系），让在线搜索直接命中
            subject = ", ".join(dishes + cuisines)
            input_dict["Food Type"] = f"{subject} (must match this cuisine/dish)"
        else:
            query_lower = query.lower()
            cuisine_keywords = {
                "chinese": "Chinese food",
                "sichuan": "Sichuan food",
                "japanese": "Japanese food",
                "korean": "Korean food",
                "thai": "Thai food",
                "indian": "Indian food",
                "italian": "Italian food",
                "french": "French food",
                "western": "Western food"
            }
            for keyword, food_type in cuisine_keywords.items():
                if keyword in query_lower:
                    input_dict["Food Type"] = food_type
                    break

        # 转换为 JSON 字符串
        return json.dumps(input_dict, ensure_ascii=False, indent=2)
    
    async def create_task_async(
        self,
        query: str,
        preferences: Dict[str, Any],
        user_id: str = "default",
        session_id: Optional[str] = None,
        use_online_agent: bool = False,
        tool_tags: Optional[List[str]] = None,
        branch_id: Optional[str] = None,
        route: Optional[Dict[str, Any]] = None,
    ) -> str:
        task_id = new_uuid()
        conversation_deleted = False
        if session_id and self.task_repository is not None:
            try:
                from business_repositories import conversation_repository

                conversation_deleted = not await conversation_repository.is_conversation_active(user_id, session_id)
            except Exception:
                import logging

                logging.getLogger(__name__).debug(
                    "Could not verify conversation activity before task creation",
                    exc_info=True,
                )
        # Persist the preferences this recommendation runs on back to the
        # conversation so they become the baseline for the next turn — this is what
        # lets a later "make it cheaper / somewhere closer" refine the prior request
        # instead of reverting to the profile/default baseline.
        if session_id and preferences and not conversation_deleted:
            try:
                from business_repositories import conversation_repository
                await conversation_repository.update_conversation_preferences(
                    user_id, session_id, preferences
                )
            except Exception:
                import logging
                logging.getLogger(__name__).debug(
                    "Could not persist task preferences to conversation", exc_info=True
                )
        status = {
            "task_id": task_id,
            "status": "pending",
            "progress": 0,
            "message": "Task created",
            "result": None,
            "error": None,
            "user_id": user_id,
            "conversation_id": session_id or "default",
            "metadata": {
                "branch_id": branch_id,
                "routing": route,
                "domain": route.get("domain") if isinstance(route, dict) else None,
            },
        }
        if conversation_deleted:
            status = self._cancelled_task_status(task_id, user_id, session_id, status)
            status["message"] = "Conversation deleted; recommendation task was not started."
            if self.task_repository is not None:
                await self.task_repository.save(user_id, session_id, task_id, status)
            else:
                session_ctx = self._get_session_context(user_id, session_id)
                session_ctx["tasks"][task_id] = status
            return task_id
        if self.task_repository is not None:
            await self.task_repository.save(user_id, session_id, task_id, status)
        else:
            session_ctx = self._get_session_context(user_id, session_id)
            session_ctx["tasks"][task_id] = status

        task = asyncio.create_task(
            self._run_scoped_task(
                task_id,
                query,
                preferences,
                user_id,
                session_id,
                use_online_agent,
                tool_tags,
                branch_id,
                route,
            )
        )
        self._running_tasks[task_id] = task
        self._running_task_scopes[task_id] = (user_id, session_id)

        def _forget_running_task(done_task: asyncio.Task[Any], *, completed_task_id: str = task_id) -> None:
            self._running_tasks.pop(completed_task_id, None)
            self._running_task_scopes.pop(completed_task_id, None)
            try:
                exc = done_task.exception()
            except asyncio.CancelledError:
                return
            if exc is not None:
                import logging

                logging.getLogger(__name__).debug(
                    "Background recommendation task finished with an exception",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        task.add_done_callback(_forget_running_task)
        return task_id
    
    def get_task_status(self, task_id: str, user_id: Optional[str] = None, session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        获取任务状态（进程内投影；Postgres 持久化路径请用 get_task_status_async）

        Args:
            task_id: 任务ID
            user_id: 用户ID（可选，如果提供则只在指定session中查找）
            session_id: 会话ID（可选）

        Returns:
            任务状态字典，如果任务不存在返回None
        """
        if user_id is None or session_id is None:
            return None
        # Read-only: never allocate a session context for an unknown scope.
        session_ctx = self.session_contexts.get(self._get_session_key(user_id, session_id))
        return (session_ctx or {}).get("tasks", {}).get(task_id)

    async def get_task_status_async(
        self,
        task_id: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if user_id is None or session_id is None:
            return None
        if self.task_repository is not None:
            try:
                persisted = await self.task_repository.load(user_id, session_id, task_id)
            except ValueError:
                # Non-UUID scope (e.g. debug-only ids) can't exist in the Postgres
                # store; treat as "not found" rather than surfacing a 500.
                persisted = None
        else:
            persisted = self.get_task_status(task_id, user_id=user_id, session_id=session_id)
        return persisted

    async def find_task_status_async(
        self,
        task_id: str,
        user_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Resolve a task by id, treating user/session as *optional* narrowing filters.

        ``get_task_status_async`` requires both because it backs the user-facing
        status route, where scope is the authorization boundary — and it returns
        None before touching storage when either is missing. Admin-side tooling
        (the /Debug arena) only holds a task id, so it needs this variant. Passing
        both narrows to exactly the same row ``get_task_status_async`` would find.
        Callers are responsible for authorizing the wider lookup.
        """
        if self.task_repository is not None:
            try:
                return await self.task_repository.load_any(task_id, user_id, session_id)
            except ValueError:
                # Non-UUID ids can't exist in the Postgres store; treat as "not
                # found" rather than surfacing a 500.
                return None
        if user_id is not None and session_id is not None:
            session_ctx = self.session_contexts.get(self._get_session_key(user_id, session_id))
            return (session_ctx or {}).get("tasks", {}).get(task_id)
        # In-memory fallback with partial scope: session keys are "{user}:{session}"
        # and user ids are UUIDs, so a single partition recovers both halves.
        for key, ctx in self.session_contexts.items():
            found = (ctx or {}).get("tasks", {}).get(task_id)
            if found is None:
                continue
            ctx_user, _, ctx_session = key.partition(":")
            if user_id is not None and ctx_user != user_id:
                continue
            if session_id is not None and ctx_session != session_id:
                continue
            return found
        return None

    async def refine_itinerary_slot(
        self,
        *,
        task_id: str,
        user_id: str,
        conversation_id: str,
        slot_index: Optional[int],
        selected_item_id: Optional[str] = None,
        prompt: Optional[str] = None,
        expected_revision: Optional[int] = None,
        accept_uncertainties: bool = False,
    ) -> Dict[str, Any]:
        lock = self._itinerary_refine_locks.setdefault(task_id, asyncio.Lock())
        async with lock:
            return await self._refine_itinerary_slot_unlocked(
                task_id=task_id,
                user_id=user_id,
                conversation_id=conversation_id,
                slot_index=slot_index,
                selected_item_id=selected_item_id,
                prompt=prompt,
                expected_revision=expected_revision,
                accept_uncertainties=accept_uncertainties,
            )

    async def _refine_itinerary_slot_unlocked(
        self,
        *,
        task_id: str,
        user_id: str,
        conversation_id: str,
        slot_index: Optional[int],
        selected_item_id: Optional[str] = None,
        prompt: Optional[str] = None,
        expected_revision: Optional[int] = None,
        accept_uncertainties: bool = False,
    ) -> Dict[str, Any]:
        """Refine one slot of a persisted itinerary: promote an alternate
        (``selected_item_id`` — zero gather calls) or re-gather the slot from a
        free-text ``prompt`` (one domain run; neighbors stay fixed). Only the
        legs adjacent to the touched slot are re-resolved (usually cache hits).
        The updated payload is re-persisted under the SAME result_id and
        returned raw — the API layer applies the client-safe projection.

        Raises RuntimeError (no result store), LookupError (no stored result),
        ValueError (invalid input / non-itinerary task / no candidates)."""
        from langgraph_metarec import eta
        from langgraph_metarec.graphs.routing_graph import tool_tags_for_domain
        from langgraph_metarec.legacy_adapters.itinerary_payload import (
            replace_legacy_slot_candidates,
            resolve_legacy_legs,
            swap_legacy_choice,
        )
        from langgraph_metarec.itinerary_runtime import resolve_itinerary_legs

        has_swap = bool(str(selected_item_id or "").strip())
        has_prompt = bool(str(prompt or "").strip())
        operations = int(has_swap) + int(has_prompt) + int(accept_uncertainties)
        if operations != 1:
            raise ValueError("Provide exactly one refinement operation")
        if not accept_uncertainties and slot_index is None:
            raise ValueError("slot_index is required for slot refinement")
        if self.result_repository is None:
            raise RuntimeError("Result store is not available")
        payload = await self.result_repository.load_by_task(user_id, conversation_id, task_id)
        if not isinstance(payload, dict):
            raise LookupError("No stored result for this task")
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        block = metadata.get("itinerary")
        if not isinstance(block, dict):
            raise ValueError("This task is not an itinerary result")
        current_revision = int(block.get("revision") or 1)
        if expected_revision is not None and expected_revision != current_revision:
            raise ItineraryConflictError(
                f"Itinerary changed from revision {expected_revision} to {current_revision}; reload and retry"
            )

        if accept_uncertainties:
            if block.get("planning_status") != "needs_refinement":
                raise ValueError("This itinerary has no pending uncertainty")
            updated_block = copy.deepcopy(block)
            updated_block["planning_status"] = "accepted_with_uncertainties"
            updated_block["uncertainties_accepted"] = True
            updated_block["revision"] = current_revision + 1
            updated_metadata = {
                **metadata,
                "itinerary": updated_block,
                "itinerary_revision": updated_block["revision"],
                "timestamp": datetime.now().isoformat(),
            }
            updated_payload = {**payload, "metadata": updated_metadata}
            branch_id = payload.get("branch_id")
            result_id = str(payload.get("result_id") or self.derive_result_id(task_id, branch_id))
            await self.result_repository.save(user_id, conversation_id, branch_id, result_id, updated_payload)
            return updated_payload

        new_restaurants: List[Restaurant] = []
        new_items: List[RecommendationItem] = []
        dynamic_request_payload = block.get("planning_request")
        is_dynamic = isinstance(dynamic_request_payload, dict)
        if has_swap:
            if not is_dynamic:
                updated_block = swap_legacy_choice(block, int(slot_index), str(selected_item_id))
        else:
            refine_prompt = str(prompt or "").strip()
            slot = next((s for s in block.get("slots") or [] if s.get("slot_index") == slot_index), None)
            if slot is None:
                raise ValueError(f"unknown slot_index {slot_index}")
            slot_domain = str(slot.get("domain") or "attraction")
            location = str(block.get("location") or "").strip()
            original_preferences = metadata.get("preferences") if isinstance(metadata.get("preferences"), dict) else {}
            slot_preferences = slot.get("slot_preferences") if isinstance(slot.get("slot_preferences"), dict) else {}
            anchor = {"location": location} if location else {}
            refine_preferences = {**original_preferences, **anchor, **slot_preferences, "domain": slot_domain}
            if slot_domain == "attraction" and not str(refine_preferences.get("region_hint") or "").strip():
                from profile_model import place_region_hint

                profile: Dict[str, Any] = {}
                try:
                    if self.profile_repository is not None:
                        profile = await self.profile_repository.get_user_profile(user_id) or {}
                except Exception:
                    profile = {}
                hint = place_region_hint(profile)
                if hint:
                    refine_preferences["region_hint"] = hint
            if slot_domain == "restaurant":
                slot_result = await self._execute_restaurant_domain_task(
                    query=refine_prompt,
                    preferences=merge_preferences(self.get_default_preferences(), refine_preferences),
                    user_id=user_id,
                    use_online_agent=False,
                    tool_tags=tool_tags_for_domain("restaurant"),
                    progress_callback=None,
                    conversation_context="",
                )
                candidates = [rec.model_dump() for rec in slot_result.restaurants[:5]]
                new_restaurants = slot_result.restaurants[:5]
            else:
                slot_result = await self._execute_generic_domain_task(
                    query=refine_prompt,
                    preferences=refine_preferences,
                    user_id=user_id,
                    domain=slot_domain,
                    use_online_agent=False,
                    tool_tags=tool_tags_for_domain(slot_domain),
                    progress_callback=None,
                )
                candidates = [item.model_dump() for item in slot_result.items[:5]]
                new_items = slot_result.items[:5]
            if not candidates:
                raise ValueError("No candidates matched that refinement — try different wording")
            if not is_dynamic:
                updated_block = replace_legacy_slot_candidates(block, int(slot_index), candidates)

        if is_dynamic:
            from dataclasses import replace
            from langgraph_metarec.itinerary_candidates import (
                apply_role_enrichment,
                normalize_candidates,
                role_enrichment_input,
            )
            from langgraph_metarec.itinerary_contracts import PlanningProblem, planning_request_from_dict
            from langgraph_metarec.itinerary_lodging import lodging_scenario_from_block
            from langgraph_metarec.itinerary_runtime import (
                apply_transport_cost,
                build_itinerary_block,
                build_travel_matrix,
                candidates_from_block,
                finalize_dynamic_metadata,
            )
            from langgraph_metarec.itinerary_solver import build_solver

            planning_request = planning_request_from_dict(dynamic_request_payload)
            pool = candidates_from_block(block)
            current_slot = next(
                (entry for entry in block.get("slots") or [] if entry.get("slot_index") == slot_index),
                {},
            )
            target_day = int(current_slot.get("day_index") or 0)
            current_id = str((current_slot.get("chosen") or {}).get("id") or "")
            if has_prompt:
                pool = [candidate for candidate in pool if candidate.id != current_id]
                prompt_candidates = normalize_candidates(candidates, planning_request)
                unresolved_roles = role_enrichment_input(prompt_candidates)
                if unresolved_roles:
                    from llm_service import classify_itinerary_candidate_roles

                    role_payload = await classify_itinerary_candidate_roles(
                        self.async_client,
                        candidates=unresolved_roles,
                        model=self.llm_model,
                    )
                    prompt_candidates = apply_role_enrichment(prompt_candidates, role_payload)
                if not prompt_candidates:
                    raise ValueError("No valid place candidates matched that refinement")
                pool.extend(prompt_candidates)
                hard = {
                    **planning_request.hard_constraints,
                    "day_candidate_options": {
                        **dict(planning_request.hard_constraints.get("day_candidate_options") or {}),
                        str(target_day): [candidate.id for candidate in prompt_candidates],
                    },
                }
                planning_request = replace(planning_request, hard_constraints=hard)
            if has_swap:
                selected = str(selected_item_id)
                if not any(candidate.id == selected for candidate in pool):
                    raise ValueError(f"item {selected} is not available for refinement")
                pool = [candidate for candidate in pool if candidate.id != current_id]
                existing_must = [
                    str(value) for value in planning_request.hard_constraints.get("must_visit") or []
                ]
                hard = {
                    **planning_request.hard_constraints,
                    "must_visit": sorted(set((*existing_must, selected))),
                    "fixed_day_candidates": {
                        **dict(planning_request.hard_constraints.get("fixed_day_candidates") or {}),
                        selected: target_day,
                    },
                }
                planning_request = replace(planning_request, hard_constraints=hard)
            solver = build_solver(os.getenv("ITINERARY_SOLVER", "beam"))
            lodging_scenario = lodging_scenario_from_block(block)
            lodging_scenarios = (lodging_scenario,) if lodging_scenario is not None else ()
            travel_matrix = build_travel_matrix(pool, planning_request, lodging_scenarios)
            solver_result = solver.solve(PlanningProblem(
                planning_request, tuple(pool), travel_matrix, lodging_scenarios
            ))
            updated_block = build_itinerary_block(
                planning_request, solver_result, pool, revision=current_revision + 1
            )
            updated_block.setdefault("solver", {})["candidate_count"] = len(pool)

        updated_block = await (
            resolve_itinerary_legs(updated_block, eta.resolve_leg)
            if is_dynamic
            else resolve_legacy_legs(updated_block, eta.resolve_leg)
        )
        if is_dynamic:
            finalize_dynamic_metadata(updated_block, planning_request, solver_result)
            apply_transport_cost(updated_block)
        from langgraph_metarec.itinerary_evaluation import evaluate_itinerary
        updated_block["evaluation"] = evaluate_itinerary(updated_block, block)

        def merge_models(existing: Any, additions: List[BaseModel]) -> List[Dict[str, Any]]:
            merged = [dict(item) for item in (existing or []) if isinstance(item, dict)]
            by_id = {str(item.get("id")): index for index, item in enumerate(merged) if item.get("id")}
            for model in additions:
                item = model.model_dump()
                key = str(item.get("id") or "")
                if key and key in by_id:
                    merged[by_id[key]] = item
                else:
                    merged.append(item)
                    if key:
                        by_id[key] = len(merged) - 1
            return merged

        restaurants = merge_models(payload.get("restaurants"), new_restaurants)
        items = merge_models(payload.get("items"), new_items)
        updated_metadata = {
            **metadata,
            "itinerary": updated_block,
            "itinerary_revision": updated_block.get("revision"),
            "restaurants_count": len(restaurants),
            "items_count": len(items),
            "timestamp": datetime.now().isoformat(),
        }
        updated_payload = {**payload, "restaurants": restaurants, "items": items, "metadata": updated_metadata}
        branch_id = payload.get("branch_id")
        result_id = str(payload.get("result_id") or self.derive_result_id(task_id, branch_id))
        await self.result_repository.save(user_id, conversation_id, branch_id, result_id, updated_payload)
        return updated_payload

    @staticmethod
    def derive_result_id(task_id: str, branch_id: Optional[str]) -> str:
        """Stable result_id for a (task, branch). Delegates to the canonical
        definition in business_models so the feedback pipeline derives the same id."""
        from business_models import derive_result_id as _derive_result_id

        return _derive_result_id(task_id, branch_id)

    async def _persist_recommendation_result(
        self,
        user_id: str,
        session_id: Optional[str],
        task_id: str,
        branch_id: Optional[str],
        status: Dict[str, Any],
    ) -> Optional[str]:
        """Persist a completed task's recommendation to recommendation_results
        (the durable, queryable source of truth) and return its result_id.

        Failures are swallowed: result persistence must never break task tracking.
        """
        if self.result_repository is None:
            return None
        try:
            # ``status["result"]`` may be a RecommendationResult model (the task graph
            # passes the object through) or an already-serialized dict — normalize to a
            # dict so the ``.get(...)`` access below cannot raise (a swallowed error here
            # previously meant the result row was never written and feedback 400'd).
            result = status.get("result") or {}
            if hasattr(result, "model_dump"):
                result = result.model_dump(mode="json")
            elif hasattr(result, "dict"):
                result = result.dict()
            if not isinstance(result, dict):
                result = {}
            status_metadata = status.get("metadata") or {}
            result_metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
            result_id = self.derive_result_id(task_id, branch_id)
            # One canonical copy only: restaurants/items/thinking_steps/metadata are
            # stored at the top level of the payload. The full result dict used to be
            # nested alongside them as ``payload["result"]``, duplicating everything
            # (including the sizable ``metadata.executions`` tool dump) in every row.
            payload = {
                "result_id": result_id,
                "task_id": task_id,
                "branch_id": branch_id,
                "domain": result_metadata.get("domain") or status_metadata.get("domain"),
                "restaurants": result.get("restaurants") or [],
                "items": result.get("items") or [],
                "thinking_steps": result.get("thinking_steps") or [],
                "metadata": result_metadata or status_metadata,
            }
            await self.result_repository.save(user_id, session_id, branch_id, result_id, payload)
            return result_id
        except Exception as exc:
            print(f"Warning: Failed to persist recommendation result for task {task_id}: {exc}")
            return None

    async def _create_confirmation_payload(
        self,
        query: str,
        preferences: Dict[str, Any],
        user_id: str,
        domain: str = "recommendation",
        guide_missing_preferences: bool = False,
    ) -> Dict[str, Any]:
        """Create a confirmation payload without mutating service session context."""
        quick_actions: Optional[List[Dict[str, Any]]] = None
        if generate_confirmation_payload:
            try:
                language = detect_language(query) if detect_language else "en"
                if self.profile_repository is not None:
                    user_profile = await self.profile_repository.get_user_profile(user_id)
                else:
                    user_profile = None
                confirmation_payload = await generate_confirmation_payload(
                    self.async_client,
                    query,
                    preferences,
                    domain=domain,
                    language=language,
                    user_profile=user_profile,
                    guide_missing_preferences=guide_missing_preferences,
                    model=self.llm_model,
                    max_text_retries=self.llm_max_format_retries,
                )
                message = str(confirmation_payload.get("message") or "").strip()
                payload_quick_actions = confirmation_payload.get("quick_actions")
                if isinstance(payload_quick_actions, list) and payload_quick_actions:
                    quick_actions = payload_quick_actions
                if not message:
                    message = self.generate_confirmation_prompt(query, preferences, domain)
            except Exception as exc:
                print(f"Error generating graph confirmation message, falling back to template: {exc}")
                message = self.generate_confirmation_prompt(query, preferences, domain)
        else:
            message = self.generate_confirmation_prompt(query, preferences, domain)
        payload = {
            "message": message,
            "preferences": preferences,
            "needs_confirmation": True,
        }
        if quick_actions:
            payload["quick_actions"] = quick_actions
        return payload

    async def _apply_conversation_summary(self, user_id: str, session_id: str, summary_update) -> None:
        """Run the rolling-summary update off the reply path. Best-effort: any failure
        is swallowed so it can never affect the live turn.

        Runs under its OWN usage scope: this task outlives the request that spawned
        it, and the request's ledger is flushed when the request returns — recording
        into it here would race the flush and silently drop the summary's tokens."""
        try:
            from llm_service import summarize_conversation
            from business_repositories import conversation_repository

            async with self._usage_scope(user_id=user_id, conversation_id=session_id, task_id=None):
                new_summary = await summarize_conversation(
                    self.async_client,
                    summary_update.prior_summary,
                    summary_update.new_turns_text,
                    model=self.llm_model,
                )
            if new_summary:
                await conversation_repository.update_conversation_context_summary(
                    user_id, session_id, new_summary, summary_update.new_watermark_id
                )
        except Exception:
            import logging
            logging.getLogger(__name__).debug("Rolling summary update failed", exc_info=True)

    async def _handle_user_request_graph(
        self,
        query: str,
        user_id: str,
        conversation_history: Optional[List[Dict[str, Any]]],
        session_id: Optional[str],
        use_online_agent: bool,
        message_id: Optional[str],
        branch_id: Optional[str],
        domain_lock: Optional[str],
        itinerary_mode: bool,
        hitl_state: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        from langgraph_metarec.graphs.request_orchestrator import (
            RequestOrchestratorAdapters,
            run_request_orchestrator,
        )

        if self.profile_repository is not None:
            user_profile = await self.profile_repository.get_user_profile(user_id)
        else:
            user_profile = None
        default_preferences = self.get_default_preferences()
        restaurant_runtime_baseline = self._select_runtime_preferences(default_preferences, user_profile, None)
        # In-conversation memory: load the persisted conversation once and build a
        # context block (recent turns incl. recommendations + feedback, accumulated
        # preferences, shown/disliked places) fed to the intent/preference LLM so
        # relative follow-ups ("cheaper", "the second one") resolve in context.
        analysis_block = ""
        try:
            from business_repositories import conversation_repository
            from conversation_context import build_conversation_context, compute_summary_update

            if session_id:
                conversation = await conversation_repository.get_full_conversation(user_id, session_id)
                stored_preferences = conversation.get("preferences") if conversation else None
                restaurant_runtime_baseline = self._select_runtime_preferences(
                    default_preferences,
                    user_profile,
                    stored_preferences,
                )
                analysis_block = build_conversation_context(
                    conversation,
                    active_branch_id=branch_id,
                    current_query=query,
                ).to_analysis_block()
                # Fold turns that have rolled out of the window into the rolling
                # summary, off the reply path (fire-and-forget, fast model).
                summary_update = compute_summary_update(conversation, active_branch_id=branch_id)
                if summary_update is not None:
                    summary_task = asyncio.create_task(
                        self._apply_conversation_summary(user_id, session_id, summary_update)
                    )
                    # Keep a strong reference so the pending task can't be GC'd.
                    self._background_tasks.add(summary_task)
                    summary_task.add_done_callback(self._background_tasks.discard)
        except Exception:
            import logging
            logging.getLogger(__name__).debug("Conversation context unavailable", exc_info=True)

        async def analyze_adapter(
            message: str,
            history: List[Dict[str, Any]],
            profile: Optional[Dict[str, Any]],
            is_in_query_flow: bool,
            pending_preferences: Optional[Dict[str, Any]],
        ) -> LLMResponse:
            return await analyze_user_message(
                self.async_client,
                message,
                history,
                profile,
                is_in_query_flow=is_in_query_flow,
                pending_preferences=pending_preferences,
                model=self.llm_model,
                max_format_retries=self.llm_max_format_retries,
                extra_context=analysis_block or None,
            )

        async def make_confirmation(
            confirmation_query: str,
            preferences: Dict[str, Any],
            domain: str = "recommendation",
            guide_missing_preferences: bool = False,
        ) -> Dict[str, Any]:
            return await self._create_confirmation_payload(
                confirmation_query,
                preferences,
                user_id,
                domain,
                guide_missing_preferences,
            )

        async def create_task_adapter(
            task_query: str,
            preferences: Dict[str, Any],
            tool_tags: Optional[List[str]],
            route: Optional[Dict[str, Any]],
        ) -> str:
            return await self.create_task_async(
                task_query,
                preferences,
                user_id,
                session_id,
                use_online_agent,
                tool_tags,
                branch_id,
                route,
            )

        def extract_preferences_adapter(preference_query: str) -> Dict[str, Any]:
            return self.extract_preferences_from_query(
                preference_query,
                user_id,
                session_id,
                persist=False,
                base_preferences=restaurant_runtime_baseline,
            )

        async def extract_itinerary_constraints_adapter(
            slot_query: str,
            preferences: Optional[Dict[str, Any]],
        ) -> Optional[Dict[str, Any]]:
            from llm_service import extract_itinerary_constraints

            return await extract_itinerary_constraints(
                self.async_client,
                query=slot_query,
                preferences=preferences,
                model=self.llm_model,
            )

        async def resolve_itinerary_anchor_adapter(
            anchor_query: str,
            destination: str,
            provider_id: Optional[str] = None,
        ) -> List[Dict[str, Any]]:
            from langgraph_metarec.itinerary_anchors import resolve_anchor_candidates
            from langgraph_metarec.tool_registry import geocode_anchor_candidates

            exact_candidates = await asyncio.to_thread(
                geocode_anchor_candidates,
                anchor_query,
                destination,
                max_results=4,
            )
            exact_resolution = resolve_anchor_candidates(
                anchor_query,
                destination,
                exact_candidates,
                provider_id=provider_id,
            )
            if exact_resolution.status in {"resolved", "ambiguous"}:
                return exact_candidates
            result = await self._execute_generic_domain_task(
                query=anchor_query,
                preferences={"location": destination, "provider_id": provider_id},
                user_id=user_id,
                domain="hotel",
                use_online_agent=False,
                tool_tags=["#place", "#hotel"],
                progress_callback=None,
            )
            broad_candidates = [item.model_dump() for item in result.items[:8]]
            return [*exact_candidates, *broad_candidates]

        runtime = await run_request_orchestrator(
            adapters=RequestOrchestratorAdapters(
                analyze_message=analyze_adapter,
                make_confirmation=make_confirmation,
                create_task=create_task_adapter,
                extract_preferences=extract_preferences_adapter,
                extract_itinerary_constraints=extract_itinerary_constraints_adapter,
                resolve_itinerary_anchor=resolve_itinerary_anchor_adapter,
            ),
            query=query,
            user_id=user_id,
            conversation_id=session_id,
            branch_id=branch_id,
            message_id=message_id,
            conversation_history=conversation_history,
            user_profile=user_profile,
            restaurant_baseline=restaurant_runtime_baseline,
            use_online_agent=use_online_agent,
            domain_lock=domain_lock,
            itinerary_mode=itinerary_mode,
            hitl_state=hitl_state,
            checkpointer=await self.runtime_checkpointer.aget(),
        )
        if self.profile_repository is not None and runtime.intent_result and runtime.intent_result.profile_updates:
            raw_updates: Dict[str, Any] = {}
            if "demographics" in runtime.intent_result.profile_updates:
                raw_updates["demographics"] = runtime.intent_result.profile_updates["demographics"]
            if "dining_habits" in runtime.intent_result.profile_updates:
                raw_updates["dining_habits"] = runtime.intent_result.profile_updates["dining_habits"]
            if raw_updates:
                profile_updates = self._normalize_profile_updates(raw_updates)
                current_profile = await self.profile_repository.get_user_profile(user_id)
                await self.profile_repository.save_user_profile(
                    user_id,
                    self._merge_profile_updates(current_profile, profile_updates),
                )
        payload = dict(runtime.response_payload)
        confirmation_request = payload.get("confirmation_request")
        if isinstance(confirmation_request, dict):
            payload["confirmation_request"] = ConfirmationRequest(**confirmation_request)
        return payload
    
    # ==================== 统一用户请求处理 ====================
    
    async def handle_user_request_async(
        self,
        query: str,
        user_id: str = "default",
        conversation_history: Optional[List[Dict[str, Any]]] = None,
        session_id: Optional[str] = None,
        use_online_agent: bool = False,
        message_id: Optional[str] = None,
        branch_id: Optional[str] = None,
        timeline_cursor: Optional[str] = None,
        domain_lock: Optional[str] = None,
        itinerary_mode: bool = False,
        hitl_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        异步处理用户请求的统一入口函数（使用 LLM 进行意图识别）
        
        这个函数会自动处理：
        1. 使用 LLM 进行意图识别和生成回复
        2. 根据意图决定后续操作：
           - "query": 触发推荐流程
           - "chat": 返回 LLM 的回复
        
        Args:
            query: 用户查询
            user_id: 用户ID
            conversation_history: 对话历史（可选）
            session_id: 会话ID（可选）
            
        Returns:
            包含以下字段的字典：
            - type: "llm_reply" | "confirmation" | "task_created" | "modify_request"
            - llm_reply: GPT-4 的回复（如果type为llm_reply）
            - task_id: 任务ID（如果type为task_created）
            - confirmation_request: 确认请求对象（如果type为confirmation）
            - message: 消息文本（如果type为modify_request）
        """
        # 添加日志，确认参数传递
        print(f"[Service] handle_user_request_async - use_online_agent: {use_online_agent} (type: {type(use_online_agent)})")

        # Delegate to the LangGraph request orchestrator: the single intent /
        # confirm / route / dispatch path. Scope LLM usage to this synchronous turn
        # (intent recognition, confirmation generation); the background recommendation
        # task, if created, opens its own scope so its calls are attributed separately.
        async with self._usage_scope(user_id=user_id, conversation_id=session_id, task_id=None):
            return await self._handle_user_request_graph(
                query=query,
                user_id=user_id,
                conversation_history=conversation_history,
                session_id=session_id,
                use_online_agent=use_online_agent,
                message_id=message_id,
                branch_id=branch_id,
                domain_lock=domain_lock,
                itinerary_mode=itinerary_mode,
                hitl_state=hitl_state,
            )


# ==================== 便捷函数 ====================

def create_service(
        async_client: Union[AsyncOpenAI, AsyncAzureOpenAI],
        sync_client: Union[OpenAI, AzureOpenAI],
        summary_model: str,
        planning_model: str,
        llm_model: str,
        restaurant_data: Optional[List[Dict]] = None,
    ) -> MetaRecService:
    """
    创建服务实例的便捷函数
    
    Args:
        async_client: async openai client
        sync_client: sync openai client
        summary_model: model name for summary task
        planning_model: model name for planning task
        llm_model: model name for other task

        restaurant_data: 可选的餐厅数据

    Returns:
        MetaRecService实例
    """
    return MetaRecService(
            async_client, 
            sync_client,
            summary_model,
            planning_model,
            llm_model,
            restaurant_data
    )
