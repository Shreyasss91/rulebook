from pathlib import Path
from collections import Counter

root = Path(r"D:\Office PC\D DRIVE\KERC")
exts = Counter()
for f in root.rglob("*"):
    if f.is_file():
        ext = f.suffix.lower()
        exts[ext] += 1

for ext, count in exts.most_common():
    label = ext if ext else "(no extension)"
    print(f"{label}: {count}")