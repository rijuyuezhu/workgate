"""Isolated per-window Win32 PrintWindow screenshot helper (Windows only)."""

import contextlib
import ctypes
import sys
from pathlib import Path

from PIL import Image

from ...errors import GuiUnavailableError
from .base import GUI_MAX_CAPTURE_DIMENSION, GUI_MAX_CAPTURE_PIXELS


def _set_thread_dpi_awareness() -> None:
    """Use physical pixels for Win32 and UIA coordinates on mixed-DPI monitors."""
    if sys.platform != "win32":
        return
    # Thread-scoped per-monitor DPI awareness; does not modify the parent process.
    with contextlib.suppress(AttributeError, OSError):
        ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))


class _WinRect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


def _native_window_bounds(hwnd: int) -> dict[str, int]:
    windll = getattr(ctypes, "windll", None)
    if windll is None:  # pragma: no cover - Windows-only runtime guard.
        raise GuiUnavailableError(
            "Win32 window geometry is unavailable on this platform"
        )
    user32 = windll.user32
    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WinRect)]
    user32.GetWindowRect.restype = ctypes.c_int
    rect = _WinRect()
    if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
        raise GuiUnavailableError(
            "Win32 could not read the target window bounds"
        )
    return {
        "x": int(rect.left),
        "y": int(rect.top),
        "width": int(rect.right - rect.left),
        "height": int(rect.bottom - rect.top),
    }


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BitmapInfoHeader),
        ("bmiColors", ctypes.c_uint32 * 1),
    ]


_WINDOW_CAPTURE_TIMEOUT_S = 15.0


def _capture_window_image_native(hwnd: int, destination: Path) -> None:
    windll = getattr(ctypes, "windll", None)
    if windll is None:  # pragma: no cover - Windows-only runtime guard.
        raise GuiUnavailableError(
            "Win32 window capture is unavailable on this platform"
        )
    user32 = windll.user32
    gdi32 = windll.gdi32

    user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_WinRect)]
    user32.GetWindowRect.restype = ctypes.c_int
    user32.IsIconic.argtypes = [ctypes.c_void_p]
    user32.IsIconic.restype = ctypes.c_int
    user32.GetWindowDC.argtypes = [ctypes.c_void_p]
    user32.GetWindowDC.restype = ctypes.c_void_p
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.PrintWindow.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint,
    ]
    user32.PrintWindow.restype = ctypes.c_int
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
    ]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.GetDIBits.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.POINTER(_BitmapInfo),
        ctypes.c_uint,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteObject.restype = ctypes.c_int
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.restype = ctypes.c_int

    rect = _WinRect()
    if not user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(rect)):
        raise GuiUnavailableError(
            "Win32 could not read the target window bounds"
        )
    width = int(rect.right - rect.left)
    height = int(rect.bottom - rect.top)
    if width <= 0 or height <= 0:
        raise GuiUnavailableError(
            "Win32 target window has invalid capture bounds"
        )
    if (
        width > GUI_MAX_CAPTURE_DIMENSION
        or height > GUI_MAX_CAPTURE_DIMENSION
        or width * height > GUI_MAX_CAPTURE_PIXELS
    ):
        raise GuiUnavailableError(
            f"Windows capture dimensions exceed the safe budget: {width}x{height}"
        )
    if user32.IsIconic(ctypes.c_void_p(hwnd)):
        raise GuiUnavailableError(
            "Cannot capture a minimized Windows window safely"
        )

    window_dc = user32.GetWindowDC(ctypes.c_void_p(hwnd))
    if not window_dc:
        raise GuiUnavailableError(
            "Win32 could not acquire the target window DC"
        )
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = (
        gdi32.CreateCompatibleBitmap(window_dc, width, height)
        if memory_dc
        else None
    )
    old_object = gdi32.SelectObject(memory_dc, bitmap) if bitmap else None
    bitmap_selected = bool(old_object)
    try:
        if not memory_dc or not bitmap or not old_object:
            raise GuiUnavailableError(
                "Win32 could not allocate an off-screen window capture"
            )
        rendered = bool(
            user32.PrintWindow(ctypes.c_void_p(hwnd), memory_dc, 0x00000002)
        )
        if not rendered:
            rendered = bool(
                user32.PrintWindow(ctypes.c_void_p(hwnd), memory_dc, 0)
            )
        if not rendered:
            raise GuiUnavailableError(
                "Win32 PrintWindow could not capture the selected window independently"
            )

        byte_count = width * height * 4
        pixels = ctypes.create_string_buffer(byte_count)
        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0
        info.bmiHeader.biSizeImage = byte_count
        restored = gdi32.SelectObject(memory_dc, old_object)
        if not restored:
            raise GuiUnavailableError(
                "Win32 could not unselect the capture bitmap before reading pixels"
            )
        bitmap_selected = False
        copied = gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            height,
            pixels,
            ctypes.byref(info),
            0,
        )
        if copied != height:
            raise GuiUnavailableError(
                "Win32 could not read the captured window bitmap"
            )
        image = Image.frombytes(
            "RGB",
            (width, height),
            bytes(pixels),
            "raw",
            "BGRX",
            width * 4,
            1,
        )
        image.save(destination, format="PNG")
    finally:
        if old_object and bitmap_selected:
            gdi32.SelectObject(memory_dc, old_object)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(ctypes.c_void_p(hwnd), window_dc)


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if sys.platform != "win32" or len(args) != 2:
        raise SystemExit(
            "usage: python -m workgate.executor.gui.windows_capture HWND PNG"
        )
    hwnd = int(args[0])
    if hwnd <= 0:
        raise SystemExit("invalid window handle")
    _set_thread_dpi_awareness()
    _capture_window_image_native(hwnd, Path(args[1]))


if __name__ == "__main__":
    main()
