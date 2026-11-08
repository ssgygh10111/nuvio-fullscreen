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


RECONNECT_MAX = b"300"  # FFmpeg reconnect_delay_max in seconds (mpv sets "7")


def _segments(d: bytes):
    """(vmaddr, fileoff, filesize) of every segment + the __text and __cstring sections."""
    ncmds = struct.unpack_from("<I", d, 16)[0]
    off, segs, sects = 32, [], {}
    for _ in range(ncmds):
        cmd, size = struct.unpack_from("<II", d, off)
        if cmd == 0x19:  # LC_SEGMENT_64
            vmaddr, _vmsize, fileoff, filesize = struct.unpack_from("<QQQQ", d, off + 24)
            segs.append((vmaddr, fileoff, filesize))
            nsects = struct.unpack_from("<I", d, off + 64)[0]
            for i in range(nsects):
                so = off + 72 + i * 80
                name = d[so:so + 16].rstrip(b"\0").decode()
                addr, sz, fo = struct.unpack_from("<QQI", d, so + 32)
                if name in ("__text", "__cstring"):
                    sects[name] = (addr, sz, fo)
        off += size
    return segs, sects


def _adrp_target(ins: int, pc: int):
    if (ins & 0x9F000000) != 0x90000000:
        return None
    imm = ((((ins >> 5) & 0x7FFFF) << 2) | ((ins >> 29) & 3))
    if imm & (1 << 20):
        imm -= 1 << 21
    return (pc & ~0xFFF) + (imm << 12)


def _adrp(rd: int, pc: int, target: int) -> int:
    imm = ((target & ~0xFFF) - (pc & ~0xFFF)) >> 12
    assert -(1 << 20) <= imm < (1 << 20)
    imm &= (1 << 21) - 1
    return 0x90000000 | ((imm & 3) << 29) | ((imm >> 2) << 5) | rd


def patch_reconnect_delay(binary: Path) -> bool:
    """Let FFmpeg keep retrying for minutes instead of ~11 s when the server is briefly unavailable.

    mpv opens network streams with reconnect=1 and reconnect_delay_max=7, so FFmpeg retries after
    0, 1, 3 and 7 s and then gives up: a Plex server that refuses for longer than ~11 s ends the
    stream and the video freezes until it is reopened. mpv's code is
        adrp x1, "reconnect_delay_max"@PAGE ; add x1, x1, @PAGEOFF
        adrp x2, "7"@PAGE                   ; add x2, x2, @PAGEOFF
    and only the second pair is changed, to point at an existing "300" string (the "7" string and
    FFmpeg's own option table are left alone). Retries then come after 0, 1, 3, 7, 15, 31, 63, 127
    and 255 s (FFmpeg also stops after 256 s of waiting in total), so a pause of up to ~4 minutes
    is ridden out: the player shows buffering and continues by itself, like the Plex app.
    """
    d = bytearray(binary.read_bytes())
    if d[:4] != b"\xcf\xfa\xed\xfe":
        return False
    segs, sects = _segments(d)
    if "__text" not in sects or "__cstring" not in sects:
        return False

    def f2v(fo):
        for va, f0, fs in segs:
            if f0 <= fo < f0 + fs:
                return va + fo - f0
        return None

    def v2f(va):
        for v0, f0, fs in segs:
            if v0 <= va < v0 + fs:
                return f0 + va - v0
        return None

    def cstr(va):
        fo = v2f(va)
        return bytes(d[fo:d.index(b"\0", fo)]) if fo is not None else None

    cs_addr, cs_size, cs_fo = sects["__cstring"]
    cs = bytes(d[cs_fo:cs_fo + cs_size])
    key = cs.find(b"\0reconnect_delay_max\0")
    if key < 0 or cs.find(b"\0reconnect_delay_max\0", key + 1) >= 0:
        print("reconnect delay: option name not found once - skipped")
        return False
    key_va = cs_addr + key + 1
    val = cs.find(b"\0" + RECONNECT_MAX + b"\0")
    if val < 0:
        print(f"reconnect delay: no '{RECONNECT_MAX.decode()}' string - skipped")
        return False
    new_va = cs_addr + val + 1

    t_addr, t_size, t_fo = sects["__text"]
    lo = key_va & 0xFFF
    hits = []
    for rn in range(31):  # add x1, xN, #lo
        pat = struct.pack("<I", 0x91000000 | (lo << 10) | (rn << 5) | 1)
        i = d.find(pat, t_fo, t_fo + t_size)
        while i >= 0:
            if (i - t_fo) % 4 == 0:
                pc = t_addr + i - t_fo
                for back in range(1, 5):  # matching adrp x(rn) shortly before
                    prev = struct.unpack_from("<I", d, i - 4 * back)[0]
                    if (prev & 31) == rn and _adrp_target(prev, pc - 4 * back) == (key_va & ~0xFFF):
                        hits.append(i)
                        break
            i = d.find(pat, i + 1, t_fo + t_size)
    if len(hits) != 1:
        print(f"reconnect delay: expected 1 use of the option name, found {len(hits)} - skipped")
        return False
    # the value: adrp x2 + add x2, x2 right after it
    for j in range(1, 6):
        fo = hits[0] + 4 * j
        ins = struct.unpack_from("<I", d, fo)[0]
        pc = t_addr + fo - t_fo
        page = _adrp_target(ins, pc)
        if page is None or (ins & 31) != 2:
            continue
        for k in range(1, 4):
            add = struct.unpack_from("<I", d, fo + 4 * k)[0]
            if (add & 0xFFC00000) == 0x91000000 and (add & 31) == 2 and ((add >> 5) & 31) == 2:
                old = cstr(page + ((add >> 10) & 0xFFF))
                if old == RECONNECT_MAX:
                    print("reconnect delay: already patched")
                    return True
                if old != b"7":
                    print(f"reconnect delay: unexpected value {old!r} - skipped")
                    return False
                struct.pack_into("<I", d, fo, _adrp(2, pc, new_va))
                struct.pack_into("<I", d, fo + 4 * k, 0x91000000 | ((new_va & 0xFFF) << 10) | (2 << 5) | 2)
                # read it back the way the CPU will
                chk = cstr(_adrp_target(struct.unpack_from("<I", d, fo)[0], pc) + ((struct.unpack_from("<I", d, fo + 4 * k)[0] >> 10) & 0xFFF))
                assert chk == RECONNECT_MAX, chk
                binary.write_bytes(bytes(d))
                print(f"FFmpeg reconnect_delay_max 7 -> {RECONNECT_MAX.decode()}s in {binary.name} @ {hex(f2v(fo))}")
                return True
        break
    print("reconnect delay: value load not found - skipped")
    return False


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
        binaries = [exe] + [fw / fw.stem for fw in sorted((apps[0] / "Frameworks").glob("*.framework"))]
        binaries = [b for b in binaries if b.is_file()]
        for fn, what in ((patch_mpv_timeout, "mpv network-timeout"), (patch_reconnect_delay, "FFmpeg reconnect delay")):
            if not any(fn(b) for b in binaries):
                print(f"WARNING: {what} not patched")

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
