"""HTTP API and static web UI.

Stateless on purpose: the browser holds the working state and sends what each
step needs, so nothing is stored on disk and the server can restart any time.
Endpoints are plain `def` functions, so FastAPI runs model calls in a thread
pool and several drafts can be written in parallel.
"""
from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, audit, config, kb, pipeline
from .export import build_bundle, slugify
from .llm import LLMError
from .schemas import (DOC_TYPES, CheckRequest, CheckResponse, Draft, ExportRequest, ExtractRequest,
                      ExtractResponse, GenerateRequest)

app = FastAPI(title="Proofmark", version=__version__,
              description="Turn a feature spec into grounded customer-support documentation.")
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


@app.exception_handler(LLMError)
def _llm_error(_: Request, exc: LLMError) -> JSONResponse:
    return JSONResponse(status_code=502, content={"error": str(exc)})


@app.exception_handler(config.ConfigError)
def _config_error(_: Request, exc: config.ConfigError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.exception_handler(pipeline.UserInputError)
def _input_error(_: Request, exc: pipeline.UserInputError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(config.STATIC_DIR / "index.html")


def _sample_dirs() -> list[Path]:
    if not config.SAMPLES_DIR.is_dir():
        return []
    return sorted(p for p in config.SAMPLES_DIR.iterdir() if (p / "sample.json").is_file())


@app.get("/api/config")
def get_config() -> dict:
    samples = []
    for folder in _sample_dirs():
        meta = json.loads((folder / "sample.json").read_text(encoding="utf-8"))
        samples.append({"id": folder.name, "title": meta.get("title", folder.name),
                        "description": meta.get("description", "")})
    return {
        **config.public_config(),
        "version": __version__,
        "doc_types": DOC_TYPES,
        "samples": samples,
        "kb_articles": len(kb.load_articles(config.KB_DIR)),
    }


def _data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


@app.get("/api/samples/{sample_id}")
def get_sample(sample_id: str) -> dict:
    folder = config.SAMPLES_DIR / sample_id
    if sample_id not in {p.name for p in _sample_dirs()}:
        raise HTTPException(status_code=404, detail="Unknown sample.")
    meta = json.loads((folder / "sample.json").read_text(encoding="utf-8"))
    files = meta.get("files", {})

    def read(key: str) -> str:
        name = files.get(key)
        return (folder / name).read_text(encoding="utf-8") if name else ""

    return {
        "id": sample_id,
        "title": meta.get("title", sample_id),
        "settings": meta.get("settings", {}),
        "sources": {
            "spec": read("spec"),
            "previous_spec": read("previous_spec"),
            "code_diff": read("code_diff"),
            "screenshots": [_data_url(folder / name) for name in files.get("screenshots", [])],
        },
    }


@app.post("/api/extract", response_model=ExtractResponse)
def extract(req: ExtractRequest) -> ExtractResponse:
    return pipeline.extract(req.sources, req.settings, req.llm)


@app.post("/api/generate", response_model=Draft)
def generate(req: GenerateRequest) -> Draft:
    return pipeline.generate(req)


@app.post("/api/check", response_model=CheckResponse)
def check(req: CheckRequest) -> CheckResponse:
    return pipeline.recheck(req.doc_type, req.markdown, req.facts, req.sources, req.settings)


@app.post("/api/export")
def export(req: ExportRequest) -> Response:
    """The zip handed to publishing. Every export is recorded in the audit trail."""
    if not req.drafts:
        raise HTTPException(status_code=400, detail="Draft at least one document before exporting.")
    record = audit.build_record(req.facts, req.drafts, req.settings, req.sources, req.extract_meta)
    bundle = build_bundle(req.facts, req.drafts, req.settings, record=record, sources=req.sources)
    summary = record["summary"]
    headers = {
        "Content-Disposition": f'attachment; filename="{slugify(req.facts.feature_name)}-docs.zip"',
        "X-Proofmark-Audit-Id": record["id"],
        "X-Proofmark-Approved": f"{summary['approved']}/{summary['documents']}",
    }
    if config.AUDIT_ENABLED:
        audit.AuditLog(config.AUDIT_DIR).append(record, bundle)
        headers["X-Proofmark-Audit-Log"] = "saved"
    return Response(content=bundle, media_type="application/zip", headers=headers)
