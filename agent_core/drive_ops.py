import os


def search_drive_files(keyword: str, *, get_service) -> str:
    """用關鍵字搜尋 Google Drive 檔案，回最新的前 10 筆（含修改日期）。"""
    print(f"\n[系統日誌] ☁️ 搜尋 Drive：{keyword}...")
    try:
        service = get_service("drive", "v3")
        safe_keyword = keyword.replace("\\", "\\\\").replace("'", "\\'")
        results = service.files().list(
            q=f"name contains '{safe_keyword}' and trashed = false",
            pageSize=25,
            fields="files(id, name, modifiedTime)",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            corpora="allDrives",
        ).execute()
        items = results.get("files", [])
        if not items:
            return "找不到檔案。"
        # 新到舊排序（client-side — allDrives 查詢的 orderBy 支援度不穩），
        # 「找最新一份庫存表/排程」就是挑第一筆。
        items.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)
        lines = ["搜尋結果（依修改日期新→舊）："]
        for item in items[:10]:
            mt = str(item.get("modifiedTime", ""))[:10]
            date_part = f", 修改: {mt}" if mt else ""
            lines.append(f"檔名: {item['name']} (ID: {item['id']}{date_part})")
        lines.append("→ 要讀整份內容：read_drive_file(file_id)")
        return "\n".join(lines)
    except Exception as e:
        return f"Drive 搜尋失敗：{e}"


def upload_to_drive(local_file_path: str, folder_id: str = None, *, clean_path_fn, get_service, media_upload_factory) -> str:
    """把本機檔案上傳到 Google Drive。"""
    print("\n[系統日誌] ☁️ 上傳至 Drive...")
    try:
        clean_path = clean_path_fn(local_file_path)
        if not os.path.exists(clean_path):
            return f"錯誤：找不到 {clean_path}"
        service = get_service("drive", "v3")
        meta = {"name": os.path.basename(clean_path)}
        if folder_id:
            meta["parents"] = [folder_id]
        media = media_upload_factory(clean_path, resumable=True)
        service.files().create(
            body=meta,
            media_body=media,
            fields="id",
            supportsAllDrives=True,
        ).execute()
        return "上傳成功！"
    except Exception as e:
        return f"上傳失敗：{e}"
