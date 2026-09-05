"""Skills Hub — install skills from configurable marketplace registries."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Optional

import httpx

from gyrfalcon.gyrfalcon_constants import get_skills_dir, get_skillshub_file
from gyrfalcon.gyrfalcon_logging import get_logger

logger = get_logger("skills_hub")

# ── Default registry included on first run ─────────────────────────────────────

_DEFAULT_REGISTRIES = [
    {
        "name": "hermes-agent",
        "url": "https://api.github.com/repos/NousResearch/hermes-agent/contents/skills",
        "raw_url": "https://raw.githubusercontent.com/NousResearch/hermes-agent/main/skills",
    },
]

# ── SkillsHubStore ─────────────────────────────────────────────────────────────

def _load_hub() -> dict:
    """Load ~/.gyrfalcon/skills/skillshub.json, migrating from config.yaml if needed."""
    p = get_skillshub_file()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"registries": []}

    # First run — migrate from config.yaml if it has skills_hub.registries
    data: dict = {"registries": []}
    try:
        from gyrfalcon.config import load_config, save_config
        import yaml as _yaml
        config = load_config()
        sh_block = config.pop("skills_hub", None)
        if sh_block and sh_block.get("registries"):
            data["registries"] = sh_block["registries"]
            logger.info(
                f"Migrated {len(data['registries'])} skillshub registry/registries "
                f"from config.yaml -> {p}"
            )
            # Strip skills_hub from config.yaml
            save_config(config)
        else:
            # Seed with built-in default
            data["registries"] = list(_DEFAULT_REGISTRIES)
    except Exception as e:
        logger.warning(f"skillshub migration error: {e}")
        data["registries"] = list(_DEFAULT_REGISTRIES)

    _save_hub(data)
    return data


def _save_hub(data: dict) -> None:
    """Write skillshub.json atomically."""
    p = get_skillshub_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _get_registries() -> list[dict]:
    """Get configured skill hub registries."""
    logger.debug("Beginning of _get_registries")
    return _load_hub().get("registries", [])


def _set_registries(registries: list[dict]) -> None:
    """Persist registry list to skillshub.json."""
    data = _load_hub()
    data["registries"] = registries
    _save_hub(data)


def add_registry(name: str, url: str, raw_url: str = "") -> dict:
    """Add a skill hub registry."""
    logger.debug("Beginning of add_registry")
    registries = _get_registries()

    for reg in registries:
        if reg.get("name") == name:
            return {"error": f"Registry '{name}' already exists. Remove it first."}

    # Auto-derive raw_url from GitHub API URL if not provided
    if not raw_url and "api.github.com/repos/" in url:
        parts = url.replace("https://api.github.com/repos/", "").split("/contents/")
        if len(parts) == 2:
            raw_url = f"https://raw.githubusercontent.com/{parts[0]}/main/{parts[1]}"

    registry_entry = {"name": name, "url": url, "raw_url": raw_url}
    registries.append(registry_entry)
    _set_registries(registries)
    logger.info(f"Added skill hub registry: {name} ({url})")
    return {"status": "added", "name": name, "url": url, "raw_url": raw_url}


def remove_registry(name: str) -> dict:
    """Remove a skill hub registry by name."""
    logger.debug("Beginning of remove_registry")
    registries = _get_registries()
    new_registries = [r for r in registries if r.get("name") != name]

    if len(new_registries) == len(registries):
        return {"error": f"Registry '{name}' not found"}

    _set_registries(new_registries)
    logger.info(f"Removed skill hub registry: {name}")
    return {"status": "removed", "name": name}


def list_registries() -> list[dict]:
    """List all configured skill hub registries."""
    logger.debug("Beginning of list_registries")
    return _get_registries()


def install_skill(name: str, source: str = "hub") -> dict:
    """Install a skill from the hub or URL.

    Args:
        name: Skill name or URL
        source: 'hub', 'url', or 'github'
    """
    logger.debug("Beginning of install_skill")
    skills_dir = get_skills_dir()

    if source == "url" or name.startswith("http"):
        return _install_from_url(name, skills_dir)
    elif source == "github" or "/" in name:
        return _install_from_github(name, skills_dir)
    else:
        return _install_from_hub(name, skills_dir)


def _install_from_url(url: str, skills_dir: Path) -> dict:
    """Install skill from direct URL."""
    logger.debug("Beginning of _install_from_url")
    try:
        response = httpx.get(url, follow_redirects=True, timeout=30)
        response.raise_for_status()
        content = response.text

        # Extract name from content or URL
        name = url.split("/")[-1].replace(".md", "")
        skill_path = skills_dir / f"{name}.md"
        skill_path.write_text(content)

        return {"status": "installed", "name": name, "path": str(skill_path)}
    except Exception as e:
        return {"error": f"Failed to install from URL: {str(e)}"}


def _install_from_github(repo_path: str, skills_dir: Path) -> dict:
    """Install skill from GitHub repo (owner/repo/path format)."""
    logger.debug("Beginning of _install_from_github")
    parts = repo_path.split("/")
    if len(parts) < 2:
        return {"error": "Format: owner/repo or owner/repo/path/to/skill.md"}

    owner = parts[0]
    repo = parts[1]
    path = "/".join(parts[2:]) if len(parts) > 2 else ""

    try:
        if path:
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/{path}"
        else:
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/SKILL.md"

        response = httpx.get(url, follow_redirects=True, timeout=30)
        response.raise_for_status()
        content = response.text

        name = path.split("/")[-1].replace(".md", "") if path else repo
        skill_path = skills_dir / f"{name}.md"
        skill_path.write_text(content)

        return {"status": "installed", "name": name, "path": str(skill_path), "source": f"github:{repo_path}"}
    except Exception as e:
        return {"error": f"Failed to install from GitHub: {str(e)}"}


def _install_from_hub(name: str, skills_dir: Path) -> dict:
    """Install from configured skill hub registries (searches all)."""
    logger.debug("Beginning of _install_from_hub")
    registries = _get_registries()

    if not registries:
        return {"error": "No skill hub registries configured. Use /skillhub add to add one."}

    errors = []
    for registry in registries:
        raw_url = registry.get("raw_url", "")
        if not raw_url:
            continue

        try:
            # Try as skills/<name>/SKILL.md
            url = f"{raw_url}/skills/{name}/SKILL.md"
            response = httpx.get(url, follow_redirects=True, timeout=30)
            if response.status_code == 200:
                skill_path = skills_dir / f"{name}.md"
                skill_path.write_text(response.text)
                return {
                    "status": "installed",
                    "name": name,
                    "path": str(skill_path),
                    "source": f"hub:{registry.get('name', 'unknown')}",
                }

            # Try as <name>/SKILL.md
            url = f"{raw_url}/{name}/SKILL.md"
            response = httpx.get(url, follow_redirects=True, timeout=30)
            if response.status_code == 200:
                skill_name = name.split("/")[-1]
                skill_path = skills_dir / f"{skill_name}.md"
                skill_path.write_text(response.text)
                return {
                    "status": "installed",
                    "name": skill_name,
                    "path": str(skill_path),
                    "source": f"hub:{registry.get('name', 'unknown')}",
                }
        except Exception as e:
            errors.append(f"{registry.get('name', '?')}: {e}")

    if errors:
        return {"error": f"Skill '{name}' not found. Errors: {'; '.join(errors)}"}
    return {"error": f"Skill '{name}' not found in any configured hub"}


def search_hub(query: str) -> list[dict]:
    """Search all configured skill hubs for matching skills."""
    logger.debug("Beginning of search_hub")
    registries = _get_registries()
    all_results = []

    for registry in registries:
        url = registry.get("url", "")
        if "api.github.com/repos/" not in url:
            continue

        # Extract owner/repo from API URL
        repo_part = url.replace("https://api.github.com/repos/", "").split("/contents/")[0]
        try:
            search_url = f"https://api.github.com/search/code?q={query}+filename:SKILL.md+repo:{repo_part}"
            response = httpx.get(search_url, timeout=30)
            if response.status_code == 200:
                data = response.json()
                for item in data.get("items", [])[:10]:
                    all_results.append({
                        "name": item.get("path", "").split("/")[-2] if "/" in item.get("path", "") else item.get("name", ""),
                        "path": item.get("path", ""),
                        "url": item.get("html_url", ""),
                        "registry": registry.get("name", ""),
                    })
        except Exception:
            pass

    return all_results


def list_hub_skills() -> list[dict]:
    """List available skills from all configured hubs."""
    logger.debug("Beginning of list_hub_skills")
    registries = _get_registries()
    all_skills = []

    for registry in registries:
        url = registry.get("url", "")
        if not url:
            continue

        try:
            response = httpx.get(url, timeout=30)
            if response.status_code == 200:
                items = response.json()
                for item in items:
                    if item.get("type") == "dir":
                        all_skills.append({
                            "name": item["name"],
                            "type": item["type"],
                            "registry": registry.get("name", ""),
                        })
        except Exception:
            pass

    return all_skills


def uninstall_skill(name: str) -> dict:
    """Remove an installed skill."""
    logger.debug("Beginning of uninstall_skill")
    skills_dir = get_skills_dir()
    skill_path = skills_dir / f"{name}.md"
    if skill_path.exists():
        skill_path.unlink()
        return {"status": "uninstalled", "name": name}

    skill_dir = skills_dir / name
    if skill_dir.exists() and skill_dir.is_dir():
        shutil.rmtree(skill_dir)
        return {"status": "uninstalled", "name": name}

    return {"error": f"Skill '{name}' not found"}
