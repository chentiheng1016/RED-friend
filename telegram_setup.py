#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram Bot 一次性設定助手
流程：
  1. 大王先去 @BotFather 建 bot，拿到 token
  2. 大王先傳一則任意訊息給自己的 bot
  3. 跑這個腳本：python3 telegram_setup.py
     腳本會問你要 token、呼叫 Telegram API 找出你的 chat_id、兩個都存進 macOS 鑰匙圈
"""
import sys
import requests
import keyring

_SERVICE = "xiaohong-agent"
_KEY_TOKEN = "telegram-bot-token"
_KEY_CHAT = "telegram-chat-id"
_API = "https://api.telegram.org"

def ask(prompt: str, default: str = "") -> str:
    hint = f" [{default}]" if default else ""
    s = input(f"{prompt}{hint}: ").strip()
    return s or default

def main():
    print("=" * 60)
    print("  小紅 Telegram Bot 設定助手")
    print("=" * 60)

    # 既有值
    existing_token = keyring.get_password(_SERVICE, _KEY_TOKEN) or ""
    existing_chat = keyring.get_password(_SERVICE, _KEY_CHAT) or ""
    if existing_token:
        print(f"\n⚠️ 偵測到已有 token（結尾 …{existing_token[-6:]}）")
        if ask("要覆蓋嗎？(y/N)").lower() != "y":
            if existing_chat:
                print(f"\n✅ 目前設定：\n   token: …{existing_token[-6:]}\n   chat_id: {existing_chat}")
                return
            print("token 留著，繼續抓 chat_id…")
            token = existing_token
        else:
            token = ""
    else:
        token = ""

    # 收集 token
    if not token:
        print("\n請貼上 BotFather 給你的 token")
        print("（長得像：7123456789:AAEhBlabla_XxYy_DdEeFf，貼完按 Enter）")
        token = ask("Token").strip()
        if not token or ":" not in token:
            print("❌ token 看起來不對（應該有一個冒號），取消。")
            sys.exit(1)

    # 驗證 token
    print("\n[1/3] 驗證 token…")
    try:
        r = requests.get(f"{_API}/bot{token}/getMe", timeout=10).json()
    except Exception as e:
        print(f"❌ 連線失敗：{e}")
        sys.exit(1)
    if not r.get("ok"):
        print(f"❌ token 無效：{r.get('description')}")
        sys.exit(1)
    bot_info = r["result"]
    print(f"   ✅ Bot 是「{bot_info.get('first_name')}」(@{bot_info.get('username')})")

    # 抓 chat_id
    print("\n[2/3] 抓你的 chat_id（你必須先傳過至少一則訊息給 bot）…")
    try:
        r = requests.get(f"{_API}/bot{token}/getUpdates", timeout=10).json()
    except Exception as e:
        print(f"❌ 連線失敗：{e}")
        sys.exit(1)
    if not r.get("ok"):
        print(f"❌ 取 update 失敗：{r}")
        sys.exit(1)
    updates = r.get("result", []) or []
    if not updates:
        print("❌ 沒收到任何訊息 —")
        print("   請先到 Telegram 找你剛建的 bot，按 Start 或傳一句「hi」再跑一次本腳本。")
        sys.exit(1)

    # 找出所有 private chat
    candidates = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") == "private" and chat.get("id"):
            candidates[chat["id"]] = chat.get("first_name") or chat.get("username") or "?"
    if not candidates:
        print("❌ 沒找到 private 對話，請從 Telegram 傳一則訊息給 bot 再試。")
        sys.exit(1)

    if len(candidates) == 1:
        chat_id, name = next(iter(candidates.items()))
        print(f"   ✅ 偵測到 chat_id = {chat_id}（{name}）")
    else:
        print("   偵測到多個 chat_id：")
        items = list(candidates.items())
        for i, (cid, name) in enumerate(items, 1):
            print(f"     {i}. {cid}  ({name})")
        sel = ask("選哪一個？(輸入編號)", "1")
        try:
            chat_id = items[int(sel) - 1][0]
        except Exception:
            print("❌ 選項無效。")
            sys.exit(1)

    # 存進 keyring
    print("\n[3/3] 寫進 macOS 鑰匙圈…")
    keyring.set_password(_SERVICE, _KEY_TOKEN, token)
    keyring.set_password(_SERVICE, _KEY_CHAT, str(chat_id))
    print(f"   ✅ token 已存（service={_SERVICE}, user={_KEY_TOKEN}）")
    print(f"   ✅ chat_id 已存（service={_SERVICE}, user={_KEY_CHAT}）")

    # 發送測試訊息
    print("\n發測試訊息驗證 end-to-end…")
    test = requests.post(
        f"{_API}/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": "🎉 小紅 Telegram 整合設定完成！\n\n以後她可以直接推訊息到這裡。"},
        timeout=10,
    ).json()
    if test.get("ok"):
        print("   ✅ 已送出測試訊息，請去 Telegram 看一下")
    else:
        print(f"   ⚠️ 送訊息失敗：{test}")

    print("\n" + "=" * 60)
    print("  🎉 設定完成！")
    print("=" * 60)
    print("\n接下來：")
    print("  1. 跑 agent.py 後就能用 telegram_push 工具（小紅會主動推訊息給你）")
    print("  2. 要啟用『你可以從 Telegram 傳訊息給小紅』的雙向模式：")
    print("     bash install_daemon.sh  ← 會同時載入 telegram bot daemon")
    print("     之後打開 Telegram 傳「哈囉」給小紅，她會回你。")

if __name__ == "__main__":
    main()
