"""Pure provider/profile policy selection for Kanban runs.

This module does not read credentials, call providers, or mutate lifecycle
state. The caller supplies an already-observed provider/model availability map;
the returned selection is therefore deterministic and suitable for evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


TIERS = {"cheap", "standard", "strong", "human"}
ERROR_TAXONOMY = {
    "AUTH_MISSING": (False, "resolve the managed credential for the authorized profile"),
    "AUTH_INVALID": (False, "refresh the managed credential and recreate the process"),
    "AUTH_SCOPE_DENIED": (False, "use a profile with the required capability"),
    "REGION_UNAVAILABLE": (True, "select an available region in the same policy class"),
    "MODEL_NOT_FOUND": (False, "select a configured model for the requested provider"),
    "MODEL_CAPACITY": (True, "retry the same provider/model under its retry policy"),
    "RATE_LIMIT": (True, "retry according to the provider rate-limit policy"),
    "PROVIDER_TIMEOUT": (True, "retry the same provider/model within the timeout budget"),
    "TRANSPORT_FAILURE": (True, "retry the same provider/model after transport recovery"),
    "MALFORMED_RESPONSE": (False, "discard the response and start a fresh bounded attempt"),
    "CONTEXT_LIMIT": (False, "reduce the input within the same role and capability class"),
    "POLICY_BLOCK": (False, "change the policy-bound request or profile explicitly"),
    "CONFIG_DIVERGENCE": (False, "reconcile the effective configuration with the policy snapshot"),
}
ADMIN_CREDENTIALS = {"gh_token", "github-token", "repository-admin", "repo-admin"}


class ProviderPolicyError(ValueError):
    """A sanitized policy or selection failure."""

    def __init__(
        self,
        code: str,
        *,
        provider: str,
        model: str,
        profile: str,
        detail: str | None = None,
    ) -> None:
        if code not in ERROR_TAXONOMY:
            raise ValueError(f"unknown provider policy error: {code}")
        self.code = code
        self.provider = provider
        self.model = model
        self.profile = profile
        self.detail = detail or ""
        super().__init__(code)

    def to_diagnostic(self) -> dict[str, Any]:
        retryable, recommended_action = ERROR_TAXONOMY[self.code]
        return {
            "code": self.code,
            "provider": self.provider,
            "model": self.model,
            "profile": self.profile,
            "retryable": retryable,
            "recommended_action": recommended_action,
            "secret_values_absent": True,
        }


@dataclass(frozen=True)
class ProviderSelection:
    requested_provider: str
    requested_model: str
    effective_provider: str
    effective_model: str
    fallback_used: bool
    credential_source_class: str
    profile: str
    role_tier: str
    selection_reason: str


def _selection_error(
    code: str, *, profile: str, provider: str, model: str, detail: str | None = None
) -> ProviderPolicyError:
    return ProviderPolicyError(
        code,
        provider=provider,
        model=model,
        profile=profile,
        detail=detail,
    )


def validate_policy(policy: Mapping[str, Any]) -> None:
    """Validate policy structure and fail closed on privilege escalation."""

    roles = policy.get("roles")
    profiles = policy.get("profiles")
    providers = policy.get("providers")
    if not isinstance(roles, Mapping) or not isinstance(profiles, Mapping) or not isinstance(providers, Mapping):
        raise ValueError("policy requires roles, profiles, and providers mappings")
    for role, role_data in roles.items():
        if not isinstance(role_data, Mapping) or role_data.get("tier") not in TIERS:
            raise ValueError(f"invalid tier for role {role}")
        fallbacks = role_data.get("fallbacks", [])
        if fallbacks is None:
            fallbacks = []
        if not isinstance(fallbacks, list):
            raise ValueError(f"fallbacks for role {role} must be a list")
        for candidate in fallbacks:
            if not isinstance(candidate, Mapping):
                raise ValueError(f"fallback for role {role} must be an object")
            if not {"provider", "model", "tier", "capability_class", "order"} <= set(candidate):
                raise ValueError(f"fallback for role {role} is incomplete")
            if candidate["tier"] != role_data["tier"]:
                raise ProviderPolicyError(
                    "POLICY_BLOCK",
                    provider=str(candidate.get("provider", "")),
                    model=str(candidate.get("model", "")),
                    profile=str(role),
                    detail="fallback quality tier differs from role tier",
                )
        orders = [candidate.get("order") for candidate in fallbacks if isinstance(candidate, Mapping)]
        if orders != list(range(1, len(orders) + 1)):
            raise ValueError(f"fallbacks for role {role} must be ordered from 1")
        seen = set()
        for candidate in fallbacks:
            key = (candidate.get("provider"), candidate.get("model"))
            if key in seen:
                raise ValueError(f"fallbacks for role {role} must be unique")
            seen.add(key)
            provider = providers.get(candidate.get("provider"))
            if not isinstance(provider, Mapping):
                raise ValueError(f"fallback for role {role} references an unknown provider")
            configured_models = provider.get("configured_models")
            if configured_models is not None and candidate.get("model") not in configured_models:
                raise ValueError(f"fallback for role {role} references an unknown model")
            if "fallback_to" in candidate or "next" in candidate or "fallbacks" in candidate:
                raise ValueError(f"fallback for role {role} cannot contain a cycle graph")
    for profile, profile_data in profiles.items():
        if not isinstance(profile_data, Mapping):
            raise ValueError(f"profile {profile} must be an object")
        if profile_data.get("role") not in roles:
            raise ValueError(f"profile {profile} references an unknown role")
        if profile_data.get("tier") not in TIERS:
            raise ValueError(f"invalid tier for profile {profile}")
        capabilities = profile_data.get("capabilities", [])
        if not isinstance(capabilities, list) or len(capabilities) != len(set(capabilities)):
            raise ValueError(f"profile {profile} capabilities must be unique")
        forbidden = {str(x).lower() for x in profile_data.get("forbidden_credentials", [])}
        credentials = {str(x).lower() for x in profile_data.get("credentials", [])}
        if profile_data.get("role") != "human" and credentials & ADMIN_CREDENTIALS:
            raise ProviderPolicyError(
                "AUTH_SCOPE_DENIED",
                provider="policy",
                model="policy",
                profile=str(profile),
                detail="worker has an administrative credential",
            )
        if credentials & forbidden:
            raise ProviderPolicyError(
                "AUTH_SCOPE_DENIED",
                provider="policy",
                model="policy",
                profile=str(profile),
                detail="profile credential is explicitly forbidden",
            )


def select_provider(
    policy: Mapping[str, Any],
    *,
    profile: str,
    role: str,
    requested_provider: str,
    requested_model: str,
    available_models: Mapping[str, set[str]],
    credential_source_class: str,
    required_capability: str = "provider-llm",
) -> ProviderSelection:
    """Select the requested provider or an explicit same-class fallback."""

    validate_policy(policy)
    profile_data = policy["profiles"].get(profile)
    role_data = policy["roles"].get(role)
    if not isinstance(profile_data, Mapping) or not isinstance(role_data, Mapping):
        raise _selection_error(
            "CONFIG_DIVERGENCE",
            profile=profile,
            provider=requested_provider,
            model=requested_model,
        )
    if profile_data.get("role") != role or profile_data.get("tier") != role_data.get("tier"):
        raise _selection_error(
            "CONFIG_DIVERGENCE",
            profile=profile,
            provider=requested_provider,
            model=requested_model,
        )
    configured_credential_source = profile_data.get("credential_source_class")
    if configured_credential_source and configured_credential_source != credential_source_class:
        raise _selection_error(
            "AUTH_SCOPE_DENIED",
            profile=profile,
            provider=requested_provider,
            model=requested_model,
        )
    if required_capability not in set(profile_data.get("capabilities", [])):
        raise _selection_error(
            "AUTH_SCOPE_DENIED",
            profile=profile,
            provider=requested_provider,
            model=requested_model,
        )
    provider_data = policy["providers"].get(requested_provider)
    if not isinstance(provider_data, Mapping):
        raise _selection_error(
            "MODEL_NOT_FOUND",
            profile=profile,
            provider=requested_provider,
            model=requested_model,
        )
    capability_class = provider_data.get("capability_class")
    if capability_class != required_capability:
        raise _selection_error(
            "POLICY_BLOCK",
            profile=profile,
            provider=requested_provider,
            model=requested_model,
        )
    if requested_model in set(available_models.get(requested_provider, set())):
        return ProviderSelection(
            requested_provider=requested_provider,
            requested_model=requested_model,
            effective_provider=requested_provider,
            effective_model=requested_model,
            fallback_used=False,
            credential_source_class=credential_source_class,
            profile=profile,
            role_tier=str(role_data["tier"]),
            selection_reason="requested primary is available",
        )

    for candidate in role_data.get("fallbacks", []) or []:
        candidate_provider = str(candidate["provider"])
        candidate_model = str(candidate["model"])
        provider_candidate = policy["providers"].get(candidate_provider, {})
        if candidate.get("capability_class") != capability_class:
            continue
        if provider_candidate.get("capability_class") != capability_class:
            continue
        if candidate.get("tier") != role_data.get("tier"):
            continue
        if candidate_model not in set(available_models.get(candidate_provider, set())):
            continue
        return ProviderSelection(
            requested_provider=requested_provider,
            requested_model=requested_model,
            effective_provider=candidate_provider,
            effective_model=candidate_model,
            fallback_used=True,
            credential_source_class=credential_source_class,
            profile=profile,
            role_tier=str(role_data["tier"]),
            selection_reason="ordered same-class fallback after primary unavailable",
        )
    raise _selection_error(
        "MODEL_NOT_FOUND",
        profile=profile,
        provider=requested_provider,
        model=requested_model,
    )
