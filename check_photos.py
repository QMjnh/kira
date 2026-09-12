from pathlib import Path

src = Path(r"E:\DCIM\100MSDCF")
dst = Path(r"G:\photos")

print("Scanning destination...")
dst_files = {p.name.lower() for p in dst.rglob("*") if p.is_file()}
print(f"Found {len(dst_files)} unique filenames in {dst}")

print("Checking source...")
missing = [p for p in src.rglob("*") if p.is_file() and p.name.lower() not in dst_files]

if not missing:
    print("OK: all files are present.")
else:
    print(f"MISSING: {len(missing)} files not found in {dst}:")
    for p in missing:
        print(p)
