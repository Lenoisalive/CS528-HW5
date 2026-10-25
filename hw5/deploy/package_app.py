"""Create a reproducible source-only tarball; never include credentials or logs."""
import gzip
import hashlib
import io
from pathlib import Path
import sys
import tarfile

root = Path(__file__).resolve().parents[1]
paths = [root / "setup_schema.py", root / "requirements-db.txt"]
for folder in ("service1", "service2", "deploy"):
    paths.extend(p for p in (root / folder).iterdir()
                 if p.is_file() and p.suffix in {".py", ".txt", ".service"})
archive = io.BytesIO()
with tarfile.open(fileobj=archive, mode="w") as tar:
    for path in sorted(paths):
        data = path.read_bytes()
        info = tarfile.TarInfo(path.relative_to(root).as_posix())
        info.size, info.mode, info.mtime = len(data), 0o644, 0
        tar.addfile(info, io.BytesIO(data))
payload = gzip.compress(archive.getvalue(), mtime=0)
Path(sys.argv[1]).write_bytes(payload)
print(hashlib.sha256(payload).hexdigest())
