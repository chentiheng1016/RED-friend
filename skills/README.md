# skills/ — 小紅的技能插件

這個資料夾裡的每一個 `.py` 檔 = 一包可以加進小紅的工具。
**不用改 agent.py 就能加功能。**

## 寫一個 skill 的規則

1. 檔名 `foo.py`（英數底線；不要以底線開頭，那會被忽略）
2. 在檔案裡定義一或多個普通的 Python 函式（每個要有 docstring，Gemini 會看這個判斷何時呼叫）
3. 檔案底部加一行：`SKILL_TOOLS = [func1, func2, ...]` — 列出要註冊成小紅工具的那些函式

## 最小範例

```python
# skills/weather.py
"""天氣查詢 skill。"""
import requests

def get_weather(city: str):
    """查指定城市目前天氣（繁體中文）。"""
    r = requests.get(f"https://wttr.in/{city}?format=j1", timeout=10)
    j = r.json()
    cond = j["current_condition"][0]
    return f"{city}：{cond['temp_C']}°C，{cond['lang_zh_tw'][0]['value']}"

SKILL_TOOLS = [get_weather]
```

做完重新啟動 agent.py（或對小紅說「重新載入技能」→ `reload_skills`），
她就可以被問「台北天氣怎樣」→ 會自動呼叫 `get_weather("台北")`。

## 可選進階

### 允許背景 dispatcher 使用
預設只有前景對話能用你的 skill。要讓排程任務（每 2 小時搜 eBay 那種）也能呼叫：
```python
def my_safe_read_only_tool(...):
    ...
my_safe_read_only_tool.background_safe = True  # 讓背景任務也能用
```
⚠️ 只在**完全只讀、無副作用**的函式上掛這個旗子（例如查天氣、搜網頁）。

### 使用憑證
寫進 macOS 鑰匙圈，在 skill 裡讀：
```python
import keyring
api_key = keyring.get_password("xiaohong-agent", "openweather-key")
```

### 依賴第三方套件
直接在 skill 裡 import。裝不到就 raise，小紅會告訴大王「請 pip install XX」。

## 常見問題

**Q：檔案裡有語法錯誤會怎樣？**
A：小紅啟動時會 log 警告跳過這個 skill，其他 skill 照常載入。

**Q：兩個 skill 定義了同名函式怎辦？**
A：後載入的蓋掉前面的，會印 warning。檔名按字母順序載入，自己命名避開。

**Q：skill 可以呼叫 agent.py 內的其他函式嗎？**
A：可以 — `from agent import send_gmail` 等。但不建議高耦合，應用級 skill 自成一體最好維護。

**Q：怎麼看載入了哪些 skill？**
A：對小紅說「看一下目前載入的技能」→ 她會呼叫 `list_skills()`。

**Q：改完怎麼即時生效？**
A：對小紅說「重新載入技能」→ `reload_skills()`，會重建 chat 套用新 skill。
