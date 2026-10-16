"""Local TCP bridge: 127.0.0.1:<port> -> Tailscale SOCKS5 (127.0.0.1:1055) -> LK_TARGET_IP:<port>.

Needed because Colab has no TUN device, so Tailscale runs in userspace mode and only
exposes a SOCKS5 proxy. The native LiveKit (Rust) SDK cannot use SOCKS, so the
LD_PRELOAD shim (lk_redirect.c) points it at these local listeners instead.

Env:
  LK_TARGET_IP  IPv4 of the LiveKit host on your tailnet (required)
  LK_PORTS      comma-separated ports to bridge (default 7880,7881,5678)
"""
import asyncio
import os
import struct
import sys

SOCKS_HOST, SOCKS_PORT = "127.0.0.1", 1055
TARGET_IP = os.environ.get("LK_TARGET_IP", "")
PORTS = [int(p) for p in os.environ.get("LK_PORTS", "7880,7881,5678").split(",") if p.strip()]

if not TARGET_IP:
    sys.exit("LK_TARGET_IP is not set")


async def pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except Exception:
        pass
    finally:
        try:
            w.close()
        except Exception:
            pass


async def handle(client_r, client_w, port):
    try:
        sr, sw = await asyncio.open_connection(SOCKS_HOST, SOCKS_PORT)
        sw.write(b"\x05\x01\x00")
        await sw.drain()
        if (await sr.readexactly(2)) != b"\x05\x00":
            client_w.close()
            sw.close()
            return
        ip_bytes = bytes(map(int, TARGET_IP.split(".")))
        sw.write(b"\x05\x01\x00\x01" + ip_bytes + struct.pack("!H", port))
        await sw.drain()
        resp = await sr.readexactly(10)
        if resp[1] != 0x00:
            client_w.close()
            sw.close()
            return
        await asyncio.gather(pipe(client_r, sw), pipe(sr, client_w))
    except Exception:
        try:
            client_w.close()
        except Exception:
            pass


async def main():
    servers = []
    for p in PORTS:
        s = await asyncio.start_server(lambda r, w, p=p: handle(r, w, p), "127.0.0.1", p)
        servers.append(s)
        print(f"[bridge] listening on 127.0.0.1:{p} -> SOCKS5 -> {TARGET_IP}:{p}", flush=True)
    await asyncio.gather(*(s.serve_forever() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
