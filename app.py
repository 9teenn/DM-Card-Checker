import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote
import re

# ==========================================
# 0. CONFIGURATIONS (ตั้งค่าตัวแปรคงที่ไว้ที่เดียว)
# ==========================================
SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQZxMlGzqgFbyEEfEcv3HKVG5DGHOrlVWuHvI6nnSUcg5c9e3lNf4I2vU5WP8AMhT5gMr_7Oq7yOP3m/pub?output=csv"
ZENROWS_API_KEY = "YOUR_ZENROWS_API_KEY" #87f57fba67309f332791dbb814dd096c90d2aa0e
EXCHANGE_RATE = 0.25

# ==========================================
# 1. ฐานข้อมูล Google Sheets
# ==========================================
@st.cache_data(ttl=600)
def load_database(url):
    try:
        df = pd.read_csv(url)
        # ล้างอักขระล่องหน
        df.columns = [col.replace('\ufeff', '').strip() for col in df.columns]
        
        # ป้องกัน KeyError ด้วยการเช็คคอลัมน์ก่อนแปลงค่า
        required_cols = ['Card ID', 'Card Name JP', 'Card Number']
        for col in required_cols:
            if col in df.columns:
                df[col] = df[col].astype(str)
            else:
                st.warning(f"ระวัง: ไม่พบคอลัมน์ '{col}' ในฐานข้อมูล")
                
        return df
    except Exception as e:
        st.error(f"ระบบฐานข้อมูลขัดข้อง: {e}")
        return pd.DataFrame()

# ==========================================
# 2. ฟังก์ชันผู้ช่วย
# ==========================================
def classify_condition(name_text):
    if any(keyword in name_text for keyword in ["傷", "キズ", "状態C", "状態B", "特価"]):
        return "มีตำหนิ (Play Condition)"
    return "สภาพปกติ"

def clean_card_number(raw_num, box_code=""):
    cleaned = re.sub(r'[^A-Za-z0-9/]', '', raw_num)
    # ใช้ box_code ที่ส่งเข้ามาแบบไดนามิก ป้องกันการพังเมื่อเปลี่ยนกล่อง
    if box_code and box_code.upper() in cleaned.upper():
        cleaned = re.split(box_code, cleaned, flags=re.IGNORECASE)[-1]
    return cleaned.strip()

# ==========================================
# 3. ระบบ Scraping
# ==========================================
@st.cache_data(ttl=300) 
def scrape_yuyutei_full_box(box_code):
    # ปั้น URL แบบไดนามิกตามกล่องที่เลือก
    safe_box_code = box_code.lower().replace("-", "") if box_code else ""
    url = f"https://yuyu-tei.jp/sell/dm/s/{safe_box_code}"
    headers = {"User-Agent": "Mozilla/5.0"}
    
    box_prices = {} 
    
    try:
        response = requests.get(url, headers=headers, timeout=15)
        response.encoding = 'utf-8'
        soup = BeautifulSoup(response.text, 'html.parser')
        
        card_containers = soup.select('div.col-md')
        
        for container in card_containers:
            num_element = container.select_one('span.border-dark')
            if not num_element: continue
                
            raw_num = num_element.text.strip()
            clean_num = clean_card_number(raw_num, box_code)
            
            name_element = container.select_one('h4.text-primary')
            if not name_element: continue
                
            card_name = name_element.text.strip()
            link_element = name_element.find_parent('a')
            card_link = urljoin("https://yuyu-tei.jp", link_element.get('href')) if link_element else url
            
            price_element = container.select_one('strong.text-end')
            if not price_element: continue
                
            price_digits = "".join([c for c in price_element.text if c.isdigit()])
            img_element = container.select_one('img.card.img-fluid')
            img_src = urljoin("https://yuyu-tei.jp", img_element.get('src', '')) if img_element else None
            
            # 🎯 ระบบเช็คสต็อก Yuyutei แบบใหม่ตามที่นายแกะมา!
            stock_status = "มีสินค้า" # ตั้งค่าเริ่มต้นไว้ก่อน
            zaiko_label = container.select_one('.cart_sell_zaiko')
            if zaiko_label and '×' in zaiko_label.text:
                stock_status = "สินค้าหมด"
            
            if price_digits:
                item_info = {
                    "รูปภาพ": img_src, 
                    "ร้านค้า": "Yuyutei", 
                    "ชื่อที่แสดง": card_name,
                    "สภาพการ์ด": classify_condition(card_name),
                    "ราคา (เยน)": int(price_digits), 
                    "สถานะ": stock_status, # อัปเดตตัวแปรตรงนี้
                    "ลิงก์สินค้า": card_link
                }
                
                if clean_num not in box_prices:
                    box_prices[clean_num] = []
                box_prices[clean_num].append(item_info)
                
        return box_prices
    except Exception as e:
        print(f"Error scraping box {box_code}: {e}")
        return {}

@st.cache_data(ttl=300)
def scrape_bigweb(card_name, card_num):
    bw_target_num = card_num.replace('(', '').replace(')', '').strip()
    core_name = card_name.split('＜')[0].split('(')[0].strip()
    encoded_kw = quote(core_name)
    base_search_url = f"https://www.bigweb.co.jp/ja/products/dm/list?name={encoded_kw}"
    headers = {"User-Agent": "Mozilla/5.0"}
    raw_results = []
    
    try:
        api_url = "https://api.bigweb.co.jp/products"
        params = {
            "game_id": 4, "name": core_name, "is_box": 0,
            "is_supply": 0, "is_purchase": 0, "page": 1
        }
        
        response = requests.get(api_url, params=params, headers=headers, timeout=10)
        
        if response.status_code == 200:
            data = response.json()
            items = data.get('items', []) or data.get('data', [])
            
            for item in items:
                item_str = str(item).replace(' ', '')
                match_exact = bw_target_num in item_str
                match_variant = ("秘" in bw_target_num and "サイン" in item_str)
                    
                if not (match_exact or match_variant):
                    continue 
                    
                name = item.get('name', 'ไม่ทราบชื่อ')
                if match_variant and "サイン" not in name:
                    name = f"{name} (Sign Secret)"
                    
                price = item.get('price', 0)
                stock_count = int(item.get('stock_count', 0))
                is_sold_out = item.get('is_sold_out', False)
                stock_status = "มีสินค้า" if (stock_count > 0 and not is_sold_out) else "สินค้าหมด"
                item_id = str(item.get('id', ''))
                specific_link = f"https://www.bigweb.co.jp/ja/products/dm/cardViewer/{item_id}" if item_id else base_search_url
                
                if price:
                    raw_results.append({
                        "รูปภาพ": None, # 🎯 ไม่ดึงรูปภาพของ Bigweb
                        "ร้านค้า": "Bigweb", 
                        "ชื่อที่แสดง": name,
                        "สภาพการ์ด": classify_condition(name),
                        "ราคา (เยน)": int(price), 
                        "สถานะ": stock_status,
                        "ลิงก์สินค้า": specific_link
                    })
        
        if not raw_results:
             return [{
                 "รูปภาพ": None, "ร้านค้า": "Bigweb", "ชื่อที่แสดง": f"ไม่พบข้อมูล {card_name} ({bw_target_num})", 
                 "สภาพการ์ด": "-", "ราคา (เยน)": 0, "สถานะ": "สินค้าหมด", "ลิงก์สินค้า": base_search_url
             }]       
        return raw_results
    except Exception as e:
        return [{
            "รูปภาพ": None, "ร้านค้า": "Bigweb (Fallback)", "ชื่อที่แสดง": f"Error: {e}",
            "สภาพการ์ด": "-", "ราคา (เยน)": 0, "สถานะ": "ดึงข้อมูลล้มเหลว", "ลิงก์สินค้า": base_search_url
        }]

@st.cache_data(ttl=300)
def scrape_dorasuta(card_name, card_num, card_rarity="", box_code=""):
    search_keyword = card_name.strip()
    raw_num = card_num.strip() # เอาแบบดิบๆ ไม่ตัดวงเล็บ
    target_rarity = card_rarity.strip().upper()
    
    # ตัวนี้โดนตัดชื่อทิ้ง เอาไว้ใช้เฉพาะตอนค้นหา "วิธีที่ 2" เท่านั้น
    clean_search_core = search_keyword.split('［')[0].split('[')[0].split('＜')[0].split('<')[0].split('(')[0].split('（')[0].strip()
    
    # 🎯 แก้ตรงนี้! ใช้ชื่อเต็ม (search_keyword) + รหัสเต็ม (raw_num)
    primary_query = f"{search_keyword}{raw_num}"
    encoded_primary = quote(primary_query, safe='!<>/?()（）') 
    fallback_url = f"https://dorasuta.jp/duelmasters/product-list?kw={encoded_primary}"
    # ดึง ZENROWS_API_KEY จากด้านบนสุดของไฟล์ 
    if not ZENROWS_API_KEY or ZENROWS_API_KEY == "YOUR_ZENROWS_API_KEY":
        return [{"รูปภาพ": None, "ร้านค้า": "Dorasuta", "ชื่อที่แสดง": "API Key Error", "สภาพการ์ด": "-", "ราคา (เยน)": 0, "สถานะ": "ตรวจสอบผ่านเว็บ", "ลิงก์สินค้า": fallback_url}]

    api_url = "https://api.zenrows.com/v1/"

    def _scrape_with_query(query_str, filter_by_rarity=False):
        encoded_kw = quote(query_str, safe='!<>/?()（）') 
        target_url = f"https://dorasuta.jp/duelmasters/product-list?kw={encoded_kw}"
        
        params = {
            "apikey": ZENROWS_API_KEY, "url": target_url, "js_render": "true",
            "premium_proxy": "true", "wait": "3000"
        }
        
        try:
            response = requests.get(api_url, params=params, timeout=60)
            if response.status_code != 200: return []

            response.encoding = 'utf-8'
            soup = BeautifulSoup(response.text, 'html.parser')
            descriptions_list = soup.select('.description')
            
            results = []
            for desc_el in descriptions_list:
                a_tag = desc_el.select_one('a')
                if not a_tag: continue
                
                full_name = a_tag.text.strip()
                link = urljoin("https://dorasuta.jp", a_tag.get('href'))
                
                # 🎯 ถ้านี่คือการหาแบบวิธีที่ 2 (กรองด้วย Rarity) ให้เช็คระดับความหายาก
                if filter_by_rarity and target_rarity:
                    change_hight_li = desc_el.select_one('li.change_hight')
                    if change_hight_li:
                        p_tags = change_hight_li.select('p')
                        # ถ้าระดับความหายากไม่ตรงกับใน DB ให้ข้ามกล่องนี้ไปเลย
                        if not any(p.text.strip().upper() == target_rarity for p in p_tags):
                            continue
                    else: continue
                
                # หาราคา
                price = 0
                for el in desc_el.select('li, p, span, div'):
                    if '円' in el.text:
                        price_digits = "".join([c for c in el.text if c.isdigit()])
                        if price_digits:
                            parsed_price = int(price_digits)
                            if 0 < parsed_price < 1000000:
                                price = parsed_price
                                break
                                
                # เช็คสถานะสินค้า
                parent_element = desc_el.find_parent('div', class_=lambda c: c and 'element' in c)
                operation_el = parent_element.select_one('.operation') if parent_element else desc_el.find_next_sibling('div', class_='operation')
                stock_status = "มีสินค้า" if (operation_el and operation_el.select_one('.popup_link')) else "สินค้าหมด"
                
                if price > 0:
                    results.append({
                        "รูปภาพ": None, # 🎯 ไม่ดึงรูปภาพของ Dorasuta
                        "ร้านค้า": "Dorasuta", "ชื่อที่แสดง": full_name,
                        "สภาพการ์ด": classify_condition(full_name), "ราคา (เยน)": price,
                        "สถานะ": stock_status, "ลิงก์สินค้า": link
                    })
            return results
        except Exception as e:
            print(f"Error in Dorasuta query '{query_str}': {e}")
            return []

    # 🚀 [วิธีที่ 1] เสิร์ชด้วย "ชื่อการ์ด + รหัส" แล้วกวาดทุกอย่างที่ขวางหน้า (ไม่ใช้ Rarity)
    raw_results = _scrape_with_query(primary_query, filter_by_rarity=False)
    
    # 🚀 [วิธีที่ 2] ถ้าวิธีแรกหาไม่เจอ ค้นหาด้วย "ชื่อการ์ดเพียวๆ" แล้วส่องหา Rarity ที่ตรงกับในระบบ
    if not raw_results and target_rarity:
        raw_results = _scrape_with_query(clean_search_core, filter_by_rarity=True)
        
    # 🚀 [วิธีสุดท้าย] ถ้ายังหาไม่เจออีก ส่งลิงก์ Fallback (ชื่อ + รหัส) ให้กดดูเอง
    if not raw_results:
        return [{
            "รูปภาพ": None, "ร้านค้า": "Dorasuta", "ชื่อที่แสดง": f"ไม่พบข้อมูล {primary_query}", 
            "สภาพการ์ด": "-", "ราคา (เยน)": 0, "สถานะ": "สินค้าหมด", 
            "ลิงก์สินค้า": fallback_url
        }]
        
    return raw_results

# ==========================================
# 4. ฟังก์ชันประมวลผลข้อมูล (แยกออกจากหน้า UI)
# ==========================================
def compile_card_prices(box_name, card_name, card_num, card_rarity): # 🎯 ลบ image_url ออกแล้ว
    clean_target_num = clean_card_number(card_num, box_code=box_name)
    
    # 1. Yuyutei
    full_box_yuyutei = scrape_yuyutei_full_box(box_name)
    res_yuyutei = full_box_yuyutei.get(clean_target_num, [])
    if not res_yuyutei:
        res_yuyutei = [{
            "รูปภาพ": None, "ร้านค้า": "Yuyutei", "ชื่อที่แสดง": f"ไม่พบรหัส {clean_target_num}", 
            "สภาพการ์ด": "-", "ราคา (เยน)": 0, "สถานะ": "สินค้าหมด", "ลิงก์สินค้า": "https://yuyu-tei.jp/"
        }]
        
    # 2. Bigweb
    res_bigweb = scrape_bigweb(card_name, card_num)
    
    # 3. Dorasuta (ส่ง box_code ตามที่สัญญาไว้แล้ว!)
    res_dorasuta = scrape_dorasuta(card_name, card_num, card_rarity, box_code=box_name)
    
    all_results = res_yuyutei + res_bigweb + res_dorasuta
    
    # 🎯 คำนวณเงินบาทอย่างเดียว ไม่มีการยัด Master Image แล้ว
    for res in all_results:
        res["ราคาบาทโดยประมาณ"] = f"{int(res['ราคา (เยน)'] * EXCHANGE_RATE):,} บาท" if res["ราคา (เยน)"] > 0 else "-"

    final_df = pd.DataFrame(all_results)
    
    # จัดเรียงคอลัมน์ให้เป็นระเบียบ
    cols = ["รูปภาพ", "ร้านค้า", "ชื่อที่แสดง", "สภาพการ์ด", "ราคา (เยน)", "ราคาบาทโดยประมาณ", "สถานะ", "ลิงก์สินค้า"]
    return final_df[[c for c in cols if c in final_df.columns]]

# ==========================================
# 5. ส่วนจัดการ UI (สะอาดและอ่านง่ายขึ้นมาก)
# ==========================================
st.set_page_config(page_title="Card Price Checker", layout="wide")

if 'selected_box' not in st.session_state:
    st.session_state.selected_box = None

def go_back_to_home():
    st.session_state.selected_box = None

db_df = load_database(SHEET_CSV_URL)

if st.session_state.selected_box is None:
    st.title("📦 คลังข้อมูล Duel Masters")
    st.markdown("เลือกชุดการ์ดเพื่อตรวจสอบราคา")
    
    if not db_df.empty and 'Box Name' in db_df.columns:
        boxes = db_df['Box Name'].dropna().unique().tolist()
        cols = st.columns(4)
        for idx, box in enumerate(boxes):
            with cols[idx % 4]:
                if st.button(f"🃏 {box}", use_container_width=True):
                    st.session_state.selected_box = box
                    st.rerun()
    else:
        st.warning("กำลังโหลดฐานข้อมูล หรือไม่พบคอลัมน์ 'Box Name'")
else:
    st.button("⬅️ ย้อนกลับ", on_click=go_back_to_home)
    st.title(f"📖 ชุดการ์ด: {st.session_state.selected_box}")
    
    cards_in_box = db_df[db_df['Box Name'] == st.session_state.selected_box]
    search_query = st.text_input("🔍 ค้นหาการ์ด (รหัสหรือชื่อ):")
    
    if search_query:
        filtered_cards = cards_in_box[
            cards_in_box['Card ID'].str.contains(search_query, case=False, na=False) | 
            cards_in_box['Card Name JP'].str.contains(search_query, case=False, na=False)
        ]
    else:
        filtered_cards = cards_in_box
        
    st.dataframe(filtered_cards[['Card ID', 'Card Name JP', 'Rarity']], hide_index=True, use_container_width=True, height=250)
    
    st.markdown("---")
    st.subheader("เปรียบเทียบราคา")
    
    card_options = filtered_cards['Card ID'] + " - " + filtered_cards['Card Name JP']
    if not card_options.empty:
        selected_card_display = st.selectbox("🎯 เลือกการ์ดที่ต้องการเช็คราคา:", card_options)
        selected_id = selected_card_display.split(" - ")[0]
        selected_row = filtered_cards[filtered_cards['Card ID'] == selected_id].iloc[0]
    
        if st.button("🚀 ตรวจสอบราคาทั้ง 3 เว็บไซต์", type="primary"):
            with st.spinner("กำลังดึงข้อมูลเทียบราคา..."):
                target_box = st.session_state.selected_box
                target_name = str(selected_row['Card Name JP']).strip()
                target_num = str(selected_row['Card Number']).strip()
                target_rarity = str(selected_row['Rarity']).strip()
                
                # 🎯 ไม่ต้องอ้างอิง Image URL จาก row อีกแล้ว ส่งแค่ 4 ค่าเพียวๆ ไปเลย
                final_df = compile_card_prices(target_box, target_name, target_num, target_rarity)
                
                st.success("รวบรวมข้อมูลเสร็จสิ้น!")
                st.dataframe(
                    final_df,
                    column_config={
                        "รูปภาพ": st.column_config.ImageColumn("ภาพประกอบ"),
                        "ลิงก์สินค้า": st.column_config.LinkColumn("หน้าร้านค้า")
                    },
                    hide_index=True, use_container_width=True
                )
    else:
        st.info("ไม่พบการ์ดที่ค้นหา")