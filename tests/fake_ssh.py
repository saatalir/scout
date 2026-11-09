#!/usr/bin/env python3
"""Stand-in for `ssh` in tests: runs the remote command locally, or forwards a port.

    ssh [opts] -N -L 127.0.0.1:LP:127.0.0.1:RP target   -> TCP forwarder LP -> RP
    ssh [opts] target 'command ...'                      -> runs the command with sh
"""
import asyncio
import subprocess
import sys

args = sys.argv[1:]
forward, rest, i = None, [], 0
while i < len(args):
    a = args[i]
    if a in ("-o", "-i"):
        i += 2
        continue
    if a == "-L":
        forward = args[i + 1]
        i += 2
        continue
    if a in ("-N", "-T"):
        i += 1
        continue
    rest.append(a)
    i += 1

if forward:
    _, lp, _, rp = forward.split(":")

    async def pipe(r, w):
        try:
            while data := await r.read(65536):
                w.write(data)
                await w.drain()
        finally:
            w.close()

    async def handle(cr, cw):
        try:
            ur, uw = await asyncio.open_connection("127.0.0.1", int(rp))
        except OSError:
            cw.close()
            return
        await asyncio.gather(pipe(cr, uw), pipe(ur, cw), return_exceptions=True)

    async def main():
        server = await asyncio.start_server(handle, "127.0.0.1", int(lp))
        async with server:
            await server.serve_forever()

    asyncio.run(main())
else:
    target, command = rest[0], " ".join(rest[1:])
    sys.exit(subprocess.call(["sh", "-c", command]))
