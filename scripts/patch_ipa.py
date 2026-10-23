#!/usr/bin/env python3
import plistlib
import sys
import tempfile
import zipfile
from pathlib import Path


def patch(src: Path, dst: Path) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        with zipfile.ZipFile(src) as z:
            z.extractall(tmp)
        apps = list((tmp / "Payload").glob("*.app"))
        if len(apps) != 1:
            sys.exit(f"expected one .app in Payload, found {len(apps)}")
        plist_path = apps[0] / "Info.plist"
        raw = plist_path.read_bytes()
        fmt = plistlib.FMT_BINARY if raw.startswith(b"bplist") else plistlib.FMT_XML
        info = plistlib.loads(raw)
        info["UILaunchScreen"] = {"UIImageRespectsSafeAreaInsets": False}
        info["UIRequiresFullScreen"] = True
        plist_path.write_bytes(plistlib.dumps(info, fmt=fmt))

        dst.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as out:
            for path in sorted(tmp.rglob("*")):
                rel = path.relative_to(tmp).as_posix()
                if path.is_symlink():
                    zi = zipfile.ZipInfo(rel)
                    zi.create_system = 3
                    zi.external_attr = (0o120777 << 16)
                    out.writestr(zi, str(path.readlink()))
                elif path.is_file():
                    out.write(path, rel)
        print(f"patched {apps[0].name} -> {dst}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: patch_ipa.py input.ipa output.ipa")
    patch(Path(sys.argv[1]), Path(sys.argv[2]))
