"""Declarative experiment config (YAML) → validated pydantic model."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Tier = Literal["low", "medium", "high"]
NetworkMode = Literal["open", "restricted"]

NETWORK_ENV = "PRIMASE_NETWORK"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelSpec(_Strict):
    id: str = Field(description="exact model id as accepted by the agent CLI, e.g. composer-2.5")
    label: str | None = None
    provider_prefix: str = Field("cursor", description="harness model string is '<prefix>/<id>'")

    @property
    def harness_name(self) -> str:
        return f"{self.provider_prefix}/{self.id}" if self.provider_prefix else self.id


class AgentSpec(_Strict):
    """Agent selection. Product-specific knobs live under ``plugin`` (validated by the agent plugin)."""

    name: str = "cursor-cli"
    kwargs: dict[str, Any] = Field(
        default_factory=dict,
        description="forwarded to Harbor/Pier as --ak key=value (e.g. config path for the agent CLI)",
    )
    plugin: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "opaque plugin config. For cursor-cli: included_models (fnmatch list), allow_non_included_models (bool)."
        ),
    )


class McpServerSpec(_Strict):
    """One MCP server (mirrors Harbor's ``[[environment.mcp_servers]]``)."""

    name: str
    transport: Literal["stdio", "sse", "streamable-http"] = "stdio"
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict, description="local backend only (Harbor ignores it)")

    @model_validator(mode="after")
    def _transport_fields(self) -> McpServerSpec:
        if self.transport == "stdio" and not self.command:
            raise ValueError(f"mcp server {self.name!r}: stdio transport needs 'command'")
        if self.transport != "stdio" and not self.url:
            raise ValueError(f"mcp server {self.name!r}: {self.transport} transport needs 'url'")
        return self


class VariantSpec(_Strict):
    """One experimental condition — a general override bundle, not only an env patch.

    A variant may override the agent (name/kwargs/plugin), the model, arbitrary
    task.toml fields, plus the classic env/setup/prebuild/require_paths/image_prefix.
    Comparing stock-vs-agentic runtimes, agent-config A-vs-B, or agent X-vs-Y all
    use this same mechanism.
    """

    description: str = ""
    dir: str | None = Field(
        None,
        description="subdirectory of work/variants/<suite>/ (defaults to the variant name)",
    )
    env: dict[str, str] = Field(default_factory=dict, description="extra env vars for this variant's runner process")
    setup: list[str] = Field(default_factory=list, description="shell commands run before each trial (cwd = task dir)")
    prebuild: list[str] = Field(
        default_factory=list, description="shell commands run once before the first trial of this variant"
    )
    require_paths: list[str] = Field(
        default_factory=list,
        description="paths relative to the task dir that must exist (validate fails otherwise)",
    )
    image_prefix: str | None = Field(
        None,
        description="if the task's docker_image starts with this prefix, restore a 'keep-' tag before run",
    )
    agent: AgentSpec | None = Field(
        None,
        description="override the experiment-level agent (name, kwargs, plugin) for this variant only",
    )
    model: ModelSpec | str | None = Field(
        None,
        description=(
            "override model for this variant. String form is a model id (provider_prefix taken from "
            "a matching top-level models[] entry, else 'cursor'). When set, this variant is not "
            "crossed with the top-level models list."
        ),
    )
    task_toml: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description=(
            "arbitrary task.toml table.key overrides, e.g. {agent: {timeout_sec: 600}, environment: {cpus: 2}}"
        ),
    )

    instruction_prepend: str | None = Field(
        None,
        description=(
            "text prepended to the task's instruction.md in the per-trial overlay (e.g. tool-usage rules). "
            "A value starting with '@' is a file path relative to root."
        ),
    )
    instruction_append: str | None = Field(
        None, description="like instruction_prepend, appended after the task instruction"
    )
    overlay_files: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "files/dirs copied into the per-trial task overlay: {dest relative to task dir: src relative to root}. "
            "E.g. ship a skill, an AGENTS.md, or a Dockerfile layer that installs a tool."
        ),
    )
    mcp_servers: list[McpServerSpec] = Field(
        default_factory=list,
        description=(
            "MCP servers exposed to the agent for this variant. Harbor: rendered as "
            "[[environment.mcp_servers]] in the task.toml overlay. local backend: written to an MCP config "
            "file handed to the agent CLI."
        ),
    )

    def has_task_overlay(self) -> bool:
        return bool(
            self.task_toml
            or self.instruction_prepend
            or self.instruction_append
            or self.overlay_files
            or self.mcp_servers
        )

    def directory_name(self, variant_name: str) -> str:
        return self.dir or variant_name

    def resolved_model(self, catalog: list[ModelSpec]) -> ModelSpec | None:
        if self.model is None:
            return None
        if isinstance(self.model, ModelSpec):
            return self.model
        mid = self.model
        for m in catalog:
            if m.id == mid:
                return m
        return ModelSpec(id=mid)

    def resolved_agent(self, default: AgentSpec) -> AgentSpec:
        if self.agent is None:
            return default
        # shallow-merge plugin/kwargs so a variant can override just kwargs
        # only switch agents when the variant names one explicitly; AgentSpec.name has a default,
        # so `agent: {kwargs: {...}}` must keep the experiment-level agent
        name = self.agent.name if "name" in self.agent.model_fields_set else default.name
        return AgentSpec(
            name=name or default.name,
            kwargs={**default.kwargs, **self.agent.kwargs},
            plugin={**default.plugin, **self.agent.plugin},
        )


class SuiteSpec(_Strict):
    id: str
    backend: str | None = Field(None, description="default: suites.json runner")
    tasks: list[str] | Literal["all"] = "all"
    agent_timeout_multiplier: float | None = None


class BudgetSpec(_Strict):
    """Generic spend caps. Product-specific allow-lists live on ``agent.plugin``."""

    max_reported_cost_usd: float = Field(
        0.0,
        ge=0,
        description="abort when the agent CLI itself reports more than this (CLI-reported cost = billed usage)",
    )
    stop_on_billed_cost: bool = Field(
        True,
        description="abort on any CLI-reported billed cost > 0 (generic; replaces stop_on_credit_billing)",
    )
    max_estimated_cost_usd: float | None = Field(
        None,
        ge=0,
        description="abort when the price-table ESTIMATE summed over the run exceeds this (runaway guard)",
    )


class ExperimentConfig(_Strict):
    name: str
    description: str = ""
    root: Path = Field(Path(".."), description="benchmark/ dir, relative to this config file")
    jobs_dir: Path = Path("jobs")
    results_dir: Path = Path("results")
    registry: Path = Path("suites.json")
    env_file: Path | None = None
    secrets: list[str] = Field(default_factory=list)
    agent: AgentSpec = Field(default_factory=AgentSpec)
    models: list[ModelSpec] = Field(default_factory=list)
    variants: dict[str, VariantSpec]
    baseline: str | None = Field(None, description="variant name treated as the comparison baseline")
    tiers: list[Tier] = Field(default_factory=lambda: ["low"])  # type: ignore[arg-type]
    suites: list[SuiteSpec]
    trials: int = Field(1, ge=1)
    concurrency: int = Field(1, ge=1)
    agent_timeout_multiplier: float | None = None
    env: Literal["docker", "modal", "daytona"] = "docker"
    network: NetworkMode = Field(
        "open",
        description=(
            "agent network policy. Default 'open' (we measure token cost, so the agent must reach its API). "
            "Set 'restricted' to attach harbor_restricted_compose / rely on the backend's egress controls. "
            "Overridden by the PRIMASE_NETWORK environment variable."
        ),
    )
    harbor_restricted_compose: Path | None = Field(
        None,
        description=(
            "when network=restricted, Harbor is passed --extra-docker-compose this file (path relative to root)"
        ),
    )
    extra_args: dict[str, list[str]] = Field(default_factory=dict, description="extra argv per backend name")
    budget: BudgetSpec = Field(default_factory=BudgetSpec)

    _source: Path | None = None

    @field_validator("name")
    @classmethod
    def _slug(cls, v: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,47}", v):
            raise ValueError("name must be a lowercase slug (a-z0-9._-, <=48 chars)")
        return v

    @field_validator("network")
    @classmethod
    def _network(cls, v: str) -> str:
        if v not in ("open", "restricted"):
            raise ValueError("network must be 'open' or 'restricted'")
        return v

    @model_validator(mode="after")
    def _check(self) -> ExperimentConfig:
        if not self.variants:
            raise ValueError("at least one variant is required")
        if self.baseline is None:
            self.baseline = next(iter(self.variants))
        if self.baseline not in self.variants:
            raise ValueError(f"baseline {self.baseline!r} not in variants {list(self.variants)}")
        ids = [m.id for m in self.models]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate model ids")
        # every variant must resolve to at least one model
        for vname, v in self.variants.items():
            if v.model is None and not self.models:
                raise ValueError(f"variant {vname!r} has no model override and experiment.models is empty")
        # agent-plugin budget hooks (e.g. cursor-cli included_models) fail fast at load
        from .agents import budget_hooks_for
        from .budget import BudgetExceeded

        try:
            for _vname, v in self.variants.items():
                agent = v.resolved_agent(self.agent)
                hooks = budget_hooks_for(agent.name, agent.plugin)
                for m in self.models_for_variant(v):
                    for h in hooks:
                        h.check_model(m.id)
            if self.models:
                hooks = budget_hooks_for(self.agent.name, self.agent.plugin)
                for m in self.models:
                    for h in hooks:
                        h.check_model(m.id)
        except BudgetExceeded as e:
            raise ValueError(str(e)) from e
        return self

    def resolved_network(self, environ: dict[str, str] | None = None) -> NetworkMode:
        """YAML value, overridden by PRIMASE_NETWORK=open|restricted when set."""
        env = (environ if environ is not None else os.environ).get(NETWORK_ENV, "").strip().lower()
        if not env:
            return self.network
        if env not in ("open", "restricted"):
            raise ValueError(f"{NETWORK_ENV} must be 'open' or 'restricted' (got {env!r})")
        return env  # type: ignore[return-value]

    def models_for_variant(self, variant: VariantSpec) -> list[ModelSpec]:
        resolved = variant.resolved_model(self.models)
        if resolved is not None:
            return [resolved]
        return list(self.models)

    def canonical_dict(self) -> dict[str, Any]:
        """Resolved, hashable experiment config (paths as strings, no private attrs)."""
        data = self.model_dump(mode="json")
        return data

    def config_hash(self) -> str:
        blob = json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    # resolved paths -------------------------------------------------------------------------
    @property
    def root_dir(self) -> Path:
        base = self._source.parent if self._source else Path.cwd()
        return (base / self.root).resolve()

    def path(self, p: Path) -> Path:
        p = Path(p).expanduser()
        return p if p.is_absolute() else (self.root_dir / p)

    @property
    def jobs_path(self) -> Path:
        return self.path(self.jobs_dir)

    @property
    def results_path(self) -> Path:
        return self.path(self.results_dir) / self.name

    @property
    def env_file_path(self) -> Path | None:
        return Path(self.env_file).expanduser() if self.env_file else None

    @property
    def baseline_name(self) -> str:
        assert self.baseline is not None
        return self.baseline

    @property
    def source_path(self) -> Path | None:
        return self._source


def _normalize_legacy(data: dict[str, Any]) -> dict[str, Any]:
    """Accept deprecated keys from earlier YAML shapes and migrate cursor budget knobs."""
    data = dict(data)
    if "arms" in data and "variants" not in data:
        variants: dict[str, Any] = {}
        for name, spec in (data.pop("arms") or {}).items():
            spec = dict(spec or {})
            if "variant" in spec and "dir" not in spec:
                spec["dir"] = spec.pop("variant")
            else:
                spec.pop("variant", None)
            variants[name] = spec
        data["variants"] = variants
    if "baseline_arm" in data and "baseline" not in data:
        data["baseline"] = data.pop("baseline_arm")
    else:
        data.pop("baseline_arm", None)
    if "open_agent_network" in data and "network" not in data:
        data["network"] = "open" if data.pop("open_agent_network") else "restricted"
    else:
        data.pop("open_agent_network", None)

    # Migrate Cursor-specific budget keys into agent.plugin; rename stop_on_credit_billing.
    budget = dict(data.get("budget") or {})
    agent_raw = data.get("agent")
    agent: dict[str, Any] = dict(agent_raw) if isinstance(agent_raw, dict) else {"name": "cursor-cli"}
    plugin_raw = agent.get("plugin") or {}
    plugin: dict[str, Any] = dict(plugin_raw) if isinstance(plugin_raw, dict) else {}
    moved = False
    for key in ("included_models", "allow_non_included_models"):
        if key in budget and key not in plugin:
            plugin[key] = budget.pop(key)
            moved = True
    if "stop_on_credit_billing" in budget:
        sob = budget.pop("stop_on_credit_billing")
        if "stop_on_billed_cost" not in budget:
            budget["stop_on_billed_cost"] = sob
    if moved or plugin:
        agent = {**agent, "plugin": plugin}
        data["agent"] = agent
    if budget or "budget" in data:
        data["budget"] = budget
    return data


def load_config(path: str | Path, environ: dict[str, str] | None = None) -> ExperimentConfig:
    p = Path(path).resolve()
    data = _normalize_legacy(yaml.safe_load(p.read_text()) or {})
    cfg = ExperimentConfig.model_validate(data)
    cfg._source = p
    # fail fast if PRIMASE_NETWORK is set to something invalid
    cfg.resolved_network(environ)
    return cfg
