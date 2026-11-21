"""Settings API endpoints."""

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.models.database import SessionLocal, get_db
from backend.models.models import Card, Settings
from backend.services.card_delete import delete_card_cascade


def _validate_watch_folder(folder: Optional[str]) -> Optional[str]:
    """Validate a user-provided watch folder path.

    Raises HTTPException if the path is invalid or suspicious.
    Returns the resolved path string, or None if folder is empty.
    """
    if not folder or not folder.strip():
        return None

    expanded = Path(folder).expanduser()
    if not expanded.is_absolute():
        raise HTTPException(status_code=400, detail="Watch folder must be an absolute path.")

    resolved = expanded.resolve(strict=False)
    home = Path.home().resolve()
    if not resolved.is_relative_to(home) and not resolved.is_relative_to(Path("/Volumes")) and not str(resolved).startswith(("/data", "/mnt", "/media")):
        raise HTTPException(status_code=400, detail="Watch folder must be within your home directory or an allowed Docker mount (/data, /mnt, /media).")

    if not resolved.exists():
        raise HTTPException(status_code=400, detail="Watch folder does not exist.")
    if not resolved.is_dir():
        raise HTTPException(status_code=400, detail="Watch folder path is not a directory.")

    return str(resolved)


def _normalize_display_currency(value: Optional[str]) -> Optional[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    code = str(value).strip().upper()[:3]
    import re
    if not re.match(r"^[A-Z]{3}$", code):
        raise HTTPException(
            status_code=400,
            detail="display_currency must be a valid 3-letter ISO 4217 code",
        )
    return code


router = APIRouter(prefix="/settings", tags=["settings"])

# Global observer for folder watcher - set by main.py
_watcher_observer = None


def set_watcher_observer(observer):
    """Set the global watcher observer for cleanup."""
    global _watcher_observer
    _watcher_observer = observer


def get_watcher_observer():
    """Get the global watcher observer."""
    return _watcher_observer


class CardInput(BaseModel):
    bank: str
    last4: str
    name: Optional[str] = None


class SetupInput(BaseModel):
    name: str
    dob_day: Optional[str] = None
    dob_month: Optional[str] = None
    dob_year: Optional[str] = None
    watch_folder: Optional[str] = None
    display_currency: Optional[str] = None
    cards: List[CardInput] = []


class SettingsUpdateInput(BaseModel):
    name: Optional[str] = None
    dob_day: Optional[str] = None
    dob_month: Optional[str] = None
    dob_year: Optional[str] = None
    watch_folder: Optional[str] = None
    display_currency: Optional[str] = None
    cards: Optional[List[CardInput]] = None


def _validated_card_rows(cards: List[CardInput]) -> List[tuple[str, str, Optional[str]]]:
    """Reject blank/invalid card rows before they reach the database."""
    rows = []
    for index, card in enumerate(cards, start=1):
        bank = (card.bank or "").strip().lower()
        last4 = "".join(ch for ch in (card.last4 or "") if ch.isdigit())
        if not bank or not re.match(r"^[a-z0-9_]+$", bank):
            raise HTTPException(
                status_code=400,
                detail=f"Card {index}: choose an issuing bank.",
            )
        if len(last4) < 4:
            raise HTTPException(
                status_code=400,
                detail=f"Card {index}: card number must end with 4 digits.",
            )
        rows.append((bank, last4[-4:], card.name))
    return rows


@router.get("")
def get_settings(db: Session = Depends(get_db)) -> Dict[str, Any]:
    """Return settings + cards, or {setup_complete: false}."""
    settings = db.query(Settings).first()
    if not settings:
        return {"setup_complete": False}

    cards = db.query(Card).all()
    return {
        "setup_complete": True,
        "settings": {
            "id": settings.id,
            "name": settings.name,
            "dob_day": settings.dob_day,
            "dob_month": settings.dob_month,
            "dob_year": settings.dob_year,
            "watch_folder": settings.watch_folder,
            "display_currency": getattr(settings, "display_currency", None),
            "created_at": settings.created_at.isoformat() if settings.created_at else None,
            "updated_at": settings.updated_at.isoformat() if settings.updated_at else None,
        },
        "cards": [
            {
                "id": c.id,
                "bank": c.bank,
                "last4": c.last4,
                "name": c.name,
            }
            for c in cards
        ],
    }


@router.post("/setup")
def setup(
    body: SetupInput,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Create settings + cards, start folder watcher."""
    existing = db.query(Settings).first()
    if existing:
        raise HTTPException(status_code=400, detail="Setup already completed. Use PUT /api/settings to update.")

    validated_folder = _validate_watch_folder(body.watch_folder)
    disp_cur = _normalize_display_currency(body.display_currency)
    card_rows = _validated_card_rows(body.cards)

    settings = Settings(
        name=body.name,
        dob_day=body.dob_day,
        dob_month=body.dob_month,
        dob_year=body.dob_year,
        watch_folder=validated_folder,
        display_currency=disp_cur,
    )
    db.add(settings)
    db.flush()

    for bank, last4, name in card_rows:
        card = Card(bank=bank, last4=last4, name=name)
        db.add(card)

    db.commit()

    from backend.services.folder_watcher import start_watcher

    observer = None
    if body.watch_folder:
        observer = start_watcher(body.watch_folder, db_session_factory=SessionLocal)
        if observer:
            set_watcher_observer(observer)

    return {
        "status": "success",
        "message": "Setup complete",
        "watcher_started": observer is not None,
    }


@router.put("")
def update_settings(
    body: SettingsUpdateInput,
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    """Update settings and optionally sync cards."""
    settings = db.query(Settings).first()
    if not settings:
        raise HTTPException(status_code=404, detail="Setup not completed. Use POST /api/settings/setup first.")

    # Validate everything before touching rows so a bad payload changes nothing.
    card_rows = _validated_card_rows(body.cards) if body.cards is not None else None
    new_watch_folder = (
        _validate_watch_folder(body.watch_folder) if body.watch_folder is not None else None
    )

    if body.name is not None:
        settings.name = body.name
    if body.dob_day is not None:
        settings.dob_day = body.dob_day
    if body.dob_month is not None:
        settings.dob_month = body.dob_month
    if body.dob_year is not None:
        settings.dob_year = body.dob_year
    if body.watch_folder is not None:
        settings.watch_folder = new_watch_folder
    update_data = body.model_dump(exclude_unset=True)
    if "display_currency" in update_data:
        settings.display_currency = _normalize_display_currency(body.display_currency)

    cards_added = 0
    if card_rows is not None:
        wanted = {(bank, last4) for bank, last4, _ in card_rows}
        for card_row in list(db.query(Card).all()):
            if (card_row.bank.lower(), card_row.last4) not in wanted:
                delete_card_cascade(db, card_row.id)

        existing = {(c.bank.lower(), c.last4) for c in db.query(Card).all()}
        for bank, last4, name in card_rows:
            if (bank, last4) not in existing:
                db.add(Card(bank=bank, last4=last4, name=name))
                cards_added += 1

    db.commit()

    from backend.services.folder_watcher import start_watcher, stop_watcher

    old_observer = get_watcher_observer()
    if old_observer:
        stop_watcher(old_observer)
        set_watcher_observer(None)
    if settings.watch_folder:
        observer = start_watcher(settings.watch_folder, db_session_factory=SessionLocal)
        if observer:
            set_watcher_observer(observer)

    return {"status": "success", "message": "Settings updated", "cards_added": cards_added}


@router.post("/browse-folder")
def browse_folder() -> Dict[str, str]:
    """Open a native folder picker dialog and return the selected path."""
    import platform
    import subprocess

    path = ""
    system = platform.system()

    try:
        if system == "Darwin":
            result = subprocess.run(
                ["osascript", "-e", 'POSIX path of (choose folder with prompt "Select watch folder")'],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                path = result.stdout.strip().rstrip("/")
        elif system == "Linux":
            result = subprocess.run(
                ["zenity", "--file-selection", "--directory", "--title=Select watch folder"],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                path = result.stdout.strip()
        elif system == "Windows":
            script = "\n".join([
                "Add-Type -AssemblyName System.Windows.Forms;",
                'Add-Type @"',
                "using System;",
                "using System.Runtime.InteropServices;",
                "using System.Text;",
                "public class BurnrateFolderPicker {",
                "  public delegate bool EnumProc(IntPtr hWnd, IntPtr lParam);",
                '  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumProc cb, IntPtr lp);',
                '  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);',
                '  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);',
                '  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);',
                '  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);',
                "  public static IntPtr FindOwnDialog(uint pid) {",
                "    IntPtr found = IntPtr.Zero;",
                "    EnumWindows((h, l) => {",
                "      uint p; GetWindowThreadProcessId(h, out p);",
                "      if (p == pid) {",
                "        var sb = new StringBuilder(64); GetClassName(h, sb, 64);",
                '        if (sb.ToString() == "#32770") { found = h; return false; }',
                "      }",
                "      return true;",
                "    }, IntPtr.Zero);",
                "    return found;",
                "  }",
                "}",
                '"@;',
                "$d = New-Object System.Windows.Forms.FolderBrowserDialog;",
                "$d.Description = 'Select watch folder';",
                # A background process cannot take the foreground, so the dialog opens
                # behind the browser. Make our own dialog topmost once it exists.
                "$t = New-Object System.Windows.Forms.Timer;",
                "$t.Interval = 300;",
                "$t.add_Tick({",
                "  $h = [BurnrateFolderPicker]::FindOwnDialog([uint32]$PID);",
                "  if ($h -ne [IntPtr]::Zero) {",
                "    [void][BurnrateFolderPicker]::SetWindowPos($h, [IntPtr](-1), 0, 0, 0, 0, 0x0003);",
                "    [void][BurnrateFolderPicker]::SetForegroundWindow($h);",
                "    $t.Stop();",
                "  }",
                "});",
                "$t.Start();",
                "if ($d.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK)",
                "{ [Console]::Out.Write($d.SelectedPath) }",
            ])
            result = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", script],
                capture_output=True, text=True, timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode == 0:
                path = result.stdout.strip()
    except Exception:
        pass

    return {"path": path}
