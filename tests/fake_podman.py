#!/usr/bin/env python3
"""Stand-in for `podman` on the "VM" in tests of the disposable desktop (D1).

State lives in $FAKE_PODMAN_DIR: `image` (exists = the image is built), `containers/<name>/` (a running
container; `run.json` keeps the argv it was started with, `actions.jsonl` what was done in it, `Downloads/` its
downloads). Screenshots are real PNGs whose bytes change after every action.
"""
import json
import os
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(os.environ["FAKE_PODMAN_DIR"])
CONTAINERS = ROOT / "containers"
args = sys.argv[1:]


def png(w: int, h: int, note: str) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    raw = b"".join(b"\0" + b"\xff" * (w * 3) for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"tEXt", b"note\0" + note.encode()) + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))


def name_filter() -> str | None:
    for i, a in enumerate(args):
        if a == "--filter" and args[i + 1].startswith("name="):
            return args[i + 1][5:].strip("^$")
    return None


cmd = args[0]
if cmd == "image":
    sys.exit(0 if (ROOT / "image").exists() else 1)
if cmd == "ps":
    names = sorted(p.name for p in CONTAINERS.iterdir()) if CONTAINERS.exists() else []
    want = name_filter()
    names = [n for n in names if want is None or n == want]
    print("\n".join(names if "--format" in args else [f"id-{n}" for n in names]))
    sys.exit(0)
if cmd == "run":
    name = args[args.index("--name") + 1]
    d = CONTAINERS / name
    (d / "Downloads").mkdir(parents=True)
    (d / "run.json").write_text(json.dumps(args))
    print(f"id-{name}")
    sys.exit(0)
if cmd == "rm":
    import shutil
    shutil.rmtree(CONTAINERS / args[-1], ignore_errors=True)
    sys.exit(0)
if cmd == "exec":
    d = CONTAINERS / args[1]
    if not d.is_dir():
        print(f"Error: no container with name or ID {args[1]!r} found", file=sys.stderr)
        sys.exit(125)
    rest = args[2:]
    log = d / "actions.jsonl"
    done = len(log.read_text().splitlines()) if log.exists() else 0
    if rest[:2] == ["xdotool", "search"]:
        print("4194307")
    elif rest[0] == "import":
        run = json.loads((d / "run.json").read_text())
        geometry = next(a.split("=", 1)[1] for a in run if a.startswith("SCOUT_GEOMETRY="))
        w, h = (int(v) for v in geometry.split("x"))
        sys.stdout.buffer.write(png(w, h, f"after {done} actions"))
    elif rest[0] == "xdotool":
        with log.open("a") as f:
            f.write(json.dumps(rest) + "\n")
    elif rest[0] == "find":
        for p in sorted((d / "Downloads").iterdir()):
            print(f"{p.name}\t{p.stat().st_size}")
    elif rest[0] == "cat":
        sys.stdout.buffer.write((d / "Downloads" / Path(rest[-1]).name).read_bytes())
    else:
        print(f"fake podman: unknown exec {rest}", file=sys.stderr)
        sys.exit(2)
    sys.exit(0)
print(f"fake podman: unknown command {args}", file=sys.stderr)
sys.exit(2)
