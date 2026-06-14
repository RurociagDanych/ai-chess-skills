from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml


FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
# Required keys plus optional keys understood by Claude Code skills. Keeping the
# set explicit means a typo in frontmatter still fails loudly while genuine
# Claude-only fields (allowed-tools, etc.) coexist with the Codex interface.
REQUIRED_FRONTMATTER_KEYS = {"name", "description"}
OPTIONAL_FRONTMATTER_KEYS = {
    "allowed-tools",
    "disallowed-tools",
    "disable-model-invocation",
    "when_to_use",
    "license",
    "metadata",
    "context",
    "agent",
}
ALLOWED_FRONTMATTER_KEYS = REQUIRED_FRONTMATTER_KEYS | OPTIONAL_FRONTMATTER_KEYS
QUOTED_FIELD_RE = {
    field: re.compile(rf"^\s*{field}:\s*(['\"]).+\1\s*$", re.MULTILINE)
    for field in ("display_name", "short_description", "default_prompt")
}


def load_frontmatter(skill_file: Path) -> dict[str, object]:
    content = skill_file.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(content)
    if match is None:
        raise ValueError("SKILL.md is missing YAML frontmatter")
    data = yaml.safe_load(match.group(1))
    if not isinstance(data, dict):
        raise ValueError("SKILL.md frontmatter must be a mapping")
    extra_keys = sorted(set(data) - ALLOWED_FRONTMATTER_KEYS)
    if extra_keys:
        raise ValueError(
            "SKILL.md frontmatter contains unsupported keys: "
            f"{', '.join(extra_keys)}; allowed: {', '.join(sorted(ALLOWED_FRONTMATTER_KEYS))}"
        )
    return data


def validate_interface_yaml(skill_dir: Path, skill_name: str) -> None:
    interface_file = skill_dir / "agents" / "openai.yaml"
    if not interface_file.is_file():
        raise ValueError("agents/openai.yaml is missing")

    content = interface_file.read_text(encoding="utf-8")
    for field, pattern in QUOTED_FIELD_RE.items():
        if pattern.search(content) is None:
            raise ValueError(f"agents/openai.yaml field {field} must be present and quoted")

    data = yaml.safe_load(content)
    if not isinstance(data, dict):
        raise ValueError("agents/openai.yaml must be a mapping")

    interface = data.get("interface")
    if not isinstance(interface, dict):
        raise ValueError("agents/openai.yaml must contain an interface mapping")

    for field in ("display_name", "short_description", "default_prompt"):
        value = interface.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"agents/openai.yaml field {field} must be a non-empty string")

    required_prompt_ref = f"${skill_name}"
    default_prompt = interface["default_prompt"]
    if required_prompt_ref not in default_prompt:
        raise ValueError(f"default_prompt must mention {required_prompt_ref}")


def validate_skill_dir(skill_dir: Path) -> None:
    if not skill_dir.exists():
        raise ValueError("skill directory does not exist")
    if not skill_dir.is_dir():
        raise ValueError("skill path is not a directory")

    skill_file = skill_dir / "SKILL.md"
    if not skill_file.is_file():
        raise ValueError("SKILL.md is missing")

    frontmatter = load_frontmatter(skill_file)
    skill_name = frontmatter.get("name")
    if not isinstance(skill_name, str) or not skill_name.strip():
        raise ValueError("SKILL.md frontmatter name must be a non-empty string")
    if skill_dir.name != skill_name:
        raise ValueError(
            f"skill directory basename must match frontmatter name: {skill_dir.name!r} != {skill_name!r}"
        )

    description = frontmatter.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("SKILL.md frontmatter description must be a non-empty string")

    validate_interface_yaml(skill_dir, skill_name)


def load_json(path: Path) -> dict[str, object]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return data


def validate_plugin_manifest(plugin_dir: Path, expected_skills: set[str]) -> None:
    """Validate the Claude Code plugin manifest and marketplace catalog.

    Skills are auto-discovered from the ``skills/`` directory at the plugin
    root, so the manifest only needs a valid ``name``; this keeps the Codex and
    Claude packaging in sync from a single source tree.
    """
    manifest_path = plugin_dir / "plugin.json"
    if not manifest_path.is_file():
        raise ValueError(".claude-plugin/plugin.json is missing")
    manifest = load_json(manifest_path)
    plugin_name = manifest.get("name")
    if not isinstance(plugin_name, str) or not plugin_name.strip():
        raise ValueError("plugin.json must define a non-empty name")

    marketplace_path = plugin_dir / "marketplace.json"
    if not marketplace_path.is_file():
        raise ValueError(".claude-plugin/marketplace.json is missing")
    marketplace = load_json(marketplace_path)
    plugins = marketplace.get("plugins")
    if not isinstance(plugins, list) or not plugins:
        raise ValueError("marketplace.json must list at least one plugin")
    names = {entry.get("name") for entry in plugins if isinstance(entry, dict)}
    if plugin_name not in names:
        raise ValueError(
            f"marketplace.json must list the plugin {plugin_name!r}; found: {sorted(names)}"
        )

    # The plugin ships the skills/ directory verbatim, so the discovered skills
    # must match what the Codex validation walked.
    skills_root = plugin_dir.parent / "skills"
    shipped = {p.name for p in skills_root.iterdir() if p.is_dir()} if skills_root.is_dir() else set()
    if shipped != expected_skills:
        raise ValueError(
            f"skills/ directory {sorted(shipped)} does not match validated skills {sorted(expected_skills)}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate skill metadata for both Codex and Claude Code packaging."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Skill directories to validate. Defaults to every direct child in ./skills.",
    )
    parser.add_argument(
        "--skip-plugin",
        action="store_true",
        help="Skip Claude Code plugin/marketplace manifest validation.",
    )
    return parser.parse_args()


def resolve_skill_dirs(paths: list[Path]) -> list[Path]:
    if paths:
        return paths
    skills_root = Path("skills")
    return sorted(path for path in skills_root.iterdir() if path.is_dir())


def main() -> int:
    args = parse_args()
    skill_dirs = resolve_skill_dirs(args.paths)
    if not skill_dirs:
        print("No skill directories found to validate.", file=sys.stderr)
        return 1

    failures: list[str] = []
    for skill_dir in skill_dirs:
        try:
            validate_skill_dir(skill_dir)
        except (OSError, yaml.YAMLError, ValueError) as exc:
            failures.append(f"{skill_dir}: {exc}")

    # Only validate the plugin manifest against the full skill set, which we
    # have when running the default discovery (no explicit paths given).
    if not args.skip_plugin and not args.paths:
        plugin_dir = Path(".claude-plugin")
        try:
            validate_plugin_manifest(plugin_dir, {d.name for d in skill_dirs})
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"{plugin_dir}: {exc}")

    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
