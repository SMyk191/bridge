"""The single door to every LLM provider.

Every model call in Bridge goes through :class:`LLMClient` so tokens and cost are
counted exactly once (CLAUDE.md), and so every run carries a budget and a
persisted trace (design law 6).

Callers name a *role*, never a model and never a provider. A role resolves
through ``config.ROLE_PROVIDER`` to a provider, and through
``config.ROLE_MODELS`` to that provider's model and token ceiling — so moving the
describer from Claude to Gemini, or back, is one line of config.

Two providers are wired and both stay working:

* :class:`AnthropicProvider` — the ``anthropic`` SDK.
* :class:`GeminiProvider` — the ``google-genai`` SDK. Note Gemini bills thinking
  tokens at the *output* rate, so they are folded into output before costing;
  counting them as free would understate a thinking model's cost several-fold.

Without credentials a provider is *unavailable*: it does not fabricate output.
Callers check :meth:`LLMClient.available_for` and degrade honestly (see
`describe.py`, which falls back to a deterministic signature-derived description
and never caches it).
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Iterable, Sequence

from .. import config


class BudgetExceeded(RuntimeError):
    """Raised when a call would push the run past its token/cost ceiling."""


class MissingCredentials(RuntimeError):
    """Raised when the provider a role is configured to use has no key.

    Deliberately fatal. A missing key is a misconfiguration, not a degraded
    condition: silently producing deterministic stubs would leave a run that
    *looks* successful — same node count, same exit code, $0.00 cost — while
    every description is worthless. Failing here names the variable to set.
    """


@dataclass(slots=True)
class Usage:
    """Cumulative accounting for a run."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    #: Gemini reasoning tokens, already counted inside `output_tokens`
    thinking_tokens: int = 0
    cost_usd: float = 0.0
    by_role: dict[str, int] = field(default_factory=dict)
    by_provider: dict[str, int] = field(default_factory=dict)
    errors: int = 0

    def add(self, other: "Usage") -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.thinking_tokens += other.thinking_tokens
        self.cost_usd += other.cost_usd
        self.errors += other.errors
        for role, count in other.by_role.items():
            self.by_role[role] = self.by_role.get(role, 0) + count
        for provider, count in other.by_provider.items():
            self.by_provider[provider] = self.by_provider.get(provider, 0) + count

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "thinking_tokens": self.thinking_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "by_role": dict(self.by_role),
            "by_provider": dict(self.by_provider),
            "errors": self.errors,
        }


@dataclass(slots=True)
class Budget:
    """A hard ceiling on one run. ``None`` on a field means unbounded.

    Four dimensions, because a run can be expensive in different ways: too many
    calls, too much money, too much context, or too much wall clock. Whichever
    binds first stops the run — and the orchestrator turns that into a partial
    answer rather than an error.
    """

    max_usd: float | None = config.DEFAULT_BUDGET_USD
    max_calls: int | None = config.DEFAULT_BUDGET_CALLS
    max_input_tokens: int | None = None
    max_seconds: float | None = None
    #: set by `start()`; wall-clock is measured from there, not from construction
    started_at: float | None = None

    @classmethod
    def unlimited(cls) -> "Budget":
        return cls(max_usd=None, max_calls=None, max_input_tokens=None, max_seconds=None)

    @classmethod
    def for_run(cls) -> "Budget":
        """The per-run ceiling from config, clock already started."""
        return cls(
            max_usd=config.RUN_BUDGET_USD,
            max_calls=config.RUN_BUDGET_CALLS,
            max_input_tokens=config.RUN_BUDGET_INPUT_TOKENS,
            max_seconds=config.RUN_BUDGET_SECONDS,
        ).start()

    def start(self) -> "Budget":
        self.started_at = time.monotonic()
        return self

    @property
    def elapsed(self) -> float:
        return 0.0 if self.started_at is None else time.monotonic() - self.started_at

    def check(self, usage: Usage) -> None:
        if self.max_calls is not None and usage.calls >= self.max_calls:
            raise BudgetExceeded(f"call budget exhausted ({usage.calls}/{self.max_calls} calls)")
        if self.max_usd is not None and usage.cost_usd >= self.max_usd:
            raise BudgetExceeded(f"cost budget exhausted (${usage.cost_usd:.4f}/${self.max_usd:.2f})")
        if self.max_input_tokens is not None and usage.input_tokens >= self.max_input_tokens:
            raise BudgetExceeded(
                f"input-token budget exhausted "
                f"({usage.input_tokens}/{self.max_input_tokens} tokens)"
            )
        if self.max_seconds is not None and self.started_at is not None:
            if self.elapsed >= self.max_seconds:
                raise BudgetExceeded(
                    f"time budget exhausted ({self.elapsed:.1f}s/{self.max_seconds:.0f}s)"
                )

    def exceeded(self, usage: Usage) -> str | None:
        """The reason the budget is spent, or None. Non-raising form."""
        try:
            self.check(usage)
        except BudgetExceeded as exc:
            return str(exc)
        return None

    def fraction_remaining(self, usage: Usage) -> float:
        """Headroom left, as the *tightest* of the four ceilings. 1.0 if unbounded.

        The minimum, not the average: a run with 90% of its money left and 5% of
        its clock left has 5% of a budget, and spending on the strength of the
        other three is how a run ends up aborted mid-answer. Used to decide
        whether an optional extra retrieval can be afforded (orchestrator/followup.py).
        """
        fractions = []
        if self.max_calls:
            fractions.append(1.0 - usage.calls / self.max_calls)
        if self.max_usd:
            fractions.append(1.0 - usage.cost_usd / self.max_usd)
        if self.max_input_tokens:
            fractions.append(1.0 - usage.input_tokens / self.max_input_tokens)
        if self.max_seconds and self.started_at is not None:
            fractions.append(1.0 - self.elapsed / self.max_seconds)
        return max(0.0, min(fractions)) if fractions else 1.0

    def tightest_ceiling(self, usage: Usage) -> str:
        """Which of the four is closest to binding — for the trace's reason line."""
        pairs = []
        if self.max_calls:
            pairs.append((1.0 - usage.calls / self.max_calls, "calls"))
        if self.max_usd:
            pairs.append((1.0 - usage.cost_usd / self.max_usd, "cost"))
        if self.max_input_tokens:
            pairs.append((1.0 - usage.input_tokens / self.max_input_tokens, "input tokens"))
        if self.max_seconds and self.started_at is not None:
            pairs.append((1.0 - self.elapsed / self.max_seconds, "time"))
        return min(pairs)[1] if pairs else "nothing"

    def remaining_usd(self, usage: Usage) -> float:
        return float("inf") if self.max_usd is None else max(0.0, self.max_usd - usage.cost_usd)

    def remaining_seconds(self) -> float:
        if self.max_seconds is None or self.started_at is None:
            return float("inf")
        return max(0.0, self.max_seconds - self.elapsed)


@dataclass(slots=True)
class LLMResponse:
    role: str
    model: str
    text: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost_usd: float
    latency_s: float
    stop_reason: str | None = None
    #: set when the model declined; the text is not usable output
    refused: bool = False
    provider: str = config.PROVIDER_ANTHROPIC
    #: Gemini reasoning tokens. Billed at the output rate and already folded into
    #: `output_tokens`; kept separately so a run can see what thinking cost.
    thinking_tokens: int = 0
    #: the sampling temperature actually sent, or None when this call omitted it
    #: (config.TEMPERATURE_EXEMPT_ANTHROPIC_ROLES_WITH_EFFORT) — carried through
    #: so the trace can show, per call, whether determinism's first line of
    #: defense (temperature 0) was even in effect.
    temperature_used: float | None = None


def resolve_role(role: str) -> tuple[str, dict]:
    """(provider, spec) for ``role``, from config alone."""
    provider = config.ROLE_PROVIDER.get(role)
    if provider is None:
        raise KeyError(f"unknown role {role!r}; add it to config.ROLE_PROVIDER")
    spec = config.ROLE_MODELS.get(provider, {}).get(role)
    if spec is None:
        raise KeyError(
            f"role {role!r} has no model configured for provider {provider!r}; "
            f"add it to config.ROLE_MODELS[{provider!r}]"
        )
    return provider, spec


# --------------------------------------------------------------------------- pricing


def price_for(
    model: str, on: date | None = None, *, input_tokens: int = 0
) -> tuple[float, float]:
    """(input, output) USD per 1M tokens for any model on any provider."""
    if model in config.GEMINI_PRICING:
        # 2.5-pro bills a higher rate above the long-context threshold.
        if input_tokens > config.GEMINI_LONG_CONTEXT_THRESHOLD:
            tiered = config.GEMINI_PRICING_LONG_CONTEXT.get(model)
            if tiered is not None:
                return tiered
        return config.GEMINI_PRICING[model]

    if model == "claude-sonnet-5":
        today = on or date.today()
        if today <= date.fromisoformat(config.SONNET_5_INTRO_UNTIL):
            return config.SONNET_5_INTRO_PRICING
    return config.MODEL_PRICING.get(model, (0.0, 0.0))


def cost_of(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    on: date | None = None,
) -> float:
    """Cost in USD. ``output_tokens`` must already include Gemini thinking tokens."""
    price_in, price_out = price_for(model, on, input_tokens=input_tokens)
    cache_read_multiplier = (
        config.GEMINI_CACHE_READ_MULTIPLIER
        if model in config.GEMINI_PRICING
        else config.CACHE_READ_MULTIPLIER
    )
    return (
        input_tokens * price_in
        + output_tokens * price_out
        + cache_read_tokens * price_in * cache_read_multiplier
        + cache_write_tokens * price_in * config.CACHE_WRITE_MULTIPLIER
    ) / 1_000_000


@dataclass(slots=True)
class ProviderResponse:
    """What a provider returns, normalised. Cost is computed by the caller."""

    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    thinking_tokens: int = 0
    stop_reason: str | None = None
    refused: bool = False
    #: what was actually sent, or None when the call omitted the parameter
    #: entirely (the Anthropic extended-thinking exemption — config.py).
    temperature_used: float | None = None


class Provider:
    """A model backend. Availability is decided at construction, never at call time."""

    name: str = ""

    def __init__(self, api_key: str | None = None) -> None:
        self._client: Any = None
        self._unavailable_reason: str | None = None
        self._setup(api_key)

    def _setup(self, api_key: str | None) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _env_key(self, api_key: str | None) -> str | None:
        # config.provider_key reads os.environ, which config.py has already
        # populated from the repo-root .env at import time.
        return api_key or config.provider_key(self.name)

    @property
    def available(self) -> bool:
        return self._client is not None

    @property
    def unavailable_reason(self) -> str | None:
        return self._unavailable_reason

    def generate(
        self,
        spec: dict,
        prompt: str,
        system: str | None,
        max_tokens: int,
        json_mode: bool = False,
    ) -> ProviderResponse:  # pragma: no cover - overridden
        raise NotImplementedError


class AnthropicProvider(Provider):
    """The `anthropic` SDK. Unchanged behaviour from before Gemini existed."""

    name = config.PROVIDER_ANTHROPIC

    def _setup(self, api_key: str | None) -> None:
        try:
            import anthropic
        except ImportError:
            self._unavailable_reason = "the `anthropic` package is not installed"
            return

        # A bare Anthropic() also resolves an `ant auth login` profile, so an
        # unset env var does not by itself mean "no credentials".
        try:
            client = anthropic.Anthropic(
                api_key=self._env_key(api_key),
                max_retries=config.LLM_MAX_RETRIES,
                timeout=config.LLM_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            self._unavailable_reason = f"no Anthropic credentials ({exc.__class__.__name__})"
            return

        # The SDK constructs happily with no credentials and only fails on the
        # first request. Detect that here so availability is honest up front and
        # callers degrade before doing the work, not after.
        if not _has_anthropic_credentials(client):
            self._unavailable_reason = (
                "no Anthropic credentials — set ANTHROPIC_API_KEY, or run `ant auth login`"
            )
            return
        self._client = client

    def generate(
        self,
        spec: dict,
        prompt: str,
        system: str | None,
        max_tokens: int,
        json_mode: bool = False,
    ) -> ProviderResponse:
        # json_mode is advisory here: the prompts state the required shape, and
        # `parse_json_object` tolerates fences either way.
        model = spec["model"]
        request: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            request["system"] = system
        effort = spec.get("effort")
        exempt = bool(effort and model in config.MODELS_SUPPORTING_EFFORT)
        if exempt:
            request["output_config"] = {"effort": effort}
            # Extended thinking (what `effort` turns on for these models) forces
            # the model's own reasoning temperature and rejects an explicit
            # override — see config.py's exemption docstring. Sending 0 here
            # would 400; sending nothing lets the API do what it requires.
            temperature_used = None
        else:
            temperature_used = spec.get("temperature", config.DEFAULT_TEMPERATURE)
            request["temperature"] = temperature_used

        message = self._client.messages.create(**request)
        usage = message.usage
        # Check stop_reason before reading content — a refusal has no usable text.
        refused = message.stop_reason == "refusal"
        text = (
            ""
            if refused
            else "".join(
                block.text
                for block in message.content
                if getattr(block, "type", None) == "text"
            )
        )
        return ProviderResponse(
            text=text,
            model=model,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            stop_reason=message.stop_reason,
            refused=refused,
            temperature_used=temperature_used,
        )


class GeminiProvider(Provider):
    """The `google-genai` SDK."""

    name = config.PROVIDER_GEMINI

    def _setup(self, api_key: str | None) -> None:
        try:
            from google import genai
        except ImportError:
            self._unavailable_reason = "the `google-genai` package is not installed"
            return

        key = self._env_key(api_key)
        if not key:
            self._unavailable_reason = config.missing_key_message(self.name)
            return
        try:
            # A bare Client() has NO request timeout, unlike the Anthropic one
            # above. That is not a cosmetic difference: every role in this
            # project runs on Gemini, the run budget is only checked *between*
            # nodes, and a call that never returns therefore hangs the run past
            # any ceiling. Observed live — a smoke suite sat on one question for
            # 37 minutes against a 150s budget. `HttpOptions.timeout` is in
            # milliseconds (google-genai 2.18).
            from google.genai import types as genai_types

            self._client = genai.Client(
                api_key=key,
                http_options=genai_types.HttpOptions(
                    timeout=int(config.LLM_TIMEOUT_SECONDS * 1000),
                    retry_options=genai_types.HttpRetryOptions(
                        attempts=config.LLM_MAX_RETRIES
                    ),
                ),
            )
        except Exception as exc:
            self._unavailable_reason = f"could not build Gemini client ({exc.__class__.__name__})"

    def generate(
        self,
        spec: dict,
        prompt: str,
        system: str | None,
        max_tokens: int,
        json_mode: bool = False,
    ) -> ProviderResponse:
        from google.genai import types

        model = spec["model"]
        temperature_used = spec.get("temperature", config.DEFAULT_TEMPERATURE)
        options: dict[str, Any] = {
            "max_output_tokens": max_tokens,
            "temperature": temperature_used,
        }
        if json_mode:
            options["response_mime_type"] = "application/json"
        if system:
            options["system_instruction"] = system
        # Only send thinking_config when the role configured one: 2.5-pro rejects
        # a zero budget, and omitting it leaves the model's own default in place.
        budget = spec.get("thinking_budget")
        if budget is not None:
            options["thinking_config"] = types.ThinkingConfig(thinking_budget=budget)

        response = self._client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(**options),
        )

        usage = response.usage_metadata
        prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
        candidate_tokens = getattr(usage, "candidates_token_count", 0) or 0
        thinking_tokens = getattr(usage, "thoughts_token_count", 0) or 0
        cached_tokens = getattr(usage, "cached_content_token_count", 0) or 0

        finish_reason, refused = _gemini_outcome(response)
        text = "" if refused else (_gemini_text(response) or "")

        return ProviderResponse(
            text=text,
            model=model,
            # Gemini reports the cached slice inside prompt_token_count; split it
            # out so it is priced at the cache rate rather than the full rate.
            input_tokens=max(0, prompt_tokens - cached_tokens),
            # Thinking is billed at the output rate — fold it in, or a thinking
            # model looks several times cheaper than it is.
            output_tokens=candidate_tokens + thinking_tokens,
            cache_read_tokens=cached_tokens,
            thinking_tokens=thinking_tokens,
            stop_reason=finish_reason,
            refused=refused,
            temperature_used=temperature_used,
        )


def _gemini_outcome(response: Any) -> tuple[str | None, bool]:
    """(finish_reason, refused). A safety stop is a refusal, not an error."""
    feedback = getattr(response, "prompt_feedback", None)
    if feedback is not None and getattr(feedback, "block_reason", None):
        return str(feedback.block_reason), True

    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None, True
    reason = getattr(candidates[0], "finish_reason", None)
    name = getattr(reason, "name", None) or (str(reason) if reason else None)
    refused = name in {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}
    return name, refused


def _gemini_text(response: Any) -> str:
    """Read text defensively.

    ``response.text`` warns or returns None when a turn produced no text part —
    which happens when thinking consumed the whole output budget — so the parts
    are walked directly.
    """
    chunks: list[str] = []
    for candidate in getattr(response, "candidates", None) or []:
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or []:
            # skip reasoning parts; they are not the answer
            if getattr(part, "thought", False):
                continue
            if getattr(part, "text", None):
                chunks.append(part.text)
    return "".join(chunks).strip()


def _has_anthropic_credentials(client: Any) -> bool:
    """Whether an SDK client can actually authenticate.

    Checks the three sources the SDK resolves in order — API key, auth token,
    and an `ant auth login` profile on disk — because an unset
    ``ANTHROPIC_API_KEY`` alone does not mean there are no credentials.
    """
    if getattr(client, "api_key", None) or getattr(client, "auth_token", None):
        return True
    if getattr(client, "credentials", None):
        return True
    config_dir = os.environ.get("ANTHROPIC_CONFIG_DIR") or os.path.expanduser(
        "~/.config/anthropic"
    )
    credentials = os.path.join(config_dir, "credentials")
    # The directory can exist while holding no profile — `ant auth login` creates
    # it before it succeeds. Require an actual stored profile.
    try:
        return any(name.endswith(".json") for name in os.listdir(credentials))
    except OSError:
        return False


#: provider name -> class. Adding a backend means adding a row here and a
#: `config.ROLE_MODELS` entry — nothing in the call path changes.
PROVIDERS: dict[str, type[Provider]] = {
    config.PROVIDER_ANTHROPIC: AnthropicProvider,
    config.PROVIDER_GEMINI: GeminiProvider,
}


class LLMClient:
    """Role-addressed multi-provider client with per-run accounting and a budget."""

    def __init__(
        self,
        budget: Budget | None = None,
        api_key: str | None = None,
        *,
        enabled: bool = True,
        api_keys: dict[str, str] | None = None,
    ) -> None:
        self.budget = budget or Budget()
        self.usage = Usage()
        self._providers: dict[str, Provider] = {}
        self._disabled_reason: str | None = None

        if not enabled:
            self._disabled_reason = "disabled by caller (--no-llm)"
            return

        # Construct EVERY registered provider, not just the ones roles currently
        # route to. Availability is then known before any work starts, and
        # `status()` can answer "could we switch back?" without a restart.
        keys = dict(api_keys or {})
        if api_key:
            keys.setdefault(config.PROVIDER_ANTHROPIC, api_key)
        for provider_name, factory in PROVIDERS.items():
            self._providers[provider_name] = factory(keys.get(provider_name))

    # -- availability -------------------------------------------------------

    def provider_for(self, role: str) -> Provider | None:
        provider_name, _spec = resolve_role(role)
        return self._providers.get(provider_name)

    def available_for(self, role: str) -> bool:
        """Whether the provider this role is configured to use can be called."""
        provider = self.provider_for(role)
        return provider is not None and provider.available

    def reason_for(self, role: str) -> str | None:
        if self._disabled_reason:
            return self._disabled_reason
        provider = self.provider_for(role)
        if provider is None:
            provider_name, _ = resolve_role(role)
            return f"no provider registered for {provider_name!r}"
        return provider.unavailable_reason

    def require(self, role: str) -> None:
        """Assert ``role`` is callable, or raise naming the missing variable.

        Call this *before* doing the work, so a misconfigured run fails in the
        first second rather than after parsing a repository. Explicitly disabling
        the LLM (``--no-llm``) is not a missing key and does not raise here.
        """
        if self._disabled_reason:
            return
        provider_name, _spec = resolve_role(role)
        provider = self._providers.get(provider_name)
        if provider is not None and provider.available:
            return
        if config.provider_key(provider_name) is None:
            raise MissingCredentials(
                f"role {role!r}: {config.missing_key_message(provider_name)}"
            )
        raise MissingCredentials(
            f"role {role!r}: provider {provider_name!r} is unavailable "
            f"({self.reason_for(role)})"
        )

    @property
    def available(self) -> bool:
        """True when at least one configured provider can be called."""
        return any(provider.available for provider in self._providers.values())

    @property
    def unavailable_reason(self) -> str | None:
        if self._disabled_reason:
            return self._disabled_reason
        if self.available:
            return None
        reasons = {
            name: provider.unavailable_reason
            for name, provider in self._providers.items()
            if provider.unavailable_reason
        }
        return "; ".join(f"{name}: {why}" for name, why in sorted(reasons.items())) or None

    def status(self) -> dict[str, str]:
        """Per-provider availability, for the run summary."""
        return {
            name: "available" if provider.available else (provider.unavailable_reason or "unavailable")
            for name, provider in sorted(self._providers.items())
        }

    # -- calls --------------------------------------------------------------

    def complete(
        self,
        role: str,
        prompt: str,
        system: str | None = None,
        *,
        max_tokens: int | None = None,
        json_mode: bool = False,
    ) -> LLMResponse:
        """One completion for ``role``. Raises if that role's provider is unavailable."""
        provider_name, spec = resolve_role(role)
        provider = self._providers.get(provider_name)
        if provider is None or not provider.available:
            raise RuntimeError(f"LLM unavailable for role {role!r}: {self.reason_for(role)}")

        self.budget.check(self.usage)

        started = time.perf_counter()
        result = provider.generate(
            spec, prompt, system, max_tokens or spec.get("max_tokens", 1024), json_mode
        )
        latency = time.perf_counter() - started

        cost = cost_of(
            result.model,
            result.input_tokens,
            result.output_tokens,
            result.cache_read_tokens,
            result.cache_write_tokens,
        )

        self.usage.calls += 1
        self.usage.input_tokens += result.input_tokens
        self.usage.output_tokens += result.output_tokens
        self.usage.cache_read_tokens += result.cache_read_tokens
        self.usage.cache_write_tokens += result.cache_write_tokens
        self.usage.thinking_tokens += result.thinking_tokens
        self.usage.cost_usd += cost
        self.usage.by_role[role] = self.usage.by_role.get(role, 0) + 1
        self.usage.by_provider[provider_name] = self.usage.by_provider.get(provider_name, 0) + 1

        return LLMResponse(
            role=role,
            model=result.model,
            provider=provider_name,
            text=result.text.strip(),
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cache_read_tokens=result.cache_read_tokens,
            cache_write_tokens=result.cache_write_tokens,
            thinking_tokens=result.thinking_tokens,
            cost_usd=cost,
            latency_s=latency,
            stop_reason=result.stop_reason,
            refused=result.refused,
            temperature_used=result.temperature_used,
        )

    def complete_many(
        self,
        role: str,
        prompts: Sequence[str],
        system: str | None = None,
        *,
        concurrency: int = config.DESCRIBE_CONCURRENCY,
        on_error: Callable[[int, Exception], None] | None = None,
    ) -> list[LLMResponse | None]:
        """Fan out ``prompts`` for one role. Results keep the input order.

        An entry is ``None`` when that prompt failed or the budget ran out —
        callers degrade per item rather than losing the whole batch.
        """
        if not prompts:
            return []
        results: list[LLMResponse | None] = [None] * len(prompts)
        stop = False

        def run(index: int) -> None:
            nonlocal stop
            if stop:
                return
            try:
                results[index] = self.complete(role, prompts[index], system)
            except BudgetExceeded:
                stop = True
            except Exception as exc:  # one bad chunk must not sink the batch
                self.usage.errors += 1
                if on_error is not None:
                    on_error(index, exc)

        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            list(pool.map(run, range(len(prompts))))
        return results


# --------------------------------------------------------------------------- prompts

_PROMPT_DIR = os.path.join(os.path.dirname(__file__), "prompts")


def load_prompt(name: str) -> str:
    """Read `agents/prompts/<name>.md`. Prompts are never inlined in Python (CLAUDE.md)."""
    path = os.path.join(_PROMPT_DIR, f"{name}.md")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def wrap_retrieved(text: str) -> str:
    """Wrap repository text so a prompt can say plainly that it is data, not instruction.

    Design law 5. The tag is only half the job — the prompt file must also tell
    the model that anything inside it is untrusted content to be described, never
    obeyed.
    """
    return f"<retrieved>\n{text}\n</retrieved>"
