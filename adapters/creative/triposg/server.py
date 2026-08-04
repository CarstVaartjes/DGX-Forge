"""Loopback HTTP wrapper around the official TripoSG inference pipeline."""

from __future__ import annotations

import json
import os
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from typing import Any

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


class TripoService:
    """Load the official pipeline once and serialize GPU inference requests."""

    def __init__(self, source_root: Path, weights_root: Path) -> None:
        self._lock = Lock()
        sys.path.insert(0, str(source_root / "scripts"))
        sys.path.insert(0, str(source_root))
        import torch  # type: ignore[import-not-found]
        from briarmbg import BriaRMBG  # type: ignore[import-not-found]
        from inference_triposg import run_triposg  # type: ignore[import-not-found]
        from triposg.pipelines.pipeline_triposg import (
            TripoSGPipeline,  # type: ignore[import-not-found]
        )

        self._torch = torch
        self._run_triposg = run_triposg
        self._rmbg = BriaRMBG.from_pretrained(str(weights_root / "RMBG-1.4")).to("cuda")
        self._rmbg.eval()
        self._pipe = TripoSGPipeline.from_pretrained(str(weights_root / "TripoSG")).to(
            "cuda", torch.float16
        )

    def generate(self, image_path: Path, output_path: Path, *, faces: int | None) -> None:
        with self._lock, self._torch.inference_mode():
            mesh = self._run_triposg(
                self._pipe,
                image_input=str(image_path),
                rmbg_net=self._rmbg,
                seed=42,
                faces=faces or -1,
            )
            mesh.export(str(output_path))


class Handler(BaseHTTPRequestHandler):
    service: TripoService | None = None
    input_root = Path(os.environ.get("TRIPOSG_INPUT_ROOT", "/srv/models/inputs/triposg"))
    output_root = Path(os.environ.get("TRIPOSG_OUTPUT_ROOT", "/srv/models/outputs/triposg"))

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
            self._json(HTTPStatus.OK, {"object": "list", "data": [{"id": "triposg"}]})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/v1/generate":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("content-length", "0"))
            payload = json.loads(self.rfile.read(length))
            request = parse_infer_request(payload, input_root=self.input_root)
            output_path = build_output_path(self.output_root, request.output_path)
            if not request.image_path.is_file():
                raise InferRequestError("image_path does not exist")
            if self.service is None:
                raise RuntimeError("model is still loading")
            self.service.generate(request.image_path, output_path, faces=request.faces)
            self._json(HTTPStatus.OK, {"model": "triposg", "output_path": str(output_path)})
        except InferRequestError as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except (OSError, RuntimeError, TypeError, ValueError) as error:  # pragma: no cover - live Spark gate
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})

    def log_message(self, format: str, *args: object) -> None:
        print(format % args, file=sys.stderr, flush=True)


def main() -> None:
    source_root = Path(os.environ["TRIPOSG_SOURCE_ROOT"])
    weights_root = Path(os.environ["TRIPOSG_WEIGHTS_ROOT"])
    port = int(os.environ.get("TRIPOSG_PORT", "9109"))
    Handler.service = TripoService(source_root, weights_root)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
