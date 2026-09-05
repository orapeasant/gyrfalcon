"""Logs CLI command — view and follow log files."""

import sys
import time
import signal
from pathlib import Path
from typing import Optional

from rich.console import Console
from gyrfalcon.gyrfalcon_constants import get_logs_dir
from gyrfalcon.gyrfalcon_logging import get_logger
logger = get_logger("logs_cmd")



def run_logs(
    follow: bool = False,
    lines: int = 50,
    log_file: str = "agent.log",
):
    """Display logs with optional continuous streaming.
    
    Args:
        follow: If True, continuously stream new log entries (like tail -f)
        lines: Number of initial lines to show
        log_file: Name of log file to read
    """
    logger.debug("Beginning of run_logs")
    console = Console()
    logs_dir = get_logs_dir()
    file_path = logs_dir / log_file
    
    if not file_path.exists():
        console.print(f"[dim]No logs yet at {file_path}[/dim]")
        return
    
    # Show initial lines
    _print_last_lines(console, file_path, lines)
    
    if not follow:
        return
    
    # Follow mode - stream continuously until Ctrl+C
    console.print(f"\n[dim]Following {log_file}... Press Ctrl+C to stop[/dim]\n")
    
    # Track file position
    last_size = file_path.stat().st_size
    last_inode = file_path.stat().st_ino
    
    # Handle Ctrl+C gracefully
    running = True
    
    def signal_handler(sig, frame):
        logger.debug("Beginning of signal_handler")
        nonlocal running
        running = False
        console.print("\n[dim]Stopped following logs.[/dim]")
    
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        while running:
            try:
                # Check if file was rotated (different inode)
                current_stat = file_path.stat()
                if current_stat.st_ino != last_inode:
                    # File rotated, start from beginning
                    last_size = 0
                    last_inode = current_stat.st_ino
                    console.print("[dim]--- Log file rotated ---[/dim]")
                
                current_size = current_stat.st_size
                
                if current_size > last_size:
                    # New content available
                    with open(file_path, "r", errors="replace") as f:
                        f.seek(last_size)
                        new_content = f.read()
                        if new_content:
                            # Print without trailing newline handling
                            for line in new_content.splitlines():
                                console.print(line)
                    last_size = current_size
                elif current_size < last_size:
                    # File was truncated
                    last_size = 0
                    console.print("[dim]--- Log file truncated ---[/dim]")
                
            except FileNotFoundError:
                # File was deleted, wait for it to reappear
                console.print("[dim]--- Waiting for log file ---[/dim]")
                last_size = 0
            except Exception as e:
                console.print(f"[red]Error reading logs: {e}[/red]")
            
            # Poll interval
            time.sleep(0.5)
            
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped following logs.[/dim]")


def _print_last_lines(console: Console, file_path: Path, num_lines: int):
    """Print the last N lines of a file."""
    logger.debug("Beginning of _print_last_lines")
    try:
        content = file_path.read_text(errors="replace").strip()
        if not content:
            console.print("[dim]Log file is empty.[/dim]")
            return
        
        lines = content.split("\n")
        for line in lines[-num_lines:]:
            console.print(line)
    except Exception as e:
        console.print(f"[red]Error reading log file: {e}[/red]")


def list_log_files():
    """List available log files."""
    logger.debug("Beginning of list_log_files")
    console = Console()
    logs_dir = get_logs_dir()
    
    if not logs_dir.exists():
        console.print("[dim]No logs directory yet.[/dim]")
        return
    
    log_files = list(logs_dir.glob("*.log"))
    if not log_files:
        console.print("[dim]No log files found.[/dim]")
        return
    
    console.print(f"[bold]Log files in {logs_dir}:[/bold]\n")
    for f in sorted(log_files):
        size = f.stat().st_size
        size_str = _format_size(size)
        console.print(f"  {f.name:20} {size_str:>10}")


def _format_size(size: int) -> str:
    """Format file size in human-readable form."""
    logger.debug("Beginning of _format_size")
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"

