"""
台北萬華區租屋比較 — Streamlit 動態版
- 貼上 591 租屋連結即可即時抓取、解析、新增到比較頁
- 可移除任一物件
- 下載 / 上傳 listings.json 保存資料（持久化方案 A）

部署：Streamlit Community Cloud，主檔案設為 app.py
"""
import re
import io
import json
import base64
import pathlib
import requests
import urllib3
import pandas as pd
import streamlit as st

# 591 的憑證鏈在部分環境（如 Streamlit Cloud 較新 OpenSSL）會驗證失敗
# （Missing Subject Key Identifier）。抓公開頁面，故允許驗證失敗時退回不驗證。
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def _get(url, timeout=15):
    """先正常驗證憑證，失敗則退回 verify=False 再試一次。"""
    try:
        return requests.get(url, headers=HEADERS, timeout=timeout)
    except requests.exceptions.SSLError:
        return requests.get(url, headers=HEADERS, timeout=timeout, verify=False)

DATA_FILE = pathlib.Path(__file__).parent / "listings.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Referer": "https://rent.591.com.tw/",
}

# ---------------------------------------------------------------- 解析 591
def _og(html, prop):
    m = re.search(rf'<meta property="og:{prop}" content="([^"]*)"', html)
    return m.group(1) if m else ""

def _val(html, label):
    """抓取 房屋詳情 中 label 對應的 value 文字。"""
    m = re.search(re.escape(label) + r'</span><span class="value"[^>]*>(?:<!---->)?(?:<span[^>]*>)?([^<]+)', html)
    return m.group(1).strip() if m else ""

@st.cache_data(show_spinner=False, ttl=3600)
def fetch_html(url):
    return _get(url).text

@st.cache_data(show_spinner=False, ttl=86400)
def fetch_img_b64(img_url):
    """伺服器端抓圖並轉 base64，避免前端 hotlink 被擋。"""
    try:
        r = _get(img_url)
        if r.ok and r.content:
            return "data:image/jpeg;base64," + base64.b64encode(r.content).decode()
    except Exception:
        pass
    return ""

def parse_listing(url):
    url = url.strip().split("?")[0].rstrip("/")
    m = re.search(r'/(\d+)$', url)
    if not m:
        raise ValueError("看起來不是有效的 591 物件連結（找不到物件編號）。")
    ident = m.group(1)
    html = fetch_html(url)
    if "頁面不存在" in html or "您訪問的頁面" in html:
        raise ValueError("找不到此物件，可能已下架或連結有誤（HTTP 404）。")
    title = _og(html, "title").replace(" - 591租屋網", "").strip()
    if not title:
        raise ValueError("抓不到頁面內容，請確認連結正確或稍後再試。")
    desc = _og(html, "description")
    img = _og(html, "image")
    dist = re.search(r'台北市(\S+?區)', desc) or re.search(r'台北市(\S+?區)', html)
    street = re.search(r'位於(.+?)，', desc)
    addr = (dist.group(1) if dist else "") + (street.group(1) if street else "")
    p = re.search(r'<strong[^>]*>([\d,]{3,})</strong>', html)
    price = p.group(1) if p else ""
    layout = re.search(r'\d房\d廳\d衛', html)
    layout = layout.group(0) if layout else ""
    floor = re.search(r'\d+F/\d+F', html)
    floor = floor.group(0) if floor else ""
    area = _val(html, "可使用面積")
    pet = re.search(r'(不可養寵物|可養寵物)', html)
    pet = pet.group(1) if pet else ""
    subsidy = "可申請租金補貼" if "可申請租金補貼" in html or "租金補貼" in title else ""
    return {
        "id": ident, "url": url, "title": title, "addr": addr, "price": price,
        "layout": layout, "floor": floor, "area": area, "pet": pet,
        "deposit": _val(html, "押金"), "fee": _val(html, "服務費"),
        "deco": _val(html, "裝潢程度"), "balcony": _val(html, "陽台"),
        "lift": _val(html, "電梯"), "subsidy": subsidy, "img": img,
    }

# ---------------------------------------------------------------- 工具
def to_int(s):
    try:
        return int(re.sub(r"[^\d]", "", str(s)))
    except Exception:
        return None

def to_float_area(s):
    m = re.search(r"[\d.]+", str(s))
    return float(m.group(0)) if m else None

def rent_per_ping(item):
    p, a = to_int(item.get("price")), to_float_area(item.get("area"))
    return round(p / a) if (p and a) else None

# ---------------------------------------------------------------- 資料載入
def load_seed():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return []

if "listings" not in st.session_state:
    st.session_state.listings = load_seed()

# ---------------------------------------------------------------- 頁面
st.set_page_config(page_title="台北萬華區租屋比較", page_icon="🏠", layout="wide")

st.markdown("""
<style>
.block-container{max-width:1300px;}
.rcard{background:#fff;border:1px solid #e3e7ec;border-radius:12px;overflow:hidden;
  box-shadow:0 1px 3px rgba(0,0,0,.06);margin-bottom:10px;}
.rcard img{width:100%;aspect-ratio:16/10;object-fit:cover;display:block;background:#e9edf1;}
.rcard .b{padding:12px 14px;}
.rcard h4{margin:0 0 6px;font-size:14px;line-height:1.4;}
.rcard .price{font-size:20px;font-weight:700;color:#c0392b;}
.rcard .meta{font-size:12.5px;color:#66707a;margin:2px 0;}
.rtag{display:inline-block;font-size:11px;background:#eef4f0;color:#2f6f4f;
  padding:2px 7px;border-radius:10px;margin:3px 3px 0 0;}
.rtag.w{background:#fdecea;color:#c0392b;}
a.src{font-size:12px;color:#2f6f4f;}
</style>
""", unsafe_allow_html=True)

st.title("🏠 台北萬華區租屋比較")
st.caption("資料來源：591 租屋網 · 貼上連結即時新增 · 資料以下載 JSON 保存")

# ---- 側欄：新增 / 保存 ----
with st.sidebar:
    st.header("➕ 新增物件")
    new_url = st.text_input("591 物件連結", placeholder="https://rent.591.com.tw/XXXXXXXX")
    if st.button("抓取並新增", type="primary", use_container_width=True):
        if new_url.strip():
            ids = {x["id"] for x in st.session_state.listings}
            try:
                with st.spinner("抓取中…"):
                    item = parse_listing(new_url)
                if item["id"] in ids:
                    st.warning("此物件已在清單中。")
                else:
                    st.session_state.listings.append(item)
                    st.success(f"已新增：{item['title'][:20]}…")
                    st.rerun()
            except Exception as e:
                st.error(f"新增失敗：{e}")
        else:
            st.info("請先貼上連結。")

    st.divider()
    st.header("💾 保存 / 還原")
    st.caption("方案 A：修改存在本次連線，下載 JSON 後 commit 回 repo 即永久生效。")
    st.download_button(
        "⬇️ 下載目前 listings.json",
        data=json.dumps(st.session_state.listings, ensure_ascii=False, indent=2),
        file_name="listings.json", mime="application/json", use_container_width=True)
    up = st.file_uploader("⬆️ 上傳 listings.json 還原", type="json")
    if up is not None:
        try:
            st.session_state.listings = json.load(io.TextIOWrapper(up, encoding="utf-8"))
            st.success("已載入。")
            st.rerun()
        except Exception as e:
            st.error(f"讀取失敗：{e}")

    if st.button("↩️ 重設為原始 5 案", use_container_width=True):
        fetch_html.clear()
        st.session_state.listings = load_seed()
        st.rerun()

items = st.session_state.listings
if not items:
    st.info("目前沒有物件，請在左側貼上 591 連結新增。")
    st.stop()

# ---- 卡片 ----
st.subheader(f"物件卡片（共 {len(items)} 筆）")
cols = st.columns(min(len(items), 4) or 1)
for i, it in enumerate(items):
    with cols[i % len(cols)]:
        img_b64 = fetch_img_b64(it.get("img", "")) if it.get("img") else ""
        rpp = rent_per_ping(it)
        tags = []
        if it.get("subsidy"):
            tags.append('<span class="rtag">租金補貼</span>')
        if it.get("pet") == "可養寵物":
            tags.append('<span class="rtag">可養寵物</span>')
        elif it.get("pet") == "不可養寵物":
            tags.append('<span class="rtag w">不可養寵</span>')
        if it.get("deco"):
            tags.append(f'<span class="rtag">{it["deco"]}</span>')
        st.markdown(f"""
<div class="rcard">
  {'<img src="'+img_b64+'">' if img_b64 else '<div style="aspect-ratio:16/10;background:#e9edf1"></div>'}
  <div class="b">
    <h4>{it.get('title','')[:34]}</h4>
    <div class="price">{it.get('price','?')} <span style="font-size:12px;color:#66707a">元/月</span></div>
    <div class="meta">{it.get('layout','')} · {it.get('area','')} · {it.get('floor','')}</div>
    <div class="meta">📍 {it.get('addr','')}</div>
    <div class="meta">租金/坪 約 {rpp if rpp else '—'}</div>
    <div>{''.join(tags)}</div>
    <div style="margin-top:6px"><a class="src" href="{it.get('url','')}" target="_blank">看 591 原始刊登 ↗</a></div>
  </div>
</div>""", unsafe_allow_html=True)
        if st.button("🗑️ 移除", key=f"del_{it['id']}", use_container_width=True):
            st.session_state.listings = [x for x in items if x["id"] != it["id"]]
            st.rerun()

# ---- 比較表 ----
st.subheader("詳細比較表")
labels = ["月租金", "格局", "可使用坪數", "租金/坪(估)", "樓層", "電梯",
          "裝潢", "陽台", "押金", "服務費", "寵物", "租金補貼", "地址", "編號"]
def row(it):
    rpp = rent_per_ping(it)
    return [it.get("price", ""), it.get("layout", ""), it.get("area", ""),
            f"約 {rpp}" if rpp else "—", it.get("floor", ""), it.get("lift", ""),
            it.get("deco", "") or "—", it.get("balcony", "") or "—",
            it.get("deposit", ""), it.get("fee", ""), it.get("pet", "") or "—",
            "可申請" if it.get("subsidy") else "—", it.get("addr", ""), it.get("id", "")]

heads = [f"案{chr(65+i)} {it.get('addr','')[:10]}" for i, it in enumerate(items)]
df = pd.DataFrame({h: row(it) for h, it in zip(heads, items)}, index=labels)

# 標示最佳（租金最低、坪數最大、租金/坪最低）
prices = [to_int(it.get("price")) for it in items]
areas = [to_float_area(it.get("area")) for it in items]
rpps = [rent_per_ping(it) for it in items]
best = {}
if any(prices):
    best["月租金"] = prices.index(min([p for p in prices if p]))
if any(areas):
    best["可使用坪數"] = areas.index(max([a for a in areas if a]))
if any(rpps):
    valid = [r for r in rpps if r]
    best["租金/坪(估)"] = rpps.index(min(valid))

def hl(data):
    styles = pd.DataFrame("", index=data.index, columns=data.columns)
    for lab, ci in best.items():
        styles.loc[lab, data.columns[ci]] = "background-color:#fef6e0;font-weight:700"
    return styles

st.dataframe(df.style.apply(hl, axis=None), use_container_width=True, height=560)
st.caption("＊淺黃 = 該列相對較佳（租金最低 / 坪數最大 / 租金每坪最低）。數字以刊登當下為準，請以看屋確認為主。")
