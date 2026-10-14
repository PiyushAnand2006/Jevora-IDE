"""Attachments (Files / Photos in the attach menu): upload, extract text, serve, delete.

Stored under ``<workspace>/.jevora/uploads``. Text/code is decoded, PDFs are text-extracted,
images are kept for vision-capable models. Size and type limits apply; executables are refused.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select

from backend.api.deps import Ctx, CurrentUser
from backend.app_context import AppContext
from backend.core.errors import AppError, NotFound
from backend.core.ids import iso, new_id
from backend.core.paths import is_binary
from backend.db.models import Attachment
from backend.vault.templates import safe_filename

router = APIRouter(prefix="/api/attachments", tags=["attachments"])

IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
BLOCKED_SUFFIXES = {".exe", ".dll", ".msi", ".com", ".scr", ".so", ".dylib", ".app", ".apk", ".jar", ".sys"}
_MAGIC: list[tuple[bytes, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"), (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"), (b"%PDF-", "application/pdf"), (b"PK\x03\x04", "application/zip"),
    (b"MZ", "application/x-msdownload"), (b"\x7fELF", "application/x-elf"),
]


def sniff_mime(head: bytes, filename: str, declared: str | None) -> str:
    for magic, mime in _MAGIC:
        if head.startswith(magic):
            return mime
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    guessed = mimetypes.guess_type(filename)[0]
    return guessed or declared or "application/octet-stream"


def extract_pdf_text(path: Path, limit: int) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    parts: list[str] = []
    total = 0
    for number, page in enumerate(reader.pages, 1):
        text = page.extract_text() or ""
        parts.append(f"--- page {number} ---\n{text}")
        total += len(text)
        if total > limit:
            break
    return "\n".join(parts)[:limit]


def public(att: Attachment) -> dict[str, Any]:
    return {"id": att.id, "filename": att.filename, "mime": att.mime, "size": att.size, "kind": att.kind,
            "sha256": att.sha256, "has_text": bool(att.text_content),
            "text_preview": (att.text_content or "")[:500], "created_at": iso(att.created_at)}


async def _store(ctx: AppContext, user_id: str, upload: UploadFile) -> Attachment:
    filename = safe_filename(Path(upload.filename or "upload").name, 120)
    if Path(filename).suffix.lower() in BLOCKED_SUFFIXES:
        raise AppError(f"{filename}: executable files are not accepted", code="unsupported_type", status_code=415)
    limit = ctx.settings.max_upload_mb * 1024 * 1024
    att_id = new_id("att_")
    folder = ctx.settings.workspace_root / ".jevora" / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{att_id}_{filename}"
    digest = hashlib.sha256()
    size = 0
    head = b""
    try:
        with target.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise AppError(f"{filename} is larger than {ctx.settings.max_upload_mb} MB",
                                   code="file_too_large", status_code=413)
                if len(head) < 8192:
                    head += chunk[: 8192 - len(head)]
                digest.update(chunk)
                handle.write(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    mime = sniff_mime(head, filename, upload.content_type)
    if mime in ("application/x-msdownload", "application/x-elf"):
        target.unlink(missing_ok=True)
        raise AppError(f"{filename}: executable files are not accepted", code="unsupported_type", status_code=415)
    text: str | None = None
    max_chars = ctx.settings.max_attachment_text_chars
    if mime in IMAGE_MIMES:
        kind = "image"
    elif mime == "application/pdf":
        kind = "pdf"
        try:
            text = await asyncio.to_thread(extract_pdf_text, target, max_chars)
        except Exception as exc:
            text = f"[could not extract PDF text: {type(exc).__name__}]"
    elif size == 0 or not is_binary(head):
        kind = "text"
        raw = await asyncio.to_thread(target.read_bytes)
        text = raw.decode("utf-8-sig", "replace")[:max_chars] if not raw.startswith((b"\xff\xfe", b"\xfe\xff")) \
            else raw.decode("utf-16", "replace")[:max_chars]
        if mime == "application/octet-stream":
            mime = "text/plain"
    else:
        kind = "binary"
    attachment = Attachment(id=att_id, user_id=user_id, filename=filename, mime=mime, size=size, kind=kind,
                            storage_path=str(target), text_content=text, sha256=digest.hexdigest())
    async with ctx.sessions() as session:
        session.add(attachment)
        await session.commit()
        await session.refresh(attachment)
    return attachment


@router.post("", status_code=201)
async def upload(user: CurrentUser, ctx: Ctx, files: list[UploadFile] = File(...)) -> dict[str, Any]:
    if not files:
        raise AppError("no files uploaded", code="no_files", status_code=422)
    if len(files) > 20:
        raise AppError("at most 20 files per upload", code="too_many_files", status_code=422)
    stored = [await _store(ctx, user.id, f) for f in files]
    return {"attachments": [public(a) for a in stored]}


@router.get("")
async def list_attachments(user: CurrentUser, ctx: Ctx) -> list[dict[str, Any]]:
    async with ctx.sessions() as session:
        rows = list(await session.scalars(select(Attachment).where(Attachment.user_id == user.id)
                                          .order_by(Attachment.created_at.desc()).limit(200)))
    return [public(a) for a in rows]


async def _owned(ctx: AppContext, user_id: str, attachment_id: str) -> Attachment:
    async with ctx.sessions() as session:
        att = await session.get(Attachment, attachment_id)
    if att is None or att.user_id != user_id:
        raise NotFound("attachment not found")
    return att


@router.get("/{attachment_id}")
async def get_attachment(attachment_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    att = await _owned(ctx, user.id, attachment_id)
    data = public(att)
    data["text"] = att.text_content
    return data


@router.get("/{attachment_id}/content", response_model=None)
async def attachment_content(attachment_id: str, user: CurrentUser, ctx: Ctx) -> FileResponse:
    att = await _owned(ctx, user.id, attachment_id)
    if not await asyncio.to_thread(Path(att.storage_path).exists):
        raise NotFound("attachment file is missing")
    return FileResponse(att.storage_path, media_type=att.mime, filename=att.filename)


@router.delete("/{attachment_id}")
async def delete_attachment(attachment_id: str, user: CurrentUser, ctx: Ctx) -> dict[str, Any]:
    att = await _owned(ctx, user.id, attachment_id)
    await asyncio.to_thread(Path(att.storage_path).unlink, True)
    async with ctx.sessions() as session:
        row = await session.get(Attachment, attachment_id)
        if row is not None:
            await session.delete(row)
            await session.commit()
    return {"ok": True, "id": attachment_id}
