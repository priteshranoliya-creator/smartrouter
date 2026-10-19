"""Token pricing: parse config/pricing.yaml once, then price usage with pure functions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, NonNegativeFloat, ValidationError, model_validator

from smartrouter.providers.base import Usage

Ttl = Literal["5m", "1h"]
PER_MILLION = 1_000_000


class PricingError(ValueError):
    """config/pricing.yaml is invalid, or a model has no price for the request."""


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Tier(_Frozen):
    max_prompt_tokens: int | None = Field(default=None, gt=0)
    input: NonNegativeFloat
    cache_read: NonNegativeFloat
    output: NonNegativeFloat


class Period(_Frozen):
    starts: date | None = Field(default=None, alias="from")
    until: date | None = None
    tiers: tuple[Tier, ...]

    @model_validator(mode="before")
    @classmethod
    def _flat_to_tiers(cls, data: Any) -> Any:
        return _lift(data, "tiers", ("input", "cache_read", "output"))

    @model_validator(mode="after")
    def _check(self) -> Period:
        caps = [t.max_prompt_tokens for t in self.tiers]
        if not caps or caps[-1] is not None or any(c is None for c in caps[:-1]):
            raise ValueError("tiers: only the last tier may (and must) have max_prompt_tokens: null")
        bounded = [c for c in caps if c is not None]
        if bounded != sorted(set(bounded)):
            raise ValueError("tiers: max_prompt_tokens must strictly increase")
        if self.starts and self.until and self.starts > self.until:
            raise ValueError("from is after until")
        return self


class ModelPrice(_Frozen):
    provider: str
    verified: bool
    periods: tuple[Period, ...]

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, data: Any) -> Any:
        data = _lift(data, "tiers", ("input", "cache_read", "output"))
        return _lift(data, "periods", ("tiers",))

    @model_validator(mode="after")
    def _ordered(self) -> ModelPrice:
        for a, b in zip(self.periods, self.periods[1:], strict=False):
            if a.until is None or b.starts is None or b.starts <= a.until:
                raise ValueError("periods must be ordered and must not overlap")
        return self


class ProviderInfo(_Frozen):
    source: str
    checked_on: date
    cache_write_multiplier: dict[Ttl, float]


class PriceBook(_Frozen):
    providers: dict[str, ProviderInfo]
    geo_multiplier: dict[str, float] = {}
    models: dict[str, ModelPrice]

    @model_validator(mode="after")
    def _known_providers(self) -> PriceBook:
        for name, price in self.models.items():
            if price.provider not in self.providers:
                raise ValueError(f"models.{name}.provider: unknown provider '{price.provider}'")
        return self


def _lift(data: Any, key: str, flat_keys: tuple[str, ...]) -> Any:
    """Wrap a flat shorthand mapping into a one-element `key` list."""
    if not isinstance(data, dict) or key in data or not any(k in data for k in flat_keys):
        return data
    inner = {k: v for k, v in data.items() if k in flat_keys or k == "max_prompt_tokens"}
    outer = {k: v for k, v in data.items() if k not in inner}
    return {**outer, key: [inner]}


def load_pricing(path: Path) -> PriceBook:
    try:
        raw = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise PricingError(f"{path}: {e}") from e
    try:
        return PriceBook.model_validate(raw)
    except ValidationError as e:
        lines = [f"{path}: {'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
        raise PricingError("\n".join(lines)) from e


@dataclass(frozen=True)
class Rates:
    """USD per token for one model, date and prompt length."""

    input: float
    cache_read: float
    cache_write_5m: float
    cache_write_1h: float
    output: float


def rates(book: PriceBook, model: str, prompt_tokens: int, on: date, geo: str | None = None) -> Rates:
    price = book.models.get(model)
    if price is None:
        raise PricingError(f"no price for model '{model}'")
    period = next(
        (
            p
            for p in price.periods
            if (p.starts is None or p.starts <= on) and (p.until is None or on <= p.until)
        ),
        None,
    )
    if period is None:
        raise PricingError(f"no price for model '{model}' on {on.isoformat()}")
    tier = next(
        t for t in period.tiers if t.max_prompt_tokens is None or prompt_tokens <= t.max_prompt_tokens
    )
    mult = book.providers[price.provider].cache_write_multiplier
    scale = book.geo_multiplier.get(geo, 1.0) if geo else 1.0
    per_token = scale / PER_MILLION
    return Rates(
        input=tier.input * per_token,
        cache_read=tier.cache_read * per_token,
        cache_write_5m=tier.input * mult["5m"] * per_token,
        cache_write_1h=tier.input * mult["1h"] * per_token,
        output=tier.output * per_token,
    )


def cost(book: PriceBook, model: str, usage: Usage, on: date) -> float:
    r = rates(book, model, usage.prompt_tokens, on, usage.inference_geo)
    return (
        usage.input_tokens * r.input
        + usage.cache_read_tokens * r.cache_read
        + usage.cache_write_5m_tokens * r.cache_write_5m
        + usage.cache_write_1h_tokens * r.cache_write_1h
        + usage.output_tokens * r.output
    )


def counterfactual(book: PriceBook, model: str, usage: Usage, warm_prefix_tokens: int, on: date) -> float:
    """Cost had `model` served this request with `warm_prefix_tokens` of its prefix still cached.

    Uncached input and output are unchanged; the cacheable part is read up to the warm prefix
    and written for the rest, at the TTL the actual request used.
    """
    cacheable = usage.cache_read_tokens + usage.cache_write_tokens
    read = min(max(warm_prefix_tokens, 0), cacheable)
    write = cacheable - read
    long_ttl = usage.cache_write_1h_tokens > 0
    as_if = Usage(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=read,
        cache_write_5m_tokens=0 if long_ttl else write,
        cache_write_1h_tokens=write if long_ttl else 0,
        inference_geo=usage.inference_geo,
    )
    return cost(book, model, as_if, on)


def prefix_cost(book: PriceBook, model: str, tokens: int, *, warm: bool, ttl: Ttl, on: date) -> float:
    """Cost of sending a `tokens`-long cached prefix: a read when warm, a write when cold."""
    r = rates(book, model, tokens, on)
    if warm:
        return tokens * r.cache_read
    return tokens * (r.cache_write_1h if ttl == "1h" else r.cache_write_5m)


def unpriced(book: PriceBook, models: Iterable[str]) -> list[str]:
    return sorted({m for m in models if m not in book.models})
