"""
opti — CLI client for the cTrader Optimization Server.

Commands:
    submit   Upload an .algo file and start an optimization job
    status   Show job status (one or all)
    watch    Live-poll a running job
    results  Display top N passes
    best     Show the single best pass
    cancel   Cancel a running job
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

import httpx
import typer
import yaml
from rich.console import Console
from rich.live import Live
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table

from client.config import API_KEY, SERVER_URL, get_headers

app = typer.Typer(
    name="opti",
    help="CLI client for the cTrader Optimization Server",
    add_completion=False,
)
console = Console()

# ── Helpers ─────────────────────────────────────────────────────────────────


def _url(path: str) -> str:
    return f"{SERVER_URL.rstrip('/')}{path}"


def _client() -> httpx.Client:
    return httpx.Client(headers=get_headers(), timeout=60.0)


def _async_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=get_headers(), timeout=60.0)


def _handle_error(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        console.print(f"[bold red]Error {resp.status_code}:[/] {detail}")
        raise typer.Exit(1)


# ── submit ──────────────────────────────────────────────────────────────────


@app.command()
def submit(
    algo: Path = typer.Option(..., "--algo", "-a", help="Path to the .algo file"),
    config: Path = typer.Option(..., "--config", "-c", help="Path to job config YAML"),
) -> None:
    """Upload an .algo file and start an optimization job."""
    if not algo.exists():
        console.print(f"[red]Algo file not found:[/] {algo}")
        raise typer.Exit(1)
    if not config.exists():
        console.print(f"[red]Config file not found:[/] {config}")
        raise typer.Exit(1)

    cfg = yaml.safe_load(config.read_text())
    cfg_json = json.dumps(cfg)

    with _client() as client:
        with open(algo, "rb") as f:
            resp = client.post(
                _url("/jobs"),
                files={"file": (algo.name, f, "application/octet-stream")},
                data={"config": cfg_json},
            )
    _handle_error(resp)

    data = resp.json()
    console.print(f"\n[bold green]✓ Job submitted[/]")
    console.print(f"  Job ID:       [cyan]{data['job_id']}[/]")
    console.print(f"  Total passes: [yellow]{data['total_passes']}[/]\n")


# ── status ──────────────────────────────────────────────────────────────────


@app.command()
def status(
    job_id: Optional[str] = typer.Argument(None, help="Job ID (omit for all jobs)"),
) -> None:
    """Show job status — a single job or all jobs."""
    with _client() as client:
        if job_id:
            resp = client.get(_url(f"/jobs/{job_id}"))
            _handle_error(resp)
            job = resp.json()
            _print_job_detail(job)
        else:
            resp = client.get(_url("/jobs"))
            _handle_error(resp)
            jobs = resp.json()
            if not jobs:
                console.print("[dim]No jobs found.[/]")
                return
            _print_jobs_table(jobs)


def _print_jobs_table(jobs: list) -> None:
    table = Table(title="Optimization Jobs", show_lines=True)
    table.add_column("Name", style="cyan", min_width=20)
    table.add_column("Status", justify="center")
    table.add_column("Strategy", justify="center")
    table.add_column("Progress", justify="center", min_width=18)
    table.add_column("Created", style="dim")
    table.add_column("Best", justify="right")
    table.add_column("Job ID", style="dim", max_width=12)

    for j in jobs:
        completed = j.get("completed_passes", 0)
        total = j.get("total_passes", 0)
        pct = f"{completed}/{total}"
        if total > 0:
            bar_pct = int((completed / total) * 20)
            bar = "█" * bar_pct + "░" * (20 - bar_pct)
            pct = f"{bar} {completed}/{total}"

        status_str = j["status"]
        style = {
            "queued": "dim",
            "running": "yellow",
            "done": "green",
            "failed": "red",
        }.get(status_str, "white")

        best_str = ""
        if j.get("best_pass_summary"):
            bp = j["best_pass_summary"]
            # Show first numeric metric
            for key in ("net_profit", "profit_factor", "sharpe_ratio", "win_rate"):
                if key in bp and bp[key]:
                    best_str = f"{key}: {bp[key]}"
                    break

        table.add_row(
            j.get("name", ""),
            f"[{style}]{status_str}[/{style}]",
            j.get("strategy", ""),
            pct,
            j.get("created_at", "")[:19],
            best_str,
            j["id"][:12],
        )

    console.print(table)


def _print_job_detail(job: dict) -> None:
    console.print(f"\n[bold cyan]{job['name']}[/bold cyan]")
    console.print(f"  ID:       {job['id']}")
    console.print(f"  Status:   {job['status']}")
    console.print(f"  Strategy: {job['strategy']}")
    console.print(f"  Progress: {job['completed_passes']}/{job['total_passes']}")
    console.print(f"  Created:  {job['created_at']}")
    console.print(f"  Updated:  {job['updated_at']}")

    if job.get("top_passes"):
        console.print(f"\n  [bold]Top passes:[/]")
        _print_passes_table(job["top_passes"][:10])


# ── watch ───────────────────────────────────────────────────────────────────


@app.command()
def watch(
    job_id: str = typer.Argument(..., help="Job ID to watch"),
    interval: int = typer.Option(5, "--interval", "-i", help="Poll interval in seconds"),
) -> None:
    """Live-poll a running job with auto-refreshing display."""
    console.print(f"[dim]Watching job {job_id} (Ctrl+C to stop)…[/]\n")

    with Live(console=console, refresh_per_second=1) as live:
        while True:
            try:
                with _client() as client:
                    resp = client.get(_url(f"/jobs/{job_id}"))
                if resp.status_code >= 400:
                    live.update(f"[red]Error: {resp.status_code}[/]")
                    break

                job = resp.json()
                display = _build_watch_display(job)
                live.update(display)

                if job["status"] in ("done", "failed"):
                    break

                time.sleep(interval)
            except KeyboardInterrupt:
                break

    console.print("[dim]Watch ended.[/]")


def _build_watch_display(job: dict) -> Table:
    completed = job.get("completed_passes", 0)
    total = job.get("total_passes", 1)
    pct = int((completed / total) * 100) if total else 0
    bar_len = 30
    filled = int(bar_len * completed / total) if total else 0
    bar = "█" * filled + "░" * (bar_len - filled)

    header_table = Table(show_header=False, box=None, padding=(0, 1))
    header_table.add_column(min_width=60)
    header_table.add_row(f"[bold cyan]{job['name']}[/]  [{job['status']}]")
    header_table.add_row(f"Progress: {bar} {pct}% ({completed}/{total})")
    header_table.add_row("")

    if job.get("top_passes"):
        top = job["top_passes"][:10]
        passes_table = Table(title="Top 10 Passes", show_lines=True, min_width=80)
        passes_table.add_column("#", justify="right", style="dim", width=4)

        # Collect all param keys
        all_params = set()
        for p in top:
            if p.get("params"):
                all_params.update(p["params"].keys())
        param_keys = sorted(all_params)
        for pk in param_keys:
            passes_table.add_column(pk, justify="right", style="cyan")

        for metric in ["net_profit", "profit_factor", "win_rate", "max_drawdown_pct", "total_trades"]:
            passes_table.add_column(metric, justify="right")

        for i, p in enumerate(top, 1):
            row = [str(i)]
            params = p.get("params", {})
            for pk in param_keys:
                row.append(str(params.get(pk, "")))
            result = p.get("result", {}) or {}
            for metric in ["net_profit", "profit_factor", "win_rate", "max_drawdown_pct", "total_trades"]:
                val = result.get(metric, "")
                if isinstance(val, float):
                    row.append(f"{val:.2f}")
                else:
                    row.append(str(val))
            passes_table.add_row(*row)

        # Combine
        outer = Table(show_header=False, box=None)
        outer.add_column()
        outer.add_row(header_table)
        outer.add_row(passes_table)
        return outer

    return header_table


# ── results ─────────────────────────────────────────────────────────────────


@app.command()
def results(
    job_id: str = typer.Argument(..., help="Job ID"),
    top: int = typer.Option(20, "--top", "-n", help="Number of top results"),
    sort_by: str = typer.Option("net_profit", "--sort-by", "-s", help="Sort metric"),
) -> None:
    """Display top N passes sorted by a metric."""
    with _client() as client:
        resp = client.get(
            _url(f"/jobs/{job_id}/passes"),
            params={"status": "done", "sort_by": sort_by, "limit": top},
        )
    _handle_error(resp)
    passes = resp.json()
    if not passes:
        console.print("[dim]No completed passes yet.[/]")
        return

    _print_passes_table(passes)


def _print_passes_table(passes: list) -> None:
    table = Table(show_lines=True)
    table.add_column("Rank", justify="right", style="dim", width=5)

    # Collect all param keys
    all_params = set()
    for p in passes:
        if p.get("params"):
            all_params.update(p["params"].keys())
    param_keys = sorted(all_params)
    for pk in param_keys:
        table.add_column(pk, justify="right", style="cyan")

    metric_cols = ["net_profit", "profit_factor", "win_rate", "max_drawdown_pct", "total_trades"]
    for mc in metric_cols:
        table.add_column(mc, justify="right")

    for i, p in enumerate(passes, 1):
        row = [str(i)]
        params = p.get("params", {})
        for pk in param_keys:
            row.append(str(params.get(pk, "")))
        result = p.get("result", {}) or {}
        for mc in metric_cols:
            val = result.get(mc, "")
            if isinstance(val, float):
                row.append(f"{val:.2f}")
            else:
                row.append(str(val))
        table.add_row(*row)

    console.print(table)


# ── best ────────────────────────────────────────────────────────────────────


@app.command()
def best(
    job_id: str = typer.Argument(..., help="Job ID"),
) -> None:
    """Show the single best pass and its winning parameters."""
    with _client() as client:
        resp = client.get(_url(f"/jobs/{job_id}/best"))
    _handle_error(resp)
    data = resp.json()

    pr = data["pass_result"]
    result = pr.get("result", {}) or {}
    params = pr.get("params", {})

    console.print("\n[bold green]🏆 Best Pass[/]\n")

    # Params
    param_table = Table(title="Winning Parameters", show_lines=True)
    param_table.add_column("Parameter", style="cyan")
    param_table.add_column("Value", justify="right")
    for k, v in sorted(params.items()):
        param_table.add_row(k, str(v))
    console.print(param_table)

    # Metrics
    metric_table = Table(title="Performance Metrics", show_lines=True)
    metric_table.add_column("Metric", style="cyan")
    metric_table.add_column("Value", justify="right")
    for k, v in sorted(result.items()):
        if k == "error":
            continue
        if isinstance(v, float):
            metric_table.add_row(k, f"{v:.4f}")
        else:
            metric_table.add_row(k, str(v))
    console.print(metric_table)

    # cbotset-ready params
    console.print("\n[bold]📋 cBotSet Parameters (copy-paste ready):[/]")
    console.print(json.dumps(data.get("cbotset_params", params), indent=2))
    console.print()


# ── cancel ──────────────────────────────────────────────────────────────────


@app.command()
def cancel(
    job_id: str = typer.Argument(..., help="Job ID to cancel"),
) -> None:
    """Cancel a running job."""
    with _client() as client:
        resp = client.delete(_url(f"/jobs/{job_id}"))
    _handle_error(resp)
    console.print(f"[bold yellow]✗ Job {job_id} cancelled.[/]")


# ── Entry point ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()
