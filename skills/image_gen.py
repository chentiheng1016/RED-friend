"""Image generation skill — 用 Gemini 的 image model 生成圖片。

適合用途：
  - 產品設計草圖（「幫我畫一款黑色工作靴的側面示意」）
  - 行銷素材（「幫我畫張類似 Blaklader 風格的防滑鞋底特寫」）
  - 簡報配圖
  - 產品上架圖的 mockup

模型：nano-banana-pro-preview（Gemini 自家生圖模型）。

⚠️ 版權：生成圖用於公司內部討論 / 草圖沒問題，實際廣告 / 公開發布前
應該再人工確認。
"""
import base64
import os
from datetime import datetime


def _default_output_dir() -> str:
    from agent_core.logging_and_paths import GENERATED_IMAGES_DIR
    d = GENERATED_IMAGES_DIR
    os.makedirs(d, exist_ok=True)
    return d


def _save_image_bytes(img_bytes: bytes, output_path: str = "") -> str:
    """寫圖到 output_path 或 default dir。被 V7 path-safety 擋會 raise ValueError。"""
    from agent_core.path_safety import safe_path
    if not output_path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = os.path.join(_default_output_dir(), f"gen_{ts}.png")
    out = safe_path(output_path)
    # 確保目錄存在
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "wb") as f:
        f.write(img_bytes)
    return out


def _deliver_to_telegram(paths, caption: str = "") -> str:
    """交付生成圖；回「要附在工具回覆末尾的標記文字」（推送路徑回空字串）。

    Telegram 對話 context（channel_context —— 大王在任一色 bot 對話發起）→
    改走 [[TG_PHOTO:]] 回覆附圖：daemon 用當前 bot token + 當前 chat_id 把圖
    送回發問的那個對話（2026-09-02 green 聊天室案：出站推送只認 keyring 預設
    chat，圖會從紅 bot 主對話冒出來）。其餘（REPL / 背景 daemon）→ 維持
    best-effort telegram_send_photo 出站推送（chat_id 空=大王預設）、失敗不拋。

    ⚠️ 大王的標記白名單只放行 generated_images 底下（daemon_telegram.
    _reply_photo_allowed_roots）：自訂 output_path 落在外面的照樣走推送，
    不然標記會被靜默丟棄、圖誰都收不到。
    """
    items = [p for p in (paths if isinstance(paths, list) else [paths]) if p]
    in_tg_chat = False
    try:
        from agent_core.channel_context import reply_consumes_tg_markers
        in_tg_chat = reply_consumes_tg_markers()
    except Exception:  # noqa: BLE001
        in_tg_chat = False
    marked: list = []
    to_push = items
    if in_tg_chat:
        try:
            from agent_core.logging_and_paths import GENERATED_IMAGES_DIR
            root = os.path.realpath(GENERATED_IMAGES_DIR)
            marked = [p for p in items
                      if os.path.realpath(p).startswith(root + os.sep)]
        except Exception:  # noqa: BLE001
            marked = []
        to_push = [p for p in items if p not in marked]
    for p in to_push:
        try:
            from agent_core.telegram import telegram_send_photo
            telegram_send_photo(p, caption=caption[:900], chat_id="")
        except Exception:  # noqa: BLE001
            pass
    if not marked:
        return ""
    return ("\n" + "\n".join(f"[[TG_PHOTO:{p}]]" for p in marked)
            + "\n⚠️ 回覆時請把上面 [[TG_PHOTO:...]] 標記行原樣保留在回覆最後，"
              "系統會自動把圖傳到當前對話（標記本身不會顯示）。")


def generate_image(prompt: str, output_path: str = "",
                   model: str = "gemini-2.5-flash-image",
                   aspect_ratio: str = "1:1") -> str:
    """用 Gemini 生成一張圖片並存檔。

    Args:
        prompt: 描述你想要的圖片（中英文都行，英文通常生得較準）。
                範例："黑色皮革工作鞋側面，白背景，專業產品攝影風格"
        output_path: 要存到哪裡；空字串 = generated_images/gen_{timestamp}.png
        model: 生圖模型，可用：
               - "gemini-2.5-flash-image" （預設，快+準）
               - "nano-banana-pro-preview" （細節更好，慢一點）
               - "imagen-4.0-generate-001"（Imagen 4，另一種風格）
        aspect_ratio: 比例，可用 "1:1" / "16:9" / "9:16" / "4:3" / "3:4"
    Returns:
        存檔位置 + 大小。
    """
    if not prompt.strip():
        return "❌ prompt 不能空"

    try:
        from agent_core.gemini_client import generate_content_tracked
        from google.genai import types as T
    except Exception as e:
        return f"❌ Gemini SDK 匯入失敗: {e}"

    try:
        # image generation 用 generate_content 路徑，config 指定 response_modalities
        # （Gemini 2.5 flash image 支援 text+image response）
        response = generate_content_tracked(
            model=model,
            contents=[prompt],
            config=T.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
                image_config=T.ImageConfig(aspect_ratio=aspect_ratio),
            ),
            caller="image_gen.generate_image",
        )
    except Exception as e:
        return f"❌ 生圖失敗 ({model}): {type(e).__name__}: {str(e)[:400]}"

    # 從 response 抽 image bytes
    saved = None
    text_parts = []
    try:
        for cand in (response.candidates or []):
            for part in (cand.content.parts or []):
                if hasattr(part, "inline_data") and part.inline_data and part.inline_data.data:
                    img_bytes = part.inline_data.data
                    # inline_data.data 可能已經是 bytes 或 base64
                    if isinstance(img_bytes, str):
                        img_bytes = base64.b64decode(img_bytes)
                    saved = _save_image_bytes(img_bytes, output_path)
                elif hasattr(part, "text") and part.text:
                    text_parts.append(part.text)
    except ValueError as e:
        # path safety 擋下：直接回 user
        return str(e)
    except Exception as e:
        return f"❌ 解析 response 失敗: {type(e).__name__}: {e}"

    if not saved:
        return f"❌ Gemini 沒回圖（只有文字）：{' '.join(text_parts)[:300]}"

    size = os.path.getsize(saved)
    out = f"✅ 已生成圖片：{saved}\n   大小：{size:,} bytes，模型：{model}，比例：{aspect_ratio}"
    if text_parts:
        out += f"\n   Gemini 附註：{''.join(text_parts)[:200]}"
    return out


def edit_image(image_path: str, instruction: str,
               output_path: str = "",
               model: str = "gemini-2.5-flash-image") -> str:
    """用 Gemini 編輯既有圖片（擦背景、改顏色、加物件等）。

    Args:
        image_path: 來源圖片（png/jpg）。
        instruction: 要怎麼改，例如：
                     - "把背景改成純白"
                     - "加一個紅色 SALE 標籤"
                     - "去掉水印"
        output_path: 要存到哪；空字串 = generated_images/edit_{timestamp}.png
        model: 預設 gemini-2.5-flash-image（支援 image+text → image）
    """
    try:
        from agent_core.path_safety import safe_path
        src = safe_path(image_path)
    except ValueError as e:
        return str(e)
    if not os.path.isfile(src):
        return f"❌ 找不到圖片：{src}"

    try:
        from agent_core.gemini_client import generate_content_tracked
        from google.genai import types as T
        from PIL import Image
    except Exception as e:
        return f"❌ SDK 匯入失敗: {e}"

    try:
        img = Image.open(src)
        response = generate_content_tracked(
            model=model,
            contents=[img, instruction],
            config=T.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
            ),
            caller="image_gen.edit_image",
        )
    except Exception as e:
        return f"❌ 編輯失敗: {type(e).__name__}: {str(e)[:300]}"

    # 抽 response
    saved = None
    text_parts = []
    try:
        for cand in (response.candidates or []):
            for part in (cand.content.parts or []):
                if hasattr(part, "inline_data") and part.inline_data and part.inline_data.data:
                    data = part.inline_data.data
                    if isinstance(data, str):
                        data = base64.b64decode(data)
                    if not output_path:
                        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                        output_path = os.path.join(_default_output_dir(), f"edit_{ts}.png")
                    saved = _save_image_bytes(data, output_path)
                elif hasattr(part, "text") and part.text:
                    text_parts.append(part.text)
    except ValueError as e:
        # path safety 擋下：直接回 user，不要被當 generic 解析錯誤
        return str(e)
    except Exception as e:
        return f"❌ 解析 response 失敗: {type(e).__name__}: {e}"

    if not saved:
        return f"❌ 沒回圖，文字：{' '.join(text_parts)[:300]}"

    size = os.path.getsize(saved)
    out = f"✅ 已編輯：{saved}（{size:,} bytes）"
    if text_parts:
        out += f"\n   Gemini 說：{''.join(text_parts)[:200]}"
    return out


def generate_product_concept(order_description: str, reference_file_id: str = "",
                             output_path: str = "", aspect_ratio: str = "4:3",
                             model: str = "nano-banana-pro-preview", views: str = "side") -> str:
    """從客人樣單生成產品概念圖。有舊款參考圖時走「改款鎖版型」——保留楦型/鞋底/比例，只改指定項，最貼近工廠實際做得出來的款。

    建議流程：先用 search_product_photos 找出最相似的舊款 → 把它的 Drive file_id 傳進 reference_file_id →
    本工具鎖住那款的版型、只依樣單需求改顏色/材質/配件，生成概念圖。⚠️ 產出是「概念視覺化」供打樣前溝通，非生產精準圖。

    Args:
        order_description: 客人樣單的描述（鞋型/顏色/材質/特徵/用途），中英文皆可。
        reference_file_id:（強烈建議）最相似舊款的 Drive file_id，走改款鎖版型模式；空 = 純文字全新款。
        output_path: 存到哪；空 = generated_images/concept_{timestamp}.png。多視角時自動加 _1/_2/_3。
        aspect_ratio: 比例，預設 "4:3"（鞋類橫式）。
        model: 生圖模型，預設 nano-banana-pro-preview（細節最好）；可退 "gemini-2.5-flash-image"。
        views: "side" 單張側面 / "multi" 側面+45度+俯視三張（3 倍成本、方便挑一致視角）。
    """
    import base64
    import io
    from datetime import datetime

    if not order_description.strip():
        return "❌ order_description 不能空"
    try:
        from agent_core.gemini_client import generate_content_tracked
        from google.genai import types as T
    except Exception as e:  # noqa: BLE001
        return f"❌ Gemini SDK 匯入失敗: {e}"

    view_labels = {"side": ["正側面視角"],
                   "multi": ["正側面視角", "正前方 45 度視角", "鞋面俯視角度"]}
    view_list = view_labels.get(views, view_labels["side"])

    # 改款鎖版型模式：先下載參考圖
    ref_img = None
    if reference_file_id.strip():
        try:
            from PIL import Image
            from agent_core.google_auth import get_service
            from googleapiclient.http import MediaIoBaseDownload
            svc = get_service("drive", "v3")
            buf = io.BytesIO()
            d = MediaIoBaseDownload(buf, svc.files().get_media(fileId=reference_file_id.strip(), supportsAllDrives=True))
            done = False
            while not done:
                _, done = d.next_chunk()
            ref_img = Image.open(io.BytesIO(buf.getvalue()))
        except Exception as e:  # noqa: BLE001
            return f"❌ 下載參考圖失敗（{reference_file_id}）: {type(e).__name__}: {str(e)[:120]}"
    ts0 = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = output_path or os.path.join(_default_output_dir(), f"concept_{ts0}.png")
    saved_paths, text_parts = [], []
    for i, view in enumerate(view_list, 1):
        if ref_img is not None:
            prompt = (
                "以這張參考圖的鞋款為基礎做「改款」。必須完整保留：楦型、鞋底結構與花紋、整體比例、"
                "鞋頭形狀、幫面結構分割線的位置與數量。只依以下需求修改指定的顏色／材質／配件／裝飾："
                f"{order_description.strip()}。"
                f"專業產品攝影、純白背景、單隻鞋、{view}、高解析清晰、真實皮革與布料質感、無文字浮水印。"
            )
            contents = [ref_img, prompt]
        else:
            prompt = (
                f"生成一張鞋類產品概念圖，需符合客人樣品單需求：{order_description.strip()}。"
                f"專業產品攝影、純白背景、單隻鞋、{view}、高解析清晰、真實材質質感、無文字浮水印。"
            )
            contents = [prompt]
        try:
            response = generate_content_tracked(
                model=model, contents=contents,
                config=T.GenerateContentConfig(
                    response_modalities=["IMAGE", "TEXT"],
                    image_config=T.ImageConfig(aspect_ratio=aspect_ratio),
                ),
                caller="image_gen.generate_product_concept",
            )
        except Exception as e:  # noqa: BLE001
            return f"❌ 生成失敗（第 {i} 視角，模型 {model}）: {type(e).__name__}: {str(e)[:300]}"

        got = None
        try:
            for cand in (response.candidates or []):
                for part in (cand.content.parts or []):
                    if getattr(part, "inline_data", None) and part.inline_data.data:
                        data = part.inline_data.data
                        if isinstance(data, str):
                            data = base64.b64decode(data)
                        if len(view_list) == 1:
                            op = base
                        else:
                            root, ext = os.path.splitext(base)
                            op = f"{root}_{i}{ext or '.png'}"
                        got = _save_image_bytes(data, op)
                    elif getattr(part, "text", None):
                        text_parts.append(part.text)
        except ValueError as e:
            return str(e)
        except Exception as e:  # noqa: BLE001
            return f"❌ 解析 response 失敗: {type(e).__name__}: {e}"
        if got:
            saved_paths.append(got)

    if not saved_paths:
        return f"❌ 沒回圖，文字：{' '.join(text_parts)[:300]}"
    mode = "改款鎖版型" if ref_img is not None else "純文字全新款"
    lines = "\n".join(f"   • {p}" for p in saved_paths)
    delivery = _deliver_to_telegram(saved_paths, f"樣單概念圖（{mode}）")
    return (f"✅ 已生成 {len(saved_paths)} 張產品概念圖（{mode}、模型 {model}）：\n{lines}\n"
            f"   ⚠️ 概念參考用、非生產精準圖；生產仍需開發打版。" + delivery)


def generate_from_sample(sample_image_path: str, extra_request: str = "", views: str = "side") -> str:
    """收客人樣品鞋照片 → 自動找最像的舊款當版型底、按樣品外觀改款生成概念圖（一鍵完成「以圖找舊款 → 改款生圖」）。

    最適合「客人丟一張樣品鞋照片、要做類似的款」這個情境。內部自動三步：① macOS Vision 指紋從
    產品照庫找最相似的舊款 ② Gemini 描述樣品照的款型/顏色/材質特徵 ③ 用該舊款鎖版型 + 樣品特徵
    （＋你的額外需求）生成概念圖。⚠️ 產出是概念視覺化供打樣前溝通、非生產精準圖。

    Args:
        sample_image_path: 客人樣品照的本機路徑（Telegram 上傳的照片路徑）。
        extra_request: 額外改款需求（如「改成紅色」「加防滑大底」「鞋面改網布」），可空。
        views: "side" 單張側面 / "multi" 側面+45度+俯視三張。
    """
    import json

    import numpy as np

    from agent_core.logging_and_paths import DATA_DIR

    if not sample_image_path or not os.path.exists(sample_image_path):
        return f"❌ 找不到樣品照：{sample_image_path}"

    # ① Vision 指紋找最像舊款（複用產品照索引 fp.npy/meta.json）
    pdir = os.path.join(DATA_DIR, "product_photos")
    try:
        fp = np.load(os.path.join(pdir, "fp.npy"))
        meta = json.load(open(os.path.join(pdir, "meta.json"), encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        return f"❌ 產品照索引未建立（先跑 build_product_photo_index）：{type(e).__name__}"
    try:
        import Vision
        from Foundation import NSData
        with open(sample_image_path, "rb") as f:
            data = f.read()
        nsd = NSData.dataWithBytes_length_(data, len(data))
        h = Vision.VNImageRequestHandler.alloc().initWithData_options_(nsd, None)
        r = Vision.VNGenerateImageFeaturePrintRequest.alloc().init()
        h.performRequests_error_([r], None)
        res = r.results()
        raw = bytes(res[0].data())
        n = res[0].elementCount()
        q = np.frombuffer(raw, dtype=(np.float32 if len(raw) == n * 4 else np.float16)).astype(np.float32)
        q = q / (np.linalg.norm(q) + 1e-9)
    except Exception as e:  # noqa: BLE001
        return f"❌ 樣品照指紋失敗（需 macOS Vision）：{type(e).__name__}: {str(e)[:80]}"
    sims = fp @ q
    idx = np.argsort(-sims)[:3]
    top = [(float(sims[i]), meta[i]) for i in idx]
    best_sim, best = top[0]

    # ② Gemini 描述樣品照特徵
    try:
        from PIL import Image
        from agent_core.gemini_client import GEMINI_MODEL, generate_content_tracked
        img = Image.open(sample_image_path)
        resp = generate_content_tracked(
            model=GEMINI_MODEL,
            contents=[img, "用一句話描述這隻鞋的款型、主要顏色、材質、鞋面結構特徵（給打樣改款參考，繁體中文）。"],
            caller="image_gen.generate_from_sample")
        desc = (resp.text or "").strip()[:200]
    except Exception as e:  # noqa: BLE001
        desc = f"（樣品照特徵描述失敗：{type(e).__name__}）"

    # ③ 用最像舊款鎖版型 + 樣品特徵（＋額外需求）生成
    order = f"參考樣品鞋特徵：{desc}"
    if extra_request.strip():
        order += f"。額外改款需求：{extra_request.strip()}"
    gen = generate_product_concept(order_description=order, reference_file_id=best["id"], views=views)

    from agent_core.customer_identify import customer_of_folder

    def _tag(m):
        c = customer_of_folder(m.get("folder", ""))
        return f"[{c}]" if c else ""

    others = "、".join(f"{m.get('model') or '?'}{_tag(m)}({s:.0%})" for s, m in top[1:]) or "無"
    return (f"📷 樣品照特徵：{desc}\n"
            f"🔍 最像舊款：{best.get('model') or '?'}{_tag(best)}（相似 {best_sim:.0%}）"
            f" https://drive.google.com/file/d/{best['id']}/view\n"
            f"   其他相似：{others}\n"
            f"　 ⚠️ 客戶標註來自產品照庫資料夾（R-=Richter、L-=Lurchi、B-=BRTK、K-=Kamik）；"
            f"未標＝庫內未分類，請勿自行臆測品牌\n"
            f"🎨 已鎖該款版型改款生成 ↓\n{gen}")


def generate_jf_shoe(description: str, steps: int = 30) -> str:
    """用本機訓練的「捷丰風格」LoRA 從文字直接生成鞋圖（本機、零 API 成本、不用參考圖、已內化捷丰款式風格）。

    與 generate_from_sample（照片改款、走 nano-banana 付費）互補：這個是純文字描述 → 捷丰風格鞋，
    適合「客人只給文字需求」或「探索捷丰式新款」。⚠️ 本機 SDXL 生圖較慢（~1-2 分鐘/張）、開源底模
    質感略遜 nano-banana、生圖時吃記憶體（避免與夜跑/大量查詢同時跑）。

    Args:
        description: 鞋款描述（如「紅色女童雪靴、毛絨鞋面、防滑大底」），中英文皆可。
        steps: 生圖步數，預設 30。
    """
    import subprocess
    from datetime import datetime

    lora_dir = "/Users/user/RED/var/data/lora_train"
    lora_weights = os.path.join(lora_dir, "lora_out2", "pytorch_lora_weights.safetensors")
    venv_py = os.path.join(lora_dir, "venv", "bin", "python")
    if not os.path.exists(lora_weights):
        return f"❌ 捷丰 LoRA 模型不存在（{lora_weights}）"
    if not os.path.exists(venv_py):
        return f"❌ LoRA 生圖環境不存在（{venv_py}）"
    if not description.strip():
        return "❌ description 不能空"

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(_default_output_dir(), f"jfshoe_{ts}.png")
    prompt = (f"jfshoe, {description.strip()}, product photo on white background, "
              "side view, professional studio lighting")
    env = dict(os.environ, PYTORCH_ENABLE_MPS_FALLBACK="1", HF_HUB_DISABLE_TELEMETRY="1")
    try:
        r = subprocess.run(
            [venv_py, os.path.join(lora_dir, "lora_gen.py"),
             "--prompt", prompt, "--out", out,
             "--lora", os.path.join(lora_dir, "lora_out2"), "--steps", str(int(steps))],
            capture_output=True, text=True, timeout=600, env=env)
    except subprocess.TimeoutExpired:
        return "❌ LoRA 生圖逾時（>10 分鐘）"
    if r.returncode != 0 or not os.path.exists(out):
        return f"❌ LoRA 生圖失敗：{(r.stderr or r.stdout or '')[-250:]}"
    delivery = _deliver_to_telegram(out, "捷丰風格鞋圖（本機 LoRA）")
    return ("✅ 已生成捷丰風格鞋圖（本機 v2 LoRA、觸發詞 jfshoe、零 API 成本）：\n"
            f"   • {out}\n"
            "   ⚠️ 概念參考用；本機開源底模質感略遜 nano-banana、細節非生產精準。"
            + delivery)


SKILL_TOOLS = [generate_image, edit_image, generate_product_concept,
               generate_from_sample, generate_jf_shoe]
