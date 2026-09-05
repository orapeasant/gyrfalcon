"""Skills tool — list, view, and manage skills (Anthropic folder-based standard).

Each skill lives in its own folder:
    ~/.gyrfalcon/skills/<skill_folder>/
        SKILL.md          ← required: YAML frontmatter + markdown body
        references/       ← optional: reference documents
        scripts/          ← optional: helper scripts

Skill folder name = skill name with spaces replaced by underscores.
Legacy flat .md files in the skills dir are still readable but new skills
are always created in folder form.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

import yaml

from gyrfalcon.tools import registry
from gyrfalcon.gyrfalcon_constants import get_skills_dir, get_optional_skills_dir
from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("tools.skills")


# ── Helpers ───────────────────────────────────────────────────────────────────

def skill_name_to_folder(name: str) -> str:
    """Convert a skill name to a safe folder name (spaces → underscores)."""
    return name.strip().replace(" ", "_").replace("/", "_").replace("\\", "_")


def folder_to_skill_name(folder: str) -> str:
    """Best-effort reverse: underscores back to spaces for display."""
    return folder.replace("_", " ")


def _list_dir_files(path: Path) -> list[dict]:
    """Return sorted list of {name, size, path} for files in a directory."""
    if not path.exists() or not path.is_dir():
        return []
    return sorted(
        [
            {"name": f.name, "size": f.stat().st_size, "path": str(f)}
            for f in path.iterdir()
            if f.is_file() and not f.name.startswith(".")
        ],
        key=lambda x: x["name"],
    )


def parse_skill_md(path: Path, folder_name: str | None = None) -> dict:
    """Parse a SKILL.md file into metadata + content."""
    logger.debug("Beginning of parse_skill_md")
    try:
        content = path.read_text(errors="replace")
    except OSError:
        return {}

    metadata: dict = {}
    body = content

    # Extract YAML frontmatter
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            try:
                metadata = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                pass
            body = parts[2].strip()

    # Derive display name: prefer frontmatter, then folder name → human readable
    if folder_name:
        default_name = folder_to_skill_name(folder_name)
    else:
        default_name = path.stem

    skill_dir = path.parent if path.name == "SKILL.md" else None

    result = {
        "name":         metadata.get("name", default_name),
        "description":  metadata.get("description", ""),
        "version":      metadata.get("version", "1.0.0"),
        "author":       metadata.get("author", ""),
        "platforms":    metadata.get("platforms", []),
        "required_environment_variables": metadata.get("required_environment_variables", []),
        "metadata":     metadata.get("metadata", {}),
        "body":         body,
        "path":         str(path),
        "folder":       folder_name or "",
        # Sub-directory listings (empty for legacy flat files)
        "references":   _list_dir_files(skill_dir / "references") if skill_dir else [],
        "scripts":      _list_dir_files(skill_dir / "scripts")    if skill_dir else [],
    }
    return result


# ── Discover ──────────────────────────────────────────────────────────────────

def discover_skills(skills_dir: Path | None = None) -> list[dict]:
    """Discover all available skills from the skills directory.

    Priority:
      1. Folder-based skills (folder/SKILL.md)  — type: "user"
      2. Legacy flat .md files                  — type: "legacy"
    """
    logger.debug("Beginning of discover_skills")
    if skills_dir is None:
        skills_dir = get_skills_dir()

    if not skills_dir.exists():
        return []

    skills: list[dict] = []
    seen_names: set[str] = set()

    # 1. Folder-based skills (Anthropic standard)
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("."):
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue
        skill = parse_skill_md(skill_file, folder_name=skill_dir.name)
        if skill:
            skill["type"] = "user"
            skills.append(skill)
            seen_names.add(skill["name"])

    # 2. Legacy flat .md files (backward compat — read-only)
    for md_file in sorted(skills_dir.glob("*.md")):
        if md_file.name.startswith(".") or md_file.name == "SKILL.md":
            continue
        skill = parse_skill_md(md_file)
        if skill and skill["name"] not in seen_names:
            skill["type"] = "legacy"
            skill["references"] = []
            skill["scripts"] = []
            skills.append(skill)

    return skills


# ── skill_view ────────────────────────────────────────────────────────────────

def skill_view(args: dict, **kwargs) -> str:
    """Load full skill content by name or path."""
    logger.debug("Beginning of skill_view")
    name   = args.get("name", "")
    file_path = args.get("file_path")

    if file_path:
        path = Path(file_path)
        if path.exists():
            folder = path.parent.name if path.name == "SKILL.md" else None
            return json.dumps(parse_skill_md(path, folder_name=folder))
        return json.dumps({"error": f"Skill file not found: {file_path}"})

    if not name:
        return json.dumps({"error": "name or file_path required"})

    skills_dir = get_skills_dir()
    folder_name = skill_name_to_folder(name)

    candidates = [
        (skills_dir / folder_name / "SKILL.md",       folder_name),
        (skills_dir / name        / "SKILL.md",       name),
        (skills_dir / f"{name}.md",                   None),
        (skills_dir / f"{folder_name}.md",            None),
    ]

    for path, fld in candidates:
        if path.exists():
            return json.dumps(parse_skill_md(path, folder_name=fld))

    return json.dumps({"error": f"Skill not found: {name}"})


# ── skill_manage ──────────────────────────────────────────────────────────────

def skill_manage(args: dict, **kwargs) -> str:
    """Manage skills: create, edit, delete, rename, add_reference, add_script,
    delete_reference, delete_script, migrate."""
    logger.debug("Beginning of skill_manage")
    action = args.get("action", "")
    name   = args.get("name", "")

    if not action:
        return json.dumps({"error": "action is required"})

    skills_dir = get_skills_dir()

    # ── create ───────────────────────────────────────────────────────────────
    if action == "create":
        content     = args.get("content", "")
        description = args.get("description", "")
        if not name or not content:
            return json.dumps({"error": "name and content required for create"})

        folder_name = skill_name_to_folder(name)
        skill_dir   = skills_dir / folder_name
        skill_file  = skill_dir / "SKILL.md"

        if skill_dir.exists():
            return json.dumps({"error": f"Skill '{name}' already exists"})

        # Ensure frontmatter is present; inject if bare content given
        if not content.strip().startswith("---"):
            fm = {"name": name, "description": description,
                  "version": "1.0.0", "author": "agent"}
            content = f"---\n{yaml.dump(fm, default_flow_style=False)}---\n\n{content}"

        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(content, encoding="utf-8")

        return json.dumps({"status": "created", "folder": folder_name,
                           "path": str(skill_file)})

    # ── edit ─────────────────────────────────────────────────────────────────
    elif action == "edit":
        content = args.get("content", "")
        if not name or not content:
            return json.dumps({"error": "name and content required for edit"})

        folder_name = skill_name_to_folder(name)
        skill_file  = _resolve_skill_file(skills_dir, name, folder_name)

        if skill_file is None:
            return json.dumps({"error": f"Skill '{name}' not found"})

        skill_file.write_text(content, encoding="utf-8")
        return json.dumps({"status": "updated", "path": str(skill_file)})

    # ── delete ────────────────────────────────────────────────────────────────
    elif action == "delete":
        if not name:
            return json.dumps({"error": "name required for delete"})

        folder_name = skill_name_to_folder(name)
        archive_dir = skills_dir / ".archive"
        archive_dir.mkdir(exist_ok=True)

        # Folder-based skill
        skill_dir = skills_dir / folder_name
        if skill_dir.exists() and (skill_dir / "SKILL.md").exists():
            dest = archive_dir / folder_name
            if dest.exists():
                shutil.rmtree(dest)
            shutil.move(str(skill_dir), str(dest))
            return json.dumps({"status": "archived", "name": name})

        # Legacy flat file
        flat = skills_dir / f"{name}.md"
        if flat.exists():
            flat.rename(archive_dir / flat.name)
            return json.dumps({"status": "archived", "name": name})

        return json.dumps({"error": f"Skill '{name}' not found"})

    # ── rename ────────────────────────────────────────────────────────────────
    elif action == "rename":
        new_name = args.get("new_name", "")
        if not name or not new_name:
            return json.dumps({"error": "name and new_name required"})

        folder_name     = skill_name_to_folder(name)
        new_folder_name = skill_name_to_folder(new_name)
        old_dir = skills_dir / folder_name
        new_dir = skills_dir / new_folder_name

        if not old_dir.exists():
            # Try legacy flat file
            old_flat = skills_dir / f"{name}.md"
            new_flat = skills_dir / f"{new_name}.md"
            if old_flat.exists():
                if new_flat.exists():
                    return json.dumps({"error": f"Skill '{new_name}' already exists"})
                old_flat.rename(new_flat)
                return json.dumps({"status": "renamed", "from": name, "to": new_name})
            return json.dumps({"error": f"Skill '{name}' not found"})

        if new_dir.exists():
            return json.dumps({"error": f"Skill '{new_name}' already exists"})

        old_dir.rename(new_dir)
        return json.dumps({"status": "renamed", "from": name, "to": new_name,
                           "folder": new_folder_name})

    # ── add_reference ─────────────────────────────────────────────────────────
    elif action == "add_reference":
        filename = args.get("filename", "")
        content  = args.get("content", "")
        if not name or not filename:
            return json.dumps({"error": "name and filename required"})
        ref_dir = _ensure_subdir(skills_dir, name, "references")
        if ref_dir is None:
            return json.dumps({"error": f"Skill '{name}' not found"})
        (ref_dir / filename).write_text(content, encoding="utf-8")
        return json.dumps({"status": "saved", "path": str(ref_dir / filename)})

    # ── add_script ────────────────────────────────────────────────────────────
    elif action == "add_script":
        filename = args.get("filename", "")
        content  = args.get("content", "")
        if not name or not filename:
            return json.dumps({"error": "name and filename required"})
        scr_dir = _ensure_subdir(skills_dir, name, "scripts")
        if scr_dir is None:
            return json.dumps({"error": f"Skill '{name}' not found"})
        (scr_dir / filename).write_text(content, encoding="utf-8")
        return json.dumps({"status": "saved", "path": str(scr_dir / filename)})

    # ── delete_reference ──────────────────────────────────────────────────────
    elif action == "delete_reference":
        filename = args.get("filename", "")
        if not name or not filename:
            return json.dumps({"error": "name and filename required"})
        folder_name = skill_name_to_folder(name)
        ref_file = skills_dir / folder_name / "references" / filename
        if not ref_file.exists():
            return json.dumps({"error": f"Reference '{filename}' not found"})
        ref_file.unlink()
        return json.dumps({"status": "deleted", "filename": filename})

    # ── delete_script ─────────────────────────────────────────────────────────
    elif action == "delete_script":
        filename = args.get("filename", "")
        if not name or not filename:
            return json.dumps({"error": "name and filename required"})
        folder_name = skill_name_to_folder(name)
        scr_file = skills_dir / folder_name / "scripts" / filename
        if not scr_file.exists():
            return json.dumps({"error": f"Script '{filename}' not found"})
        scr_file.unlink()
        return json.dumps({"status": "deleted", "filename": filename})

    # ── migrate ───────────────────────────────────────────────────────────────
    elif action == "migrate":
        """Migrate all legacy flat .md files to folder-based structure."""
        migrated = []
        for md_file in list(skills_dir.glob("*.md")):
            if md_file.name.startswith("."):
                continue
            skill_name  = md_file.stem
            folder_name = skill_name_to_folder(skill_name)
            skill_dir   = skills_dir / folder_name
            if skill_dir.exists():
                continue  # already exists
            skill_dir.mkdir(parents=True, exist_ok=True)
            content = md_file.read_text(encoding="utf-8", errors="replace")
            (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
            md_file.rename(skills_dir / ".archive" / md_file.name
                           if (skills_dir / ".archive").exists()
                           else skills_dir / f".{md_file.name}.bak")
            migrated.append(skill_name)
        return json.dumps({"status": "migrated", "count": len(migrated),
                           "skills": migrated})

    return json.dumps({"error": f"Unknown action: {action}"})


# ── internal helpers ──────────────────────────────────────────────────────────

def _resolve_skill_file(skills_dir: Path, name: str, folder_name: str) -> Path | None:
    """Return the SKILL.md path for a skill, or None if not found."""
    candidates = [
        skills_dir / folder_name / "SKILL.md",
        skills_dir / name        / "SKILL.md",
        skills_dir / f"{name}.md",
        skills_dir / f"{folder_name}.md",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _ensure_subdir(skills_dir: Path, name: str, subdir: str) -> Path | None:
    """Return (creating if needed) the references/ or scripts/ subdir, or None if skill missing."""
    folder_name = skill_name_to_folder(name)
    skill_dir   = skills_dir / folder_name
    if not skill_dir.exists() or not (skill_dir / "SKILL.md").exists():
        return None
    d = skill_dir / subdir
    d.mkdir(exist_ok=True)
    return d


# ── read a single reference or script file ───────────────────────────────────

def skill_read_file(args: dict, **kwargs) -> str:
    """Read a reference or script file from a skill folder."""
    logger.debug("Beginning of skill_read_file")
    name     = args.get("name", "")
    subdir   = args.get("subdir", "")   # "references" or "scripts"
    filename = args.get("filename", "")
    if not name or not subdir or not filename:
        return json.dumps({"error": "name, subdir, and filename required"})
    skills_dir  = get_skills_dir()
    folder_name = skill_name_to_folder(name)
    file_path   = skills_dir / folder_name / subdir / filename
    if not file_path.exists():
        return json.dumps({"error": f"File not found: {filename}"})
    content = file_path.read_text(encoding="utf-8", errors="replace")
    return json.dumps({"name": filename, "content": content,
                       "path": str(file_path)})


# ── Tool registrations ────────────────────────────────────────────────────────

registry.register(
    name="skills_list",
    toolset="skills",
    schema={
        "name": "skills_list",
        "description": "List available skills with metadata.",
        "parameters": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Filter by category"},
            },
        },
    },
    handler=lambda args, **kw: json.dumps({
        "skills": [
            {"name": s["name"], "description": s["description"],
             "type": s.get("type"), "version": s.get("version", ""),
             "folder": s.get("folder", "")}
            for s in discover_skills()
            if not args.get("category") or
               args["category"].lower() in str(s.get("metadata", {})).lower()
        ]
    }),
    emoji="📚",
)

registry.register(
    name="skill_view",
    toolset="skills",
    schema={
        "name": "skill_view",
        "description": "Load full skill content by name or path.",
        "parameters": {
            "type": "object",
            "properties": {
                "name":      {"type": "string", "description": "Skill name"},
                "file_path": {"type": "string", "description": "Direct path to SKILL.md"},
            },
        },
    },
    handler=skill_view,
    emoji="👁️",
    read_only=True,
)

registry.register(
    name="skill_manage",
    toolset="skills",
    schema={
        "name": "skill_manage",
        "description": (
            "Manage skills: create/edit/delete/rename a skill, "
            "or add_reference/add_script/delete_reference/delete_script to manage sub-files, "
            "or migrate to convert legacy flat .md files to folder structure."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action":   {"type": "string",
                             "enum": ["create", "edit", "delete", "rename",
                                      "add_reference", "add_script",
                                      "delete_reference", "delete_script",
                                      "migrate"]},
                "name":      {"type": "string", "description": "Skill name"},
                "content":   {"type": "string", "description": "Content (SKILL.md or file)"},
                "description": {"type": "string", "description": "Description (for create)"},
                "new_name":  {"type": "string", "description": "New name (for rename)"},
                "filename":  {"type": "string", "description": "File name (for reference/script)"},
            },
            "required": ["action", "name"],
        },
    },
    handler=skill_manage,
    emoji="🛠️",
)

registry.register(
    name="skill_read_file",
    toolset="skills",
    schema={
        "name": "skill_read_file",
        "description": "Read a reference or script file from a skill folder.",
        "parameters": {
            "type": "object",
            "properties": {
                "name":     {"type": "string", "description": "Skill name"},
                "subdir":   {"type": "string", "enum": ["references", "scripts"]},
                "filename": {"type": "string", "description": "File name"},
            },
            "required": ["name", "subdir", "filename"],
        },
    },
    handler=skill_read_file,
    emoji="📄",
    read_only=True,
)


