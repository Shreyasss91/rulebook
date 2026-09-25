#!/usr/bin/env python3
"""
Scan all file extensions in configured source folders.
Uses config.yaml for source paths.
"""

import yaml
from pathlib import Path
from collections import Counter


CONFIG_PATH = Path("config.yaml")


def load_config() -> dict:
    """Load configuration from config.yaml."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config file not found: {CONFIG_PATH}")
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def main():
    config = load_config()
    sources = config.get("docs_source", [])

    exts = Counter()
    total_files = 0

    for source in sources:
        source_path = Path(source).expanduser().resolve()
        if not source_path.exists():
            print(f"Warning: Source path does not exist: {source_path}")
            continue

        print(f"Scanning: {source_path}")
        for f in source_path.rglob("*"):
            if f.is_file():
                ext = f.suffix.lower()
                exts[ext] += 1
                total_files += 1

    print(f"\nTotal files: {total_files}")
    print("\nExtensions:")
    for ext, count in exts.most_common():
        label = ext if ext else "(no extension)"
        print(f"  {label}: {count}")


if __name__ == "__main__":
    main()