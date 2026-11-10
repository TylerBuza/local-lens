"""Capture the README screenshots of Local Lens using the CC0 demo photos in assets/demo/.

Run:  python tools/make_demo.py
Writes assets/screenshot-start.png, assets/screenshot-detect.png, assets/screenshot-select.png.
"""

import ctypes
import importlib.util
import os
import time

from PIL import Image, ImageGrab

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")


def main():
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    spec = importlib.util.spec_from_file_location("ll", os.path.join(ROOT, "local_lens.pyw"))
    ll = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ll)
    app = ll.LocalLens()
    app.geometry(f"{int(1280 * app.S)}x{int(860 * app.S)}+30+30")

    def pump(n=20):
        for _ in range(n):
            app.update()
            time.sleep(0.05)

    def shot(name):
        app.canvas.delete("toast")
        app.lift()
        app.attributes("-topmost", 1)
        pump(6)
        x, y = app.winfo_rootx(), app.winfo_rooty()
        ImageGrab.grab((x, y, x + app.winfo_width(), y + app.winfo_height()), all_screens=True).save(
            os.path.join(ASSETS, name))

    def load(name):
        app.load_image(Image.open(os.path.join(ASSETS, "demo", name)))
        while app.busy:
            pump(2)
        pump(10)

    def idx(pred, start=0):
        return next(i for i in range(start, len(app.words)) if pred(app.words[i]["text"]))

    def drag_words(i, j):
        """Simulate a mouse drag from word i to word j."""
        class E:
            pass

        def ev(ix, iy):
            e = E()
            e.x, e.y = int(app.offset[0] + ix * app.scale), int(app.offset[1] + iy * app.scale)
            return e

        a, b = app.words[i]["box"], app.words[j]["box"]
        app._on_press(ev(a[0] + 2, (a[1] + a[3]) / 2))
        app._on_drag(ev(b[2] - 2, (b[1] + b[3]) / 2))
        app._on_release(ev(b[2] - 2, (b[1] + b[3]) / 2))
        pump(8)
        return app.text.get("1.0", "end").strip()

    pump()
    shot("screenshot-start.png")

    load("heritage-trail-panel.jpg")
    print("panel words:", len(app.words))
    shot("screenshot-detect.png")

    load("snowmobile-marker.jpg")
    print("marker words:", len(app.words))
    start = idx(lambda t: t == "Carl")
    print("selected:", repr(drag_words(start, idx(lambda t: t.startswith("track"), start))))
    shot("screenshot-select.png")
    app.destroy()


if __name__ == "__main__":
    main()
