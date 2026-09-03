import argparse
import csv


def load_by_timestamp(path):
    data = {}
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = row.get("timestamp")
            if ts is not None:
                data[ts] = row
    return data


def merge_metrics(rps_path, completion_path, queue_path):
    rps = load_by_timestamp(rps_path)
    completion = load_by_timestamp(completion_path)
    queue = load_by_timestamp(queue_path)

    timestamps = set(rps) | set(completion) | set(queue)
    rows = []
    for ts in sorted(timestamps):
        merged = {
            "timestamp": ts,
            "rps": rps.get(ts, {}).get("rps"),
            "completed": completion.get(ts, {}).get("completed"),
            "avg": queue.get(ts, {}).get("avg"),
            "max": queue.get(ts, {}).get("max"),
            "min": queue.get(ts, {}).get("min"),
            "latest": queue.get(ts, {}).get("latest"),
            "zero_count": queue.get(ts, {}).get("zero_count"),
            "zero_ratio": queue.get(ts, {}).get("zero_ratio"),
        }
        rows.append(merged)
    return rows


def write_csv(rows, output_path):
    fieldnames = [
        "timestamp",
        "rps",
        "completed",
        "avg",
        "max",
        "min",
        "latest",
        "zero_count",
        "zero_ratio",
    ]
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def main():
    parser = argparse.ArgumentParser(description="Merge metric CSV files by timestamp")
    parser.add_argument("--rps-path", default="logs/rps_stats.csv")
    parser.add_argument("--completion-path", default="logs/completion_stats.csv")
    parser.add_argument("--queue-path", default="logs/queue_stats.csv")
    parser.add_argument("--output-path", default="logs/merged_stats.csv")
    args = parser.parse_args()

    rows = merge_metrics(args.rps_path, args.completion_path, args.queue_path)
    write_csv(rows, args.output_path)


if __name__ == "__main__":
    main()
