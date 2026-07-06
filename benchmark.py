import os
import json
import uuid
import glob
import re
from datetime import datetime
from dotenv import load_dotenv

# Load .env file into environment variables
load_dotenv()

# Support custom token for benchmarking (Override before importing anything that loads settings)
bench_token = os.getenv("GITHUB_TOKEN_BENCH")
if bench_token:
    os.environ["GITHUB_TOKEN"] = bench_token

import asyncio
import time
import logging
from core.orchestrator import run_agent_pipeline
# Important: importing llm_client after os.environ is set
from core.orchestrator import llm_client

from rich.live import Live
from rich.table import Table
from rich.console import Console

# Configure your test parameters here
REPO_URL = os.getenv("TEST_REPO_URL", "https://github.com/ZenDoesCoding/co_resolve_testing.git")
COMMIT_SHA = os.getenv("TEST_COMMIT_SHA", "main")
BRANCH = os.getenv("TEST_BRANCH", "main")
RUN_ID = 12345

# Detect next Benchmark Number relative to this branch
log_dir = "logs/benchmark_runs"
os.makedirs(log_dir, exist_ok=True)
existing_files = glob.glob(os.path.join(log_dir, "run_*_bench_*.json"))

max_bench = 0
for f in existing_files:
    match = re.search(r"run_\d+_bench_(\d+)", os.path.basename(f))
    if match:
        max_bench = max(max_bench, int(match.group(1)))
        
BENCHMARK_NUMBER = max_bench + 1

N_RUNS = int(os.getenv("N_RUNS", "3"))

# Suppress excessive logging during benchmark to keep the rich UI clean
logging.getLogger().setLevel(logging.ERROR)

async def update_ui(live_ctx, start_time, run_state):
    while not run_state['done']:
        metrics = llm_client.get_metrics()
        elapsed = int(time.time() - start_time)
        m, s = divmod(elapsed, 60)
        h, m = divmod(m, 60)
        time_str = f"{h:02d}:{m:02d}:{s:02d}"
        
        # Build new table
        new_table = Table(title=f"Benchmark Run {run_state['current_run']}/{N_RUNS}", title_style="bold magenta")
        new_table.add_column("Timer", style="cyan", justify="center")
        new_table.add_column("API Calls", style="green", justify="center")
        new_table.add_column("Input Tokens", style="yellow", justify="center")
        new_table.add_column("Output Tokens", style="blue", justify="center")
        
        in_delta = f" [green](+{metrics['last_input_tokens']})[/green]" if metrics['last_input_tokens'] > 0 else ""
        out_delta = f" [green](+{metrics['last_output_tokens']})[/green]" if metrics['last_output_tokens'] > 0 else ""
        
        est_flag = " (Estimated)" if metrics['is_estimated'] else ""
        
        new_table.add_row(
            time_str,
            str(metrics['api_invocations']),
            f"{metrics['input_tokens']}{in_delta}{est_flag}",
            f"{metrics['output_tokens']}{out_delta}{est_flag}"
        )
        
        live_ctx.update(new_table)
        await asyncio.sleep(1)

async def run_benchmark():
    console = Console()
    console.print("Checking Docker daemon status...", style="bold yellow")
    from utils.docker_helper import ensure_docker_running
    ensure_docker_running(interactive=True)
    
    console.print(f"🚀 Starting Benchmark on branch: {os.popen('git branch --show-current').read().strip()}", style="bold green")
    console.print(f"Target Repo: {REPO_URL}")
    console.print(f"Runs: {N_RUNS}\n")
    
    os.makedirs("logs/benchmark_runs", exist_ok=True)
    results = []
    
    for i in range(N_RUNS):
        run_state = {'done': False, 'current_run': i+1}
        start_time = time.time()
        
        table = Table(title=f"Benchmark Run {i+1}/{N_RUNS}", title_style="bold magenta")
        table.add_column("Timer", style="cyan", justify="center")
        table.add_column("API Calls", style="green", justify="center")
        table.add_column("Input Tokens", style="yellow", justify="center")
        table.add_column("Output Tokens", style="blue", justify="center")
        
        with Live(table, console=console, refresh_per_second=4) as live:
            ui_task = asyncio.create_task(update_ui(live, start_time, run_state))
            
            try:
                success, messages, metrics = await run_agent_pipeline(REPO_URL, COMMIT_SHA, BRANCH, RUN_ID)
                duration = time.time() - start_time
                results.append(duration)
                
                # Write Telemetry Log
                log_file = f"logs/benchmark_runs/run_{i+1}_bench_{BENCHMARK_NUMBER}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
                
                try:
                    agent_sha = os.popen('git rev-parse --short HEAD').read().strip()
                    agent_version = os.popen('git describe --tags --always').read().strip()
                except Exception:
                    agent_sha = "unknown"
                    agent_version = "untagged"
                    
                with open(log_file, "w") as f:
                    json.dump({
                        "benchmark_number": BENCHMARK_NUMBER,
                        "run_number": i+1,
                        "version": agent_version,
                        "commit_sha": agent_sha,
                        "run_id": RUN_ID,
                        "branch": BRANCH,
                        "duration": duration,
                        "success": success,
                        "metrics": metrics,
                        "messages": messages
                    }, f, indent=2)
                
                status_msg = f"✅ Run {i+1} completed in {duration:.2f}s | Telemetry saved to {log_file}"
                console.print(status_msg, style="bold green")
                
            except Exception as e:
                console.print(f"❌ Run {i+1} failed: {e}", style="bold red")
                results.append(None)
            finally:
                run_state['done'] = True
                await ui_task
                
    print("\n=== Benchmark Summary ===")
    valid_results = [r for r in results if r is not None]
    if valid_results:
        avg = sum(valid_results) / len(valid_results)
        print(f"Average Time: {avg:.2f}s")
        
        output_file = "benchmark_results.txt"
        with open(output_file, "a") as f:
            f.write(f"Average: {avg:.2f}s | Runs: {[f'{r:.2f}s' for r in valid_results]}\n")
        print(f"Results appended to {output_file}")
    else:
        print("All runs failed.")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        try:
            arc_num = int(sys.argv[1])
            if arc_num in (1, 2, 3):
                from utils.active_arc import set_active_arc
                set_active_arc(arc_num)
                print(f"Setting active ARC to {arc_num} for this benchmark run.")
            else:
                print("Usage: python benchmark.py <1-3>")
                sys.exit(1)
        except ValueError:
            print("Usage: python benchmark.py <1-3>")
            sys.exit(1)

    try:
        asyncio.run(run_benchmark())
    except KeyboardInterrupt:
        print("\n[!] Benchmark cancelled by user. Cleaning up fragmented logs...")
        import glob
        pattern = f"logs/benchmark_runs/run_*_bench_{BENCHMARK_NUMBER}_*.json"
        deleted_count = 0
        for f in glob.glob(pattern):
            try:
                os.remove(f)
                deleted_count += 1
            except Exception as e:
                print(f"Error deleting {f}: {e}")
        print(f"Deleted {deleted_count} fragmented log file(s).")
