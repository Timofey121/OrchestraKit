from __future__ import annotations

import json
from typing import Any

from .config import ProjectConfig


COMPLEXITIES = ("simple", "standard", "complex", "critical")
RISKS = ("low", "medium", "high", "critical")
FAILURES = ("context", "implementation", "capability", "environment")
BRIEF_SECTIONS = ("GOAL", "SOURCES OF TRUTH", "SCOPE", "ACCEPTANCE CRITERIA", "VERIFICATION", "CONTEXT")


def agent_identity(config: ProjectConfig, role: str, profile: str) -> tuple[str, str]:
    if config.roles[role].profile == profile:
        return f"orchestra_{role}", f"orchestra-{role}.toml"
    return f"orchestra_{role}__{profile}", f"orchestra-{role}--{profile}.toml"


def route_task(
    config: ProjectConfig,
    *,
    role: str,
    complexity: str = "standard",
    risk: str = "medium",
    size: str = "substantial",
    independent: bool = False,
    required_capabilities: list[str] | tuple[str, ...] = (),
    failure: str | None = None,
    previous_profile: str | None = None,
    attempt: int = 0,
) -> dict[str, Any]:
    if role not in config.roles:
        raise ValueError(f"unknown role: {role}")
    if complexity not in COMPLEXITIES or risk not in RISKS or size not in {"small", "substantial"}:
        raise ValueError("invalid complexity, risk or size")
    if type(independent) is not bool or type(attempt) is not int or attempt < 0:
        raise ValueError("independent must be boolean and attempt a nonnegative integer")
    if failure is not None and failure not in FAILURES:
        raise ValueError("unknown failure cause")
    if previous_profile is not None and previous_profile not in config.profiles:
        raise ValueError("unknown previous_profile")
    if not isinstance(required_capabilities, (list, tuple)) or not all(isinstance(item, str) and item for item in required_capabilities):
        raise ValueError("required_capabilities must be a list of capability names")

    order = config.routing.profile_order
    role_config = config.roles[role]
    tier = max(COMPLEXITIES.index(complexity), RISKS.index(risk))
    index = min(tier, len(order) - 1) if config.routing.enabled else order.index(role_config.profile)
    if role == "reviewer":
        index = max(index, order.index(role_config.profile))
    if previous_profile is not None and config.routing.enabled:
        index = max(index, order.index(previous_profile))

    action = "execute"
    reason = "Selected from task complexity, risk and configured profile order."
    if failure is not None:
        if failure == "environment":
            action, reason = "stop", "Diagnose the environment before another execution attempt."
        elif failure == "capability":
            action, reason = "keep-root", "Check the unavailable tool, host feature, or execution capability before another model attempt."
        elif attempt >= config.workflow.max_repair_cycles:
            action, reason = "stop", "The configured repair budget is exhausted."
        elif failure == "context":
            action, reason = "gather-context", "Gather missing evidence before changing model strength."
        elif failure == "implementation" and attempt == 0:
            action, reason = "repair", "Repair the concrete failure using its verification evidence."
        elif config.routing.enabled and index + 1 < len(order):
            index += 1
            action, reason = "escalate", "Use the next configured profile with the original criteria and failure evidence."
        else:
            action, reason = "repair", "No stronger enabled profile remains; repair within the remaining budget."

    verification = ("light", "focused", "full", "full")[RISKS.index(risk)]
    if config.workflow.mode in {"strict", "program"}:
        verification = "full"
    execution = "leaf" if role == "reviewer" or (size == "substantial" and independent) else "root"
    if action == 'keep-root':
        execution = 'root'
    compatible = []
    candidates = order[index:] if config.routing.enabled else (role_config.profile,)
    for name in candidates:
        profile = config.profiles[name]
        if profile.provider is None or set(required_capabilities) <= set(config.providers[profile.provider].capabilities):
            compatible.append(name)
    selected = compatible[0] if compatible else None
    if selected is None and action != "stop":
        action, execution = "keep-root", "root"
        reason = "No compatible leaf profile exists. The root must check its own capabilities or report the missing capability."
    profile = config.profiles[selected] if selected else None
    agent_type = agent_identity(config, role, selected)[0] if selected else None
    return {
        "action": action,
        "execution": execution,
        "role": role,
        "profile": selected,
        "model": profile.model if profile else None,
        "effort": profile.effort if profile else None,
        "provider": profile.provider if profile else None,
        "agent_type": agent_type,
        "sandbox": role_config.sandbox,
        "verification": verification,
        "review_levels": list(config.workflow.review_levels),
        "remaining_repairs": max(0, config.workflow.max_repair_cycles - attempt),
        "max_parallel": config.max_parallel,
        "max_brief_chars": config.context.max_brief_chars,
        "max_result_chars": config.context.max_result_chars,
        "max_skill_catalog_tokens": config.context.max_skill_catalog_tokens,
        "max_observed_tokens": config.workflow.max_observed_tokens,
        "reason": reason,
    }


def build_brief(config: ProjectConfig, data: Any) -> str:
    if not isinstance(data, dict):
        raise ValueError("brief must be an object")
    missing = [name for name in BRIEF_SECTIONS if name not in data or data[name] in (None, "", [], {})]
    if missing:
        raise ValueError("brief is missing required section(s): " + ", ".join(missing))
    unknown = set(data) - set(BRIEF_SECTIONS)
    if unknown:
        raise ValueError("unknown brief sections: " + ", ".join(sorted(unknown)))
    sections = []
    for name in BRIEF_SECTIONS:
        value = data[name]
        if not isinstance(value, (str, list, dict)) or (isinstance(value, str) and not value.strip()):
            raise ValueError(f"{name} must contain nonempty text or structured context")
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)
        sections.append(f"{name}\n{text.strip()}")
    brief = "\n\n".join(sections) + "\n"
    if len(brief) > config.context.max_brief_chars:
        raise ValueError(f"brief exceeds context budget ({len(brief)} > {config.context.max_brief_chars} characters); narrow sources, never drop constraints")
    return brief
