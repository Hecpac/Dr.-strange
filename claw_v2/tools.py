from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from claw_v2.memory import MemoryStore
from claw_v2.network_proxy import DomainAllowlistEnforcer
from claw_v2.sandbox import SandboxPolicy, sandbox_hook
from claw_v2.types import AgentClass


ToolHandler = Callable[[dict], dict]

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
}


def is_valid_agent_class(value: str) -> bool:
    return value in SUPPORTED_AGENT_CLASSES


def default_allowed_tools_for(agent_class: AgentClass) -> list[str]:
    if not is_valid_agent_class(agent_class):
        raise ValueError(f"agent_class must be one of: {', '.join(SUPPORTED_AGENT_CLASSES)}")
    return sorted(name for name, classes in DEFAULT_TOOL_AGENT_CLASSES.items() if agent_class in classes)


@dataclass(slots=True)
class ToolDefinition:
    name: str
    description: str
    allowed_agent_classes: tuple[AgentClass, ...]
    handler: ToolHandler
    mutates_state: bool = False
    requires_network: bool = False


class ToolRegistry:
    def __init__(self, *, workspace_root: Path | str, memory: MemoryStore | None = None) -> None:
        self.workspace_root = Path(workspace_root)
        self.memory = memory
        self._definitions: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
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

    def execute(
        self,
        name: str,
        args: dict,
        *,
        agent_class: AgentClass,
        policy: SandboxPolicy | None = None,
        network_enforcer: DomainAllowlistEnforcer | None = None,
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
        return definition.handler(args)

    @classmethod
    def default(
        cls,
        *,
        workspace_root: Path | str,
        memory: MemoryStore | None = None,
        wiki: object | None = None,
    ) -> "ToolRegistry":
        registry = cls(workspace_root=workspace_root, memory=memory)

        def read_file(args: dict) -> dict:
            path = Path(args["path"])
            return {"path": str(path), "content": path.read_text(encoding="utf-8")}

        def write_file(args: dict) -> dict:
            path = Path(args["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(args.get("content", ""), encoding="utf-8")
            return {"path": str(path), "written": len(args.get("content", ""))}

        def edit_file(args: dict) -> dict:
            path = Path(args["path"])
            content = path.read_text(encoding="utf-8")
            old_text = args.get("old_text", "")
            new_text = args.get("new_text", "")
            if old_text not in content:
                raise ValueError("old_text not found in file")
            updated = content.replace(old_text, new_text, 1)
            path.write_text(updated, encoding="utf-8")
            return {"path": str(path), "replaced": True}

        def glob_files(args: dict) -> dict:
            root = Path(args.get("root", registry.workspace_root))
            pattern = args.get("pattern", "**/*")
            matches = [str(path) for path in root.glob(pattern)]
            return {"matches": matches[:200]}

        def grep_files(args: dict) -> dict:
            root = Path(args.get("root", "."))
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
            )
        )
        registry.register(
            ToolDefinition(
                name="Write",
                description="Write a file in the workspace.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["Write"],
                handler=write_file,
                mutates_state=True,
            )
        )
        registry.register(
            ToolDefinition(
                name="Edit",
                description="Replace a text span inside a file.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["Edit"],
                handler=edit_file,
                mutates_state=True,
            )
        )
        registry.register(
            ToolDefinition(
                name="Glob",
                description="List files matching a glob pattern.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["Glob"],
                handler=glob_files,
            )
        )
        registry.register(
            ToolDefinition(
                name="Grep",
                description="Search text content across files.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["Grep"],
                handler=grep_files,
            )
        )
        registry.register(
            ToolDefinition(
                name="Bash",
                description="Run an SDK-managed shell command.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["Bash"],
                handler=external_stub,
                mutates_state=True,
            )
        )
        registry.register(
            ToolDefinition(
                name="WebSearch",
                description="Search the web through the provider runtime.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["WebSearch"],
                handler=external_stub,
                requires_network=True,
            )
        )
        registry.register(
            ToolDefinition(
                name="WebFetch",
                description="Fetch a single webpage through the provider runtime.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["WebFetch"],
                handler=external_stub,
                requires_network=True,
            )
        )
        registry.register(
            ToolDefinition(
                name="SearchMemory",
                description="Search stored semantic facts.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["SearchMemory"],
                handler=search_memory,
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
                return wiki.deep_lint()  # type: ignore[union-attr]
            return wiki.lint()  # type: ignore[union-attr]

        registry.register(
            ToolDefinition(
                name="WikiSearch",
                description="Semantic search across wiki pages. Args: query (str), limit (int, default 5).",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["WikiSearch"],
                handler=wiki_search,
            )
        )
        registry.register(
            ToolDefinition(
                name="WikiLint",
                description="Audit wiki health. Args: deep (bool) for LLM-powered analysis of contradictions, stale content, and gaps.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["WikiLint"],
                handler=wiki_lint,
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
            )
        )
        registry.register(
            ToolDefinition(
                name="WikiGraph",
                description="Query the knowledge graph. Args: slug (str, optional) for a node's edges & neighbors, depth (int, default 1). Without slug returns graph summary.",
                allowed_agent_classes=DEFAULT_TOOL_AGENT_CLASSES["WikiGraph"],
                handler=wiki_graph,
            )
        )
        return registry
