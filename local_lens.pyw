"""Local Lens - a Google-Lens-style text grabber that runs fully offline.

Uses the OCR engine built into Windows (Windows.Media.Ocr). Nothing leaves the PC.

  Ctrl+V          paste an image (screenshot, copied image, copied image file)
  Drop a file     drag an image file onto the window
  Click a word    select it; drag across words to select a range
  Double-click    select the whole line
  Drag on empty   draw a box; selects every word inside it
  Ctrl+A          select all text
  Ctrl+C          copy the selected text
  Ctrl+N          snip the screen
  Ctrl+O / Ctrl+S open / save image
  Esc             clear selection
  Delete          clear the image
  Right-click     menu
"""

import hashlib
import os
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog

from PIL import Image, ImageDraw, ImageEnhance, ImageGrab, ImageOps, ImageTk

import winocr

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    BaseTk = TkinterDnD.Tk
except Exception:  # drag-and-drop is optional
    DND_FILES = None
    BaseTk = tk.Tk

__version__ = "1.0.0"
APP_TITLE = "Local Lens"
HERE = os.path.dirname(os.path.abspath(__file__))
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff", ".webp")
MAX_OCR_DIM = 4000

# Palette (Google-ish dark)
BG = "#202124"
CANVAS_BG = "#131314"
SURFACE = "#2d2e31"
TEXT = "#e8eaed"
MUTED = "#9aa0a6"
ACCENT = "#8ab4f8"
ACCENT_HOVER = "#aecbfa"
CHIP = "#3c4043"
CHIP_HOVER = "#4a4d51"
SELECT_RGB = (66, 133, 244)

ICONS = {"paste": "", "snip": "", "open": "", "copy": "",
         "all": "", "clear": "", "image": ""}


# --------------------------------------------------------------------------- OCR
def _overlap(a, b):
    """Fraction of box a covered by box b."""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    return ix * iy / area


# Windows OCR only reads upright text. Sideways/upside-down photos are retried rotated.
ROTATIONS = {90: Image.ROTATE_90, 270: Image.ROTATE_270, 180: Image.ROTATE_180}
COMMON_WORDS = set("""the be to of and in that have it for not on with he as you do at this but his by
from they we say her she or an will my one all would there their what so up out if about who get which
go me when make can like time no just him know take people into year your good some could them see
other than then now look only come its over think also back after use two how our work first well way
even new want because any these give day most us is are was were has had been am may per off
""".split())


def _text_score(lines):
    """How much the OCR result looks like real text (used to pick the right orientation)."""
    score = 0
    for ln in lines:
        for w in ln:
            t = w["text"].strip(".,:;!?\"'()[]{}")
            if len(t) < 2:
                continue
            if t.lower() in COMMON_WORDS:
                score += 3 * len(t)
            elif t.isdigit() and len(t) >= 3:
                score += len(t)
            elif t.isalpha() and (t.islower() or t.isupper() or t.istitle()) \
                    and any(c in "aeiouyAEIOUY" for c in t):
                score += len(t)
    return score


def _unrotate_box(b, rot, W, H):
    """Map a box from an image rotated by `rot` (PIL transpose) back to the original W x H image."""
    x0, y0, x1, y1 = b
    if rot == 90:  # rotated (u, v) -> original (W - v, u)
        return (W - y1, x0, W - y0, x1)
    if rot == 270:  # rotated (u, v) -> original (v, H - u)
        return (y0, H - x1, y1, H - x0)
    if rot == 180:
        return (W - x1, H - y1, W - x0, H - y0)
    return b


def ocr_image(img: Image.Image, lang: str):
    """Return (words, text). Each word: {'text', 'line', 'rot', 'box', 'rbox'}.

    'box' is in original image coords; 'rbox' is in the upright (reading) frame, used for
    ordering text. If the image has little readable text upright, rotated versions are tried
    and the orientation that reads best wins (photos of cards, sideways screenshots, etc.).
    """
    img = img.convert("RGB")
    W, H = img.size
    lines, rot = _ocr_multiscale(img, lang), 0
    best = _text_score(lines)
    if best < 80:
        for r, op in ROTATIONS.items():
            cand = _ocr_multiscale(img.transpose(op), lang)
            sc = _text_score(cand)
            if sc > best * 1.5 + 10:
                lines, rot, best = cand, r, sc
    words = []
    for li, ln in enumerate(lines):
        for w in ln:
            words.append({"text": w["text"], "line": li, "rot": rot, "rbox": w["box"],
                          "box": _unrotate_box(w["box"], rot, W, H)})
    return words, "\n".join(" ".join(w["text"] for w in ln) for ln in lines).strip()


def _rotate_box(b, rot, W, H):
    """Inverse of _unrotate_box: map an original-image box into the frame rotated by `rot`."""
    x0, y0, x1, y1 = b
    if rot == 90:  # original (x, y) -> rotated (y, W - x)
        return (y0, W - x1, y1, W - x0)
    if rot == 270:  # original (x, y) -> rotated (H - y, x)
        return (H - y1, x0, H - y0, x1)
    if rot == 180:
        return (W - x1, H - y1, W - x0, H - y0)
    return b


# ---------------------------------------------------------------- enhanced pass
# Optional second engine (RapidOCR, PaddleOCR models, bundled in the package, runs offline).
# Windows OCR misses thin, faint or tiny text in photos; RapidOCR finds those text regions.
# Each new region is re-read by Windows OCR as a tight crop (keeps spaces and word boxes),
# with RapidOCR's own reading as the fallback.
_rapid = None
_rapid_lock = threading.Lock()


def get_rapid():
    global _rapid
    with _rapid_lock:
        if _rapid is None:
            try:
                import logging

                logging.disable(logging.WARNING)
                from rapidocr_onnxruntime import RapidOCR

                _rapid = RapidOCR()
            except Exception:
                _rapid = False
        return _rapid or None


def _norm(s):
    return "".join(c for c in s.lower() if c.isalnum())


def _inside(bb, words):
    """Words whose center lies inside box bb (with a little margin)."""
    m = 0.3 * min(bb[2] - bb[0], bb[3] - bb[1])
    bb = (bb[0] - m, bb[1] - m, bb[2] + m, bb[3] + m)
    out = []
    for w in words:
        b = w["box"]
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        if bb[0] <= cx <= bb[2] and bb[1] <= cy <= bb[3]:
            out.append(w)
    return out


def _known_frac(texts):
    """Share of tokens that are real words (wordninja's offline dictionary) or numbers."""
    try:
        import wordninja

        vocab = wordninja.DEFAULT_LANGUAGE_MODEL._wordcost
    except Exception:
        return 0.0
    toks = [_norm(t) for t in texts for t in t.split()]
    toks = [t for t in toks if t]
    if not toks:
        return 0.0
    return sum(1 for t in toks if t.isdigit() or (len(t) > 1 and t in vocab)) / len(toks)


def _split_line(text, bb, rot):
    """Turn one detected line into words with estimated boxes.

    The extra engine often drops spaces ("HAVEYOURCARDTO"), so long letters-only runs are
    split into words with wordninja (offline, bundled word list). URLs etc. are left alone.
    """
    tokens = []
    for tok in text.split():
        if tok.isalpha() and len(tok) >= 8:
            try:
                import wordninja

                parts = wordninja.split(tok) or [tok]
                junk = sum(1 for p in parts if len(p) <= 2 and p.lower() not in COMMON_WORDS
                           and p.upper() not in ("A", "I"))
                tokens += parts if junk <= len(parts) / 4 else [tok]
                continue
            except Exception:
                pass
        tokens.append(tok)
    total = max(1, sum(len(t) for t in tokens) + len(tokens) - 1)
    x0, y0, x1, y1 = bb
    w, h = x1 - x0, y1 - y0
    out, pos = [], 0
    for t in tokens:
        a, b = pos / total, (pos + len(t)) / total
        pos += len(t) + 1
        if rot == 90:  # reads top to bottom
            box = (x0, y0 + a * h, x1, y0 + b * h)
        elif rot == 270:  # reads bottom to top
            box = (x0, y1 - b * h, x1, y1 - a * h)
        else:
            box = (x0 + a * w, y0, x0 + b * w, y1)
        out.append({"text": t, "rot": rot, "box": box})
    return out


def enhance_words(img: Image.Image, words, lang: str):
    """Return a new word list: `words` plus text the extra engine found in uncovered areas."""
    eng = get_rapid()
    if not eng:
        return words
    import difflib

    import numpy as np

    img = img.convert("RGB")
    W, H = img.size
    page_rot = words[0]["rot"] if words else 0
    with _rapid_lock:
        res, _ = eng(np.array(img)[:, :, ::-1].copy())
    new_lines, removed = [], set()
    for quad, text, conf in res or []:
        if float(conf) < 0.6 or len(_norm(text)) < 1:
            continue
        xs, ys = [p[0] for p in quad], [p[1] for p in quad]
        bb = (max(0, min(xs)), max(0, min(ys)), min(W, max(xs)), min(H, max(ys)))
        if bb[2] - bb[0] < 3 or bb[3] - bb[1] < 3:
            continue
        inside = [w for w in _inside(bb, words) if id(w) not in removed]
        if sum(len(_norm(w["text"])) for w in inside) > 0.5 * len(_norm(text)):
            # Already read by Windows OCR. Replace it only if Windows' reading is gibberish
            # and this one is made of real words (e.g. "qarfilla" -> "Vanilla").
            old = [w["text"] for w in inside]
            if not (float(conf) >= 0.8 and _known_frac([text]) >= _known_frac(old) + 0.5
                    and difflib.SequenceMatcher(None, _norm("".join(old)), _norm(text)).ratio() < 0.85):
                continue
            removed.update(id(w) for w in inside)
            vertical = (bb[3] - bb[1]) > 1.5 * (bb[2] - bb[0]) and len(_norm(text)) > 1
            rot = inside[0]["rot"] if inside else 0
            line = _split_line(text, bb, rot if vertical or rot else 0)
            for w in line:
                w["rbox"] = _rotate_box(w["box"], page_rot, W, H)
            new_lines.append(line)
            continue
        vertical = (bb[3] - bb[1]) > 1.5 * (bb[2] - bb[0]) and len(_norm(text)) > 1
        m = 0.15 * min(bb[2] - bb[0], bb[3] - bb[1]) + 2
        cb = (int(max(0, bb[0] - m)), int(max(0, bb[1] - m)), int(min(W, bb[2] + m)), int(min(H, bb[3] + m)))
        crop = img.crop(cb)
        best = None
        for rot in ((90, 270) if vertical else (0,)):
            c = crop.transpose(ROTATIONS[rot]) if rot else crop
            ws = [w for ln in _ocr_lines(c, lang) for w in ln]
            ratio = difflib.SequenceMatcher(None, _norm("".join(w["text"] for w in ws)), _norm(text)).ratio()
            if ws and (best is None or ratio > best[0]):
                best = (ratio, rot, ws)
        line = []
        if best and best[0] >= 0.6:
            ratio, rot, ws = best
            for w in ws:
                b = _unrotate_box(w["box"], rot, crop.width, crop.height)
                line.append({"text": w["text"], "rot": rot,
                             "box": (b[0] + cb[0], b[1] + cb[1], b[2] + cb[0], b[3] + cb[1])})
        elif float(conf) >= 0.75:  # fall back to the extra engine's own reading
            rot = (page_rot if page_rot in (90, 270) else 90) if vertical else 0
            line = _split_line(text, bb, rot)
        if not line:
            continue
        for w in line:
            w["rbox"] = _rotate_box(w["box"], page_rot, W, H)
        new_lines.append(line)
    if not new_lines:
        return words

    # Insert new lines among the existing ones by position in the reading frame.
    lines = []
    for w in words:
        if id(w) in removed:
            continue
        if not lines or lines[-1][0]["line"] != w["line"]:
            lines.append([])
        lines[-1].append(w)
    for ln in new_lines:
        top = min(w["rbox"][1] for w in ln)
        pos = next((i for i, l2 in enumerate(lines) if min(o["rbox"][1] for o in l2) > top), len(lines))
        lines.insert(pos, ln)
    out = []
    for li, ln in enumerate(lines):
        for w in ln:
            w["line"] = li
            out.append(w)
    return out


def ocr_crop(img: Image.Image, lang: str):
    """OCR for a user-drawn box: Windows OCR first, the extra engine if that finds nothing."""
    words, text = ocr_image(img, lang)
    if not text:
        words = enhance_words(img, [], lang)
        text = words_to_rows_text(words, 0.0) if words else ""
    return words, text


def _ocr_multiscale(img: Image.Image, lang: str):
    """Windows OCR reads normal-sized text well but misses very large text (signs, headings),
    so big images get extra downscaled passes whose large words are merged in."""
    lines = _ocr_lines(img, lang)
    heights = sorted(w["box"][3] - w["box"][1] for ln in lines for w in ln)
    base_h = heights[len(heights) // 2] if heights else 0
    for s in (0.5, 0.25):
        if min(img.size) * s < 160:
            break
        small = img.resize((int(img.width * s), int(img.height * s)), Image.LANCZOS)
        for ln in _ocr_lines(small, lang):
            ln = [{"text": w["text"], "box": tuple(v / s for v in w["box"])} for w in ln]
            ln = [w for w in ln
                  if w["box"][3] - w["box"][1] >= max(1.8 * base_h, 1)
                  and not any(_overlap(w["box"], o["box"]) > 0.3 for l2 in lines for o in l2)]
            if not ln:
                continue
            top = min(w["box"][1] for w in ln)
            pos = next((i for i, l2 in enumerate(lines) if min(o["box"][1] for o in l2) > top), len(lines))
            lines.insert(pos, ln)
    return lines


def _ocr_lines(img: Image.Image, lang: str):
    """One Windows OCR pass. Returns a list of lines, each a list of {'text', 'box'}."""
    f = 1.0
    w, h = img.size
    # Small images and thin crops read much better upscaled.
    s = 1
    if min(w, h) < 300:
        s = max(s, 300 // max(1, min(w, h)) + 1)
    if max(w, h) < 1000:
        s = max(s, -(-1000 // max(1, max(w, h))))
    s = min(s, 4)
    if s > 1:
        img = img.resize((w * s, h * s), Image.LANCZOS)
        f *= s
    w, h = img.size
    if max(w, h) > MAX_OCR_DIM:
        s = MAX_OCR_DIM / max(w, h)
        img = img.resize((int(w * s), int(h * s)), Image.LANCZOS)
        f *= s
    pad = 12
    img = ImageOps.expand(img, border=pad, fill=img.getpixel((0, 0)))
    result = winocr.recognize_pil_sync(img, lang)

    lines = []
    for ln in result.get("lines", []):
        out = []
        for wd in ln.get("words", []):
            r = wd["bounding_rect"]
            x0, y0 = (r["x"] - pad) / f, (r["y"] - pad) / f
            out.append({"text": wd["text"], "box": (x0, y0, x0 + r["width"] / f, y0 + r["height"] / f)})
        if out:
            lines.append(out)
    return lines


def words_to_text(words):
    out, cur, last = [], [], None
    for w in words:
        if last is not None and w["line"] != last:
            out.append(" ".join(cur))
            cur = []
        cur.append(w["text"])
        last = w["line"]
    if cur:
        out.append(" ".join(cur))
    return "\n".join(out)


def estimate_skew(words):
    """Median text slope (dy/dx) from OCR lines with several words; handles tilted photos."""
    by_line = {}
    for w in words:
        by_line.setdefault(w["line"], []).append(w)
    slopes = []
    for ws in by_line.values():
        if len(ws) < 2:
            continue
        a, b = ws[0].get("rbox", ws[0]["box"]), ws[-1].get("rbox", ws[-1]["box"])
        dx = (b[0] + b[2]) / 2 - (a[0] + a[2]) / 2
        if dx > (a[3] - a[1]) * 2:
            slopes.append(((b[1] + b[3]) / 2 - (a[1] + a[3]) / 2) / dx)
    slopes.sort()
    return slopes[len(slopes) // 2] if slopes else 0.0


def words_to_rows_text(words, skew=0.0):
    """Rebuild text by visual rows, so table columns (e.g. receipt labels and prices) stay together."""
    if not words:
        return ""
    items = []
    for w in words:
        x0, y0, x1, y1 = w.get("rbox", w["box"])
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        items.append((cy - cx * skew, x0, y1 - y0, w["text"]))
    items.sort()
    hs = sorted(i[2] for i in items)
    tol = hs[len(hs) // 2] * 0.55
    rows = []
    for it in items:
        if rows and abs(it[0] - rows[-1]["y"]) < tol:
            r = rows[-1]
            r["items"].append(it)
            r["y"] = sum(i[0] for i in r["items"]) / len(r["items"])
        else:
            rows.append({"y": it[0], "items": [it]})
    return "\n".join(" ".join(i[3] for i in sorted(r["items"], key=lambda i: i[1])) for r in rows)


def available_languages():
    try:
        from winrt.windows.media.ocr import OcrEngine

        return [l.language_tag for l in OcrEngine.available_recognizer_languages] or ["en-US"]
    except Exception:
        return ["en-US"]


def pill_image(w, h, color, radius=None):
    """Anti-aliased rounded pill as an RGBA image (works over any background)."""
    ss = 4
    im = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    r = (radius if radius is not None else h / 2) * ss
    ImageDraw.Draw(im).rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius=r, fill=color)
    return im.resize((w, h), Image.LANCZOS)


# ---------------------------------------------------------------------- widgets
class Pill(tk.Canvas):
    def __init__(self, master, text, icon=None, command=None, primary=False, s=1.0):
        super().__init__(master, bg=master["bg"], highlightthickness=0, bd=0, cursor="hand2")
        self.command = command
        fill, hover, fg = (ACCENT, ACCENT_HOVER, BG) if primary else (CHIP, CHIP_HOVER, TEXT)
        font = tkfont.Font(family="Segoe UI Semibold", size=10)
        ifont = tkfont.Font(family="Segoe Fluent Icons", size=11)
        h = int(36 * s)
        padx = int(16 * s)
        iw = int(26 * s) if icon else 0
        w = padx + iw + font.measure(text) + padx
        self.configure(width=w, height=h)
        self.imgs = {k: ImageTk.PhotoImage(pill_image(w, h, c)) for k, c in (("n", fill), ("h", hover))}
        self.bgi = self.create_image(0, 0, anchor=tk.NW, image=self.imgs["n"])
        x = padx
        if icon:
            self.create_text(x + int(9 * s), h // 2 + 1, text=ICONS[icon], font=ifont, fill=fg)
            x += iw
        self.create_text(x, h // 2, anchor=tk.W, text=text, font=font, fill=fg)
        self.bind("<Enter>", lambda e: self.itemconfig(self.bgi, image=self.imgs["h"]))
        self.bind("<Leave>", lambda e: self.itemconfig(self.bgi, image=self.imgs["n"]))
        self.bind("<ButtonRelease-1>", self._click)

    def _click(self, e):
        if 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height() and self.command:
            self.command()


# -------------------------------------------------------------------------- app
class LocalLens(BaseTk):
    def __init__(self):
        super().__init__()
        self.S = max(1.0, self.winfo_fpixels("1i") / 96)
        self.title(APP_TITLE)
        self.configure(bg=BG)
        self.geometry(f"{int(1180 * self.S)}x{int(800 * self.S)}")
        self.minsize(int(640 * self.S), int(480 * self.S))
        ico = os.path.join(HERE, "local_lens.ico")
        if os.path.exists(ico):
            try:
                self.iconbitmap(ico)
            except tk.TclError:
                pass

        self.image = None
        self.words = []
        self.skew = 0.0
        self.sel = set()
        self.busy = False
        self.enhancing = False
        self.gen = 0
        threading.Thread(target=get_rapid, daemon=True).start()  # load the extra engine early
        self.mode = None
        self.anchor = None
        self.box_start = None
        self.scale = 1.0
        self.offset = (0, 0)
        self.dsize = (1, 1)
        self.disp = self.dim = self.lit = self.round_mask = None
        self.toast_job = self.resize_job = None
        self.scan_y = 0
        langs = available_languages()
        self.lang = tk.StringVar(value=next((l for l in langs if l.lower().startswith("en")), langs[0]))

        self._build_ui(langs)
        self._bind_keys()
        self.after(10, self._dark_titlebar)
        self.after(50, self._render_layout)

    # ------------------------------------------------------------------ UI
    def _build_ui(self, langs):
        S = self.S
        top = tk.Frame(self, bg=BG, padx=int(16 * S), pady=int(10 * S))
        top.pack(side=tk.TOP, fill=tk.X)

        for name in ("assets/logo.png", "local_lens.ico"):
            logo_path = os.path.join(HERE, name)
            if os.path.exists(logo_path):
                lg = Image.open(logo_path).convert("RGBA").resize((int(28 * S), int(28 * S)), Image.LANCZOS)
                self.logo = ImageTk.PhotoImage(lg)
                tk.Label(top, image=self.logo, bg=BG).pack(side=tk.LEFT, padx=(0, int(10 * S)))
                break
        tk.Label(top, text="Local Lens", bg=BG, fg=TEXT,
                 font=("Segoe UI Variable Display Semib", 15)).pack(side=tk.LEFT)
        bf = tkfont.Font(family="Segoe UI Semibold", size=8)
        badge_text = "\U0001F512  PRIVATE · OFFLINE"
        bw, bh = bf.measure(badge_text) + int(22 * S), int(22 * S)
        self.badge_img = ImageTk.PhotoImage(pill_image(bw, bh, (138, 180, 248, 40)))
        badge = tk.Canvas(top, width=bw, height=bh, bg=BG, highlightthickness=0, bd=0)
        badge.create_image(0, 0, anchor=tk.NW, image=self.badge_img)
        badge.create_text(bw // 2, bh // 2, text=badge_text, fill=ACCENT, font=bf)
        badge.pack(side=tk.LEFT, padx=int(12 * S), pady=(int(3 * S), 0))

        for text, icon, cmd in (("Clear", "clear", self.clear),
                                ("Open", "open", self.open_file),
                                ("Snip screen", "snip", self.snip),
                                ("Paste", "paste", self.paste)):
            Pill(top, text, icon, cmd, primary=(text == "Paste"), s=S).pack(side=tk.RIGHT, padx=int(4 * S))
        if len(langs) > 1:
            om = tk.OptionMenu(top, self.lang, *langs)
            om.configure(bg=CHIP, fg=TEXT, activebackground=CHIP_HOVER, activeforeground=TEXT,
                         relief=tk.FLAT, highlightthickness=0, bd=0)
            om.pack(side=tk.RIGHT, padx=int(8 * S))

        # Bottom sheet
        sheet = tk.Frame(self, bg=SURFACE, padx=int(18 * S), pady=int(12 * S))
        sheet.pack(side=tk.BOTTOM, fill=tk.X)
        head = tk.Frame(sheet, bg=SURFACE)
        head.pack(fill=tk.X)
        self.sheet_title = tk.StringVar(value="Paste an image to get started")
        tk.Label(head, textvariable=self.sheet_title, bg=SURFACE, fg=MUTED,
                 font=("Segoe UI Semibold", 10)).pack(side=tk.LEFT)
        Pill(head, "Copy text", "copy", self.copy_text, primary=True, s=S).pack(side=tk.RIGHT, padx=int(4 * S))
        Pill(head, "Select all text", "all", self.select_all, s=S).pack(side=tk.RIGHT, padx=int(4 * S))
        self.text = tk.Text(sheet, height=6, wrap=tk.WORD, bg=SURFACE, fg=TEXT, insertbackground=TEXT,
                            selectbackground="#3b5b8f", relief=tk.FLAT, bd=0, highlightthickness=0,
                            font=("Segoe UI", 11), padx=2, pady=int(8 * S), undo=True)
        self.text.pack(fill=tk.BOTH, expand=True)

        self.canvas = tk.Canvas(self, bg=CANVAS_BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.img_item = self.canvas.create_image(0, 0, anchor=tk.NW)

        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", self._on_double)
        self.canvas.bind("<Motion>", self._on_hover)
        self.canvas.bind("<Button-3>", self._context_menu)

        self.menu = tk.Menu(self, tearoff=0, bg=SURFACE, fg=TEXT, activebackground=CHIP_HOVER,
                            activeforeground=TEXT, bd=0, relief=tk.FLAT, font=("Segoe UI", 10))
        self.menu.add_command(label="Copy text", accelerator="Ctrl+C", command=self.copy_text)
        self.menu.add_command(label="Select all text", accelerator="Ctrl+A", command=self.select_all)
        self.menu.add_command(label="Deselect", accelerator="Esc",
                              command=lambda: self._set_sel(set()) or self._clear_brackets())
        self.menu.add_separator()
        self.menu.add_command(label="Save image as…", command=self.save_image)
        self.menu.add_command(label="Clear image", accelerator="Del", command=self.clear)

        if DND_FILES:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)

    def _dark_titlebar(self):
        try:
            import ctypes

            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            on = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4)
            color = ctypes.c_int(0x00242120)  # COLORREF of BG (0x00BBGGRR)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(color), 4)
        except Exception:
            pass

    def _bind_keys(self):
        self.bind_all("<Control-v>", self._key_paste)
        self.bind_all("<Control-V>", self._key_paste)
        self.bind_all("<Control-o>", lambda e: self.open_file())
        self.bind_all("<Control-n>", lambda e: self.snip())
        self.bind_all("<Control-s>", lambda e: self.save_image())
        self.bind("<Control-a>", self._key_select_all)
        self.bind("<Control-c>", self._key_copy)
        self.bind("<Escape>", lambda e: self._set_sel(set()) or self._clear_brackets())
        self.bind("<Delete>", lambda e: None if e.widget is self.text else self.clear())

    def _key_paste(self, e):
        if e.widget is self.text and not isinstance(self._grab_clipboard(), Image.Image):
            return None
        self.paste()
        return "break"

    def _key_select_all(self, e):
        if e.widget is self.text:
            self.text.tag_add(tk.SEL, "1.0", tk.END)
            return "break"
        self.select_all()
        return "break"

    def _key_copy(self, e):
        if e.widget is self.text and self.text.tag_ranges(tk.SEL):
            return None
        self.copy_text()
        return "break"

    # -------------------------------------------------------------- loading
    def _grab_clipboard(self):
        try:
            data = ImageGrab.grabclipboard()
        except Exception:
            return None
        if isinstance(data, list):
            for p in data:
                if p.lower().endswith(IMAGE_EXTS) and os.path.isfile(p):
                    try:
                        return Image.open(p)
                    except Exception:
                        pass
            return None
        return data

    def paste(self):
        img = self._grab_clipboard()
        if not isinstance(img, Image.Image):
            self.toast("No image on the clipboard — copy a screenshot first (Win+Shift+S)")
            return
        self.load_image(img)

    def open_file(self):
        path = filedialog.askopenfilename(
            title="Open image", filetypes=[("Images", " ".join("*" + e for e in IMAGE_EXTS)), ("All files", "*.*")])
        if path:
            self._load_path(path)

    def _on_drop(self, e):
        for p in self.tk.splitlist(e.data):
            if p.lower().endswith(IMAGE_EXTS):
                self._load_path(p)
                return
        self.toast("That's not an image file")

    def _load_path(self, path):
        try:
            self.load_image(Image.open(path))
        except Exception as ex:
            self.toast(f"Couldn't open image: {ex}")

    def snip(self):
        before = self._clip_hash()
        self.iconify()
        self.after(250, lambda: os.startfile("ms-screenclip:"))
        self._watch_clipboard(before, tries=100)

    def _clip_hash(self):
        img = self._grab_clipboard()
        return hashlib.md5(img.tobytes()).hexdigest() if isinstance(img, Image.Image) else None

    def _watch_clipboard(self, before, tries):
        h = self._clip_hash()
        if (h and h != before) or tries <= 0:
            self.deiconify()
            self.lift()
            self.focus_force()
            if h and h != before:
                self.load_image(self._grab_clipboard())
            return
        self.after(200, self._watch_clipboard, before, tries - 1)

    def _context_menu(self, e):
        if self.image is None or self.busy:
            return
        self.menu.tk_popup(e.x_root, e.y_root)

    def save_image(self):
        if self.image is None:
            return
        path = filedialog.asksaveasfilename(title="Save image", defaultextension=".png",
                                            filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg")])
        if path:
            self.image.save(path)
            self.toast("Image saved")

    def clear(self):
        if self.busy:
            return
        self.image = None
        self.gen += 1
        self.enhancing = False
        self.words, self.sel = [], set()
        self.disp = self.dim = self.lit = self.round_mask = None
        self.tk_img = None
        self._clear_brackets()
        self.canvas.delete("toast")
        self.text.delete("1.0", tk.END)
        self.sheet_title.set("Paste an image to get started")
        self._render_layout()

    def load_image(self, img):
        img.load()
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, "white")
            bg.paste(img, mask=img.split()[3])
            img = bg
        self.image = img.convert("RGB")
        self.gen += 1
        self.enhancing = False
        self.words, self.sel = [], set()
        self._clear_brackets()
        self.text.delete("1.0", tk.END)
        self._render_layout()
        self.run_ocr(self.image, self._full_done)

    # -------------------------------------------------------------- drawing
    def _on_configure(self, e):
        if self.resize_job:
            self.after_cancel(self.resize_job)
        self.resize_job = self.after(60, self._render_layout)

    def _render_layout(self):
        self.resize_job = None
        c = self.canvas
        cw, ch = c.winfo_width(), c.winfo_height()
        c.delete("ph")
        if self.image is None:
            c.itemconfig(self.img_item, image="")
            self._placeholder(cw, ch)
            return
        if cw < 20 or ch < 20:
            return
        m = int(24 * self.S)
        iw, ih = self.image.size
        self.scale = min((cw - 2 * m) / iw, (ch - 2 * m) / ih, 2.0)
        dw, dh = max(1, int(iw * self.scale)), max(1, int(ih * self.scale))
        self.dsize = (dw, dh)
        self.offset = ((cw - dw) // 2, (ch - dh) // 2)
        self.disp = self.image.resize((dw, dh), Image.LANCZOS)
        self.dim = ImageEnhance.Brightness(self.disp).enhance(0.45)
        self.round_mask = Image.new("L", (dw, dh), 0)
        ImageDraw.Draw(self.round_mask).rounded_rectangle((0, 0, dw - 1, dh - 1), radius=int(14 * self.S), fill=255)
        self._build_lit()
        c.coords(self.img_item, *self.offset)
        self._clear_brackets()
        self._compose()

    def _placeholder(self, cw, ch):
        S = self.S
        cx, cy = cw // 2, ch // 2
        c = self.canvas
        bw, bh = min(int(520 * S), cw - 40), min(int(300 * S), ch - 40)
        if bw > 50 and bh > 50:
            c.create_rectangle(cx - bw // 2, cy - bh // 2, cx + bw // 2, cy + bh // 2,
                               outline="#3c4043", dash=(6, 4), width=2, tags="ph")
        c.create_text(cx, cy - int(60 * S), text=ICONS["image"], fill=ACCENT,
                      font=("Segoe Fluent Icons", 40), tags="ph")
        c.create_text(cx, cy + int(5 * S), text="Paste an image to search its text", fill=TEXT,
                      font=("Segoe UI Semibold", 16), tags="ph")
        c.create_text(cx, cy + int(45 * S), fill=MUTED, font=("Segoe UI", 11), tags="ph", justify=tk.CENTER,
                      text="Ctrl+V  ·  drop a file here  ·  or use Snip screen")

    def _wrect(self, w, pad=True):
        """Word box in display-image coords (no canvas offset)."""
        x0, y0, x1, y1 = (v * self.scale for v in w["box"])
        if pad:
            if w.get("rot") in (90, 270):  # sideways text: the box width is the text height
                h = x1 - x0
                x0, x1, y0, y1 = x0 - h * 0.14, x1 + h * 0.14, y0 - h * 0.22, y1 + h * 0.22
            else:
                h = y1 - y0
                x0, x1, y0, y1 = x0 - h * 0.22, x1 + h * 0.22, y0 - h * 0.14, y1 + h * 0.14
        return (max(0, x0), max(0, y0), min(self.dsize[0], x1), min(self.dsize[1], y1))

    def _mask_for(self, words):
        """Mask with one rounded highlight per run of nearby words on the same line."""
        m = Image.new("L", self.dsize, 0)
        d = ImageDraw.Draw(m)
        runs, cur = [], None
        for w in words:
            r = self._wrect(w)
            if cur and cur[0] == w["line"] and r[0] - cur[1][2] < (r[3] - r[1]) * 1.2:
                c = cur[1]
                cur[1] = (c[0], min(c[1], r[1]), max(c[2], r[2]), max(c[3], r[3]))
            else:
                cur = [w["line"], r]
                runs.append(cur)
        for _, r in runs:
            d.rounded_rectangle(r, radius=max(2, (r[3] - r[1]) * 0.28), fill=255)
        return m

    def _build_lit(self):
        if self.disp is None:
            return
        if self.words and not self.busy:
            self.lit = self.dim.copy()
            self.lit.paste(self.disp, mask=self._mask_for(self.words))
        else:
            self.lit = self.disp

    def _compose(self):
        if self.disp is None:
            return
        out = self.lit
        if self.sel:
            out = out.copy()
            tint = Image.blend(self.disp, Image.new("RGB", self.dsize, SELECT_RGB), 0.5)
            out.paste(tint, mask=self._mask_for([self.words[i] for i in sorted(self.sel)]))
        final = Image.new("RGB", self.dsize, CANVAS_BG)
        final.paste(out, mask=self.round_mask)
        self.tk_img = ImageTk.PhotoImage(final)
        self.canvas.itemconfig(self.img_item, image=self.tk_img)

    def _draw_brackets(self, x0, y0, x1, y1):
        c = self.canvas
        c.delete("brk")
        l, r = sorted((x0, x1))
        t, b = sorted((y0, y1))
        L = max(6, min(int(26 * self.S), (r - l) // 3, (b - t) // 3))
        wd = max(3, int(4 * self.S))
        for (px, py, dx, dy) in ((l, t, 1, 1), (r, t, -1, 1), (l, b, 1, -1), (r, b, -1, -1)):
            c.create_line(px + dx * L, py, px, py, px, py + dy * L, fill="white", width=wd,
                          capstyle=tk.ROUND, joinstyle=tk.ROUND, tags="brk")

    def _clear_brackets(self):
        self.canvas.delete("brk")

    # -------------------------------------------------------------- toasts
    def toast(self, msg, ms=2200):
        c = self.canvas
        c.delete("toast")
        if self.toast_job:
            self.after_cancel(self.toast_job)
        f = tkfont.Font(family="Segoe UI Semibold", size=10)
        w, h = f.measure(msg) + int(40 * self.S), int(38 * self.S)
        cx, y = c.winfo_width() // 2, c.winfo_height() - int(28 * self.S)
        self._toast_img = ImageTk.PhotoImage(pill_image(w, h, (60, 64, 67, 245)))
        c.create_image(cx, y, image=self._toast_img, tags="toast")
        c.create_text(cx, y, text=msg, fill=TEXT, font=f, tags="toast")
        self.toast_job = self.after(ms, lambda: c.delete("toast"))

    # -------------------------------------------------------------- mouse
    def _to_disp(self, x, y):
        return x - self.offset[0], y - self.offset[1]

    def _word_at(self, x, y):
        dx, dy = self._to_disp(x, y)
        for i, w in enumerate(self.words):
            x0, y0, x1, y1 = self._wrect(w)
            if x0 <= dx <= x1 and y0 <= dy <= y1:
                return i
        return None

    def _nearest_word(self, x, y):
        dx, dy = self._to_disp(x, y)
        best, bd = None, None
        for i, w in enumerate(self.words):
            x0, y0, x1, y1 = self._wrect(w)
            ddx = max(x0 - dx, 0, dx - x1)
            ddy = max(y0 - dy, 0, dy - y1)
            if w.get("rot") in (90, 270):  # prefer staying on the same line
                d = (ddx * 3) ** 2 + ddy * ddy
            else:
                d = ddx * ddx + (ddy * 3) ** 2
            if bd is None or d < bd:
                best, bd = i, d
        return best

    def _on_hover(self, e):
        if self.image is None:
            self.canvas.configure(cursor="hand2")
        elif self.busy:
            self.canvas.configure(cursor="watch")
        else:
            self.canvas.configure(cursor="xterm" if self._word_at(e.x, e.y) is not None else "crosshair")

    def _on_press(self, e):
        if self.image is None:
            self.paste()
            return
        if self.busy:
            return
        self.canvas.focus_set()
        self._clear_brackets()
        i = self._word_at(e.x, e.y)
        if i is not None:
            self.mode, self.anchor = "text", i
            self._set_sel({i})
        else:
            self.mode, self.box_start = "box", (e.x, e.y)

    def _on_drag(self, e):
        if self.mode == "text":
            j = self._word_at(e.x, e.y)
            if j is None:
                j = self._nearest_word(e.x, e.y)
            if j is not None:
                a, b = sorted((self.anchor, j))
                self._set_sel(set(range(a, b + 1)))
        elif self.mode == "box":
            x0, y0 = self.box_start
            if abs(e.x - x0) + abs(e.y - y0) < 6:
                return
            self._draw_brackets(x0, y0, e.x, e.y)
            l, r = sorted((x0, e.x))
            t, b = sorted((y0, e.y))
            (l, t), (r, b) = self._to_disp(l, t), self._to_disp(r, b)
            inside = set()
            for i, w in enumerate(self.words):
                x0w, y0w, x1w, y1w = self._wrect(w, pad=False)
                if l <= (x0w + x1w) / 2 <= r and t <= (y0w + y1w) / 2 <= b:
                    inside.add(i)
            self._set_sel(inside)

    def _on_release(self, e):
        mode, self.mode = self.mode, None
        if mode == "box":
            x0, y0 = self.box_start
            if abs(e.x - x0) + abs(e.y - y0) < 6:
                self._set_sel(set())
                return
            if not self.sel:
                box = self._box_to_image(x0, y0, e.x, e.y)
                if box:
                    self.run_ocr(self.image.crop(box), self._crop_done, fn=ocr_crop)
                return
        if self.sel and mode:
            self.copy_text(quiet_if_empty=True)

    def _on_double(self, e):
        i = self._word_at(e.x, e.y)
        if i is None:
            return
        line = self.words[i]["line"]
        self._set_sel({k for k, w in enumerate(self.words) if w["line"] == line})
        self.copy_text()

    def _box_to_image(self, x0, y0, x1, y1):
        (x0, y0), (x1, y1) = self._to_disp(x0, y0), self._to_disp(x1, y1)
        l, r = sorted((x0 / self.scale, x1 / self.scale))
        t, b = sorted((y0 / self.scale, y1 / self.scale))
        l, t = max(0, int(l)), max(0, int(t))
        r, b = min(self.image.width, int(r + 1)), min(self.image.height, int(b + 1))
        return (l, t, r, b) if r - l > 2 and b - t > 2 else None

    # -------------------------------------------------------------- selection
    def _set_sel(self, s):
        if s != self.sel:
            self.sel = s
            self._compose()
            self._update_sheet()

    def _update_sheet(self):
        self.text.delete("1.0", tk.END)
        if self.sel:
            chosen = [self.words[i] for i in sorted(self.sel)]
            if len(chosen) == len(self.words):  # whole image: keep OCR's block order
                self.text.insert("1.0", words_to_text(chosen))
            else:
                self.text.insert("1.0", words_to_rows_text(chosen, self.skew))
            n = len(chosen)
            self.sheet_title.set(f"{n} word{'s' if n != 1 else ''} selected")
        elif self.words:
            self.text.insert("1.0", words_to_text(self.words))
            self.sheet_title.set(f"All text · {len(self.words)} words  —  click or drag on the image to select")
        elif self.image is not None and not self.busy:
            self.sheet_title.set("Still looking…" if self.enhancing
                                 else "No text found — try dragging a box around the text")
        if self.enhancing and self.words:
            self.sheet_title.set(self.sheet_title.get() + "  · finding more text…")

    def select_all(self):
        if self.words:
            self._clear_brackets()
            self._set_sel(set(range(len(self.words))))
            self.copy_text()

    def copy_text(self, quiet_if_empty=False):
        txt = self.text.get("1.0", tk.END).strip()
        if not txt:
            if not quiet_if_empty:
                self.toast("Nothing to copy yet")
            return
        self.clipboard_clear()
        self.clipboard_append(txt)
        self.update_idletasks()
        self.toast("Copied to clipboard", 1500)

    # -------------------------------------------------------------- OCR runner
    def run_ocr(self, img, callback, fn=None):
        fn = fn or ocr_image
        self.busy = True
        self._build_lit()
        self._compose()
        self.sheet_title.set("Looking for text…")
        self.scan_y = 0
        self._scan()
        lang = self.lang.get()
        result = {}

        def work():
            try:
                result["v"], result["err"] = fn(img, lang), None
            except Exception as ex:
                result["v"], result["err"] = ([], ""), ex

        t = threading.Thread(target=work, daemon=True)
        t.start()

        def check():
            if t.is_alive():
                self.after(40, check)
                return
            self.busy = False
            self.canvas.delete("scan")
            if result["err"]:
                self.sheet_title.set(f"OCR failed: {result['err']}")
                self._build_lit()
                self._compose()
                return
            callback(*result["v"])

        self.after(40, check)

    def _scan(self):
        c = self.canvas
        c.delete("scan")
        if not self.busy or self.disp is None:
            return
        ox, oy = self.offset
        dw, dh = self.dsize
        y = oy + self.scan_y
        for k, col in enumerate(("#8ab4f8", "#6a8fc7", "#4c6a96", "#344a68", "#243246")):
            yy = y - k * 3
            if oy <= yy <= oy + dh:
                c.create_line(ox, yy, ox + dw, yy, fill=col, width=3, tags="scan")
        self.scan_y = (self.scan_y + max(4, dh // 70)) % (dh + 15)
        self.after(16, self._scan)

    def _full_done(self, words, text):
        self.words = words
        self.skew = estimate_skew(words)
        self.sel = set()
        self._build_lit()
        self._compose()
        self._update_sheet()
        if words:
            self.toast(f"Found {len(words)} words — click, drag, or Select all")
        elif not get_rapid():
            self.toast("No text found")
        self._start_enhance()

    def _start_enhance(self):
        """Second pass with the extra engine, in the background; results merge in when ready."""
        if _rapid is False or self.image is None:
            return
        gen, img, lang = self.gen, self.image, self.lang.get()
        copies = [dict(w) for w in self.words]
        old_index = {id(w): i for i, w in enumerate(copies)}
        result = {}
        self.enhancing = True
        self._update_sheet()

        def work():
            try:
                result["v"] = enhance_words(img, copies, lang)
            except Exception:
                result["v"] = None

        t = threading.Thread(target=work, daemon=True)
        t.start()

        def check():
            if t.is_alive():
                self.after(80, check)
                return
            if gen != self.gen:  # a different image was loaded or cleared meanwhile
                return
            self.enhancing = False
            new = result["v"]
            added = len(new) - len(self.words) if new else 0
            if new is not None and new is not copies:
                remap = {old_index[id(w)]: ni for ni, w in enumerate(new) if id(w) in old_index}
                self.sel = {remap[i] for i in self.sel if i in remap}
                self.words = new
                self.skew = estimate_skew(new)
                self._build_lit()
                self._compose()
                if added > 0:
                    self.toast(f"Found {added} more word{'s' if added != 1 else ''}")
            elif not self.words:
                self.toast("No text found")
            if not self.sel:
                self._update_sheet()
            else:
                self.sheet_title.set(self.sheet_title.get().replace("  · finding more text…", ""))

        self.after(80, check)

    def _crop_done(self, words, text):
        self._build_lit()
        self._compose()
        self.text.delete("1.0", tk.END)
        if text:
            self.text.insert("1.0", text)
            self.sheet_title.set("Text from selected area")
            self.copy_text()
        else:
            self.sheet_title.set("No text in that area")
            self._clear_brackets()


def main():
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app = LocalLens()
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        app.after(200, lambda: app._load_path(sys.argv[1]))
    app.mainloop()


if __name__ == "__main__":
    main()
