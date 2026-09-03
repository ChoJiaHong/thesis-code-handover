import csv
import argparse
from statistics import mean, median


def load_rows(path):
    """Load CSV rows from the given path."""
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


def _get_float(row, field):
    try:
        return float(row[field])
    except (KeyError, ValueError, TypeError):
        return None


def analyze(rows):
    metrics = ["wait_ms", "inference_ms", "postprocess_ms", "total_ms"]
    summary = {}
    for m in metrics:
        values = [_get_float(r, m) for r in rows]
        values = [v for v in values if v is not None]
        if not values:
            continue
        summary[m] = {
            "count": len(values),
            "average": mean(values),
            "median": median(values),
            "max": max(values),
            "min": min(values),
        }
    return summary


def print_summary(stats):
    for name, s in stats.items():
        print(name)
        for k, v in s.items():
            if isinstance(v, float):
                print(f"  {k}: {v:.2f} ms")
            else:
                print(f"  {k}: {v}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Analyze request trace CSV")
    parser.add_argument("csv_path", nargs="?", default="logs/request_trace.csv")
    args = parser.parse_args()

    rows = load_rows(args.csv_path)
    stats = analyze(rows)
    print_summary(stats)


if __name__ == "__main__":
    main()
