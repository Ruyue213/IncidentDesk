"""Conservative ownership proof for a registered loopback health listener.

No HTTP response is used as proof of ownership. Inspect socket metadata only,
with bounded subprocess output/time and a Linux procfs fallback. Failure to
inspect is False. For localhost, every resolved address must be owned, avoiding
attribution to another process listening on the other address family.
"""

import asyncio
import ipaddress
import os
import shutil
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

MAX_OUTPUT = 65536
MAX_FDS = 4096
PROBE_SECONDS = 3


async def _targets(url: str) -> tuple[set[str], int] | None:
    if not isinstance(url, str) or len(url) > 2048:
        return None
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or not parsed.hostname
        ):
            return None
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            return None
        if parsed.hostname == "localhost":
            resolved = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo("localhost", port, type=socket.SOCK_STREAM),
                timeout=0.5,
            )
            addresses = {ipaddress.ip_address(item[4][0]).compressed for item in resolved}
        else:
            addresses = {ipaddress.ip_address(parsed.hostname).compressed}
        if not addresses or any(not ipaddress.ip_address(value).is_loopback for value in addresses):
            return None
        return addresses, port
    except (OSError, ValueError, TimeoutError):
        return None


def _covered(targets: set[str], bindings: set[str]) -> bool:
    for target in targets:
        wildcard = "0.0.0.0" if ipaddress.ip_address(target).version == 4 else "::"
        if target not in bindings and wildcard not in bindings:
            return False
    return True


def _lsof_bindings(output: bytes, pid: int, port: int) -> set[str]:
    bindings = set()
    current_pid = None
    family = None
    for line in output.decode("utf-8", errors="replace").splitlines():
        if line.startswith("p"):
            current_pid = int(line[1:]) if line[1:].isdigit() else None
            family = None
        elif line.startswith("f"):
            family = None
        elif line.startswith("t"):
            family = line[1:]
        elif line.startswith("n") and current_pid == pid:
            try:
                address, found_port = line[1:].rsplit(":", 1)
                if int(found_port) != port or "->" in address:
                    continue
                if address == "*":
                    if family not in {"IPv4", "IPv6"}:
                        continue
                    address = "0.0.0.0" if family == "IPv4" else "::"
                bindings.add(ipaddress.ip_address(address.strip("[]")).compressed)
            except ValueError:
                continue
    return bindings


async def _lsof(pid: int, port: int) -> set[str] | None:
    executable = shutil.which("lsof")
    if not executable:
        return None
    process = None
    try:
        async with asyncio.timeout(PROBE_SECONDS):
            process = await asyncio.create_subprocess_exec(
                executable,
                "-nP",
                "-a",
                "-p",
                str(pid),
                f"-iTCP:{port}",
                "-sTCP:LISTEN",
                "-F",
                "pftn",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            output = bytearray()
            while chunk := await process.stdout.read(min(4096, MAX_OUTPUT + 1 - len(output))):
                output.extend(chunk)
                if len(output) > MAX_OUTPUT:
                    return None
            status = await process.wait()
            if status not in (0, 1):
                return None
            return _lsof_bindings(bytes(output), pid, port)
    except (OSError, TimeoutError):
        return None
    finally:
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=0.5)
            except TimeoutError:
                pass


def _decode_proc_address(value: str, ipv6: bool) -> str:
    raw = bytes.fromhex(value)
    if sys.byteorder == "little":
        width = 4 if ipv6 else len(raw)
        raw = b"".join(raw[index : index + width][::-1] for index in range(0, len(raw), width))
    return socket.inet_ntop(socket.AF_INET6 if ipv6 else socket.AF_INET, raw)


def _proc_bindings(pid: int, port: int) -> set[str] | None:
    """Bounded procfs reads. Never inspect argv, environment, memory or file data."""
    try:
        base = Path("/proc")
        process = base / str(pid)
        # A listener in another network namespace cannot serve this caller's loopback URL.
        if os.stat(process / "ns/net").st_ino != os.stat(base / "self/ns/net").st_ino:
            return None
        started = (process / "stat").read_bytes()[:4096]
        inodes = set()
        with os.scandir(process / "fd") as descriptors:
            for count, descriptor in enumerate(descriptors):
                if count >= MAX_FDS:
                    return None
                try:
                    value = os.readlink(descriptor.path)
                except FileNotFoundError:
                    continue
                if value.startswith("socket:[") and value.endswith("]"):
                    inodes.add(value[8:-1])
        bindings = set()
        for filename in ("tcp", "tcp6"):
            path = base / "net" / filename
            if not path.exists():
                continue
            with path.open("rb") as source:
                content = source.read(MAX_OUTPUT + 1)
            if len(content) > MAX_OUTPUT:
                return None
            for row in content.decode("ascii").splitlines()[1:]:
                fields = row.split()
                if len(fields) < 10 or fields[3] != "0A" or fields[9] not in inodes:
                    continue
                address, found_port = fields[1].split(":")
                if int(found_port, 16) == port:
                    bindings.add(_decode_proc_address(address, filename == "tcp6"))
        # Check the same process still exists after both ownership snapshots.
        if (process / "stat").read_bytes()[:4096].rsplit(b")", 1)[-1].split()[19] != (
            started.rsplit(b")", 1)[-1].split()[19]
        ):
            return None
        return bindings
    except (OSError, ValueError, IndexError):
        return None


async def owns_health_listener(pid: int, url: str) -> bool:
    if type(pid) is not int or not 0 < pid <= 2**31 - 1:
        return False
    targets = await _targets(url)
    if targets is None:
        return False
    addresses, port = targets
    bindings = await _lsof(pid, port)
    if bindings is None and sys.platform.startswith("linux"):
        try:
            bindings = await asyncio.wait_for(
                asyncio.to_thread(_proc_bindings, pid, port), timeout=1
            )
        except TimeoutError:
            return False
    return bindings is not None and _covered(addresses, bindings)
