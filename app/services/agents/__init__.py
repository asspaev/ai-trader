"""Слой LLM-агентов (PRICE, NEWS, TRADER).

Каждый агент — тонкая обёртка над :class:`OpenRouterClient`:
шаблонизирует промпт из ``.md``-файла, валидирует структуру ответа
(JSON), маппит в типизированный :mod:`dataclass` для последующего
использования pipeline-слоем. Реэкспорт публичных имён.
"""

from app.services.agents.base import (
    AgentError,
    AgentJSONParseError,
    Sentiment,
    extract_assistant_content,
    parse_strict_json,
    render_prompt,
)
from app.services.agents.news_agent import (
    NewsAgenda,
    NewsAgent,
    NewsFinalScore,
    NewsSummary,
)
from app.services.agents.price_agent import PriceAgent, PriceSummary
from app.services.agents.trader_agent import TraderAgent, TraderDecision

__all__ = [
    "AgentError",
    "AgentJSONParseError",
    "NewsAgenda",
    "NewsAgent",
    "NewsFinalScore",
    "NewsSummary",
    "PriceAgent",
    "PriceSummary",
    "Sentiment",
    "TraderAgent",
    "TraderDecision",
    "extract_assistant_content",
    "parse_strict_json",
    "render_prompt",
]
