# agent_core/data — 內建對照資料

## pantone_tcx.json
Pantone Fashion, Home + Interiors（TCX/TPG）全 2310 色的 **社群近似 sRGB 值**。
來源：https://github.com/Margaret2/pantone-colors（pantone-numbers.json，2026-09-01 取得，
已抽查年度代表色 19-4052/13-1520/17-1463/18-3838/16-1546 與公認值一致）。
色名為 Pantone 商標；hex 是螢幕近似值——**拿來做生圖色錨與 ΔE 驗證夠用，
不能當正式對色依據**（正式簽色仍以實體色卡為準）。
格式：`{"19-4052": {"name": "classic-blue", "hex": "0f4c81"}}`。

## pantone_pms.json
Pantone PMS（印刷 Solid：Formula Guide／Metallics／Pastels & Neons）4574 色號的
**社群近似 sRGB 值**（客戶完稿寫 `PANTONE 286C`、`Cool Gray 9C` 這系用）。
兩個來源合併（2026-09-03 取得）：
- 主：https://github.com/brettapeters/pantones（coated/uncoated/metallic/pastels-neons
  四檔共 3172 條）——對 pantone.com 官方公布 hex 抽查 11 色，10 色全同、
  286-c 差 1（`0033a0` vs 官方 `0032a0`，ΔE<1）。
- 補缺：https://github.com/aj90909/unofficial-pantone-solid-coated-2024-v5
  （colors.csv 的 Lab 值以 D65 轉 sRGB，補主來源沒有的 1402 條 coated——
  2016 後新增的 2000 系、10xxx 金屬系等）。與主來源重疊的 1327 色實測
  中位 ΔE 1.7 / p90 4.0，只補缺、不覆蓋主來源。
格式：`{"286-c": "0033a0", "cool-gray-9-c": "75787b"}`（後綴必為 `-c`/`-u`；
U 查無退 coated，見 `color_anchor._pms_lookup`）。同 TCX 表的誠實界線：
螢幕近似值，生圖色錨與 ΔE 驗證夠用，正式簽色以實體色卡為準。

## material_aliases.json
材質詞（中英文）→ 材質圖庫 canonical key 的對照（`agent_core/material_anchor.py` 用）。
圖庫本體是 runtime 資產、不入 git：把真實材質特寫照命名為 `<key>.jpg|png|webp`
放進 `var/data/material_swatches/`（例：麂皮特寫 → `suede.jpg`），生圖時自動附上
當紋理參考。特寫要領：平拍、填滿畫面、光線均勻；顏色無所謂（prompt 明講只取
紋理、顏色以色票為準）。新的料先在這張表加別名，再補圖。

## color_names.json
常見中英文顏色詞 → 近似 hex（客人樣品單用文字敘述顏色時的內建錨點）。
比對規則見 `agent_core/color_anchor.resolve_color`：Pantone 色號 > 客戶色彙表
（`var/state/customer_colors.json`）> 這張內建表 > LLM 推估。
客戶對某個詞有自己的定義（例：Richter 的 "nude"）請加進客戶色彙表，不要改這裡。
