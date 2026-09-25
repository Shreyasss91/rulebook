#!/usr/bin/env python3
"""
Deduplication script - configurable via config.yaml.
Creates an ignore list (files to skip) without deleting anything.
Supports multiple source folders and file types.
"""

import hashlib
import json
import yaml
from pathlib import Path
from collections import defaultdict
import sys

# Optional imports for different file types
try:
    import pdfplumber
    HAS_PDFPLUMBER = True
except ImportError:
    HAS_PDFPLUMBER = False

try:
    from docx import Document
    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False


CONFIG_PATH = Path("config.yaml")


def load_config() -> dict:
    """Load configuration from config.yaml."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config file not found: {CONFIG_PATH}")
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def get_file_signature(file_path: Path, config: dict) -> str:
    """
    Generate a content signature for deduplication.
    Strategy varies by file type.
    """
    stat = file_path.stat()
    size = stat.st_size
    prefix_chars = config.get("signature_prefix_chars", 300)
    suffix = file_path.suffix.lower()

    try:
        if suffix == ".pdf" and HAS_PDFPLUMBER:
            with pdfplumber.open(file_path) as pdf:
                n = len(pdf.pages)
                first = (pdf.pages[0].extract_text() or "")[:prefix_chars] if n > 0 else ""
                last = (pdf.pages[-1].extract_text() or "")[:prefix_chars] if n > 1 else ""
            return hashlib.md5(f"{size}|{n}|{first}|{last}".encode()).hexdigest()[:12]

        elif suffix in [".docx"] and HAS_DOCX:
            doc = Document(file_path)
            full_text = "\n".join(p.text for p in doc.paragraphs)
            first = full_text[:prefix_chars]
            last = full_text[-prefix_chars:] if len(full_text) > prefix_chars else ""
            return hashlib.md5(f"{size}|{len(full_text)}|{first}|{last}".encode()).hexdigest()[:12]

        elif suffix in [".txt", ".md"]:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
            first = text[:prefix_chars]
            last = text[-prefix_chars:] if len(text) > prefix_chars else ""
            return hashlib.md5(f"{size}|{len(text)}|{first}|{last}".encode()).hexdigest()[:12]

        elif suffix in [".doc"]:
            # .doc requires antiword or libreoffice - fallback to size+path
            pass

    except Exception:
        pass

    # Fallback: size + path hash
    return hashlib.md5(f"{size}|{file_path}".encode()).hexdigest()[:12]


def find_files(config: dict) -> list[Path]:
    """Recursively find all matching files in all source folders."""
    file_types = config.get("file_types", ["pdf"])
    sources = config.get("docs_source", [])
    extensions = [f".{ext.lower().lstrip('.')}" for ext in file_types]

    all_files = []
    for source in sources:
        source_path = Path(source).expanduser().resolve()
        if not source_path.exists():
            print(f"Warning: Source path does not exist: {source_path}")
            continue
        for ext in extensions:
            all_files.extend(source_path.rglob(f"*{ext}"))
    return all_files


def get_relative_path(file_path: Path, sources: list) -> str:
    """Get relative path from the matching source folder."""
    for source in sources:
        source_path = Path(source).expanduser().resolve()
        try:
            return str(file_path.relative_to(source_path))
        except ValueError:
            continue
    # Fallback: use full path
    return str(file_path)


def main():
    config = load_config()

    ignore_list_path = Path(config["ignore_list_path"])
    progress_path = Path(config["progress_path"])
    batch_size = config.get("batch_size", 100)

    # Ensure output directories exist
    ignore_list_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path.parent.mkdir(parents=True, exist_ok=True)

    # Find all files
    all_files = find_files(config)
    print(f"Found {len(all_files)} files across {len(config['docs_source'])} source(s)")
    print(f"File types: {config['file_types']}")

    # Load progress if exists
    done = set()
    sig_to_files = defaultdict(list)
    if progress_path.exists():
        data = json.loads(progress_path.read_text(encoding="utf-8"))
        done = set(data.get("done", []))
        for sig, files in data.get("sig_to_files", {}).items():
            sig_to_files[sig] = files
        print(f"Resumed: {len(done)} already processed")

    sources = config["docs_source"]

    # Process in batches
    for i, file_path in enumerate(all_files):
        rel = get_relative_path(file_path, sources)
        if rel in done:
            continue

        sig = get_file_signature(file_path, config)
        sig_to_files[sig].append(rel)
        done.add(rel)

        if (i + 1) % batch_size == 0:
            print(f"  {i+1}/{len(all_files)}")
            progress_path.write_text(json.dumps({
                "done": list(done),
                "sig_to_files": dict(sig_to_files)
            }, indent=2))

    # Final save
    progress_path.write_text(json.dumps({
        "done": list(done),
        "sig_to_files": dict(sig_to_files)
    }, indent=2))

    # Generate ignore list
    duplicates = {}
    for sig, files in sig_to_files.items():
        if len(files) > 1:
            files.sort(key=lambda x: (len(x), x))
            duplicates[files[0]] = files[1:]

    ignore_list = []
    for kept, ignored in duplicates.items():
        ignore_list.extend(ignored)

    ignore_list_path.write_text(json.dumps(sorted(ignore_list), indent=2))

    # Report
    print(f"\nDone! Total: {len(all_files)}, Unique: {len(all_files) - len(ignore_list)}, Ignored: {len(ignore_list)}")
    print(f"Ignore list: {ignore_list_path}")


if __name__ == "__main__":
    main()