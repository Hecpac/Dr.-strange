from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from claw_v2.memory import MemoryStore
from claw_v2.network_proxy import DomainAllowlistEnforcer
from claw_v2.sandbox import SandboxPolicy, sandbox_hook
from claw_v2.sanitizer import extract_structured, sanitize
from claw_v2.types import AgentClass, SanitizedContent

if TYPE_CHECKING:
    from claw_v2.a2a import A2AService
    from claw_v2.skills import SkillRegistry


ToolHandler = Callable[[dict], dict]
_FIRECRAWL_CONTENT_LIMIT = 12_000
_FIRECRAWL_CREDIT_PATTERNS = (
    "insufficient credits",
    "not enough credits",
    "credit balance",
    "payment required",
)
_FIRECRAWL_RATE_LIMIT_PATTERNS = (
    "rate limit",
    "too many requests",
    "429",
)


class FirecrawlUnavailableError(RuntimeError):
    """Raised when Firecrawl cannot serve a request for operational reasons."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"Firecrawl unavailable: {reason}: {detail[:300]}")
        self.reason = reason
        self.detail = detail


def classify_firecrawl_error(text: str, *, status_code: int | None = None) -> str | None:
    normalized = (text or "").lower()
    if any(pattern in normalized for pattern in _FIRECRAWL_CREDIT_PATTERNS) or status_code == 402:
        return "insufficient_credits"
    if any(pattern in normalized for pattern in _FIRECRAWL_RATE_LIMIT_PATTERNS) or status_code == 429:
        return "rate_limited"
    return None


def _firecrawl_post_json(endpoint: str, *, api_key: str, payload: dict, timeout: float) -> dict:
    request = Request(
        f"https://api.firecrawl.dev/v1/{endpoint}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read())
    except HTTPError as exc:
        detail = _read_http_error(exc)
        reason = classify_firecrawl_error(detail, status_code=exc.code)
        if reason is not None:
            raise FirecrawlUnavailableError(reason, detail) from exc
        raise
    reason = classify_firecrawl_error(json.dumps(body, default=str))
    if reason is not None:
        raise FirecrawlUnavailableError(reason, json.dumps(body, default=str))
    return body


def _read_http_error(exc: HTTPError) -> str:
    try:
        raw = exc.read()
    except Exception:
        raw = b""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw)

# Autonomy tiers (per SOUL.md). Enforced in code, not prompt.
#   1 = read-only / local-safe / observation  -> auto-execute, no approval
#   2 = local mutation / run scripts / web read -> auto-execute, logged
#   3 = irreversible / spends money / sends externally -> requires approval
#
# Canonical mapping for default tools (audited 2026-04-24 per HEC-14):
#   Tier 1: Read, Glob, Grep, SearchMemory, WebSearch, WebFetch, WikiSearch,
#           WikiGraph, SkillList, A2ACard, A2APeers, FirecrawlScrape,
#           FirecrawlSearch, FirecrawlExtract
#     note (Bash): Tier 2 assumes the handler stays `external_stub` and the SDK
#       runtime enforces its own shell approval. If Bash ever executes locally,
#       promote to Tier 3.
#     note (Firecrawl): consumes paid credits (~$0.001/call). Rate-limit by
#       credit budget is the circuit breaker. Tier 1 accepted.
#     note (SkillExecute): Tier 3 (fail-safe). A registered skill may invoke
#       sub-tools of any tier; without tier-introspection of the skill body the
#       conservative default is to treat every skill run as approval-gated.
#   Tier 2: Write, Edit, Bash, WikiLint, SkillGenerate, AnalyzeImage
#   Tier 3: WikiDelete, A2ASend, HeyGenVideo, GPTImage, SkillExecute
TIER_READ_ONLY = 1
TIER_LOCAL_MUTATION = 2
TIER_REQUIRES_APPROVAL = 3
DEFAULT_TOOL_TIER = TIER_LOCAL_MUTATION


def tool_requires_approval(tier: int) -> bool:
    """Return True iff a tool of this tier must go through ApprovalManager."""
    return tier >= TIER_REQUIRES_APPROVAL


SUPPORTED_AGENT_CLASSES: tuple[AgentClass, ...] = ("researcher", "operator", "deployer")
DEFAULT_TOOL_AGENT_CLASSES: dict[str, tuple[AgentClass, ...]] = {
    "Read": ("researcher", "operator", "deployer"),
    "Write": ("operator", "deployer"),
    "Edit": ("operator", "deployer"),
    "Glob": ("researcher", "operator", "deployer"),
    "Grep": ("researcher", "operator", "deployer"),
    "Bash": ("operator", "deployer"),
    "WebSearch": ("researcher",),
    "WebFetch": ("researcher",),
    "SearchMemory": ("researcher", "operator", "deployer"),
    "WikiSearch": ("researcher", "operator", "deployer"),
    "WikiLint": ("researcher", "operator", "deployer"),
    "WikiDelete": ("operator", "deployer"),
    "WikiGraph": ("researcher", "operator", "deployer"),
    "SkillList": ("researcher", "operator", "deployer"),
    "SkillGenerate": ("operator", "deployer"),
    "SkillExecute": ("operator", "deployer"),
    "A2ACard": ("researcher", "operator", "deployer"),
    "A2APeers": ("researcher", "operator", "deployer"),
    "A2ASend": ("operator", "deployer"),
    "HeyGenVideo": ("operator", "deployer"),
}


def is_valid_agent_class(value: str) -> bool:
    return value in SUPPORTED_AGENT_CLASSES


def default_allowed_tools_for(agent_class: AgentClass) -> list[str]:
    if not is_valid_agent_class(agent_class):
        raise ValueError(f"agent_class must be one of: {', '.join(SUPPORTED_AGENT_CLASSES)}")
    return sorted(name for name, classes in DEFAULT_TOOL_AGENT_CLASSES.items() if agent_class in classes)


def _ensure_strict_schema(schema: dict) -> dict:
    """Recursively add additionalProperties=false to all object schemas."""
    if not isinstance(schema, dict):
        return schema
    cleaned = dict(schema)
    if cleaned.get("type") == "object" and "additionalProperties" not in cleaned:
        cleaned["additionalProperties"] = False
    properties = cleaned.get("properties")
    if isinstance(properties, dict):
        cleaned["properties"] = {
            key: _ensure_strict_schema(value) for key, value in properties.items()
        }
    items = cleaned.get("items")
    if isinstance(items, dict):
        cleaned["items"] = _ensure_strict_schema(items)
    return cleaned


_OPENAI_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _openai_tool_name(name: str) -> str:
    """Return an OpenAI-compatible function name for a local tool name."""
    if _OPENAI_TOOL_NAME_RE.fullmatch(name):
        return name
    return "".join(
        char if re.fullmatch(r"[a-zA-Z0-9_-]", char) else f"_x{ord(char):02x}_"
        for char in name
    )


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    allowed_agent_classes: tuple[AgentClass, ...]
    handler: ToolHandler
    mutates_state: bool = False
    requires_network: bool = False
    parameter_schema: dict | None = None
    ingests_external_content: bool = False
    sanitize_fields: tuple[str, ...] = ()
    tier: int = DEFAULT_TOOL_TIER


_CODE_FENCE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`[^`]*`")


def _strip_code_blocks(text: str) -> str:
    without_fences = _CODE_FENCE_RE.sub(" ", text)
    return _INLINE_CODE_RE.sub(" ", without_fences)


def _collect_strings(value: object) -> list[str]:
    """Recursively extract non-empty strings from nested lists/dicts."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        parts: list[str] = []
        for v in value.values():
            parts.extend(_collect_strings(v))
        return parts
    if isinstance(value, list):
        parts = []
        for item in value:
            parts.extend(_collect_strings(item))
        return parts
    return []


def _extract_sanitizable_text(result: dict, fields: tuple[str, ...]) -> tuple[str, str | None]:
    """Return (text_to_scan, field_used). Checks declared fields first, then falls back to common keys."""
    candidates = list(fields) if fields else ["content", "text", "body", "markdown", "result", "output"]
    for field_name in candidates:
        value = result.get(field_name)
        if value is None:
            continue
        parts = _collect_strings(value)
        if parts:
            return "\n".join(parts), field_name
    return "", None


def sanitize_tool_output(
    definition: "ToolDefinition",
    result: dict,
    *,
    agent_class: AgentClass,
    source_hint: str | None = None,
) -> dict:
    """Scan external-content tool output for prompt-injection patterns.

    Patterns in code fences / backticks are ignored (quoted content is assumed inert).
    Malicious outputs are replaced with a structured quarantine payload so the agent
    can see that something was filtered instead of silently losing the result.
    """
    if not definition.ingests_external_content:
        return result
    text, field_name = _extract_sanitizable_text(result, definition.sanitize_fields)
    if not text:
        return result
    scrubbed = _strip_code_blocks(text)
    source = source_hint or definition.name
    verdict: SanitizedContent = sanitize(scrubbed, source=source, target_agent_class=agent_class)
    if verdict.verdict != "malicious":
        return result
    quarantine = extract_structured(
        text,
        source_url=result.get("url") if isinstance(result.get("url"), str) else None,
        reason=verdict.reason or "suspicious pattern",
    )
    return {
        "sanitized": True,
        "verdict": "malicious",
        "reason": verdict.reason,
        "source": source,
        "field_quarantined": field_name,
        "quarantine": asdict(quarantine),
    }


ApprovalGate = Callable[["ToolDefinition", dict], None]


class ToolRegistry:
    def __init__(
        self,
        *,
        workspace_root: Path | str,
        memory: MemoryStore | None = None,
        observe: object | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root)
        self.memory = memory
        self.observe = observe
        self._definitions: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        if definition.parameter_schema is not None:
            definition.parameter_schema = _ensure_strict_schema(definition.parameter_schema)
        self._definitions[definition.name] = definition

    def get(self, name: str) -> ToolDefinition:
        if name not in self._definitions:
            raise KeyError(f"Unknown tool '{name}'.")
        return self._definitions[name]

    def allowed_tools(self, agent_class: AgentClass) -> list[str]:
        return sorted(
            definition.name
            for definition in self._definitions.values()
            if agent_class in definition.allowed_agent_classes
        )

    def openai_tool_schemas(self, agent_class: AgentClass | None = None) -> list[dict]:
        """Export tool definitions as OpenAI function-calling schemas."""
        schemas: list[dict] = []
        seen_names: dict[str, str] = {}
        for defn in self._definitions.values():
            if defn.parameter_schema is None:
                continue
            if agent_class and agent_class not in defn.allowed_agent_classes:
                continue
            openai_name = _openai_tool_name(defn.name)
            if openai_name in seen_names and seen_names[openai_name] != defn.name:
                raise ValueError(
                    f"OpenAI tool name collision: {seen_names[openai_name]} and {defn.name}"
                )
            seen_names[openai_name] = defn.name
            schemas.append({
                "type": "function",
                "name": openai_name,
                "description": defn.description,
                "parameters": defn.parameter_schema,
            })
        return schemas

    def original_tool_name_from_openai(self, name: str) -> str:
        if name in self._definitions:
            return name
        for defn in self._definitions.values():
            if _openai_tool_name(defn.name) == name:
                return defn.name
        return name

    def execute(
        self,
        name: str,
        args: dict,
        *,
        agent_class: AgentClass,
        policy: SandboxPolicy | None = None,
        network_enforcer: DomainAllowlistEnforcer | None = None,
        approval_gate: ApprovalGate | None = None,
    ) -> dict:
        definition = self.get(name)
        if agent_class not in definition.allowed_agent_classes:
            raise PermissionError(f"Agent class '{agent_class}' cannot use tool '{name}'.")
        if policy is not None:
            decision = sandbox_hook(
                name,
                args,
                policy=policy,
                network_enforcer=network_enforcer,
                actor=agent_class,
            )
            if not decision.allowed:
                raise PermissionError(decision.reason or "tool invocation blocked by sandbox")
        # Tier enforcement (HEC-14): bypass approval for Tier 1/2, gate Tier 3.
        # Logged to observe stream for audit. approval_gate may raise to block.
        if tool_requires_approval(definition.tier):
            if approval_gate is None:
                raise PermissionError(
                    f"Tool '{name}' is Tier {definition.tier} (requires approval) "
                    f"but no approval_gate was provided to the dispatcher."
                )
            approval_gate(definition, args)
            self._emit_autonomy_event("AUTONOMY_APPROVED", definition, agent_class)
        else:
            self._emit_autonomy_event("AUTONOMY_BYPASS", definition, agent_class)
        result = definition.handler(args)
        if definition.ingests_external_content and isinstance(result, dict):
            return sanitize_tool_output(definition, result, agent_class=agent_class)
        return result

    def _emit_autonomy_event(
        self, event: str, definition: "ToolDefinition", agent_class: AgentClass
    ) -> None:
        if self.observe is None:
            return
        try:
            self.observe.emit(  # type: ignore[attr-defined]
                event,
                lane="tool_dispatcher",
                payload={
                    "tool": definition.name,
                    "tier": definition.tier,
                    "agent_class": agent_class,
                },
            )
        except Exception:
            # Observability is best-effort; never block execution on log failure.
            pass

    async def execute_async(
        self,
        name: str,
        args: dict,
        *,
        agent_class: AgentClass,
        policy: SandboxPolicy | None = None,
        network_enforcer: DomainAllowlistEnforcer | None = None,
        approval_gate: ApprovalGate | None = None,
    ) -> dict:
        """Async-safe wrapper: offloads blocking handlers to a worker thread.

        Use from async call sites (bot, daemon) so that shell/HTTP/file I/O in
        handlers never blocks the event loop. SQLite (WAL) and sandbox/sanitizer
        helpers are thread-safe; handlers with thread-local state should not be
        marked for async execution.
        """
        import asyncio

        return await asyncio.to_thread(
            self.execute,
            name,
            args,
            agent_class=agent_class,
            policy=policy,
            network_enforcer=network_enforcer,
            approval_gate=approval_gate,
        )

    @classmethod
    def default(
        cls,
        *,
        workspace_root: Path | str,
        memory: MemoryStore | None = None,
        wiki: object | None = None,
        skill_registry: SkillRegistry | None = None,
        a2a: A2AService | None = None,
    ) -> "ToolRegistry":
        registry = cls(workspace_root=workspace_root, memory=memory)
        _ws = Path(workspace_root).resolve()

        def _safe_path(raw: str | Path) -> Path:
            resolved = Path(raw).resolve()
            if not resolved.is_relative_to(_ws):
                raise PermissionError(f"path {raw} is outside workspace root")
            return resolved

        def read_file(args: dict) -> dict:
            path = _safe_path(args["path"])
            return {"path": str(path), "content": path.read_text(encoding="utf-8")}

        def read_workspace_nonsecret(args: dict) -> dict:
            from claw_v2.tool_policy import validate_workspace_path

            path = validate_workspace_path(args["path"], workspace_root=workspace_root)
            return {"path": str(path), "content": path.read_text(encoding="utf-8")}

        def write_file(args: dict) -> dict:
            path = _safe_path(args["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args.get("content", ""), encoding="utf-8")
            return {"path": str(path), "written": len(args.get("content", ""))}

        def edit_file(args: dict) -> dict:
            path = _safe_path(args["path"])
            content = path.read_text(encoding="utf-8")
            old_text = args.get("old_text", "")
            new_text = args.get("new_text", "")
            if old_text not in content:
                raise ValueError("old_text not found in file")
            updated = content.replace(old_text, new_text, 1)
            path.write_text(updated, encoding="utf-8")
            return {"path": str(path), "replaced": True}

        def glob_files(args: dict) -> dict:
            root = _safe_path(args.get("root", registry.workspace_root))
            pattern = args.get("pattern", "**/*")
            matches = [str(path) for path in root.glob(pattern)]
            return {"matches": matches[:200]}

        def grep_files(args: dict) -> dict:
            root = _safe_path(args.get("root", registry.workspace_root))
            needle = args.get("query", "")
            matches: list[dict] = []
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    content = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                for line_number, line in enumerate(content.splitlines(), start=1):
                    if needle in line:
                        matches.append({"path": str(path), "line_number": line_number, "line": line})
                        if len(matches) >= 100:
                            return {"matches": matches}
            return {"matches": matches}

        def search_memory(args: dict) -> dict:
            if memory is None:
                raise RuntimeError("memory-backed tool is unavailable")
            return {"matches": memory.search_facts(args.get("query", ""), limit=int(args.get("limit", 10)))}

        def external_stub(args: dict) -> dict:
            return {
                "status": "delegated_to_provider_runtime",
                "input": args,
            }

        registry.register(
            ToolDefinition(
                name="Read",
                description="Read a file from the workspace.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["Read"],
                handler=read_file,
                tier=TIER_READ_ONLY,
                parameter_schema={"type": "object", "properties": {"path": {"type": "string", "description": "Absolute file path"}}, "required": ["path"]},
            )
        )
        registry.register(
            ToolDefinition(
                name="file.read_workspace_nonsecret",
                description="Read a non-secret file from inside WORKSPACE_ROOT (daemon-safe).",
                allowed_agent_classes=("researcher", "operator", "deployer"),
                handler=read_workspace_nonsecret,
                tier=TIER_READ_ONLY,
                parameter_schema={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path inside WORKSPACE_ROOT; secret paths are rejected.",
                        }
                    },
                    "required": ["path"],
                },
            )
        )
        registry.register(
            ToolDefinition(
                name="Write",
                description="Write a file in the workspace.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["Write"],
                handler=write_file,
                mutates_state=True,
                parameter_schema={"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
            )
        )
        registry.register(
            ToolDefinition(
                name="Edit",
                description="Replace a text span inside a file.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["Edit"],
                handler=edit_file,
                mutates_state=True,
                parameter_schema={"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]},
            )
        )
        registry.register(
            ToolDefinition(
                name="Glob",
                description="List files matching a glob pattern.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["Glob"],
                handler=glob_files,
                tier=TIER_READ_ONLY,
                parameter_schema={"type": "object", "properties": {"pattern": {"type": "string", "description": "Glob pattern (e.g. **/*.py)"}, "root": {"type": "string", "description": "Root directory (optional)"}}, "required": ["pattern"]},
            )
        )
        registry.register(
            ToolDefinition(
                name="Grep",
                description="Search text content across files.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["Grep"],
                handler=grep_files,
                tier=TIER_READ_ONLY,
                parameter_schema={"type": "object", "properties": {"query": {"type": "string", "description": "Text to search for"}, "root": {"type": "string", "description": "Root directory (optional)"}}, "required": ["query"]},
            )
        )
        registry.register(
            ToolDefinition(
                name="Bash",
                description="Run an SDK-managed shell command.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["Bash"],
                handler=external_stub,
                mutates_state=True,
                tier=TIER_LOCAL_MUTATION,
            )
        )
        registry.register(
            ToolDefinition(
                name="WebSearch",
                description="Search the web through the provider runtime.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["WebSearch"],
                handler=external_stub,
                requires_network=True,
                ingests_external_content=True,
                sanitize_fields=("content", "markdown", "results", "text"),
                tier=TIER_READ_ONLY,
            )
        )
        registry.register(
            ToolDefinition(
                name="WebFetch",
                description="Fetch a single webpage through the provider runtime.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["WebFetch"],
                handler=external_stub,
                requires_network=True,
                ingests_external_content=True,
                sanitize_fields=("content", "markdown", "text", "body"),
                tier=TIER_READ_ONLY,
            )
        )
        registry.register(
            ToolDefinition(
                name="SearchMemory",
                description="Search stored semantic facts.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["SearchMemory"],
                handler=search_memory,
                tier=TIER_READ_ONLY,
                parameter_schema={"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 10}}, "required": ["query"]},
            )
        )

        def wiki_search(args: dict) -> dict:
            if wiki is None:
                raise RuntimeError("WikiService not configured")
            return {"results": wiki.search(args.get("query", ""), limit=int(args.get("limit", 5)))}  # type: ignore[union-attr]

        def wiki_lint(args: dict) -> dict:
            if wiki is None:
                raise RuntimeError("WikiService not configured")
            deep = args.get("deep", False)
            if deep:
                return wiki.deep_lint(auto_fix=bool(args.get("auto_fix", False)))  # type: ignore[union-attr]
            return wiki.lint()  # type: ignore[union-attr]

        registry.register(
            ToolDefinition(
                name="WikiSearch",
                description="Semantic search across wiki pages. Args: query (str), limit (int, default 5).",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["WikiSearch"],
                handler=wiki_search,
                tier=TIER_READ_ONLY,
                parameter_schema={"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 5}}, "required": ["query"]},
            )
        )
        registry.register(
            ToolDefinition(
                name="WikiLint",
                description="Audit wiki health. Args: deep (bool) for LLM-powered analysis, auto_fix (bool) to auto-deprecate stale pages and create gap stubs.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["WikiLint"],
                handler=wiki_lint,
                tier=TIER_LOCAL_MUTATION,
                parameter_schema={"type": "object", "properties": {"deep": {"type": "boolean", "default": False}, "auto_fix": {"type": "boolean", "default": False}}, "required": []},
            )
        )

        def wiki_delete(args: dict) -> dict:
            if wiki is None:
                raise RuntimeError("WikiService not configured")
            slug = args.get("slug", "")
            if not slug:
                return {"error": "slug is required"}
            return wiki.delete(slug)

        def wiki_graph(args: dict) -> dict:
            if wiki is None:
                raise RuntimeError("WikiService not configured")
            slug = args.get("slug", "")
            if slug:
                edges = wiki._graph.get(slug, [])
                neighbors = wiki._graph_neighbors(slug, depth=int(args.get("depth", 1)))
                return {"slug": slug, "edges": edges, "neighbors": neighbors}
            # Full graph summary
            nodes = list(wiki._graph.keys())
            total_edges = sum(len(v) for v in wiki._graph.values())
            return {"nodes": len(nodes), "total_edges": total_edges, "top_nodes": nodes[:20]}

        registry.register(
            ToolDefinition(
                name="WikiDelete",
                description="Cascade-delete a wiki entry. Removes raw source, wiki page, embeddings, graph edges, and index references. Args: slug (str).",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["WikiDelete"],
                handler=wiki_delete,
                mutates_state=True,
                tier=TIER_REQUIRES_APPROVAL,
                parameter_schema={"type": "object", "properties": {"slug": {"type": "string"}}, "required": ["slug"]},
            )
        )
        registry.register(
            ToolDefinition(
                name="WikiGraph",
                description="Query the knowledge graph. Args: slug (str, optional) for a node's edges & neighbors, depth (int, default 1). Without slug returns graph summary.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["WikiGraph"],
                handler=wiki_graph,
                tier=TIER_READ_ONLY,
                parameter_schema={"type": "object", "properties": {"slug": {"type": "string"}, "depth": {"type": "integer", "default": 1}}, "required": []},
            )
        )

        # --- Memento-Skills tools ---
        def skill_list(args: dict) -> dict:
            if skill_registry is None:
                raise RuntimeError("SkillRegistry not configured")
            return {"skills": skill_registry.list_skills(), "stats": skill_registry.stats()}

        def skill_generate(args: dict) -> dict:
            if skill_registry is None:
                raise RuntimeError("SkillRegistry not configured")
            task = args.get("task", "")
            tags = args.get("tags", [])
            return skill_registry.generate_skill(task_description=task, tags=tags)

        def skill_execute(args: dict) -> dict:
            if skill_registry is None:
                raise RuntimeError("SkillRegistry not configured")
            name = args.get("name", "")
            kwargs = args.get("kwargs", {})
            return skill_registry.execute_skill(name, **kwargs)

        registry.register(ToolDefinition(
            name="SkillList", description="List all registered skills and stats.",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["SkillList"], handler=skill_list,
            tier=TIER_READ_ONLY,
        ))
        registry.register(ToolDefinition(
            name="SkillGenerate",
            description="Generate a new skill from description. Args: task (str), tags (list[str], optional).",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["SkillGenerate"],
            handler=skill_generate, mutates_state=True,
            tier=TIER_LOCAL_MUTATION,
        ))
        registry.register(ToolDefinition(
            name="SkillExecute",
            description="Execute a registered skill. Args: name (str), kwargs (dict, optional).",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["SkillExecute"],
            handler=skill_execute, mutates_state=True,
            tier=TIER_REQUIRES_APPROVAL,
        ))

        # --- A2A Protocol tools ---
        def a2a_card(args: dict) -> dict:
            if a2a is None:
                raise RuntimeError("A2AService not configured")
            return a2a.get_card()

        def a2a_peers(args: dict) -> dict:
            if a2a is None:
                raise RuntimeError("A2AService not configured")
            return {"peers": a2a.list_peers(), "stats": a2a.stats()}

        def a2a_send(args: dict) -> dict:
            if a2a is None:
                raise RuntimeError("A2AService not configured")
            return a2a.send_task(
                to_agent=args.get("to_agent", ""),
                action=args.get("action", ""),
                payload=args.get("payload", {}),
            )

        registry.register(ToolDefinition(
            name="A2ACard", description="Get this agent's A2A identity card.",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["A2ACard"], handler=a2a_card,
            tier=TIER_READ_ONLY,
        ))
        registry.register(ToolDefinition(
            name="A2APeers", description="List registered A2A peer agents and stats.",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["A2APeers"], handler=a2a_peers,
            tier=TIER_READ_ONLY,
        ))
        registry.register(ToolDefinition(
            name="A2ASend",
            description="Send a task to an A2A peer. Args: to_agent (str), action (str), payload (dict).",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["A2ASend"],
            handler=a2a_send, mutates_state=True, requires_network=True,
            tier=TIER_REQUIRES_APPROVAL,
        ))

        # --- HeyGen Video tool ---
        def _heygen_api_key() -> str:
            result = subprocess.run(
                ["security", "find-generic-password", "-a", "heygen", "-s", "HEYGEN_API_KEY", "-w"],
                capture_output=True, text=True, timeout=5,
            )
            key = result.stdout.strip()
            if not key:
                raise RuntimeError("HEYGEN_API_KEY not found in Keychain")
            return key

        def heygen_video(args: dict) -> dict:
            text = args.get("text", "")
            if not text:
                raise ValueError("text is required")
            avatar_id = args.get("avatar_id", "284630e731f04f49ae7ba9f5d839e6bb")
            voice_id = args.get("voice_id", "398936ac428244c6966feefe6d151c6a")
            title = args.get("title", "Claw Briefing")

            api_key = _heygen_api_key()
            payload = json.dumps({
                "video_inputs": [{
                    "character": {"type": "avatar", "avatar_id": avatar_id, "avatar_style": "normal"},
                    "voice": {"type": "text", "input_text": text, "voice_id": voice_id},
                }],
                "title": title,
                "dimension": {"width": 1280, "height": 720},
            }).encode()
            req = Request(
                "https://api.heygen.com/v2/video/generate",
                data=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "X-Api-Key": api_key,
                },
                method="POST",
            )
            with urlopen(req, timeout=30) as resp:
                body = json.loads(resp.read())
            return {"video_id": body.get("data", {}).get("video_id"), "status": body.get("data", {}).get("status")}

        registry.register(ToolDefinition(
            name="HeyGenVideo",
            description="Generate a video with a talking avatar. Args: text (str, required), avatar_id (str, optional), voice_id (str, optional), title (str, optional).",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["HeyGenVideo"],
            handler=heygen_video, mutates_state=True, requires_network=True,
            tier=TIER_REQUIRES_APPROVAL,
            parameter_schema={"type": "object", "properties": {"text": {"type": "string"}, "avatar_id": {"type": "string"}, "voice_id": {"type": "string"}, "title": {"type": "string"}}, "required": ["text"]},
        ))

        # --- GPT Image generation tool ---
        def _openai_api_key() -> str:
            import os as _os
            key = _os.getenv("OPENAI_API_KEY", "")
            if not key:
                result = subprocess.run(
                    ["security", "find-generic-password", "-a", "openai", "-s", "OPENAI_API_KEY", "-w"],
                    capture_output=True, text=True, timeout=5,
                )
                key = result.stdout.strip()
            if not key:
                raise RuntimeError("OPENAI_API_KEY not found")
            return key

        def gpt_image(args: dict) -> dict:
            prompt_text = args.get("prompt", "")
            if not prompt_text:
                raise ValueError("prompt is required")
            size = args.get("size", "1024x1024")
            quality = args.get("quality", "auto")
            api_key = _openai_api_key()
            payload = json.dumps({
                "model": "gpt-image-1",
                "prompt": prompt_text,
                "size": size,
                "quality": quality,
            }).encode()
            req = Request(
                "https://api.openai.com/v1/images/generations",
                data=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read())
            images = body.get("data", [])
            # Save base64 images to files if present
            saved: list[str] = []
            output_dir = registry.workspace_root / "generated_images"
            output_dir.mkdir(exist_ok=True)
            import base64
            import time as _time
            for i, img in enumerate(images):
                if img.get("b64_json"):
                    fname = f"gpt_image_{int(_time.time())}_{i}.png"
                    fpath = output_dir / fname
                    fpath.write_bytes(base64.b64decode(img["b64_json"]))
                    saved.append(str(fpath))
                elif img.get("url"):
                    saved.append(img["url"])
            return {"images": saved, "revised_prompt": images[0].get("revised_prompt", "") if images else ""}

        DEFAULT_TOOL_AGENT_CLASSES["GPTImage"] = ("operator", "deployer")
        registry.register(ToolDefinition(
            name="GPTImage",
            description="Generate images using GPT Image API. Args: prompt (str, required), size (str, default '1024x1024'), quality (str, default 'auto').",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["GPTImage"],
            handler=gpt_image, mutates_state=True, requires_network=True,
            tier=TIER_REQUIRES_APPROVAL,
            parameter_schema={"type": "object", "properties": {"prompt": {"type": "string", "description": "Image description"}, "size": {"type": "string", "enum": ["1024x1024", "1536x1024", "1024x1536"], "default": "1024x1024"}, "quality": {"type": "string", "enum": ["auto", "low", "medium", "high"], "default": "auto"}}, "required": ["prompt"]},
        ))

        # --- GPT Vision / Image Analysis tool ---
        def analyze_image(args: dict) -> dict:
            image_path = args.get("image_path", "")
            image_url = args.get("image_url", "")
            question = args.get("question", "Describe this image in detail.")
            if not image_path and not image_url:
                raise ValueError("image_path or image_url is required")
            api_key = _openai_api_key()
            content: list[dict] = [{"type": "input_text", "text": question}]
            if image_path:
                import base64 as _b64
                p = Path(image_path).resolve()
                if not p.exists():
                    raise ValueError(f"Image file not found: {image_path}")
                data = _b64.b64encode(p.read_bytes()).decode()
                suffix = p.suffix.lower()
                media = {".png": "image/png", ".gif": "image/gif", ".webp": "image/webp"}.get(suffix, "image/jpeg")
                content.append({"type": "input_image", "image_url": f"data:{media};base64,{data}"})
            else:
                content.append({"type": "input_image", "image_url": image_url})
            payload = json.dumps({
                "model": "gpt-5.4-mini",
                "input": [{"role": "user", "content": content}],
            }).encode()
            req = Request(
                "https://api.openai.com/v1/responses",
                data=payload,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req, timeout=60) as resp:
                body = json.loads(resp.read())
            return {"analysis": body.get("output_text", ""), "model": "gpt-5.4-mini"}

        DEFAULT_TOOL_AGENT_CLASSES["AnalyzeImage"] = ("researcher", "operator", "deployer")
        registry.register(ToolDefinition(
            name="AnalyzeImage",
            description="Analyze an image using GPT vision. Args: image_path (str) or image_url (str), question (str, optional).",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["AnalyzeImage"],
            handler=analyze_image, requires_network=True,
            tier=TIER_LOCAL_MUTATION,
            parameter_schema={
                "type": "object",
                "properties": {
                    "image_path": {"type": "string", "description": "Local file path to the image"},
                    "image_url": {"type": "string", "description": "URL of the image to analyze"},
                    "question": {"type": "string", "description": "What to analyze (default: describe the image)", "default": "Describe this image in detail."},
                },
                "required": [],
            },
        ))

        # --- Firecrawl Scrape tool ---
        def _firecrawl_api_key() -> str:
            import os as _os
            key = _os.getenv("FIRECRAWL_API_KEY", "")
            if not key:
                result = subprocess.run(
                    ["security", "find-generic-password", "-a", "firecrawl", "-s", "FIRECRAWL_API_KEY", "-w"],
                    capture_output=True, text=True, timeout=5,
                )
                key = result.stdout.strip()
            if not key:
                raise RuntimeError("FIRECRAWL_API_KEY not found in env or Keychain")
            return key

        def firecrawl_scrape(args: dict) -> dict:
            url = args.get("url", "")
            if not url:
                raise ValueError("url is required")
            formats = args.get("formats", ["markdown"])
            api_key = _firecrawl_api_key()
            body = _firecrawl_post_json(
                "scrape",
                api_key=api_key,
                payload={"url": url, "formats": formats},
                timeout=60,
            )
            data = body.get("data", {})
            return {
                "markdown": data.get("markdown", "")[:_FIRECRAWL_CONTENT_LIMIT],
                "metadata": data.get("metadata", {}),
                "url": data.get("metadata", {}).get("sourceURL", url),
            }

        def firecrawl_search(args: dict) -> dict:
            query = args.get("query", "")
            if not query:
                raise ValueError("query is required")
            limit = int(args.get("limit", 5))
            api_key = _firecrawl_api_key()
            body = _firecrawl_post_json(
                "search",
                api_key=api_key,
                payload={
                    "query": query,
                    "limit": limit,
                    "scrapeOptions": {"formats": ["markdown"]},
                },
                timeout=60,
            )
            results = []
            for item in body.get("data", [])[:limit]:
                results.append({
                    "title": item.get("metadata", {}).get("title", ""),
                    "url": item.get("metadata", {}).get("sourceURL", ""),
                    "markdown": item.get("markdown", "")[:2000],
                })
            return {"results": results, "count": len(results)}

        def firecrawl_extract(args: dict) -> dict:
            url = args.get("url", "")
            if not url:
                raise ValueError("url is required")
            schema = args.get("schema", {})
            if not schema:
                raise ValueError("schema is required")
            prompt = args.get("prompt", "")
            api_key = _firecrawl_api_key()
            body: dict = {"urls": [url], "schema": schema}
            if prompt:
                body["prompt"] = prompt
            result = _firecrawl_post_json(
                "extract",
                api_key=api_key,
                payload=body,
                timeout=90,
            )
            data = result.get("data", [])
            return {"extracted": data[0] if len(data) == 1 else data, "success": result.get("success", False)}

        DEFAULT_TOOL_AGENT_CLASSES["FirecrawlExtract"] = ("researcher", "operator", "deployer")
        DEFAULT_TOOL_AGENT_CLASSES["FirecrawlScrape"] = ("researcher", "operator", "deployer")
        DEFAULT_TOOL_AGENT_CLASSES["FirecrawlSearch"] = ("researcher", "operator", "deployer")
        registry.register(ToolDefinition(
            name="FirecrawlScrape",
            description="Scrape a URL and return markdown content. Works with JS-rendered pages, SPAs, social media. Args: url (str, required), formats (list[str], default ['markdown']).",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["FirecrawlScrape"],
            handler=firecrawl_scrape, requires_network=True,
            ingests_external_content=True,
            sanitize_fields=("markdown", "content", "html", "text"),
            tier=TIER_READ_ONLY,
            parameter_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to scrape"},
                    "formats": {"type": "array", "items": {"type": "string"}, "default": ["markdown"]},
                },
                "required": ["url"],
            },
        ))
        registry.register(ToolDefinition(
            name="FirecrawlSearch",
            description="Search the web and return scraped results with markdown content. Args: query (str, required), limit (int, default 5).",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["FirecrawlSearch"],
            handler=firecrawl_search, requires_network=True,
            ingests_external_content=True,
            sanitize_fields=("markdown", "content", "results"),
            tier=TIER_READ_ONLY,
            parameter_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "default": 5, "description": "Max results"},
                },
                "required": ["query"],
            },
        ))
        registry.register(ToolDefinition(
            name="FirecrawlExtract",
            description="Extract structured data from a URL using a JSON schema. Args: url (str, required), schema (dict, required), prompt (str, optional guidance).",
            allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["FirecrawlExtract"],
            handler=firecrawl_extract, requires_network=True,
            ingests_external_content=True,
            sanitize_fields=("data", "extracted", "markdown", "content"),
            tier=TIER_READ_ONLY,
            parameter_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to extract data from"},
                    "schema": {"type": "object", "description": "JSON schema for the data to extract"},
                    "prompt": {"type": "string", "description": "Optional prompt to guide extraction"},
                },
                "required": ["url", "schema"],
            },
        ))

        return registry
