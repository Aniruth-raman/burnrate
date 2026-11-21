import logging
import os
import shutil
import sqlite3
import tempfile
import zipfile
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import pyzipper
from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from backend.models.database import DATA_DIR, UPLOADS_DIR

logger = logging.getLogger(__name__)

router = APIRouter(tags=["data"])

_REQUIRED_TABLES = {"settings", "cards", "statements", "transactions"}
_KEY_FILE = DATA_DIR / ".oauth_fernet.key"


def cleanup_tmp(path: str):
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def check_csv_safe(filepath: Path) -> bool:
    """Reject spreadsheet formulas without rejecting ordinary negative amounts."""
    try:
        import csv

        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for row in csv.reader(f):
                for cell in row:
                    value = cell.strip()
                    if value.startswith(("=", "@")):
                        return False
                    if value.startswith(("+", "-")):
                        try:
                            Decimal(value.replace(",", "").replace(" ", ""))
                        except InvalidOperation:
                            return False
        return True
    except Exception:
        return False


def _snapshot_db(dest: Path) -> None:
    """Copy the live database consistently, including data still in the WAL."""
    src = sqlite3.connect(DATA_DIR / "tuesday.db")
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _db_has_required_tables(path: Path) -> bool:
    try:
        con = sqlite3.connect(path)
        try:
            tables = {
                row[0]
                for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            con.close()
    except sqlite3.DatabaseError:
        return False
    return _REQUIRED_TABLES.issubset(tables)


def _rewrite_statement_paths(db_path: Path) -> None:
    """Point rows at this machine's uploads folder after a restore."""
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT id, file_path FROM statements WHERE file_path IS NOT NULL"
        ).fetchall()
        for statement_id, old_path in rows:
            candidate = UPLOADS_DIR / Path(old_path).name
            if candidate.is_file():
                con.execute(
                    "UPDATE statements SET file_path = ? WHERE id = ?",
                    (str(candidate), statement_id),
                )
        con.commit()
    finally:
        con.close()


def _clear_encrypted_secrets(db_path: Path) -> None:
    """Saved PDF passwords cannot be decrypted without the backup's key file."""
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "UPDATE statements SET encrypted_password = NULL "
            "WHERE encrypted_password IS NOT NULL"
        )
        con.commit()
    finally:
        con.close()


def _origin_is_local(request: Request) -> bool:
    origin = (request.headers.get("origin") or request.headers.get("referer") or "").strip()
    if not origin:
        # Non-browser clients do not send Origin; cross-site browser POSTs do.
        return True
    host = urlparse(origin).hostname
    return host in {"localhost", "127.0.0.1", "::1", "tauri.localhost"}


@router.post("/data/export")
def export_data(background_tasks: BackgroundTasks, password: Optional[str] = Form(None)):
    """Export the SQLite database and uploads as a ZIP, optionally AES encrypted."""
    tmp_dir = tempfile.mkdtemp()
    background_tasks.add_task(cleanup_tmp, tmp_dir)
    zip_path = Path(tmp_dir) / "burnrate_backup.zip"

    if password:
        zf = pyzipper.AESZipFile(
            zip_path, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES
        )
        zf.setpassword(password.encode("utf-8"))
    else:
        zf = zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED)

    with zf:
        if (DATA_DIR / "tuesday.db").exists():
            snapshot = Path(tmp_dir) / "tuesday.db"
            _snapshot_db(snapshot)
            zf.write(snapshot, "tuesday.db")

        # The key is only safe to include when the archive itself is encrypted.
        if password and _KEY_FILE.exists():
            zf.write(_KEY_FILE, _KEY_FILE.name)

        if UPLOADS_DIR.exists():
            for root, _, files in os.walk(UPLOADS_DIR):
                for name in files:
                    file_path = Path(root) / name
                    arcname = Path("statements") / file_path.relative_to(UPLOADS_DIR)
                    zf.write(file_path, str(arcname))

    return FileResponse(
        path=zip_path,
        filename="burnrate_backup.zip",
        media_type="application/zip",
    )


@router.post("/data/import")
def import_data(
    request: Request,
    file: UploadFile = File(...),
    password: Optional[str] = Form(None),
):
    """Replace the local database and uploads with a Burnrate backup."""
    if not _origin_is_local(request):
        raise HTTPException(status_code=403, detail="Cross-Site Request Forgery attempt detected")
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Must be a .zip file")

    tmp_dir = tempfile.mkdtemp()
    watcher_was_running = False
    old_key: Optional[bytes] = None
    try:
        from backend.routers.settings import get_watcher_observer, set_watcher_observer
        from backend.services.folder_watcher import start_watcher, stop_watcher

        observer = get_watcher_observer()
        if observer:
            stop_watcher(observer)
            set_watcher_observer(None)
            watcher_was_running = True

        max_extract_size = 1024 * 1024 * 1024  # 1 GB
        extracted_size = 0

        def copy_with_limit(src, dst, initial_chunk: bytes = b"") -> None:
            nonlocal extracted_size
            chunk = initial_chunk
            while True:
                if chunk:
                    extracted_size += len(chunk)
                    if extracted_size > max_extract_size:
                        raise HTTPException(
                            status_code=400,
                            detail="Zip bomb detected: extraction size exceeds 1GB limit",
                        )
                    dst.write(chunk)
                chunk = src.read(64 * 1024)
                if not chunk:
                    break

        upload_path = Path(tmp_dir) / "uploaded.zip"
        with open(upload_path, "wb") as out:
            shutil.copyfileobj(file.file, out)

        extract_dir = Path(tmp_dir) / "extracted"
        extract_dir.mkdir()
        extracted_key: Optional[Path] = None

        with pyzipper.AESZipFile(upload_path, "r") as zf:
            if password:
                zf.setpassword(password.encode("utf-8"))
            names = zf.namelist()
            if "tuesday.db" not in names and "burnrate.db" not in names:
                raise HTTPException(status_code=400, detail="Missing database (tuesday.db) in backup")

            for name in names:
                if name.endswith("/"):
                    continue
                if ".." in name or name.startswith("/"):
                    raise HTTPException(status_code=400, detail=f"Invalid path in zip: {name}")

                if name in ("tuesday.db", "burnrate.db"):
                    target = extract_dir / "tuesday.db"
                    with zf.open(name) as src, open(target, "wb") as dst:
                        header = src.read(16)
                        if header != b"SQLite format 3\x00":
                            raise HTTPException(status_code=400, detail="Invalid database file format")
                        copy_with_limit(src, dst, header)

                elif name == _KEY_FILE.name:
                    target = extract_dir / _KEY_FILE.name
                    with zf.open(name) as src, open(target, "wb") as dst:
                        copy_with_limit(src, dst)
                    extracted_key = target

                elif name.startswith("statements/"):
                    ext = os.path.splitext(name)[1].lower()
                    if ext not in {".pdf", ".csv"}:
                        continue
                    target = extract_dir / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(name) as src, open(target, "wb") as dst:
                        if ext == ".pdf":
                            header = src.read(5)
                            if not header.startswith(b"%PDF-"):
                                continue
                            copy_with_limit(src, dst, header)
                        else:
                            copy_with_limit(src, dst)
                    if ext == ".csv" and not check_csv_safe(target):
                        raise HTTPException(
                            status_code=400,
                            detail="Malicious CSV content detected (CSV Injection)",
                        )

        new_db = extract_dir / "tuesday.db"
        if not new_db.exists() or not _db_has_required_tables(new_db):
            raise HTTPException(
                status_code=400,
                detail="Valid Burnrate database not found in archive",
            )

        # Save the current state for rollback.
        backup_dir = DATA_DIR.parent / "data.backup"
        if backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
        backup_dir.mkdir(parents=True)
        if (DATA_DIR / "tuesday.db").exists():
            _snapshot_db(backup_dir / "tuesday.db")
        if UPLOADS_DIR.exists():
            shutil.copytree(UPLOADS_DIR, backup_dir / "uploads")
        if _KEY_FILE.exists():
            old_key = _KEY_FILE.read_bytes()

        from backend.models.database import engine, init_db

        try:
            engine.dispose()

            db_file = DATA_DIR / "tuesday.db"
            for suffix in ("", "-wal", "-shm"):
                stale = Path(str(db_file) + suffix)
                if stale.exists():
                    stale.unlink()

            shutil.copy(new_db, db_file)

            extracted_uploads = extract_dir / "statements"
            if extracted_uploads.exists():
                if UPLOADS_DIR.exists():
                    shutil.rmtree(UPLOADS_DIR, ignore_errors=True)
                shutil.copytree(extracted_uploads, UPLOADS_DIR)

            if extracted_key is not None:
                shutil.copy(extracted_key, _KEY_FILE)
            else:
                _clear_encrypted_secrets(db_file)

            _rewrite_statement_paths(db_file)
            init_db()

            from backend.models.database import SessionLocal
            from backend.models.models import Settings

            session = SessionLocal()
            try:
                settings_row = session.query(Settings).first()
                if settings_row and settings_row.watch_folder:
                    if not os.path.exists(settings_row.watch_folder):
                        settings_row.watch_folder = None
                        session.commit()
            finally:
                session.close()

            if settings_row and settings_row.watch_folder:
                new_observer = start_watcher(
                    settings_row.watch_folder, db_session_factory=SessionLocal
                )
                if new_observer:
                    set_watcher_observer(new_observer)
            watcher_was_running = False

        except Exception as exc:
            logger.exception("Import failed during replacement, rolling back")
            engine.dispose()
            db_file = DATA_DIR / "tuesday.db"
            for suffix in ("", "-wal", "-shm"):
                stale = Path(str(db_file) + suffix)
                if stale.exists():
                    stale.unlink()
            if (backup_dir / "tuesday.db").exists():
                shutil.copy(backup_dir / "tuesday.db", db_file)
            if UPLOADS_DIR.exists():
                shutil.rmtree(UPLOADS_DIR, ignore_errors=True)
            if (backup_dir / "uploads").exists():
                shutil.copytree(backup_dir / "uploads", UPLOADS_DIR)
            if old_key is not None:
                _KEY_FILE.write_bytes(old_key)
            init_db()
            raise HTTPException(
                status_code=500,
                detail="Import failed. Database rolled back to previous state.",
            ) from exc

        return {"status": "success", "message": "Data imported successfully"}
    except RuntimeError as exc:
        if "password" in str(exc).lower():
            raise HTTPException(
                status_code=400,
                detail="Invalid or missing password for encrypted backup",
            ) from exc
        raise
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail="Invalid zip file") from exc
    finally:
        cleanup_tmp(tmp_dir)
        if watcher_was_running:
            from backend.routers.settings import set_watcher_observer
            from backend.models.database import SessionLocal
            from backend.models.models import Settings
            from backend.services.folder_watcher import start_watcher

            session = SessionLocal()
            try:
                settings_row = session.query(Settings).first()
                folder = settings_row.watch_folder if settings_row else None
            finally:
                session.close()
            if folder:
                observer = start_watcher(folder, db_session_factory=SessionLocal)
                if observer:
                    set_watcher_observer(observer)
