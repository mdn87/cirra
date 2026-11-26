"""Send the BWS witness only to the root client of the systemd socket."""

import os
import socket
import struct
import sys


def deliver(channel):
    if channel.family != socket.AF_UNIX:
        raise ValueError
    credentials = channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _, uid, _ = struct.unpack("3i", credentials)
    key = os.environ.pop("CIRRA_WITNESS_KEY", "").encode("utf-8")
    if uid != 0 or not 32 <= len(key) <= 65536:
        raise ValueError
    channel.settimeout(5)
    channel.sendall(key)


if __name__ == "__main__":
    try:
        with socket.socket(fileno=os.dup(0)) as connection:
            deliver(connection)
    except (OSError, ValueError):
        sys.exit("Cirra could not deliver the backup witness.")
