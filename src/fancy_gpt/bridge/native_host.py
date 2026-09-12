from __future__ import annotations

import argparse
import json
import struct
import sys
import os
import threading
from pathlib import Path
from typing import BinaryIO

MAX_NATIVE_INBOUND_BYTES = 8 * 1024 * 1024
MAX_NATIVE_OUTBOUND_BYTES = 1 * 1024 * 1024

from websockets.sync.client import connect

from .protocol import dumps, hello, loads
from fancy_gpt.runtime_paths import default_bridge_native_config


def _read_native(stream: BinaryIO) -> dict | None:
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise EOFError("truncated native messaging header")
    size = struct.unpack("=I", header)[0]
    if size > MAX_NATIVE_INBOUND_BYTES:
        raise ValueError("native message exceeds FancyGPT safety limit")
    payload = stream.read(size)
    if len(payload) != size:
        raise EOFError("truncated native messaging payload")
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("native message must be an object")
    return value


def _write_native(stream: BinaryIO, message: dict) -> None:
    payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_NATIVE_OUTBOUND_BYTES:
        raise ValueError("native host response exceeds browser 1 MiB Native Messaging limit")
    stream.write(struct.pack("=I", len(payload)))
    stream.write(payload)
    stream.flush()



def _configure_binary_stdio() -> None:
    if os.name != "nt":
        return
    import msvcrt
    msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
    msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)


def run_native_host(config_path: Path) -> None:
    _configure_binary_stdio()
    config = json.loads(config_path.expanduser().read_text(encoding="utf-8"))
    endpoint = str(config["endpoint"])
    token = str(config["token"])
    tunnel_ids = [str(item) for item in config.get("tunnel_ids", [])]
    if not tunnel_ids or "*" in tunnel_ids:
        raise ValueError("native host requires one or more exact tunnel_ids; wildcard registration is forbidden")
    browser = str(config.get("browser", "native-extension"))
    connection = connect(endpoint, open_timeout=10, max_size=8 * 1024 * 1024)
    connection.send(dumps(hello(role="browser", token=token, tunnel_ids=tunnel_ids, browser=browser)))
    ack = loads(connection.recv(timeout=10))
    if ack.get("type") != "hello_ack":
        raise RuntimeError(f"bridge refused native browser worker: {ack}")

    stopped = threading.Event()

    def bridge_to_extension() -> None:
        try:
            while not stopped.is_set():
                raw = connection.recv()
                _write_native(sys.stdout.buffer, loads(raw))
        except Exception:
            stopped.set()

    thread = threading.Thread(target=bridge_to_extension, name="fancy-native-bridge-rx", daemon=True)
    thread.start()
    try:
        while not stopped.is_set():
            message = _read_native(sys.stdin.buffer)
            if message is None:
                break
            connection.send(dumps(message))
    finally:
        stopped.set()
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="FancyGPT browser-extension Native Messaging host")
    parser.add_argument(
        "--config",
        type=Path,
        default=default_bridge_native_config(),
    )
    args, _unknown = parser.parse_known_args()
    run_native_host(args.config)


if __name__ == "__main__":
    main()
