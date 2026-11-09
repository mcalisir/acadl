"""PyInstaller runtime hook: patchright browser yolunu ayarla, console=False icin fd'leri koru."""
import os
import sys
import io

if getattr(sys, 'frozen', False):
    base = sys._MEIPASS
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(base, "ms-playwright")

    # C-seviye fd 0/1/2'yi devnull'a yonlendir (console=False modunda
    # C uzantilari — curl_cffi gibi — gecerli fd olmadan crash olur)
    try:
        _devnull = os.open(os.devnull, os.O_RDWR)
        for _fd in (0, 1, 2):
            try:
                os.dup2(_devnull, _fd)
            except OSError:
                pass
        os.close(_devnull)
    except Exception:
        pass

    # Python-seviye stdout/stderr sarmalama
    if sys.stdout is None or not hasattr(sys.stdout, 'write'):
        sys.stdout = io.TextIOWrapper(io.open(os.devnull, 'wb'), encoding='utf-8')
    if sys.stderr is None or not hasattr(sys.stderr, 'write'):
        sys.stderr = io.TextIOWrapper(io.open(os.devnull, 'wb'), encoding='utf-8')
