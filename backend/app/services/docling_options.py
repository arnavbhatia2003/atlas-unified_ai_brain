"""Validated parser settings; no credentials, environment file or model loading."""
import os
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DoclingOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)
    threads: int = Field(default=2, ge=1, le=32)
    ocr_batch_size: int = Field(default=1, ge=1, le=8)
    layout_batch_size: int = Field(default=1, ge=1, le=8)
    table_batch_size: int = Field(default=1, ge=1, le=8)
    queue_max_size: int = Field(default=2, ge=1, le=16)
    table_mode: Literal["fast", "accurate"] = "fast"
    ocr_mode: Literal["auto", "full"] = "auto"
    document_timeout_seconds: int = Field(default=180, ge=30, le=900)
    export_page_images: bool = False
    page_image_scale: float = Field(default=2.0, ge=1, le=3)


def docling_threads(value: int | str | None = None) -> int:
    available = os.process_cpu_count() or 1
    try:
        threads = int(os.getenv("DOCLING_NUM_THREADS", "2") if value is None else value)
    except (TypeError, ValueError):
        raise ValueError("Inference threads must be a whole number") from None
    if not 1 <= threads <= available:
        raise ValueError(f"Inference threads must be between 1 and {available} available CPUs")
    return threads


def load_options(path: Path) -> DoclingOptions:
    if path.stat().st_size > 16384:
        raise ValueError("Parser configuration exceeds 16 KiB")
    def unique(pairs):
        result = {}
        for key,value in pairs:
            if key in result:
                raise ValueError("Duplicate parser configuration key")
            result[key] = value
        return result
    return DoclingOptions.model_validate(json.loads(path.read_text(encoding="utf-8"),object_pairs_hook=unique))


def configured_options() -> DoclingOptions:
    raw = os.getenv("ATLAS_DOCLING_OPTIONS")
    options = DoclingOptions.model_validate_json(raw) if raw else DoclingOptions()
    threads = docling_threads(os.getenv("DOCLING_NUM_THREADS", str(options.threads)))
    return DoclingOptions.model_validate({**options.model_dump(), "threads": threads})
