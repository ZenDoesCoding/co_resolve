import os
import json
import glob
from collections import defaultdict

def analyze():
    log_dir = "logs/benchmark_runs"
    if not os.path.exists(log_dir):
        print("No logs directory found.")
        return

    files = glob.glob(os.path.join(log_dir, "*.json"))
    if not files:
        print("No log files found.")
        return

    # Group by benchmark_number
    benchmarks = defaultdict(list)
    for f in files:
        try:
            with open(f, "r") as file:
                data = json.load(file)
                bench_num = data.get("benchmark_number")
                if bench_num is not None:
                    benchmarks[bench_num].append((f, data))
        except Exception:
            continue

    if not benchmarks:
        print("No valid benchmark data found.")
        return

    # Calculate metrics for each benchmark
    bench_summaries = {}
    for num, runs in benchmarks.items():
        # Sort by run_number
        runs.sort(key=lambda x: x[1].get("run_number", 0))
        
        durations = [r[1].get("duration", 0) for r in runs if r[1].get("success")]
        success = all(r[1].get("success") for r in runs) and len(runs) == 3 # assuming 3 runs
        
        avg_duration = sum(durations) / len(durations) if durations else float('inf')
        
        # Get commit SHA (assuming same for all runs in a bench)
        sha = runs[0][1].get("commit_sha", "unknown")
        
        bench_summaries[num] = {
            "avg_duration": avg_duration,
            "success": success,
            "files": [r[0] for r in runs],
            "sha": sha,
            "raw_runs": runs
        }

    sorted_bench_nums = sorted(bench_summaries.keys())
    
    latest_num = sorted_bench_nums[-1]
    predecessor_num = sorted_bench_nums[-2] if len(sorted_bench_nums) > 1 else None
    
    # Find champion (best successful run)
    champion_num = None
    min_dur = float('inf')
    for num, summary in bench_summaries.items():
        if summary["success"] and summary["avg_duration"] < min_dur:
            min_dur = summary["avg_duration"]
            champion_num = num

    # Fallback to latest if no successful champion yet
    if champion_num is None:
         champion_num = latest_num

    print("=== BENCHMARK ANALYSIS ===")
    
    def print_bench(title, num):
        if num is None or num not in bench_summaries:
            print(f"\n{title}: None")
            return
        s = bench_summaries[num]
        print(f"\n{title}: Benchmark {num}")
        print(f"  SHA: {s['sha']}")
        print(f"  Avg Duration: {s['avg_duration']:.2f}s" if s['avg_duration'] != float('inf') else "  Avg Duration: N/A")
        print(f"  Success: {s['success']}")
        print("  Files:")
        for f in s['files']:
            print(f"    - {f}")

    print_bench("CHAMPION", champion_num)
    print_bench("PREDECESSOR", predecessor_num)
    print_bench("LATEST RUN", latest_num)

    print("\n=== STATUS ===")
    latest = bench_summaries[latest_num]
    champ = bench_summaries[champion_num]
    
    if not latest["success"]:
        print("RESULT: FAILURE (Latest run failed)")
    elif latest_num == champion_num:
        print("RESULT: NEW CHAMPION (or tied)")
    elif latest["avg_duration"] > champ["avg_duration"]:
        print(f"RESULT: REGRESSION (Slower than Champion by {latest['avg_duration'] - champ['avg_duration']:.2f}s)")
    else:
        print("RESULT: IMPROVEMENT (but not champion)")

if __name__ == "__main__":
    analyze()
