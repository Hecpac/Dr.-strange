"""Memento-Skills — dynamic skill registry that lets Claw generate, test, and store
executable skills without retraining the underlying model.

Pattern: agent identifies a gap → LLM generates a Python function → sandbox test
→ if it passes, register in the skill library → available for future use.

Skills are stored as individual .py files under ~/.claw/skills/ with metadata.
"""
from __future__ import annotations

import ast
import json
import logging
import textwrap
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from claw_v2.llm import LLMRouter

logger = logging.getLogger(__name__)

_DEFAULT_SKILLS_ROOT = Path.home() / ".claw" / "skills"
_REGISTRY_FILE = "registry.json"
_ALLOWED_IMPORTS = {
    "collections",
    "datetime",
    "decimal",
    "functools",
    "itertools",
    "json",
    "math",
    "random",
    "re",
    "statistics",
    "string",
}
_FORBIDDEN_CALLS = {
    "__import__",
    "breakpoint",
    "compile",
    "delattr",
    "eval",
    "exec",
    "getattr",
    "globals",
    "help",
    "input",
    "locals",
    "open",
    "print",
    "setattr",
    "type",
    "vars",
}
_FORBIDDEN_ATTRS = {
    "chmod",
    "chown",
    "connect",
    "execv",
    "execve",
    "fork",
    "kill",
    "mkdir",
    "open",
    "popen",
    "read_bytes",
    "read_text",
    "remove",
    "rename",
    "replace",
    "rmdir",
    "run",
    "send",
    "socket",
    "spawn",
    "startfile",
    "system",
    "touch",
    "unlink",
    "write_bytes",
    "write_text",
}
_SKILL_HARNESS = textwrap.dedent("""\
    import json
    import resource
    import sys

    resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
    memory_limit = 256 * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
    except (ValueError, OSError):
        pass

    skill_path = sys.argv[1]
    function_name = sys.argv[2]
    kwargs = json.loads(sys.argv[3])
    source = open(skill_path, "r", encoding="utf-8").read()

    _orig_import = __import__

    def _safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        root = name.split(".", 1)[0]
        if root not in {{allowed_imports}}:
            raise ImportError(f"Import '{{root}}' not allowed in generated skills")
        return _orig_import(name, globals, locals, fromlist, level)

    safe_builtins = {
        "__import__": _safe_import,
        "abs": abs,
        "all": all,
        "any": any,
        "bool": bool,
        "dict": dict,
        "enumerate": enumerate,
        "Exception": Exception,
        "filter": filter,
        "float": float,
        "int": int,
        "isinstance": isinstance,
        "len": len,
        "list": list,
        "map": map,
        "max": max,
        "min": min,
        "pow": pow,
        "range": range,
        "reversed": reversed,
        "round": round,
        "set": set,
        "sorted": sorted,
        "str": str,
        "sum": sum,
        "tuple": tuple,
        "zip": zip,
    }
    globals_dict = {"__builtins__": safe_builtins, "__name__": "__skill__"}
    exec(compile(source, skill_path, "exec"), globals_dict, globals_dict)
    func = globals_dict.get(function_name)
    if func is None:
        raise RuntimeError(f"Function '{{function_name}}' not found")
    result = func(**kwargs)
    if not isinstance(result, dict) or "result" not in result:
        raise RuntimeError("Skill must return a dict with a 'result' key")
    print(json.dumps({"passed": True, "output": result}, ensure_ascii=False))
""").replace("{{allowed_imports}}", repr(_ALLOWED_IMPORTS))


@dataclass(slots=True)
class Skill:
    name: str
    description: str
    source_file: str
    function_name: str
    created: str
    use_count: int = 0
    last_used: str | None = None
    tags: list[str] = field(default_factory=list)
    status: str = "active"  # active, deprecated, failed


class SkillRegistry:
    """Manages dynamically generated skills."""

    def __init__(self, *, router: LLMRouter | None = None, root: Path | None = None) -> None:
        self.router = router
        self.root = root or _DEFAULT_SKILLS_ROOT
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._registry: dict[str, Skill] = {}
        self._load_registry()

    def _registry_path(self) -> Path:
        return self.root / _REGISTRY_FILE

    def _load_registry(self) -> None:
        path = self._registry_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for name, entry in data.items():
                self._registry[name] = Skill(
                    name=entry["name"],
                    description=entry["description"],
                    source_file=entry["source_file"],
                    function_name=entry["function_name"],
                    created=entry["created"],
                    use_count=entry.get("use_count", 0),
                    last_used=entry.get("last_used"),
                    tags=entry.get("tags", []),
                    status=entry.get("status", "active"),
                )
        except Exception:
            logger.exception("Failed to load skill registry")

    def _save_registry(self) -> None:
        data = {}
        for name, skill in self._registry.items():
            data[name] = {
                "name": skill.name,
                "description": skill.description,
                "source_file": skill.source_file,
                "function_name": skill.function_name,
                "created": skill.created,
                "use_count": skill.use_count,
                "last_used": skill.last_used,
                "tags": skill.tags,
                "status": skill.status,
            }
        self._registry_path().write_text(json.dumps(data, indent=2), encoding="utf-8")

    def list_skills(self) -> list[dict[str, Any]]:
        return [
            {"name": s.name, "description": s.description, "use_count": s.use_count,
             "status": s.status, "tags": s.tags}
            for s in self._registry.values()
        ]

    def generate_skill(self, *, task_description: str, tags: list[str] | None = None) -> dict:
        """Use LLM to generate a new skill, test it in sandbox, register if valid."""
        if self.router is None:
            return {"success": False, "error": "No LLM router configured"}

        prompt = textwrap.dedent(f"""\
            You are a skill generator for an autonomous AI agent called Claw.
            Generate a single, self-contained Python function that accomplishes this task:

            Task: {task_description}

            Requirements:
            - The function must be named with a descriptive snake_case name.
            - It must be self-contained (import everything it needs inside the function body).
            - It must return a dict with at least a "result" key.
            - It must NOT use any external credentials or API keys.
            - It must complete in under 10 seconds.
            - Include a docstring explaining what it does.

            Respond with ONLY a JSON object:
            {{
                "name": "skill_name",
                "description": "what it does in one line",
                "function_name": "the_function_name",
                "code": "the complete Python code"
            }}
        """)

        try:
            resp = self.router.ask(prompt, lane="worker", max_budget=0.15, timeout=60.0,
                                   evidence_pack={"operation": "generate_skill"})
            content = resp.content.strip()
            if content.startswith("```"):
                import re
                content = re.sub(r"^```[a-zA-Z]*\n?", "", content)
                content = re.sub(r"\n?```$", "", content)
            start = content.find("{")
            end = content.rfind("}") + 1
            if start == -1 or end == 0:
                return {"success": False, "error": "LLM did not return valid JSON"}
            spec = json.loads(content[start:end])
        except Exception as e:
            return {"success": False, "error": f"Generation failed: {e}"}

        name = spec.get("name", "")
        code = spec.get("code", "")
        func_name = spec.get("function_name", name)
        description = spec.get("description", task_description)

        if not name or not code or not func_name:
            return {"success": False, "error": "Incomplete skill spec from LLM"}

        if name in self._registry:
            return {"success": False, "error": f"Skill '{name}' already exists"}

        validation_error = self._validate_skill_code(code, func_name)
        if validation_error is not None:
            return {"success": False, "error": f"Unsafe skill rejected: {validation_error}"}

        # Write skill file
        skill_file = self.root / f"{name}.py"
        skill_file.write_text(code, encoding="utf-8")

        # Test it in sandbox
        test_result = self._test_skill(skill_file, func_name)
        if not test_result["passed"]:
            skill_file.unlink(missing_ok=True)
            return {"success": False, "error": f"Skill test failed: {test_result.get('error', 'unknown')}"}

        # Register
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            skill = Skill(
                name=name,
                description=description,
                source_file=str(skill_file),
                function_name=func_name,
                created=now,
                tags=tags or [],
            )
            self._registry[name] = skill
            self._save_registry()

        logger.info("Skill generated and registered: %s", name)
        return {"success": True, "name": name, "description": description}

    def _test_skill(self, skill_file: Path, func_name: str) -> dict:
        """Execute the skill in a subprocess with restricted builtins/imports."""
        import subprocess
        import sys

        try:
            proc = subprocess.run(
                [sys.executable, "-I", "-c", _SKILL_HARNESS, str(skill_file), func_name, "{}"],
                capture_output=True, text=True, timeout=15,
            )
            if proc.returncode != 0:
                return {"passed": False, "error": proc.stderr[:500]}
            return json.loads(proc.stdout.strip())
        except Exception as e:
            return {"passed": False, "error": str(e)}

    def execute_skill(self, name: str, **kwargs: Any) -> dict:
        """Execute a registered skill by name."""
        skill = self._registry.get(name)
        if not skill or skill.status != "active":
            return {"success": False, "error": f"Skill '{name}' not found or inactive"}
        skill_path = Path(skill.source_file)
        if not skill_path.exists():
            return {"success": False, "error": f"Skill source missing: {skill.source_file}"}
        validation_error = self._validate_skill_code(skill_path.read_text(encoding="utf-8"), skill.function_name)
        if validation_error is not None:
            return {"success": False, "error": f"Skill validation failed: {validation_error}"}

        try:
            result = self._run_skill_subprocess(skill_path, skill.function_name, kwargs)
            with self._lock:
                skill.use_count += 1
                skill.last_used = datetime.now(timezone.utc).isoformat()
                self._save_registry()
            return {"success": True, "result": result}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _run_skill_subprocess(self, skill_file: Path, func_name: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, "-I", "-c", _SKILL_HARNESS, str(skill_file), func_name, json.dumps(kwargs)],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.strip() or proc.stdout.strip()
            raise RuntimeError(stderr[:500] or "Skill execution failed")
        payload = json.loads(proc.stdout.strip())
        if not payload.get("passed"):
            raise RuntimeError(payload.get("error", "Skill execution failed"))
        output = payload.get("output")
        if not isinstance(output, dict):
            raise RuntimeError("Skill output must be a dict")
        return output

    def _validate_skill_code(self, code: str, func_name: str) -> str | None:
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            return f"syntax error: {exc.msg}"

        top_level = [
            node for node in tree.body
            if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str))
        ]
        function_defs = [node for node in top_level if isinstance(node, ast.FunctionDef)]
        if len(function_defs) != 1 or function_defs[0].name != func_name:
            return "skill must define exactly one top-level function matching function_name"
        if any(not isinstance(node, ast.FunctionDef) for node in top_level):
            return "top-level executable statements are not allowed"

        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.ClassDef, ast.Global, ast.Nonlocal, ast.Lambda)):
                return f"unsupported syntax: {type(node).__name__}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".", 1)[0]
                    if root not in _ALLOWED_IMPORTS:
                        return f"import '{root}' is not allowed"
            if isinstance(node, ast.ImportFrom):
                if node.level != 0 or node.module is None:
                    return "relative imports are not allowed"
                root = node.module.split(".", 1)[0]
                if root not in _ALLOWED_IMPORTS:
                    return f"import '{root}' is not allowed"
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_CALLS:
                    return f"call to '{node.func.id}' is not allowed"
                if isinstance(node.func, ast.Attribute) and node.func.attr in _FORBIDDEN_ATTRS:
                    return f"attribute call '{node.func.attr}' is not allowed"
            if isinstance(node, ast.Attribute) and node.attr.startswith("__") and node.attr.endswith("__"):
                return f"dunder attribute access '{node.attr}' is not allowed"
            if isinstance(node, ast.Name) and node.id in {"os", "subprocess", "socket", "sys", "httpx", "requests", "pathlib"}:
                return f"name '{node.id}' is not allowed in generated skills"
        return None

    def discover_gaps(self) -> dict:
        """Use LLM to identify skills that would be useful but don't exist yet."""
        if self.router is None:
            return {"gaps": [], "error": "No LLM router"}

        existing = "\n".join(
            f"- {s.name}: {s.description}" for s in self._registry.values()
            if s.status == "active"
        ) or "(no skills registered yet)"

        prompt = textwrap.dedent(f"""\
            You are analyzing an autonomous AI agent called Claw that runs 24/7.
            It has a wiki, web scraping, site monitoring, social publishing, and deploy capabilities.

            Existing skills:
            {existing}

            Suggest 3 useful skills that are MISSING and would help the agent work more autonomously.
            Each skill should be a concrete, self-contained utility function.

            Respond with ONLY a JSON array:
            [{{"name": "skill_name", "description": "what it does", "task_description": "detailed spec for generating it"}}]
        """)

        try:
            resp = self.router.ask(
                prompt,
                lane="judge",
                evidence_pack={
                    "operation": "skill_gap_discovery",
                    "active_skill_count": sum(1 for skill in self._registry.values() if skill.status == "active"),
                    "active_skills": [
                        {"name": skill.name, "description": skill.description}
                        for skill in self._registry.values()
                        if skill.status == "active"
                    ][:50],
                },
                max_budget=0.10,
                timeout=60.0,
            )
            content = resp.content.strip()
            start = content.find("[")
            end = content.rfind("]") + 1
            if start == -1 or end == 0:
                return {"gaps": []}
            gaps = json.loads(content[start:end])
            return {"gaps": gaps[:3]}
        except Exception as e:
            return {"gaps": [], "error": str(e)}

    def auto_expand(self, *, max_new: int = 2) -> dict:
        """Discover gaps and auto-generate skills to fill them. Designed for cron."""
        gap_result = self.discover_gaps()
        gaps = gap_result.get("gaps", [])
        generated = 0
        for gap in gaps[:max_new]:
            task = gap.get("task_description", gap.get("description", ""))
            if not task:
                continue
            result = self.generate_skill(task_description=task, tags=["auto-generated"])
            if result.get("success"):
                generated += 1
        logger.info("Skill auto_expand: gaps=%d generated=%d", len(gaps), generated)
        return {"gaps_found": len(gaps), "skills_generated": generated}

    def stats(self) -> dict:
        active = sum(1 for s in self._registry.values() if s.status == "active")
        total_uses = sum(s.use_count for s in self._registry.values())
        return {"total_skills": len(self._registry), "active": active, "total_uses": total_uses}
