"""
台北萬華區租屋比較 — Streamlit 動態版
- 貼上 591 租屋連結即可即時抓取、解析、新增到比較頁
- 可移除任一物件
- 新增 / 移除即自動寫回 listings.json；設定 GITHUB_TOKEN 後並自動 commit 回 GitHub
  （容器重啟也不遺失。token 放 Streamlit secrets，勿寫入程式碼）

部署：Streamlit Community Cloud，主檔案設為 app.py
"""
import re
import json
import math
import base64
import pathlib
import requests
import urllib3
import pandas as pd
import streamlit as st
import folium
from streamlit_folium import st_folium

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

# ---------------------------------------------------------------- 地理位置 / 地圖
SCHOOL_NAME = "光仁小學"
# 正確地址：108臺北市萬華區壽德里萬大路423巷15號（門牌無法 geocode，用巷弄中心）
SCHOOL_QUERY = "台北市萬華區萬大路423巷"
SCHOOL_FALLBACK = (25.0217317, 121.5012011)  # 解析失敗時的備援座標（萬華萬大路423巷）

@st.cache_data(show_spinner=False, ttl=7 * 86400)
def geocode(query):
    """用 OpenStreetMap Nominatim 將地址解析為 (lat, lon)，失敗回 None。"""
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": query, "format": "json", "limit": 1, "countrycodes": "tw"},
            headers={"User-Agent": "taipei-rental-survey/1.0 (github.com/audachang)"},
            timeout=15)
        j = r.json()
        if j:
            return float(j[0]["lat"]), float(j[0]["lon"])
    except Exception:
        pass
    return None

def geocode_listing(it):
    addr = (it.get("addr") or "").strip()
    if not addr:
        return None
    # 已含縣市（台北市/新北市）者直接查詢；否則預設補上台北市
    if not (addr.startswith("台北") or addr.startswith("新北")):
        addr = "台北市" + addr
    # 完整地址查不到時，逐步去掉 弄→巷→號，退到街路層級再試
    queries = [addr]
    for pat in (r"\d+弄$", r"\d+巷(\d+弄)?$", r"\d+(-\d+)?號.*$"):
        q = re.sub(pat, "", addr)
        if q and q not in queries:
            queries.append(q)
    for q in queries:
        loc = geocode(q)
        if loc:
            return loc
    return None

def haversine_km(a, b):
    """兩組 (lat, lon) 的直線距離（km）。"""
    R = 6371.0
    dlat, dlon = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    h = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(a[0])) * math.cos(math.radians(b[0])) *
         math.sin(dlon / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(h))

# ---------------------------------------------------------------- GitHub 同步
def _gh_conf():
    """讀取 Streamlit secrets 的 GitHub 設定；未設定 token 時回 None（僅存本機）。"""
    try:
        tok = st.secrets.get("GITHUB_TOKEN", "")
        if not tok:
            return None
        return {
            "token": tok,
            "repo": st.secrets.get("GITHUB_REPO", "audachang/taipei-rental-survey"),
            "branch": st.secrets.get("GITHUB_BRANCH", "main"),
            "path": st.secrets.get("GITHUB_PATH", "listings.json"),
        }
    except Exception:          # 無 secrets.toml 時 st.secrets 會拋例外
        return None

def _gh_url(conf):
    return f"https://api.github.com/repos/{conf['repo']}/contents/{conf['path']}"

def _gh_headers(conf):
    return {"Authorization": f"Bearer {conf['token']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}

def gh_load():
    """從 GitHub repo 讀取 listings.json；未設定或失敗回 None。"""
    conf = _gh_conf()
    if not conf:
        return None
    try:
        r = requests.get(_gh_url(conf), headers=_gh_headers(conf),
                         params={"ref": conf["branch"]}, timeout=15)
        if r.status_code == 200:
            raw = base64.b64decode(r.json()["content"]).decode("utf-8")
            return json.loads(raw)
    except Exception:
        pass
    return None

def gh_save(text):
    """把 listings.json 內容 commit 回 GitHub repo；回傳 (成功, 訊息)。"""
    conf = _gh_conf()
    if not conf:
        return True, "local-only"
    try:
        url, headers = _gh_url(conf), _gh_headers(conf)
        sha = None
        r = requests.get(url, headers=headers,
                         params={"ref": conf["branch"]}, timeout=15)
        if r.status_code == 200:
            sha = r.json().get("sha")
        body = {"message": f"web 自動存檔：租屋清單共 {len(st.session_state.listings)} 筆",
                "content": base64.b64encode(text.encode("utf-8")).decode(),
                "branch": conf["branch"]}
        if sha:
            body["sha"] = sha
        r = requests.put(url, headers=headers, json=body, timeout=20)
        if r.status_code in (200, 201):
            return True, "ok"
        return False, f"GitHub API {r.status_code}: {r.json().get('message', '')}"
    except Exception as e:
        return False, str(e)

# ---------------------------------------------------------------- 資料載入
def load_seed():
    """優先讀 GitHub（雲端重啟後仍是最新）；否則讀本機 listings.json。"""
    remote = gh_load()
    if remote is not None:
        try:    # 寫一份到本機當快取；失敗不影響使用
            DATA_FILE.write_text(json.dumps(remote, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        except Exception:
            pass
        return remote
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return []

def save_seed():
    """寫回本機 listings.json；若設定 GITHUB_TOKEN 則同步 commit 到 GitHub。"""
    text = json.dumps(st.session_state.listings, ensure_ascii=False, indent=2)
    ok = True
    try:
        DATA_FILE.write_text(text, encoding="utf-8")
    except Exception as e:
        ok = False
        st.warning(f"本機儲存失敗：{e}")
    ok_gh, msg = gh_save(text)
    if not ok_gh:
        ok = False
        st.warning(f"GitHub 同步失敗（資料仍保留於本次連線）：{msg}")
    return ok

if "listings" not in st.session_state:
    st.session_state.listings = load_seed()

# ---------------------------------------------------------------- 頁面
st.set_page_config(page_title="台北萬板區域租屋比較", page_icon="🏠", layout="wide")

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

st.title("🏠 台北萬板區域租屋比較（近光仁小學）")
st.caption("資料來源：591 租屋網 · 涵蓋萬華與板橋 · 貼上連結即時新增 · 變更自動寫回 listings.json")

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
                    save_seed()
                    st.success(f"已新增：{item['title'][:20]}…")
                    st.rerun()
            except Exception as e:
                st.error(f"新增失敗：{e}")
        else:
            st.info("請先貼上連結。")

    st.divider()
    st.header("💾 資料保存")
    if _gh_conf():
        st.caption("☁️ 新增／移除自動存檔並 commit 回 GitHub repo，容器重啟不遺失。")
    else:
        st.caption("💻 新增／移除自動寫回本機 listings.json。"
                   "未設定 GITHUB_TOKEN（Secrets），雲端容器重啟會還原為 repo 版本。")
    st.download_button(
        "⬇️ 下載備份 listings.json",
        data=json.dumps(st.session_state.listings, ensure_ascii=False, indent=2),
        file_name="listings.json", mime="application/json", use_container_width=True)

items = st.session_state.listings
if not items:
    st.info("目前沒有物件，請在左側貼上 591 連結新增。")
    st.stop()

# ---- 編號依「距光仁小學直線距離」排序：案A = 最近，無法定位者殿後 ----
_school = geocode(SCHOOL_QUERY) or SCHOOL_FALLBACK
with st.spinner("依距離排序中…"):
    def _dist_key(it):
        loc = geocode_listing(it)
        return haversine_km(_school, loc) if loc else float("inf")
    items = sorted(items, key=_dist_key)

tab_cmp, tab_map = st.tabs(["📊 比較", "🗺️ 地圖"])

# ================================================================ 比較 tab
with tab_cmp:
    # ---- 卡片 ----
    st.subheader(f"物件卡片（共 {len(items)} 筆，依距光仁小學由近到遠）")
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
                save_seed()
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
    st.caption("＊編號依距光仁小學直線距離排序（案A 最近，無法定位者殿後）。"
               "淺黃 = 該列相對較佳（租金最低 / 坪數最大 / 租金每坪最低）。數字以刊登當下為準，請以看屋確認為主。")

# ================================================================ 地圖 tab
with tab_map:
    st.subheader(f"位置圖（以 {SCHOOL_NAME} 為中心）")
    school = geocode(SCHOOL_QUERY) or SCHOOL_FALLBACK
    with st.spinner("定位各物件中…"):
        located, missing = [], []
        for i, it in enumerate(items):
            loc = geocode_listing(it)
            if loc:
                located.append((i, it, loc))
            else:
                missing.append(it)

    fmap = folium.Map(location=list(school), zoom_start=15,
                      tiles="OpenStreetMap", control_scale=True)
    folium.Marker(list(school), tooltip=f"🎒 {SCHOOL_NAME}", popup=SCHOOL_NAME,
                  icon=folium.Icon(color="red", icon="star")).add_to(fmap)
    bounds = [list(school)]
    for i, it, loc in located:
        label = f"案{chr(65 + i)}"
        dist = haversine_km(school, loc)
        gmap = f"https://www.google.com/maps/search/?api=1&query={loc[0]},{loc[1]}"
        popup_html = (
            f"<b>{label}</b> {it.get('title','')[:22]}<br>"
            f"💰 {it.get('price','?')} 元/月　{it.get('layout','')}<br>"
            f"📍 {it.get('addr','')}<br>"
            f"🎒 距 {SCHOOL_NAME} 約 {dist:.1f} km（直線）<br>"
            f"<a href='{it.get('url','')}' target='_blank'>591 刊登 ↗</a>　"
            f"<a href='{gmap}' target='_blank'>Google 地圖 ↗</a>")
        folium.Marker(list(loc), tooltip=f"{label}｜{it.get('price','')}元",
                      popup=folium.Popup(popup_html, max_width=260),
                      icon=folium.Icon(color="blue", icon="home")).add_to(fmap)
        bounds.append(list(loc))
    if len(bounds) > 1:
        fmap.fit_bounds(bounds, padding=(30, 30))
    _ = st_folium(fmap, use_container_width=True, height=560, returned_objects=[])

    if located:
        drows = [{"物件": f"案{chr(65 + i)}", "地址": it.get("addr", ""),
                  "月租金": it.get("price", ""),
                  f"距{SCHOOL_NAME}(km)": round(haversine_km(school, loc), 2)}
                 for i, it, loc in located]
        st.dataframe(
            pd.DataFrame(drows).sort_values(f"距{SCHOOL_NAME}(km)"),
            use_container_width=True, hide_index=True)
    if missing:
        st.warning("下列物件地址無法定位，未顯示於地圖：" +
                   "、".join(m.get("addr") or m.get("title", "")[:10] for m in missing))
    st.caption(f"🎒 紅色 = {SCHOOL_NAME}；🏠 藍色 = 租案。距離為直線估算，"
               "實際步行／車程請點各 marker 內的 Google 地圖連結確認。底圖 © OpenStreetMap。")
