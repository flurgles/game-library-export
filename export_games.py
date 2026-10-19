#!/usr/bin/env python3
"""Export owned games from Heroic (Epic/GOG/Amazon) + Steam to plain-text lists.

Skips DLC, demos, tools, soundtracks, editors, test branches and unnamed/unlisted
entries. Fully offline: reads Heroic's store caches and Steam's local files only.

Usage: export_games.py [output_dir]   (default: script's directory)
"""

import glob, json, re, struct, sys
from pathlib import Path

HOME = Path.home()
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent


def write(fname, titles):
    t = sorted({x.strip() for x in titles if x and x.strip()}, key=str.lower)
    (OUT / fname).write_text("\n".join(t) + "\n", encoding="utf-8")
    print(f"{fname}: {len(t)} games")


# ====== Heroic (Epic/GOG/Amazon) ======
def heroic_cache():
    for base in (
        HOME / ".var/app/com.heroicgameslauncher.hgl/config/heroic",
        HOME / ".config/heroic",
    ):
        if (base / "store_cache").is_dir():
            return base / "store_cache"
    raise SystemExit("Heroic store_cache not found")


NOT_A_GAME = re.compile(
    r"\bsoundtrack\b|\beditor\b|public testing|test branch|\bdemo\b", re.I
)


def heroic_games(fname, key):
    lib = json.loads((heroic_cache() / fname).read_text())[key]
    return [
        e["title"]
        for e in lib
        if e.get("title")
        and not e.get("install", {}).get("is_dlc")
        and not NOT_A_GAME.search(e["title"])
    ]


write("epic_games.txt", heroic_games("legendary_library.json", "library"))
write("gog_games.txt", heroic_games("gog_library.json", "games"))
write("amazon_games.txt", heroic_games("nile_library.json", "library"))


# ============== Steam (offline) ============
def vdf(text):  # tiny text-VDF parser (tolerates comments, top-level pairs)
    i, n = 0, len(text)

    def tk():
        nonlocal i
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i + 1 < n and text[i] == "/" and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j < 0 else j + 1
            return tk()
        if i >= n:
            return None
        if text[i] == '"':
            i += 1
            buf = []
            while text[i] != '"':
                if text[i] == "\\":
                    i += 1
                buf.append(text[i])
                i += 1
            i += 1
            return "".join(buf)
        c = text[i]
        i += 1
        return c

    def obj():
        d = {}
        while True:
            k = tk()
            if k is None or k == "}":
                return d
            v = tk()
            d[k] = obj() if v == "{" else v

    return obj()


def appinfo(path):  # binary VDF v41 -> {appid: (name, type)}
    b = path.read_bytes()
    st = struct.unpack_from("<q", b, 8)[0]
    n = struct.unpack_from("<I", b, st)[0]
    p = st + 4
    keys = []
    for _ in range(n):
        e = b.index(b"\0", p)
        keys.append(b[p:e].decode("utf-8", "replace"))
        p = e + 1

    def kv(p, end, d):
        while p < end:
            t = b[p]
            p += 1
            if t in (8, 11):
                break
            k = keys[struct.unpack_from("<i", b, p)[0]]
            p += 4
            if t == 0:
                d[k] = {}
                p = kv(p, end, d[k])
            elif t == 1:
                e = b.index(b"\0", p)
                d[k] = b[p:e].decode("utf-8", "replace")
                p = e + 1
            elif t in (2, 4):
                d[k] = struct.unpack_from("<i", b, p)[0]
                p += 4
            elif t == 3:
                d[k] = struct.unpack_from("<f", b, p)[0]
                p += 4
            elif t in (7, 10):
                d[k] = struct.unpack_from("<q", b, p)[0]
                p += 8
            else:
                raise ValueError(t)
        return p

    apps, p = {}, 16
    while p + 8 <= len(b):
        aid = struct.unpack_from("<I", b, p)[0]
        if aid == 0:
            break
        size = struct.unpack_from("<I", b, p + 4)[0]
        try:
            d = {}
            kv(p + 68, p + 8 + size, d)  # +60 bytes of entry metadata
            common = d.get("appinfo", d).get("common", {})
            apps[str(aid)] = (common.get("name", ""), common.get("type", ""))
        except Exception:
            pass
        p += 8 + size
    return apps


STEAM = HOME / ".steam/steam"
if not STEAM.exists():
    STEAM = HOME / ".local/share/Steam"

meta = appinfo(STEAM / "appcache/appinfo.vdf")
ids, fallback = set(), {}

lf = vdf((STEAM / "steamapps/libraryfolders.vdf").read_text(errors="replace"))
for lib in lf.get("libraryfolders", {}).values():
    if isinstance(lib, dict) and "path" in lib:
        for acf in glob.glob(lib["path"] + "/steamapps/appmanifest_*.acf"):
            a = vdf(Path(acf).read_text(errors="replace")).get("AppState", {})
            ids.add(a.get("appid", ""))
            fallback[a.get("appid", "")] = a.get("name", "")

ud = next(
    d for d in (STEAM / "userdata").iterdir() if (d / "config/localconfig.vdf").exists()
)
lcroot = vdf((ud / "config/localconfig.vdf").read_text(errors="replace"))
used = lcroot["UserLocalConfigStore"]["Software"]["Valve"]["Steam"]["apps"]
ids |= set(used)
for a, v in used.items():
    if isinstance(v, dict) and v.get("name"):
        fallback.setdefault(a, v["name"])

ids |= {f.stem for f in (ud / "config/librarycache").glob("*.json") if f.stem.isdigit()}
ids |= {
    m.group()
    for f in (ud / "config/grid").glob("*.*")
    if (m := re.match(r"\d+", f.name))
}
ids = {a for a in ids if a.isdigit() and 0 < int(a) < 100_000_000}

SKIP_TYPES = {"tool", "demo", "config", "music", "video", "application", "beta", "dlc"}
SKIP_IDS = {"1675200"}  # Steam Deck hardware


def game_of(a):
    name, typ = meta.get(a, ("", ""))
    name = name or fallback.get(a, "")
    if not name or a in SKIP_IDS or typ.lower() in SKIP_TYPES:
        return None
    if not typ and re.search(r"\bdemo\b", name, re.I):
        return None
    return name


write("steam_games.txt", [g for a in ids if (g := game_of(a))])
