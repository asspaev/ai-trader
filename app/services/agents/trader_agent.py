"""TRADER-агент: финальное торговое решение по одной монете.

Принимает сводки PRICE и NEWS, состояние кошелька, историю последних
решений и возвращает :class:`TraderDecision` с одним из ``BUY|SELL|HOLD``.

Парсинг строгий: при битом JSON делаем один локальный ретрай —
вызываем LLM ещё раз с пометкой «JSON parse failed», чтобы модель
имела шанс исправиться, не дёргая весь pipeline-уровень повторно.
Двух попыток обычно достаточно; если и они не дали валидного JSON —
поднимаем :class:`AgentJSONParseError`, и pipeline пометит шаг как
``executed=False`` + ``not_executed_reason="LLM_PARSE_FAILED"``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Sequence

from app.config import settings
from app.models import Decision
from app.models.enums import DecisionAction
from app.services.agents.base import (
    AgentJSONParseError,
    BaseAgent,
    ChatLLM,
    parse_strict_json,
    render_prompt,
)
from app.services.agents.news_agent import NewsFinalScore
from app.services.agents.price_agent import PriceSummary


@dataclass(frozen=True, slots=True)
class WalletSnapshot:
    """Текущее состояние кошелька по одной монете на момент решения.

    Передаётся в TRADER-агент. ``asset_balance`` уже округлён под
    ``stepSize`` или нет — нам неважно: агент видит её как «реальную
    позицию», а исполнитель сделки потом дополнительно округлит.
    """

    free_usdt: Decimal
    asset_balance: Decimal
    asset_price_usdt: Decimal

    @property
    def asset_value_usdt(self) -> Decimal:
        return (self.asset_balance * self.asset_price_usdt).quantize(Decimal("0.00000001"))


@dataclass(frozen=True, slots=True)
class TraderDecision:
    """Распарсенный ответ TRADER-агента.

    ``buy_fraction`` обязателен только для BUY и должен быть в (0, 1].
    Для SELL и HOLD он всегда ``None`` — это инвариант после парсинга.
    """

    action: DecisionAction
    buy_fraction: Decimal | None
    reasoning: str


class TraderAgent(BaseAgent):
    """Тонкая обёртка над LLM для генерации :class:`TraderDecision`.

    Args:
        llm_client: LLM-клиент.
        model: Имя модели; по умолчанию — ``settings.agent.trader_model``.
        history_limit: Сколько прошлых решений показывать в промпте.
            По умолчанию — ``settings.trading.decisions_history_limit``.
    """

    AGENT_NAME = "trader"
    PROMPT = "trader_decision"
    LOG_COMPONENT = "trader_agent"

    def __init__(
        self,
        llm_client: ChatLLM,
        *,
        model: str | None = None,
        history_limit: int | None = None,
    ) -> None:
        super().__init__(llm_client, model=model or settings.agent.trader_model)
        self._history_limit = (
            history_limit
            if history_limit is not None
            else settings.trading.decisions_history_limit
        )

    async def decide(
        self,
        *,
        asset: str,
        wallet: WalletSnapshot,
        price: PriceSummary,
        news: NewsFinalScore,
        history: Sequence[Decision],
        pipeline_run_id: uuid.UUID | None = None,
    ) -> TraderDecision:
        """Запросить решение у LLM и распарсить ответ.

        Парсинг-ретрай (до 2 попыток + reminder во втором сообщении)
        реализован в :class:`BaseAgent`.
        """
        prompt = self._render_prompt(asset, wallet, price, news, history)

        return await self._chat_with_parse_retry(
            agent_name=self.AGENT_NAME,
            messages_factory=lambda prior_error: _build_messages(
                prompt, prior_error=prior_error
            ),
            parser=parse_trader_decision,
            pipeline_run_id=pipeline_run_id,
            log_extra={"asset": asset.upper()},
        )

    # ---------- internals ----------

    def _render_prompt(
        self,
        asset: str,
        wallet: WalletSnapshot,
        price: PriceSummary,
        news: NewsFinalScore,
        history: Sequence[Decision],
    ) -> str:
        return render_prompt(
            self.PROMPT,
            asset=asset.upper(),
            free_usdt=_fmt_decimal(wallet.free_usdt),
            asset_balance=_fmt_decimal(wallet.asset_balance),
            asset_price_usdt=_fmt_decimal(wallet.asset_price_usdt),
            asset_value_usdt=_fmt_decimal(wallet.asset_value_usdt),
            price_sentiment=price.sentiment.value,
            price_summary=price.summary.strip(),
            news_sentiment=news.sentiment.value,
            news_score=news.score.strip(),
            decisions_block=format_decisions_block(history, self._history_limit),
        )


# ---------- pure helpers ----------


def parse_trader_decision(content: str) -> TraderDecision:
    """JSON ответа TRADER-агента → :class:`TraderDecision`.

    Бросает :class:`AgentJSONParseError`, если что-либо не так:

    * JSON невалиден.
    * ``action`` отсутствует или не из ``{BUY, SELL, HOLD}``.
    * ``buy_fraction`` не в (0, 1] на BUY либо задан на SELL/HOLD.
    * ``reasoning`` пуст.
    """
    data = parse_strict_json(content)

    action_raw = data.get("action")
    if not isinstance(action_raw, str):
        raise AgentJSONParseError(
            "TraderAgent: 'action' must be a string",
            raw_content=content,
        )
    action_normalized = action_raw.strip().upper()
    try:
        action = DecisionAction(action_normalized)
    except ValueError as exc:
        raise AgentJSONParseError(
            f"TraderAgent: unknown 'action': {action_raw!r}",
            raw_content=content,
        ) from exc

    raw_fraction = data.get("buy_fraction")
    buy_fraction = _coerce_buy_fraction(raw_fraction, action=action, raw_content=content)

    reasoning = data.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise AgentJSONParseError(
            "TraderAgent: 'reasoning' must be a non-empty string",
            raw_content=content,
        )

    return TraderDecision(
        action=action,
        buy_fraction=buy_fraction,
        reasoning=reasoning.strip(),
    )


def format_decisions_block(
    history: Sequence[Decision], limit: int
) -> str:
    """Текст-блок с историей решений для промпта.

    Каждая строка — ``[YYYY-MM-DD HH:MM] ACTION executed=true/false
    fraction=0.25 — reasoning``. Сортировка — от свежего к старому,
    реверс делаем тут (CRUD отдаёт уже отсортированный, но если
    кто-то передаст развёрнутый — отрезаем сами).
    """
    if not history:
        return "Истории решений пока нет — это первый тик по активу."

    truncated = list(history)[:limit]
    lines: list[str] = []
    for idx, decision in enumerate(truncated, start=1):
        ts = decision.created_at.strftime("%Y-%m-%d %H:%M UTC")
        fraction = (
            f"fraction={_fmt_decimal(decision.buy_fraction)}"
            if decision.buy_fraction is not None
            else "fraction=n/a"
        )
        executed = (
            "executed=true"
            if decision.executed is True
            else ("executed=false" if decision.executed is False else "executed=null")
        )
        reasoning = (decision.reasoning or "").strip().replace("\n", " ")
        if len(reasoning) > 280:
            reasoning = reasoning[:280].rstrip() + "…"
        lines.append(
            f"[{idx}] {ts} {decision.action.value} {executed} {fraction} — "
            f"{reasoning or '<нет обоснования>'}"
        )
    return "\n".join(lines)


def _coerce_buy_fraction(
    raw: object,
    *,
    action: DecisionAction,
    raw_content: str,
) -> Decimal | None:
    """Привести raw к :class:`Decimal` и валидировать совместимость с action."""
    if action is DecisionAction.BUY:
        if raw is None:
            raise AgentJSONParseError(
                "TraderAgent: 'buy_fraction' is required for BUY",
                raw_content=raw_content,
            )
        try:
            fraction = _to_decimal(raw)
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise AgentJSONParseError(
                f"TraderAgent: 'buy_fraction' is not a number: {raw!r}",
                raw_content=raw_content,
            ) from exc
        if fraction <= 0 or fraction > 1:
            raise AgentJSONParseError(
                f"TraderAgent: 'buy_fraction' must be in (0, 1], got {fraction}",
                raw_content=raw_content,
            )
        return fraction

    # SELL/HOLD: buy_fraction должен быть null. Если LLM прислал «0»
    # или объект — игнорируем (мы сами обнулим), но строкой/числом
    # ненулевым — это противоречие, лучше упасть.
    if raw is None:
        return None
    try:
        fraction = _to_decimal(raw)
    except (InvalidOperation, ValueError, TypeError):
        raise AgentJSONParseError(
            f"TraderAgent: 'buy_fraction' must be null on {action.value}, "
            f"got {raw!r}",
            raw_content=raw_content,
        )
    if fraction != 0:
        raise AgentJSONParseError(
            f"TraderAgent: 'buy_fraction' must be null/0 on {action.value}, "
            f"got {fraction}",
            raw_content=raw_content,
        )
    return None


def _to_decimal(raw: object) -> Decimal:
    """Универсальный приведение int/float/str к :class:`Decimal`."""
    if isinstance(raw, bool):  # bool — подкласс int, нам не подходит
        raise ValueError("buy_fraction must not be a boolean")
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, (int, float)):
        return Decimal(str(raw))
    if isinstance(raw, str):
        return Decimal(raw.strip())
    raise ValueError(f"Unsupported buy_fraction type: {type(raw).__name__}")


def _fmt_decimal(value: Decimal, *, places: int = 8) -> str:
    """Локальный formatter (дублирует price_agent, но без cross-import)."""
    quantized = value.quantize(Decimal(10) ** -places)
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".") or "0"
    return text


def _build_messages(
    prompt: str, *, prior_error: AgentJSONParseError | None = None
) -> list[dict]:
    """Системный + user; при ретрае добавляем reminder про формат."""
    messages: list[dict] = [
        {
            "role": "system",
            "content": (
                "Ты — трейдер-аналитик. Без воды, без markdown в JSON, "
                "без advisor-дисклеймеров. Отвечай строго одним JSON-объектом."
            ),
        },
        {"role": "user", "content": prompt},
    ]
    if prior_error is not None:
        messages.append(
            {
                "role": "user",
                "content": (
                    "Предыдущий ответ не удалось распарсить как JSON. "
                    f"Причина: {prior_error}. Верни строго один JSON-объект "
                    "без какого-либо текста до или после, без блоков ```json```."
                ),
            }
        )
    return messages


__all__ = [
    "TraderAgent",
    "TraderDecision",
    "WalletSnapshot",
    "format_decisions_block",
    "parse_trader_decision",
]
