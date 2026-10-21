#Makes BATS able to open-up and write in the notepad to pass information to the user.
"""
keyboard_writer — drop-in JARVIS plugin.

Lets the assistant use the keyboard: open a .txt in Notepad and type into it by
itself (code, long answers, drafts), type into whatever window is focused,
press single keys, fire hotkeys, save, and stop mid-typing.

Setup:  pip install pyautogui pyperclip
Safety: slam the mouse into any screen corner to abort (pyautogui fail-safe),
        or just say "stop writing".
"""

import os
import re
import time
import threading
import subprocess
import datetime
from pathlib import Path

PLUGIN = {
    "name": "keyboard_writer",
    "description": (
        "Controls the user's keyboard so the assistant can WRITE for them. "
        "Use when the user says 'start writing', 'write this in Notepad', 'open a text file and write', "
        "'type this', 'write the code for me', 'put the full answer in a text file', or 'stop writing'. "
        "Actions: 'write_notepad' (creates a .txt, opens it in Notepad and types the text there; "
        "best for code, long answers, drafts), 'type_here' (types into the window that is currently focused), "
        "'press_key' (one key, e.g. enter, tab, esc), 'hotkey' (combo like ctrl+s), 'save' (Ctrl+S), "
        "'stop' (abort a typing job in progress). "
        "Put the COMPLETE text to be written in the 'text' argument. "
        "Do NOT use this for opening apps (use the app opener) or for controlling Roblox Studio."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "One of: write_notepad, type_here, press_key, hotkey, save, stop. Default: write_notepad.",
            },
            "text": {
                "type": "STRING",
                "description": "The full text/code to type (for write_notepad and type_here). Keep line breaks and indentation.",
            },
            "filename": {
                "type": "STRING",
                "description": "Optional file name for write_notepad, e.g. 'meu_script.txt'. Extension defaults to .txt.",
            },
            "speed": {
                "type": "STRING",
                "description": "Typing speed: 'slow', 'normal' (default) or 'fast'.",
            },
            "keys": {
                "type": "STRING",
                "description": "For press_key: a key name (enter, tab, esc, backspace...). For hotkey: a combo with '+' (ctrl+a, ctrl+shift+s, alt+tab).",
            },
            "delay": {
                "type": "NUMBER",
                "description": "Seconds to wait before typing for type_here, so the user can click the target window. Default 3.",
            },
        },
        "required": [],
    },
}

# --------------------------------------------------------------------------- #
# Internals
# --------------------------------------------------------------------------- #

OUTPUT_DIR = Path.home() / "Documents" / "JARVIS_Writing"

_SPEEDS = {
    # (delay between characters, delay between lines)
    "slow": (0.05, 0.15),
    "normal": (0.02, 0.06),
    "fast": (0.004, 0.02),
}

_job_lock = threading.Lock()
_job_thread = None
_stop_event = threading.Event()


def _load_libs():
    """Import GUI libs lazily so a missing package gives a spoken error, not a crash at startup."""
    import pyautogui  # noqa: WPS433
    import pyperclip  # noqa: WPS433

    pyautogui.PAUSE = 0
    pyautogui.FAILSAFE = True
    return pyautogui, pyperclip


def _log(player, msg: str):
    if player:
        try:
            player.write_log(f"JARVIS: {msg}")
        except Exception:
            pass


def _safe_filename(name: str) -> str:
    name = (name or "").strip()
    if not name:
        name = "jarvis_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    if not os.path.splitext(name)[1]:
        name += ".txt"
    return name


def _active_title(pag) -> str:
    try:
        return pag.getActiveWindowTitle() or ""
    except Exception:
        return ""


def _wait_for_window(pag, stem: str, timeout: float = 6.0) -> bool:
    """Wait until the active window title contains the file's name (Notepad shows it)."""
    end = time.time() + timeout
    stem = stem.lower()
    while time.time() < end:
        if stem in _active_title(pag).lower():
            return True
        time.sleep(0.2)
    return False


def _type_text(pag, clip, text: str, speed: str, guard_stem: str = None) -> str:
    """
    Types text line by line. ASCII lines are typed key by key (looks like real typing);
    lines with accents/symbols (ç, ã, é, emojis...) are pasted via the clipboard, because
    pyautogui cannot type non-ASCII characters.
    Returns "" on success or an error/abort message.
    """
    char_iv, line_iv = _SPEEDS.get(speed, _SPEEDS["normal"])
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")

    try:
        original_clip = clip.paste()
    except Exception:
        original_clip = None

    try:
        for i, line in enumerate(lines):
            if _stop_event.is_set():
                return "stopped"

            # Safety: never type into the wrong window.
            if guard_stem and guard_stem.lower() not in _active_title(pag).lower():
                return "the writing window lost focus, so I stopped"

            if line:
                if line.isascii():
                    pag.write(line, interval=char_iv)
                else:
                    clip.copy(line)
                    pag.hotkey("ctrl", "v")
                    time.sleep(0.05)

            if i < len(lines) - 1:
                pag.press("enter")
            time.sleep(line_iv)
    finally:
        if original_clip is not None:
            try:
                clip.copy(original_clip)
            except Exception:
                pass
    return ""


def _job_write_notepad(player, path: Path, text: str, speed: str):
    try:
        pag, clip = _load_libs()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)  # Notepad opens an existing (empty) file directly

        subprocess.Popen(["notepad.exe", str(path)])

        if not _wait_for_window(pag, path.stem):
            _log(player, "Notepad window not detected, aborting.")
            return

        time.sleep(0.4)
        err = _type_text(pag, clip, text, speed, guard_stem=path.stem)
        if err:
            _log(player, f"Writing interrupted: {err}.")
            return

        pag.hotkey("ctrl", "s")
        _log(player, f"Finished writing: {path}")
    except Exception as e:
        _log(player, f"keyboard_writer error: {e}")
    finally:
        _stop_event.clear()


def _job_type_here(player, text: str, speed: str, delay: float):
    try:
        pag, clip = _load_libs()
        end = time.time() + max(0.0, delay)
        while time.time() < end:
            if _stop_event.is_set():
                return
            time.sleep(0.1)
        err = _type_text(pag, clip, text, speed)
        if err:
            _log(player, f"Writing interrupted: {err}.")
        else:
            _log(player, "Finished typing.")
    except Exception as e:
        _log(player, f"keyboard_writer error: {e}")
    finally:
        _stop_event.clear()


def _start_job(target, *args) -> bool:
    """Run a typing job in the background so the assistant can keep talking / be told to stop."""
    global _job_thread
    with _job_lock:
        if _job_thread is not None and _job_thread.is_alive():
            return False
        _stop_event.clear()
        _job_thread = threading.Thread(target=target, args=args, daemon=True)
        _job_thread.start()
        return True


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def run(parameters: dict, player=None, session_memory=None) -> str:
    """
    parameters: args extracted by Gemini (see PLUGIN['parameters']).
    player: the JarvisUI instance (may be None).
    Returns a short sentence that is spoken back to the user.
    """
    try:
        parameters = parameters or {}
        action = (parameters.get("action") or "write_notepad").strip().lower()
        text = parameters.get("text") or ""
        speed = (parameters.get("speed") or "normal").strip().lower()
        if speed not in _SPEEDS:
            speed = "normal"

        # --- stop -----------------------------------------------------------
        if action == "stop":
            if _job_thread is not None and _job_thread.is_alive():
                _stop_event.set()
                result = "Understood, Sir. I stopped writing."
            else:
                result = "I wasn't writing anything, Sir."
            _log(player, result)
            return result

        # Everything else needs the GUI libraries.
        try:
            pag, _clip = _load_libs()
        except ImportError:
            return "Sir, I need pyautogui and pyperclip installed. Run pip install pyautogui pyperclip, then restart me."

        # --- write in Notepad ----------------------------------------------
        if action == "write_notepad":
            if not text.strip():
                return "Sir, I have nothing to write. What should the text say?"
            path = OUTPUT_DIR / _safe_filename(parameters.get("filename"))
            if not _start_job(_job_write_notepad, player, path, text, speed):
                return "Sir, I'm still writing the previous text. Say stop writing if you want me to cancel it."
            result = f"Opening Notepad and writing in {path.name}, Sir. I'll save it when I'm done."
            _log(player, result)
            return result

        # --- type in the focused window ------------------------------------
        if action == "type_here":
            if not text.strip():
                return "Sir, I have nothing to type."
            try:
                delay = float(parameters.get("delay", 3))
            except (TypeError, ValueError):
                delay = 3.0
            if not _start_job(_job_type_here, player, text, speed, delay):
                return "Sir, I'm still writing the previous text. Say stop writing to cancel it."
            result = f"Typing in the active window in {int(delay)} seconds, Sir. Click where you want it."
            _log(player, result)
            return result

        # --- single key ------------------------------------------------------
        if action == "press_key":
            key = (parameters.get("keys") or "").strip().lower()
            if not key:
                return "Sir, which key should I press?"
            pag.press(key)
            result = f"Pressed {key}, Sir."
            _log(player, result)
            return result

        # --- hotkey ----------------------------------------------------------
        if action == "hotkey":
            combo = [k.strip().lower() for k in (parameters.get("keys") or "").split("+") if k.strip()]
            if not combo:
                return "Sir, which key combination should I use?"
            pag.hotkey(*combo)
            result = f"Done, Sir: {'+'.join(combo)}."
            _log(player, result)
            return result

        # --- save ------------------------------------------------------------
        if action == "save":
            pag.hotkey("ctrl", "s")
            result = "Saved, Sir."
            _log(player, result)
            return result

        return f"Sir, I don't know the keyboard action '{action}'."

    except Exception as e:
        return f"Sir, keyboard_writer failed: {e}"
