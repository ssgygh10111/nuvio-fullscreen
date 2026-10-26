#!/usr/bin/env python3
import plistlib
import struct
import sys
import tempfile
import zipfile
from pathlib import Path


NET_TIMEOUT = 5.0  # seconds without data before mpv drops the connection and reconnects (mpv default: 60)


def patch_mpv_timeout(binary: Path) -> bool:
    """Change libmpv's default network-timeout (60s) to NET_TIMEOUT.

    The default lives in a const struct next to the "libmpv" user-agent pointer:
    { ..., useragent="libmpv", <6 qwords>, timeout=60.0, ... }. mpv already asks
    FFmpeg to reconnect, so a shorter timeout means a stalled stream resumes in
    seconds instead of hanging for a minute.
    """
    d = bytearray(binary.read_bytes())
    if d[:4] not in (b"\xcf\xfa\xed\xfe",):
        return False
    libs = set()
    i = 0
    while (i := d.find(b"\0libmpv\0", i)) >= 0:
        libs.add(i + 1)
        i += 1
    if not libs:
        return False
    sixty = struct.pack("<d", 60.0)
    hits = []
    i = 0
    while (i := d.find(sixty, i)) >= 0:
        if i % 8 == 0 and i >= 56:
            q = struct.unpack_from("<Q", d, i - 56)[0]
            # chained-fixup pointer: low 36 bits hold the target offset (file offset == vm offset here)
            if (q & 0xFFFFFFFFF) in libs:
                hits.append(i)
        i += 1
    if len(hits) != 1:
        print(f"mpv timeout: expected 1 match in {binary.name}, found {len(hits)} - skipped")
        return False
    struct.pack_into("<d", d, hits[0], NET_TIMEOUT)
    binary.write_bytes(bytes(d))
    print(f"mpv network-timeout 60 -> {NET_TIMEOUT:g}s in {binary.name} @ {hex(hits[0])}")
    return True


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

        exe = apps[0] / info.get("CFBundleExecutable", apps[0].stem)
        done = patch_mpv_timeout(exe)
        if not done:
            for fw in (apps[0] / "Frameworks").glob("*.framework"):
                b = fw / fw.stem
                if b.is_file() and patch_mpv_timeout(b):
                    done = True
                    break
        if not done:
            print("WARNING: mpv network-timeout not patched")

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
