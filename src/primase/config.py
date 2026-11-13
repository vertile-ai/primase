"""Declarative experiment config (YAML) → validated pydantic model."""

from __future__ import annotations

import fnmatch
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


class VariantSpec(_Strict):
    """One experimental condition.

    Paths, env, and hooks are generic: a variant may pin a task-directory name,
    inject environment variables, run setup/prebuild shell commands, require
    extra files under the task dir, or restore a prebuilt image tag prefix.
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

    def directory_name(self, variant_name: str) -> str:
        return self.dir or variant_name


class SuiteSpec(_Strict):
    id: str
    backend: str | None = Field(None, description="default: suites.json runner")
    tasks: list[str] | Literal["all"] = "all"
    agent_timeout_multiplier: float | None = None


class AgentSpec(_Strict):
    name: str = "cursor-cli"
    kwargs: dict[str, str] = Field(default_factory=dict)


class BudgetSpec(_Strict):
    max_reported_cost_usd: float = Field(
        0.0,
        ge=0,
        description="abort when the agent CLI itself reports more than this (CLI-reported cost = billed usage)",
    )
    stop_on_credit_billing: bool = Field(True, description="abort on any CLI-reported billed cost > 0")
    max_estimated_cost_usd: float | None = Field(
        None,
        ge=0,
        description="abort when the price-table ESTIMATE summed over the run exceeds this (runaway guard)",
    )
    included_models: list[str] = Field(
        default_factory=list, description="fnmatch patterns of models known to draw from included usage"
    )
    allow_non_included_models: bool = False

    def model_allowed(self, model_id: str) -> bool:
        if self.allow_non_included_models or not self.included_models:
            return True
        return any(fnmatch.fnmatchcase(model_id, p) for p in self.included_models)


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
    models: list[ModelSpec]
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
        for m in self.models:
            if not self.budget.model_allowed(m.id):
                raise ValueError(
                    f"model {m.id!r} is not in budget.included_models; "
                    "set allow_non_included_models: true to bill credits"
                )
        return self

    def resolved_network(self, environ: dict[str, str] | None = None) -> NetworkMode:
        """YAML value, overridden by PRIMASE_NETWORK=open|restricted when set."""
        env = (environ if environ is not None else os.environ).get(NETWORK_ENV, "").strip().lower()
        if not env:
            return self.network
        if env not in ("open", "restricted"):
            raise ValueError(f"{NETWORK_ENV} must be 'open' or 'restricted' (got {env!r})")
        return env  # type: ignore[return-value]

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


def _normalize_legacy(data: dict[str, Any]) -> dict[str, Any]:
    """Accept a few deprecated keys from the earlier anbench YAML shape."""
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
    return data


def load_config(path: str | Path, environ: dict[str, str] | None = None) -> ExperimentConfig:
    p = Path(path).resolve()
    data = _normalize_legacy(yaml.safe_load(p.read_text()) or {})
    cfg = ExperimentConfig.model_validate(data)
    cfg._source = p
    # fail fast if PRIMASE_NETWORK is set to something invalid
    cfg.resolved_network(environ)
    return cfg
