#!/usr/bin/env python3
"""
Simplified deduplication - processes in batches, saves progress.
"""
import hashlib
import json
from pathlib import Path
import pdfplumber
from collections import defaultdict
import sys

KERC_ROOT = Path(r"D:\Office PC\D DRIVE\KERC")
IGNORE_LIST_PATH = Path("docs/deduplication_ignore_list.json")
PROGRESS_PATH = Path("docs/deduplication_progress.json")

def get_signature(pdf_path: Path) -> str:
    try:
        stat = pdf_path.stat()
        size = stat.st_size
        with pdfplumber.open(pdf_path) as pdf:
            n = len(pdf.pages)
            first = (pdf.pages[0].extract_text() or "")[:300] if n > 0 else ""
            last = (pdf.pages[-1].extract_text() or "")[:300] if n > 1 else ""
        return hashlib.md5(f"{size}|{n}|{first}|{last}".encode()).hexdigest()[:12]
    except:
        return hashlib.md5(f"{pdf_path.stat().st_size}|{pdf_path}".encode()).hexdigest()[:12]

def main():
    pdf_files = list(KERC_ROOT.rglob("*.pdf"))
    print(f"Total: {len(pdf_files)} PDFs")

    # Load progress if exists
    done = set()
    sig_to_files = defaultdict(list)
    if PROGRESS_PATH.exists():
        data = json.loads(PROGRESS_PATH.read_text())
        done = set(data.get("done", []))
        for sig, files in data.get("sig_to_files", {}).items():
            sig_to_files[sig] = files
        print(f"Resumed: {len(done)} already processed")

    # Process in batches
    batch_size = 100
    for i, pdf_path in enumerate(pdf_files):
        rel = str(pdf_path.relative_to(KERC_ROOT))
        if rel in done:
            continue

        sig = get_signature(pdf_path)
        sig_to_files[sig].append(rel)
        done.add(rel)

        if (i + 1) % batch_size == 0:
            print(f"  {i+1}/{len(pdf_files)}")
            # Save progress
            PROGRESS_PATH.write_text(json.dumps({
                "done": list(done),
                "sig_to_files": dict(sig_to_files)
            }))

    # Final save
    PROGRESS_PATH.write_text(json.dumps({
        "done": list(done),
        "sig_to_files": dict(sig_to_files)
    }))

    # Generate ignore list
    duplicates = {}
    for sig, files in sig_to_files.items():
        if len(files) > 1:
            files.sort(key=lambda x: (len(x), x))
            duplicates[files[0]] = files[1:]

    ignore_list = []
    for kept, ignored in duplicates.items():
        ignore_list.extend(ignored)

    IGNORE_LIST_PATH.write_text(json.dumps(sorted(ignore_list), indent=2))

    # Report
    print(f"\nDone! Total: {len(pdf_files)}, Unique: {len(pdf_files) - len(ignore_list)}, Ignored: {len(ignore_list)}")
    print(f"Ignore list: {IGNORE_LIST_PATH}")

if __name__ == "__main__":
    main()