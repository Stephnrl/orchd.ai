"""Bounded capture for trusted executables; never a general shell interface."""
import os
import subprocess
import threading
import time


def capture(argv, *, cwd=None, env=None, timeout=20, limit=65536, input_bytes=None, cancel_event=None):
    process = subprocess.Popen(argv, cwd=cwd, env=env, shell=False, stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    streams = [bytearray(), bytearray()]
    overflow = threading.Event()

    def drain(source, output):
        while chunk := source.read(4096):
            available = max(0, limit - len(output))
            output.extend(chunk[:available])
            if len(chunk) > available:
                overflow.set()
        source.close()

    readers = [threading.Thread(target=drain, args=(source, output), daemon=True)
               for source, output in zip((process.stdout, process.stderr), streams)]
    for reader in readers:
        reader.start()
    writer = None
    if input_bytes is not None:
        # A non-reading child must not block the caller before the deadline loop.
        def write_input():
            try:
                process.stdin.write(input_bytes)
            except (BrokenPipeError, OSError):
                pass
            finally:
                try:
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
        writer = threading.Thread(target=write_input, daemon=True)
        writer.start()
    deadline = time.monotonic() + timeout
    failure = None
    while process.poll() is None:
        if overflow.is_set() or time.monotonic() >= deadline or (cancel_event and cancel_event.is_set()):
            failure = "cancelled" if cancel_event and cancel_event.is_set() else "output_limit" if overflow.is_set() else "timeout"
            process.kill()
            break
        time.sleep(0.01)
    process.wait(timeout=5)
    for reader in readers:
        reader.join(timeout=2)
    if writer:
        writer.join(timeout=2)
    if any(reader.is_alive() for reader in readers):
        failure = "unreaped_stream"
    if overflow.is_set():
        failure = "output_limit"
    return {"code": process.returncode, "stdout": bytes(streams[0]).decode(errors="replace"),
            "stderr": bytes(streams[1]).decode(errors="replace"), "truncated": overflow.is_set(), "failure": failure}
