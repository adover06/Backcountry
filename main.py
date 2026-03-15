"""
California Backpacking Trail Advisor — CLI
"""

import sys
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
import agent
from data import get_trails

console = Console()

BANNER = """[bold green]
  California Backpacking Trail Advisor
[/bold green]
[dim]Powered by Ollama · 1,194 backpacking trails · Weather · AQI · Permits[/dim]
[dim]Type your question or describe your ideal trip. Type 'exit' to quit.[/dim]
"""

ENV_HINTS = [
    ("AIRNOW_API_KEY",          "AQI data",       "airnow.gov/api (free)"),
    ("RECREATION_GOV_API_KEY",  "permit lookup",  "ridb.recreation.gov (free)"),
]


def show_env_status():
    import os
    table = Table(show_header=False, box=None, padding=(0, 2))
    for env, feature, url in ENV_HINTS:
        if os.environ.get(env):
            table.add_row(f"[green]✓[/green] {feature}", "[dim]active[/dim]")
        else:
            table.add_row(f"[yellow]○[/yellow] {feature}", f"[dim]set {env} ({url})[/dim]")
    console.print(table)
    console.print()


def show_stats():
    trails = get_trails()
    areas = len({t["area"] for t in trails})
    console.print(
        f"[dim]Loaded [bold]{len(trails)}[/bold] backpacking trails across "
        f"[bold]{areas}[/bold] areas in California.[/dim]\n"
    )


def main():
    console.print(BANNER)
    show_stats()
    show_env_status()

    history = []

    while True:
        try:
            user_input = console.input("[bold cyan]You:[/bold cyan] ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]Happy trails.[/dim]")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit", "q"):
            console.print("[dim]Happy trails.[/dim]")
            break

        console.print()
        with console.status("[cyan]Thinking...[/cyan]", spinner="dots"):
            try:
                response, history, _prefs = agent.run(user_input, history)
            except Exception as e:
                console.print(f"[red]Agent error:[/red] {e}")
                console.print("[dim]Make sure Ollama is running: [bold]ollama serve[/bold][/dim]")
                continue

        console.print(Rule("[bold green]Advisor[/bold green]", style="green"))
        console.print(Markdown(response))
        console.print()


if __name__ == "__main__":
    main()
