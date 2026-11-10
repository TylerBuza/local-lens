"""Generate the Local Lens logo (assets/logo.png) and Windows icon (local_lens.ico).

Run:  python tools/make_icon.py
"""

import os

from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SS = 4  # supersampling for smooth edges


def gradient(size, c1, c2):
    """Diagonal gradient from top-left c1 to bottom-right c2."""
    w, h = size
    g = Image.new("RGB", (w, h))
    px = g.load()
    for y in range(h):
        for x in range(w):
            t = (x + y) / (w + h - 2)
            px[x, y] = tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))
    return g


def logo(size=1024):
    S = size * SS // 4  # build at 1/4 then scale for speed, still supersampled
    big = S
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))

    # Squircle background with blue -> violet gradient
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, big - 1, big - 1), radius=int(big * 0.23), fill=255)
    bg = gradient((big, big), (59, 130, 246), (124, 58, 237))
    img.paste(bg, (0, 0), mask)

    # Soft top highlight
    hl = Image.new("L", (big, big), 0)
    ImageDraw.Draw(hl).ellipse((-big * 0.3, -big * 0.75, big * 1.3, big * 0.45), fill=28)
    hl = hl.filter(ImageFilter.GaussianBlur(big * 0.12))
    white = Image.new("RGBA", (big, big), (255, 255, 255, 255))
    img.paste(white, (0, 0), Image.composite(hl, Image.new("L", (big, big), 0), mask))

    d = ImageDraw.Draw(img)
    # Corner brackets (the "lens" selection)
    m, L, wd = big * 0.2, big * 0.17, int(big * 0.065)
    r = wd // 2
    for (px, py, dx, dy) in ((m, m, 1, 1), (big - m, m, -1, 1), (m, big - m, 1, -1), (big - m, big - m, -1, -1)):
        d.line([(px + dx * L, py), (px, py), (px, py + dy * L)], fill="white", width=wd, joint="curve")
        for cx, cy in ((px + dx * L, py), (px, py + dy * L), (px, py)):
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill="white")

    # Text lines inside, the first one "selected"
    lh = big * 0.07
    rows = [(0.35, 0.65, True), (0.35, 0.61, False), (0.35, 0.52, False)]
    y = big * 0.36
    glow = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for x0, x1, sel in rows:
        box = (big * x0, y, big * x1, y + lh)
        if sel:  # translucent "selected" highlight behind the first line
            pad = lh * 0.55
            gd.rounded_rectangle((box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad),
                                 radius=lh * 0.7, fill=(255, 255, 255, 60))
        y += lh * 2.0
    img = Image.alpha_composite(img, glow)
    d = ImageDraw.Draw(img)
    y = big * 0.36
    for x0, x1, sel in rows:
        d.rounded_rectangle((big * x0, y, big * x1, y + lh), radius=lh / 2, fill="white")
        y += lh * 2.0

    return img.resize((size, size), Image.LANCZOS)


def main():
    os.makedirs(os.path.join(ROOT, "assets"), exist_ok=True)
    big = logo(1024)
    big.save(os.path.join(ROOT, "assets", "logo.png"))
    big.resize((256, 256), Image.LANCZOS).save(
        os.path.join(ROOT, "local_lens.ico"),
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote assets/logo.png and local_lens.ico")


if __name__ == "__main__":
    main()
