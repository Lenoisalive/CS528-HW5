"""Plot real per-phase output from monitor.py summarize; never fill missing data."""

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys


METRICS = ("cpu_busy_pct", "memory_used_mib", "memory_used_pct", "rx_mbps", "tx_mbps")


def load_rows(path, allow_partial):
    rows, excluded, seen = [], [], set()
    for line_number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Line {line_number}: invalid JSON ({exc.msg})") from exc
        if not isinstance(row, dict):
            raise ValueError(f"Line {line_number}: expected a JSON object")
        if "status" in row:
            message = f"Line {line_number}: {row.get('phase', 'phase')} has status {row['status']}"
            if not allow_partial:
                raise ValueError(message + "; fix the data or use --allow-partial")
            excluded.append(message)
            continue
        count = row.get("clients")
        if type(count) is not int or count not in (1, 2, 3, 4):
            raise ValueError(f"Line {line_number}: clients must be an integer from 1 to 4")
        if count in seen:
            raise ValueError(f"Duplicate {count}-client phase; select one experiment, do not mix runs")
        keys = ["window_seconds", "coverage_seconds", "samples"] + [
            f"{stat}_{metric}" for metric in METRICS for stat in ("peak", "mean")
        ]
        for key in keys:
            value = row.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"Line {line_number}: missing or nonnumeric {key}")
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"Line {line_number}: invalid {key}")
        if not 179 <= row["window_seconds"] <= 182:
            raise ValueError(f"Line {line_number}: expected a 180-second measurement window")
        if not 0.95 <= row["coverage_seconds"] / row["window_seconds"] <= 1.001:
            raise ValueError(f"Line {line_number}: invalid or insufficient sample coverage")
        if row["samples"] < 1:
            raise ValueError(f"Line {line_number}: no samples")
        for metric in METRICS:
            peak, mean = row[f"peak_{metric}"], row[f"mean_{metric}"]
            if mean > peak + 0.001:
                raise ValueError(f"Line {line_number}: mean exceeds peak for {metric}")
            if metric.endswith("_pct") and peak > 100.001:
                raise ValueError(f"Line {line_number}: {metric} exceeds 100%")
        seen.add(count)
        rows.append(row)
    if not rows:
        raise ValueError("No valid measurement rows found")
    missing = sorted({1, 2, 3, 4} - seen)
    if missing and not allow_partial:
        raise ValueError(f"Missing client counts {missing}; wait for results or use --allow-partial")
    return sorted(rows, key=lambda row: row["clients"]), excluded, missing


def describe(values):
    if len(values) < 2:
        return "只有一个有效测量点，无法比较趋势"
    deltas = [b - a for a, b in zip(values, values[1:])]
    if all(abs(delta) < 1e-9 for delta in deltas):
        return "各测量点相同（按输入数据精度）"
    if all(delta >= 0 for delta in deltas):
        return "随客户端数量单调不减"
    if all(delta <= 0 for delta in deltas):
        return "随客户端数量单调不增"
    return "随客户端数量波动，并非单调变化"


def export_tables(rows, output, unit, factor, machine, excluded, missing):
    records = []
    for row in rows:
        record = {key: row[key] for key in ("clients", "window_seconds", "coverage_seconds", "samples")}
        for stat in ("peak", "mean"):
            for metric in ("cpu_busy_pct", "memory_used_mib", "memory_used_pct"):
                record[f"{stat}_{metric}"] = row[f"{stat}_{metric}"]
            for direction in ("rx", "tx"):
                record[f"{stat}_{direction}_{unit}"] = round(row[f"{stat}_{direction}_mbps"] * factor, 6)
        records.append(record)
    with (output / "performance.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)

    lines = ["# Performance results", "", f"Server machine type: {machine}", "",
             "以下数据来自实际汇总文件；每点对应一组三分钟实验。", "",
             f"| Clients | Peak CPU (%) | Peak memory (MiB) | Peak memory (%) | Peak RX ({unit}) | Peak TX ({unit}) |",
             "| --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        values = [row["peak_cpu_busy_pct"], row["peak_memory_used_mib"],
                  row["peak_memory_used_pct"], row["peak_rx_mbps"] * factor,
                  row["peak_tx_mbps"] * factor]
        lines.append(f"| {row['clients']} | " + " | ".join(f"{v:.3f}" for v in values) + " |")
    lines += ["", "## 数据观察", ""]
    for label, metric, scale, suffix in (
        ("CPU 峰值", "cpu_busy_pct", 1, "%"),
        ("内存峰值", "memory_used_mib", 1, " MiB"),
        ("接收带宽峰值", "rx_mbps", factor, f" {unit}"),
        ("发送带宽峰值", "tx_mbps", factor, f" {unit}"),
    ):
        values = [row[f"peak_{metric}"] * scale for row in rows]
        lines.append(f"- {label}：{describe(values)}；"
                     f"{rows[0]['clients']} client(s) 为 {values[0]:.3f}{suffix}，"
                     f"{rows[-1]['clients']} client(s) 为 {values[-1]:.3f}{suffix}。")
    if missing or excluded:
        lines += ["", "这是部分结果，缺失或无效阶段没有补值。", f"缺少客户端数量：{missing}"]
        lines += [f"- {item}" for item in excluded]
    lines += ["", "## 指标和结论边界", "",
              "- 实线表示一秒采样峰值，虚线表示窗口均值；不是重复实验的误差范围。",
              "- CPU 为整台 VM 可见 vCPU 的平均忙碌比例；共享核的 steal/配额需要结合原始指标解释。",
              "- 内存为 MemTotal - MemAvailable，不是仅 Python 进程 RSS；单位为 MiB。",
              f"- 带宽为服务器网卡 RX/TX，包含 GCS、HTTP、日志和其他流量；图中单位为 {unit}。",
              "- Mbps 是百万 bit/s；MB/s 是百万 byte/s，两者相差 8 倍。RX/TX 峰值可能发生在不同时间，不能相加当作总峰值。",
              "- 原始数据未被平滑、归一化或强制设为随并发上升。每点只有一次实验，不代表统计显著性。",
              "- HTTP 成功率、超时及其他错误未包含在资源汇总中，不能从图推断请求全部成功或服务器已饱和。",
              "", "## 报告分析建议（需要结合请求日志验证）", "",
              "- CPU 若增加，可讨论请求处理、GCS 下载及日志开销；若未增加，检查实际吞吐、等待时间和客户端瓶颈。",
              "- 带宽若增加，可能对应更多文件下载和响应；本图测量网卡全部流量，不能把它全部归因于响应正文。",
              "- 内存若变化较小，可结合固定 Gunicorn worker 数、线程共享进程资源解释；若增加，检查在途文件缓冲、客户端请求量及系统缓存。",
              "- 只有结合错误、延迟、吞吐和客户端资源，才能判断是否出现容量瓶颈。仅一个 CPU 峰值不够。",
              "- 若最多四个客户端未观察到过载，应限定结论为本实验负载范围，不声称绝对最大容量。", ""]
    (output / "performance.md").write_text("\n".join(lines), encoding="utf-8")


def draw_panel(ax, rows, kind, unit, factor):
    x = [row["clients"] for row in rows]
    panels = {
        "cpu": ("CPU vs clients", "CPU utilization (%)", [("cpu_busy_pct", "CPU", "#2166ac", 1)]),
        "memory": ("Memory vs clients", "Used memory (MiB)", [("memory_used_mib", "Memory", "#238b45", 1)]),
        "bandwidth": ("Bandwidth vs clients", f"Network rate ({unit})",
                      [("rx_mbps", "RX", "#2166ac", factor), ("tx_mbps", "TX", "#d95f02", factor)]),
    }
    title, ylabel, series = panels[kind]
    largest = 0
    for series_index, (metric, label, color, scale) in enumerate(series):
        for stat, style, marker in (("peak", "-", "o"), ("mean", "--", "s")):
            values = [row[f"{stat}_{metric}"] * scale for row in rows]
            largest = max(largest, max(values))
            # Keep absent concurrency levels as gaps, rather than interpolate them.
            points = {count: value for count, value in zip(x, values)}
            y = [points.get(count, float("nan")) for count in (1, 2, 3, 4)]
            ax.plot([1, 2, 3, 4], y, style, marker=marker, color=color,
                    linewidth=2 if stat == "peak" else 1.5, markersize=5,
                    alpha=1 if stat == "peak" else 0.65, label=f"{label} {stat}")
            if stat == "peak":
                for count, value in zip(x, values):
                    ax.annotate(f"{value:.2f}", (count, value),
                                xytext=(0, 7 + series_index * 11),
                                textcoords="offset points", ha="center", fontsize=8, color=color)
    ax.set_title(title, loc="left", fontweight="bold", pad=12)
    ax.set_xlabel("Concurrent client instances")
    ax.set_ylabel(ylabel)
    ax.set_xticks([1, 2, 3, 4])
    ax.set_xlim(0.7, 4.3)
    ax.set_ylim(0, 110 if kind == "cpu" else max(1, largest * 1.3))
    if kind == "cpu":
        ax.set_yticks([0, 20, 40, 60, 80, 100])
    ax.grid(axis="y", color="#d9dfe5", linewidth=0.7)
    ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)
    ax.legend(loc="best", fontsize=8, framealpha=0.9)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("stress-results/server-summary.jsonl"))
    parser.add_argument("--output", type=Path, default=Path("stress-results/figures"))
    parser.add_argument("--machine-type", default="not specified")
    parser.add_argument("--bandwidth-unit", choices=("Mbps", "MB/s"), default="Mbps")
    parser.add_argument("--allow-partial", action="store_true",
                        help="Plot available valid phases, explicitly marking missing results")
    args = parser.parse_args()
    try:
        rows, excluded, missing = load_rows(args.input, args.allow_partial)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    try:
        import matplotlib
        matplotlib.use("Agg")  # Save artifacts without requiring a GUI/display.
        import matplotlib.pyplot as plt
    except ImportError:
        parser.error("Install plotting dependencies: python -m pip install -r stress/requirements-plot.txt")
    factor = 1 if args.bandwidth_unit == "Mbps" else 1 / 8
    args.output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10,
                         "axes.titlepad": 12, "savefig.facecolor": "white"})
    partial = bool(excluded or missing)
    title = f"Web server resource usage | {args.machine_type}"
    if partial:
        title += " | PARTIAL RESULTS"
    footer = "180-second windows | Peak and mean of sampled whole-VM metrics | Warmup excluded"
    if missing:
        footer += f"\nMissing client counts: {', '.join(map(str, missing))}"

    fig, axes = plt.subplots(1, 3, figsize=(15, 5.2))
    for ax, kind in zip(axes, ("cpu", "memory", "bandwidth")):
        draw_panel(ax, rows, kind, args.bandwidth_unit, factor)
    fig.suptitle(title, fontsize=16, fontweight="bold", y=0.98)
    fig.text(0.5, 0.02, footer, ha="center", fontsize=9, color="#505a64")
    fig.tight_layout(rect=(0, 0.09, 1, 0.91))
    for extension in ("png", "pdf"):
        fig.savefig(args.output / f"performance.{extension}", dpi=300, bbox_inches="tight")
    plt.close(fig)

    for kind in ("cpu", "memory", "bandwidth"):
        fig, ax = plt.subplots(figsize=(6.4, 4.8))
        draw_panel(ax, rows, kind, args.bandwidth_unit, factor)
        fig.suptitle(title, fontsize=10)
        fig.text(0.5, 0.02, footer.replace(" | ", "\n"), ha="center", fontsize=8)
        fig.tight_layout(rect=(0, 0.19, 1, 0.93))
        for extension in ("png", "pdf"):
            fig.savefig(args.output / f"{kind}.{extension}", dpi=300, bbox_inches="tight")
        plt.close(fig)

    export_tables(rows, args.output, args.bandwidth_unit, factor,
                  args.machine_type, excluded, missing)
    provenance = {
        "input": str(args.input.resolve()),
        "input_sha256": hashlib.sha256(args.input.read_bytes()).hexdigest(),
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "machine_type_user_supplied": args.machine_type,
        "bandwidth_unit": args.bandwidth_unit,
        "clients": [row["clients"] for row in rows],
        "missing_clients": missing, "excluded_rows": excluded,
        "python_version": sys.version, "matplotlib_version": matplotlib.__version__,
    }
    (args.output / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")
    print(f"Saved combined and individual PNG/PDF graphs to {args.output.resolve()}")
    print("Also saved performance.csv, performance.md and provenance.json.")
    if partial:
        print("PARTIAL RESULTS: missing or invalid phases were not filled in.")


if __name__ == "__main__":
    main()
