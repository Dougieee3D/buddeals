"""Windows extras: tray icon, notifications, start with Windows, QR codes. Every piece is optional — if a library
is missing the app still runs, it just skips that feature."""
import base64
import io
import os
import sys
import threading

try:
    import pystray
    from PIL import Image, ImageDraw
except Exception:  # pragma: no cover
    pystray = None

try:
    import qrcode
except Exception:  # pragma: no cover
    qrcode = None

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def icon_image(size=64):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, size - 2, size - 2), fill=(15, 23, 18, 255), outline=(94, 224, 138, 255), width=4)
    c = size // 2
    for dx, dy in ((0, -16), (-13, -6), (13, -6), (-9, 8), (9, 8)):  # simple five-leaf shape
        d.ellipse((c + dx * size // 64 - 7, c + dy * size // 64 - 9, c + dx * size // 64 + 7, c + dy * size // 64 + 9), fill=(94, 224, 138, 255))
    d.rectangle((c - 2, c + 6, c + 2, c + 22), fill=(94, 224, 138, 255))
    return img


class Tray:
    def __init__(self, on_open, on_check, on_quit):
        self.icon = None
        if not pystray:
            return
        menu = pystray.Menu(
            pystray.MenuItem("Open BudDeals", lambda *_: on_open(), default=True),
            pystray.MenuItem("Check deals now", lambda *_: on_check()),
            pystray.MenuItem("Quit", lambda *_: on_quit()),
        )
        self.icon = pystray.Icon("BudDeals", icon_image(), "BudDeals", menu)

    def start(self):
        if self.icon:
            threading.Thread(target=self.icon.run, daemon=True).start()

    def notify(self, title, message):
        if not self.icon:
            return False
        try:
            self.icon.notify(message[:250], title[:60])
            return True
        except Exception:
            return False

    def stop(self):
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass


def exe_path():
    return sys.executable if getattr(sys, "frozen", False) else ""


def get_startup():
    if os.name != "nt":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, "BudDeals")
            return True
    except Exception:
        return False


def set_startup(on):
    if os.name != "nt" or not exe_path():
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, "BudDeals", 0, winreg.REG_SZ, f'"{exe_path()}" --tray')
            else:
                try:
                    winreg.DeleteValue(k, "BudDeals")
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


def qr_data_url(text):
    if not qrcode:
        return None
    img = qrcode.make(text[:1800], box_size=6, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
