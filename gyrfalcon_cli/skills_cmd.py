"""Skills CLI command."""

from rich.console import Console
from gyrfalcon.tools.skills_tool import discover_skills
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("skills_cmd")



def run_skills_cli():
    logger.debug("Beginning of run_skills_cli")
    console = Console()
    skills = discover_skills()
    if not skills:
        console.print("[dim]No skills found.[/dim]")
        return
    console.print(f"[bold]Skills ({len(skills)}):[/bold]\n")
    for s in skills:
        console.print(f"  📚 {s['name']}: {s.get('description', '')}")
