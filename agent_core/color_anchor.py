"""顏色錨定：把樣品單上的顏色（Pantone 色號／文字敘述）落成確定的 sRGB，供生圖與驗證。

為什麼需要這層（2026-09-01 大王「Pantone 要正確、文字敘述顏色也要準」）：
圖生圖模型看不懂「PANTONE 18-1140 TCX」也拿捏不準「深寶藍」——顏色以文字進
prompt 就是在讓模型猜。治本是：

  1. **先解析成 hex**（本模組，確定性、零 LLM）：Pantone 色號查內建近似表
     （TCX 服裝家飾系＋PMS 印刷系，兩張表）
     > 客戶色彙表（var/state/customer_colors.json，同一個詞各客戶定義不同）
     > 內建中英文色名表 > 都查不到才留給呼叫端用 LLM 推估。
  2. **畫成色票條**（render_swatch_strip）當第二張輸入圖餵圖生圖——模型「抄」
     眼前的色塊，遠比理解色號文字準（同 #297 線稿鎖形狀的思路：能給圖就不要給字）。
  3. **事後 ΔE 驗證**（find_color_misses）：成品照抽主色、逐部位比 CIE76 色差，
     超容差讓呼叫端帶糾正語重生（同 #351 OCR 驗數字＋重試的模式）。

⚠️ 誠實界線：生成模型不是色度計，這裡做到的是「螢幕上感知接近＋量化驗證」；
正式對色簽核仍以實體色卡為準（agent_core/data/README.md 有寫）。
"""
import json
import os
import re

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

# TCX 色號長相：11-0103 ~ 19-xxxx（首兩碼 11-19 = 明度分群）。TPG/TPX 同號同色系，
# 一律以 TCX 近似值錨定（差異遠小於生成模型本身的變異）。
_PANTONE_CODE_RE = re.compile(r"\b(1[1-9]-\d{4})\b")
# PMS（印刷 Solid）色號長相：3~5 位數＋C/U 後綴（286C、2925 U、10101 C），CP/UP
# （Color Bridge）同號視同 C/U。後綴省略但有 PANTONE/PMS 開頭字樣時預設 coated。
# 命名色（Cool Gray 9 C、Reflex Blue…）另走 _pms_named_lookup，且必須點名
# PANTONE/PMS 才比對——不然 "black"/"yellow" 這種常字會誤傷一般敘述。
_PMS_SUFFIXED_RE = re.compile(r"\b(\d{3,5})\s*(cp?|up?)\b")
_PMS_PREFIXED_RE = re.compile(r"\b(?:pantone|pms)\s*#?\s*(\d{3,5})\b")
_PMS_CONTEXT_RE = re.compile(r"\b(?:pantone|pms)\b")
_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")

_pantone_cache: dict | None = None
_pms_cache: dict | None = None
_pms_named_cache: list | None = None
_names_cache: list | None = None


def _pantone_table() -> dict:
    global _pantone_cache
    if _pantone_cache is None:
        try:
            with open(os.path.join(_DATA_DIR, "pantone_tcx.json"), encoding="utf-8") as f:
                _pantone_cache = json.load(f)
        except (OSError, ValueError):
            _pantone_cache = {}
    return _pantone_cache


def _pms_table() -> dict:
    """PMS 對照表：{"286-c": "0033a0", "cool-gray-9-c": "75787b", ...}（後綴必為 -c/-u）。"""
    global _pms_cache
    if _pms_cache is None:
        try:
            with open(os.path.join(_DATA_DIR, "pantone_pms.json"), encoding="utf-8") as f:
                _pms_cache = json.load(f)
        except (OSError, ValueError):
            _pms_cache = {}
    return _pms_cache


def _pms_named() -> list:
    """PMS 命名色 [(帶空格 base, 表 key base)]，base 長度降冪——「black 2」要贏過「black」。"""
    global _pms_named_cache
    if _pms_named_cache is None:
        bases = {k[:-2] for k in _pms_table() if not k[:1].isdigit()}
        _pms_named_cache = sorted(((b.replace("-", " "), b) for b in bases),
                                  key=lambda t: -len(t[0]))
    return _pms_named_cache


def _pms_numeric_lookup(low: str) -> dict | None:
    """PMS 數字碼（286C / PANTONE 485）→ hex；查不到回 None。排位由呼叫端決定。

    U 查無時退 coated（同號 C/U 是同一油墨在不同紙上的樣子，對生圖錨定而言
    coated 近似值遠勝 LLM 瞎猜）。
    """
    table = _pms_table()
    if not table:
        return None
    cands = [(m.group(1), "u" if m.group(2).startswith("u") else "c")
             for m in _PMS_SUFFIXED_RE.finditer(low)]
    cands += [(m.group(1), "c") for m in _PMS_PREFIXED_RE.finditer(low)]
    for num, suffix in cands:
        for suf in dict.fromkeys((suffix, "c")):
            hexv = table.get(f"{num}-{suf}")
            if hexv:
                return {"hex": hexv.lower(), "label": f"Pantone {num} {suf.upper()}",
                        "source": "pantone"}
    return None


def _pms_named_lookup(low: str, has_context: bool) -> dict | None:
    """PMS 命名色（Cool Gray 9 C / Reflex Blue C…）→ hex；查不到回 None。

    無 PANTONE/PMS 字樣時只認「多字 base＋明確 C/U 後綴」：單字 base
    （black/green/yellow…）是常用色詞，無語境必誤傷一般敘述（#468 原則）；
    多字 base 帶後綴（REFLEX BLUE C）夠獨特，規格欄常只寫這樣——之前一律
    要求語境會讓它落到內建通用色（實測 ΔE 34.7 的錯錨還掛確定性 source）。
    """
    table = _pms_table()
    if not table:
        return None
    # 數字黏字母要切開（"Cool Gray 9C" → "cool gray 9 c"），base 才對得上
    ntext = re.sub(r"(?<=\d)(?=[a-z])", " ", re.sub(r"[^a-z0-9]+", " ", low))
    ntext = " " + ntext.strip() + " "
    for base_spaced, base_key in _pms_named():
        idx = ntext.find(" " + base_spaced + " ")
        if idx < 0:
            continue
        after = ntext[idx + len(base_spaced) + 2:].split()
        has_suffix = bool(after) and after[0] in ("c", "u", "cp", "up")
        if not has_context and not (has_suffix and " " in base_spaced):
            continue
        suffix = "u" if after and after[0] in ("u", "up") else "c"
        for suf in dict.fromkeys((suffix, "c")):
            hexv = table.get(f"{base_key}-{suf}")
            if hexv:
                return {"hex": hexv.lower(),
                        "label": f"Pantone {base_spaced.title()} {suf.upper()}",
                        "source": "pantone"}
    return None


def _builtin_names() -> list:
    """內建色名表，key 長度降冪排序——「深寶藍」要贏過「寶藍」「藍」。"""
    global _names_cache
    if _names_cache is None:
        try:
            with open(os.path.join(_DATA_DIR, "color_names.json"), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            d = {}
        _names_cache = sorted(d.items(), key=lambda kv: -len(kv[0]))
    return _names_cache


def _customer_glossary() -> dict:
    """客戶色彙表（runtime 檔、可累積更正）：{"richter": {"nude": "e3bc9a"}, "*": {...}}。"""
    try:
        from agent_core.logging_and_paths import STATE_DIR
        with open(os.path.join(STATE_DIR, "customer_colors.json"), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def match_term(text_low: str, key: str) -> bool:
    """詞彙比對（color/material 共用）：中文 key 用子字串；純 ASCII key 加詞界
    （避免 "red" 命中 "bordered"、"pu" 命中 "tpu"）。text_low 需已 lower()。"""
    if key.isascii():
        return re.search(r"(?<![a-z])" + re.escape(key) + r"(?![a-z])", text_low) is not None
    return key in text_low


def resolve_color(color_text: str, customer: str = "") -> dict | None:
    """顏色敘述 → {"hex","label","source"}；全鏈查不到回 None（呼叫端可退 LLM 推估）。

    source: "pantone"（色號查表）/ "glossary"（客戶色彙）/ "builtin"（內建色名表）。

    排位（2026-09-08 review C2/C9 修訂）：TCX 色號 > 有 PANTONE/PMS 字樣的
    數字碼 > 客戶色彙 > PMS 命名色 > 內建色名 > 裸數字碼。裸數字碼（286C 這種
    無語境後綴碼）殿後——3~5 位數＋C/U 也可能是貨號/尺寸（實測「黑色 貨號
    485C」曾被解成 Pantone 485 C 大紅），文內任何解得動的色詞都要贏過它；
    客戶色彙排在 PMS 命名色之前，客戶專屬定義才能更正 green/black 這類弱命名。
    """
    text = str(color_text or "").strip()
    if not text:
        return None
    for code in _PANTONE_CODE_RE.findall(text):
        entry = _pantone_table().get(code)
        if entry:
            return {"hex": entry["hex"].lower(), "label": f"Pantone {code} ({entry['name']})",
                    "source": "pantone"}
    low = text.lower()
    has_ctx = _PMS_CONTEXT_RE.search(low) is not None
    if has_ctx:
        got = _pms_numeric_lookup(low)
        if got:
            return got
    glossary = _customer_glossary()
    for scope in ((customer or "").strip().lower(), "*"):
        entries = glossary.get(scope) if scope else None
        if not isinstance(entries, dict):
            continue
        for key in sorted(entries, key=len, reverse=True):
            m = _HEX_RE.match(str(entries[key]))
            if m and match_term(low, str(key).lower()):
                return {"hex": m.group(1).lower(), "label": f"{key}（客戶色彙）", "source": "glossary"}
    got = _pms_named_lookup(low, has_ctx)
    if got:
        return got
    for key, hexv in _builtin_names():
        if match_term(low, key):
            return {"hex": hexv.lower(), "label": key, "source": "builtin"}
    if not has_ctx:
        got = _pms_numeric_lookup(low)
        if got:
            return got
    return None


def parse_hex(text: str) -> str:
    """"#0F4C81" / "0f4c81" → "0f4c81"；不合法回 ""。（LLM 推估 hex 的守門）"""
    m = _HEX_RE.match(str(text or "").strip())
    return m.group(1).lower() if m else ""


# ── 色彩數學：sRGB → Lab（D65）與 CIE76 ΔE ────────────────────────────
def hex_to_rgb(hexv: str) -> tuple:
    h = parse_hex(hexv)
    if not h:
        raise ValueError(f"非法 hex: {hexv!r}")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_lab(rgb) -> tuple:
    def _lin(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (_lin(c) for c in rgb)
    # sRGB D65
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = (0.2126 * r + 0.7152 * g + 0.0722 * b)
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def _f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    fx, fy, fz = _f(x), _f(y), _f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(lab1, lab2) -> float:
    """CIE76：夠當生成圖的閘門（生成本身的變異遠大於 CIE76 vs CIEDE2000 的差）。"""
    return sum((a - b) ** 2 for a, b in zip(lab1, lab2)) ** 0.5


def dominant_lab_colors(image_path: str, max_colors: int = 12) -> list:
    """成品照的主色盤：[(lab, 佔比), ...]，佔比降冪；讀不了圖回 []（絕不 raise）。"""
    try:
        from PIL import Image
        with Image.open(image_path) as raw:
            im = raw.convert("RGB")
        im.thumbnail((256, 256))
        q = im.quantize(colors=max_colors, method=Image.Quantize.MEDIANCUT)
        palette = q.getpalette()
        counts = sorted(q.getcolors(maxcolors=max_colors * 2) or [], reverse=True)
        total = float(sum(n for n, _ in counts)) or 1.0
        out = []
        for n, idx in counts:
            frac = n / total
            if frac < 0.005:
                continue
            rgb = tuple(palette[idx * 3:idx * 3 + 3])
            out.append((rgb_to_lab(rgb), frac))
        return out
    except Exception:  # noqa: BLE001
        return []


def dominant_hex_colors(image_path: str, max_colors: int = 10, min_frac: float = 0.04) -> list:
    """圖片主色盤（hex）：[(hex, 佔比)] 佔比降冪、剔近白（背景/白底）；失敗回 []。

    用途（2026-09-01 PSS 8 案）：客人「上色完稿」本身就是最權威的配色來源——
    抽完稿主色當 ΔE 驗證目標，渲染成品不能偏離客人自己上的色。
    """
    try:
        from PIL import Image
        with Image.open(image_path) as raw:
            im = raw.convert("RGB")
        im.thumbnail((256, 256))
        q = im.quantize(colors=max_colors, method=Image.Quantize.MEDIANCUT)
        palette = q.getpalette()
        counts = sorted(q.getcolors(maxcolors=max_colors * 2) or [], reverse=True)
        total = float(sum(n for n, _ in counts)) or 1.0
        out = []
        for n, idx in counts:
            frac = n / total
            if frac < min_frac:
                continue
            rgb = tuple(palette[idx * 3:idx * 3 + 3])
            if _is_whitish(rgb_to_lab(rgb)):
                continue
            out.append(("%02x%02x%02x" % rgb, round(frac, 3)))
        return out
    except Exception:  # noqa: BLE001
        return []


def _chroma_clusters(image_path: str, min_frac: float, min_chroma: float) -> list:
    """高彩度色塊叢集：[(rgb, lab, 佔比)] 佔比降冪、無數量上限；失敗回 []。

    RGB bucket 聚後逐 bucket 算 Lab，彩度夠的依像素數起叢集（ΔE ≤ 12 就近
    吸收反鋸齒鄰色），對全圖佔比 ≥ min_frac 才算數。accent_hex_colors 與
    find_color_misses 的小色塊補盤共用這一段（抽取與驗證必須同一把尺）。
    """
    try:
        from PIL import Image
        with Image.open(image_path) as raw:
            im = raw.convert("RGB")
        im.thumbnail((384, 384))
        total = float(im.width * im.height) or 1.0
        buckets: dict = {}
        for n, (r, g, b) in im.getcolors(maxcolors=im.width * im.height) or []:
            key = (r >> 4, g >> 4, b >> 4)
            acc = buckets.get(key)
            if acc is None:
                buckets[key] = [n, r * n, g * n, b * n]
            else:
                acc[0] += n
                acc[1] += r * n
                acc[2] += g * n
                acc[3] += b * n
        cands = []
        for n, rs, gs, bs in buckets.values():
            rgb = (rs // n, gs // n, bs // n)
            lab = rgb_to_lab(rgb)
            if not 8.0 < lab[0] < 92.0:
                continue
            if (lab[1] * lab[1] + lab[2] * lab[2]) ** 0.5 < min_chroma:
                continue
            cands.append((n, rgb, lab))
        cands.sort(key=lambda t: -t[0])
        clusters: list = []  # [count, rgb, lab]，rgb 固定為種子（眾數 bucket 的加權均值）
        for n, rgb, lab in cands:
            for c in clusters:
                if delta_e(lab, c[2]) <= 12.0:
                    c[0] += n
                    break
            else:
                clusters.append([n, rgb, lab])
        clusters.sort(key=lambda c: -c[0])
        return [(rgb, lab, n / total) for n, rgb, lab in clusters
                if n / total >= min_frac]
    except Exception:  # noqa: BLE001
        return []


def accent_hex_colors(image_path: str, max_colors: int = 2, min_frac: float = 0.008,
                      min_chroma: float = 24.0) -> list:
    """完稿的高彩度點綴色：[(hex, 佔比)]，佔比降冪；失敗回 []（絕不 raise）。

    dominant_hex_colors 天生看不見小色塊（2026-09-03 PSS 2 案：後套寶藍只佔
    完稿 2.3%，MEDIANCUT 量化直接把它併進深藍鞋身叢集）——這裡改走高彩度
    像素遮罩：深藍鞋身/灰白反光條的 chroma 都在門檻下、客人特別畫上去的亮色
    遠在其上。⚠️ 高彩度「大」色塊（彩色鞋身的明暗叢集）也會入列且排前面
    （2026-09-08 review：max_colors=2 曾被鞋身色階吃光名額）——呼叫端要自己
    對主色去重後再截斷，別直接拿前兩名當點綴色。
    """
    return [("%02x%02x%02x" % rgb, round(frac, 3))
            for rgb, _lab, frac in _chroma_clusters(image_path, min_frac, min_chroma)
            [:max_colors]]


def _is_whitish(lab) -> bool:
    ell, a, b = lab
    return ell > 88 and (a * a + b * b) ** 0.5 < 14


def find_color_misses(image_path: str, targets: list, de_max: float) -> list:
    """驗證每個目標色是否出現在成品照主色盤內；回未達標清單（空 = 全過或無從驗）。

    targets: [{"part": "鞋身", "hex": "0f4c81", ...}]。
    規則：
      - 近白目標跳過（白鞋面 vs 純白背景分不開，驗了只會誤殺）。
      - 對每個目標取「與所有主色的最小 ΔE」，> de_max 才算 miss——部位面積小
        （扣件/包邊）主色盤可能抓不到，所以這是「顏色明顯不對」的閘，不是精密比色。
      - 讀不了圖／算不出主色 → 回 []（不擋交件，同 OCR 那道保險的姿勢）。
    """
    doms = dominant_lab_colors(image_path)
    if not doms:
        return []
    # 小色塊補盤（2026-09-08 review C1）：12 色 MEDIANCUT 對 1–4% 點綴色失明，
    # 正確渲染的後套寶藍會被併進鞋身叢集——實測「拿完稿自驗」都報 ΔE 32.4。
    # 把渲染圖自己的高彩度小色塊叢集併進比對盤（同一把 _chroma_clusters 尺、
    # 不設數量上限故不會被大色塊排擠）；只擴大「找得到」的面、不放寬容差。
    doms = doms + [(lab, frac) for _rgb, lab, frac
                   in _chroma_clusters(image_path, min_frac=0.004, min_chroma=24.0)]
    misses = []
    for t in targets:
        hexv = parse_hex(t.get("hex", ""))
        if not hexv:
            continue
        lab = rgb_to_lab(hex_to_rgb(hexv))
        if _is_whitish(lab):
            continue
        best = min(delta_e(lab, d) for d, _ in doms)
        if best > de_max:
            misses.append({"part": str(t.get("part", "?")), "hex": hexv,
                           "delta_e": round(best, 1)})
    return misses


# ── 色票條：部位色塊圖，當第二張輸入圖餵圖生圖 ──────────────────────
_FONT_CANDIDATES = (
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
)


def _cjk_font(size: int):
    from PIL import ImageFont
    for path in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_swatch_strip(entries: list, out_path: str) -> str:
    """[{"part","hex","label"}] → 一條橫向色票圖（色塊＋部位名＋色名）。失敗回 ""。"""
    entries = [e for e in entries if parse_hex(e.get("hex", ""))][:8]
    if not entries:
        return ""
    try:
        from PIL import Image, ImageDraw
        cell_w, block_h, label_h, pad = 230, 130, 74, 6
        im = Image.new("RGB", (cell_w * len(entries), block_h + label_h), (255, 255, 255))
        draw = ImageDraw.Draw(im)
        font_big, font_small = _cjk_font(26), _cjk_font(17)
        for i, e in enumerate(entries):
            x0 = i * cell_w
            rgb = hex_to_rgb(e["hex"])
            draw.rectangle((x0 + pad, pad, x0 + cell_w - pad, block_h - pad),
                           fill=rgb, outline=(120, 120, 120))
            draw.text((x0 + pad + 4, block_h + 2), str(e.get("part", "?"))[:10],
                      fill=(20, 20, 20), font=font_big)
            draw.text((x0 + pad + 4, block_h + 36), str(e.get("label", ""))[:22],
                      fill=(90, 90, 90), font=font_small)
        im.save(out_path)
        return out_path
    except Exception:  # noqa: BLE001 — 色票畫不出來就退回純文字路徑，不擋生圖
        return ""
