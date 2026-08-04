"""Loopback wrapper around the official SkinTokens TokenRig pipeline."""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.request import urlopen

try:
    from .runtime import (
        InferRequestError,
        build_output_path,
        health_payload,
        parse_infer_request,
    )
except ImportError:  # direct execution by the remote adapter release
    from runtime import (  # type: ignore[no-redef]
        InferRequestError,
        build_output_path,
        health_payload,
        parse_infer_request,
    )


class TokenRigService:
    """Load the official model once and serialize GPU rigging requests."""

    def __init__(self, source_root: Path, weights_root: Path, blender_bin: str) -> None:
        self._lock = Lock()
        self._source_root = source_root
        self._weights_root = weights_root
        self._bpy = subprocess.Popen(
            [blender_bin, "--background", "--python", str(source_root / "bpy_server.py")],
            cwd=source_root,
            env={
                **os.environ,
                "PYTHONPATH": f"{source_root}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        atexit.register(self.close)
        self._wait_for_bpy()
        sys.path.insert(0, str(source_root))
        import torch  # type: ignore[import-not-found]
        from demo import load_model, run_rig  # type: ignore[import-not-found]

        self._torch = torch
        self._run_rig = run_rig
        checkpoint = weights_root / "experiments/articulation_xl_quantization_256_token_4/grpo_1400.ckpt"
        load_model(str(checkpoint), None)
        self._ready = True

    def _wait_for_bpy(self) -> None:
        import time

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self._bpy.poll() is not None:
                raise RuntimeError("official Blender server exited during startup")
            try:
                with urlopen("http://127.0.0.1:59876/ping", timeout=1) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(0.5)
        raise RuntimeError("official Blender server did not become ready")

    def generate(self, input_path: Path, output_path: Path, *, use_transfer: bool, use_postprocess: bool) -> None:
        with self._lock, self._torch.inference_mode():
            self._run_rig(
                [input_path],
                5,
                0.95,
                1.0,
                2.0,
                10,
                False,
                use_transfer,
                use_postprocess,
                [output_path],
                str(self._weights_root / "experiments/articulation_xl_quantization_256_token_4/grpo_1400.ckpt"),
                None,
            )

    def close(self) -> None:
        if self._bpy.poll() is None:
            self._bpy.terminate()
            try:
                self._bpy.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._bpy.kill()


class Handler(BaseHTTPRequestHandler):
    service: TokenRigService | None = None
    input_root = Path(os.environ.get("TOKENRIG_INPUT_ROOT", "/srv/models/inputs/tokenrig-single"))
    output_root = Path(os.environ.get("TOKENRIG_OUTPUT_ROOT", "/srv/models/outputs/tokenrig-single"))

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._json(HTTPStatus.OK, health_payload(ready=self.service is not None))
            return
        if self.path == "/v1/models":
            self._json(HTTPStatus.OK, {"object": "list", "data": [{"id": "tokenrig"}]})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/v1/rig":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length))
            request = parse_infer_request(payload, input_root=self.input_root)
            output_path = build_output_path(self.output_root, request.output_path)
            if not request.input_path.is_file():
                raise InferRequestError("input_path does not exist")
            if self.service is None:
                raise RuntimeError("model is still loading")
            self.service.generate(
                request.input_path,
                output_path,
                use_transfer=request.use_transfer,
                use_postprocess=request.use_postprocess,
            )
            self._json(HTTPStatus.OK, {"model": "tokenrig", "output_path": str(output_path)})
        except InferRequestError as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except (OSError, RuntimeError, TypeError, ValueError) as error:  # pragma: no cover - live Spark gate
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})

    def log_message(self, format: str, *args: object) -> None:
        print(format % args, file=sys.stderr, flush=True)


def main() -> None:
    source_root = Path(os.environ["TOKENRIG_SOURCE_ROOT"])
    weights_root = Path(os.environ["TOKENRIG_WEIGHTS_ROOT"])
    blender_bin = os.environ.get("TOKENRIG_BLENDER_BIN", "blender")
    port = int(os.environ.get("TOKENRIG_PORT", "9107"))
    Handler.service = TokenRigService(source_root, weights_root, blender_bin)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
