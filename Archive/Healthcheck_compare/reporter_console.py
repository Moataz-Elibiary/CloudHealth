"""Console live progress reporter using rich."""
from __future__ import annotations
from typing import List, Optional
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich import box

from core.result import ClusterResult, Section, Status

_STYLE = {
    Status.PASS:  "bold green",
    Status.FAIL:  "bold red",
    Status.WARN:  "bold yellow",
    Status.INFO:  "cyan",
    Status.SKIP:  "dim",
    Status.ERROR: "bold red",
}
_ICON = {
    Status.PASS:  "✔",
    Status.FAIL:  "✘",
    Status.WARN:  "⚠",
    Status.INFO:  "ℹ",
    Status.SKIP:  "–",
    Status.ERROR: "✘",
}


class ConsoleReporter:
    def __init__(self, verbose: bool = False):
        self.console = Console()
        self.verbose = verbose

    def cluster_start(self, name: str, ctype: str):
        self.console.print(f"\n[bold cyan]▶  {ctype.upper()} · [white]{name}[/white][/bold cyan]")

    def section_start(self, name: str):
        self.console.print(f"   [dim]├─[/dim] [italic]{name}[/italic] …", end="")

    def section_done(self, s: Section):
        ws   = _STYLE[s.worst_status]
        icon = _ICON[s.worst_status]
        dur  = f"[dim]({s.duration_s:.1f}s)[/dim]" if s.duration_s else ""
        cnts = (f"[green]{s.pass_count}✔[/green] "
                f"[red]{s.fail_count}✘[/red] "
                f"[yellow]{s.warn_count}⚠[/yellow]")
        self.console.print(
            f"\r   [dim]├─[/dim] [{ws}]{icon}[/{ws}] {s.name}  {cnts} {dur}")
        if self.verbose or s.fail_count > 0:
            for item in s.items:
                if item.status in (Status.FAIL, Status.ERROR) or self.verbose:
                    st = _STYLE[item.status]; ic = _ICON[item.status]
                    self.console.print(f"   [dim]│   [{st}]{ic}[/{st}][/dim] {item.message}")

    def cluster_done(self, r: ClusterResult):
        ws   = _STYLE[r.overall_status]
        icon = _ICON[r.overall_status]
        dur  = f" ({r.duration_s:.0f}s)" if r.duration_s else ""
        self.console.print(
            f"   [dim]└─[/dim] [{ws}]{icon} {r.cluster_name} "
            f"PASS:{r.pass_count} FAIL:{r.fail_count} WARN:{r.warn_count}{dur}[/{ws}]\n")

    def final_summary(self, results: List[ClusterResult], html_file: Optional[Path]):
        t = Table(title="[bold]ClusterPulse – Summary[/bold]", box=box.ROUNDED,
                  header_style="bold cyan")
        t.add_column("Cluster", style="white", no_wrap=True)
        t.add_column("Type",   justify="center")
        t.add_column("Status", justify="center")
        t.add_column("PASS",   justify="right", style="green")
        t.add_column("FAIL",   justify="right", style="red")
        t.add_column("WARN",   justify="right", style="yellow")
        t.add_column("Time",   justify="right", style="dim")
        for r in results:
            ws   = _STYLE[r.overall_status]
            icon = _ICON[r.overall_status]
            dur  = f"{r.duration_s:.0f}s" if r.duration_s else "—"
            t.add_row(r.cluster_name, r.cluster_type.upper(),
                      f"[{ws}]{icon} {r.overall_status.value}[/{ws}]",
                      str(r.pass_count), str(r.fail_count), str(r.warn_count), dur)
        self.console.print()
        self.console.print(t)
        if html_file:
            self.console.print(f"\n[bold]HTML Report → [link file://{html_file.absolute()}]{html_file}[/link][/bold]\n")
