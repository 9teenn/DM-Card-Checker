import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote
import re
from datetime import datetime, timedelta
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import json

# ==========================================
# 0. CONFIGURATIONS (ตั้งค่าตัวแปรคงที่ไว้ที่เดียว)
# ==========================================
SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vQZxMlGzqgFbyEEfEcv3HKVG5DGHOrlVWuHvI6nnSUcg5c9e3lNf4I2vU5WP8AMhT5gMr_7Oq7yOP3m/pub?gid=437587964&single=true&output=csv"
ZENROWS_API_KEY = st.secrets["ZENROWS_API_KEY"] 
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

def log_bug_to_sheet(box_name, card_num, card_name, store_name, reason):
    try:
        scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
        
        # 🎯 เปลี่ยนจากการอ่านไฟล์ มาเป็นการดึงข้อความจาก Secrets แล้วแปลงกลับเป็น Dictionary
        creds_dict = json.loads(st.secrets["GCP_CREDENTIALS"])
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        
        client = gspread.authorize(creds)
        
        sheet = client.open("DM_Master_Database").worksheet("Bug_Report")
        
        # ประทับเวลาปัจจุบัน
        thai_time = datetime.utcnow() + timedelta(hours=7)
        timestamp = thai_time.strftime("%Y-%m-%d %H:%M:%S")
        
        # จัดเรียงข้อมูลให้ตรงกับคอลัมน์แล้วยิงขึ้น Sheet
        row_data = [timestamp, box_name, card_num, card_name, store_name, reason]
        sheet.append_row(row_data)
        
    except Exception as e:
        print(f"ระบบ Report Bug ทำงานล้มเหลว: {e}")
        st.error(f"⚠️ แจ้งเตือนจากระบบ Report Bug: {e}")

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

def calculate_tiered_price(price):
    if price <= 0:
        return 0
        
    # ต่ำกว่า 100 เยน -> ปัดเป็น 100
    if price < 100:
        return 100 
        
    # หลักร้อย (100 - 999) -> ปัดทีละ 50 หรือ 100
    elif price < 1000:
        base = (price // 100) * 100
        remainder = price % 100
        
        if remainder == 0:
            return price
        elif remainder <= 50:
            return base + 50          
        else:
            return base + 100         
            
    # หลักพัน (1,000 - 9,999) -> ปัดทีละ 500 หรือ 1,000
    elif price < 10000:
        base = (price // 1000) * 1000
        remainder = price % 1000
        
        if remainder == 0:
            return price
        elif remainder <= 500:
            return base + 500         
        else:
            return base + 1000        
            
    # หลักหมื่นและหลักแสน (10,000 เยนขึ้นไป) -> ล็อกการปัดทีละ 5,000 หรือ 10,000
    else:
        # ใช้หลักหมื่นเป็นฐานคำนวณ แม้ราคาจะทะลุหลักแสนก็ตาม
        base = (price // 10000) * 10000
        remainder = price % 10000
        
        if remainder == 0:
            return price
        elif remainder <= 5000:
            return base + 5000         # เช่น 12,000 -> 15,000 / 112,000 -> 115,000
        else:
            return base + 10000        # เช่น 16,000 -> 20,000 / 116,000 -> 120,000

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
            
            # 🎯 ระบบสแกนราคา "ตั้งต้น" เสมอ!
            price_digits = ""
            
            # 1. พยายามหาราคาตั้งต้น (ราคาขีดฆ่าเวลาเซลล์) ก่อน
            original_price_el = container.select_one('small.d-block.text-end.fs-9')
            
            if original_price_el:
                # เจอคลาสนี้ แปลว่าจัดเซลล์! เราจะดูดตัวเลขจากตัวที่ขีดฆ่ามาเป็นราคาหลัก
                price_digits = "".join([c for c in original_price_el.text if c.isdigit()])
            else:
                # ไม่เจอ แปลว่าเป็นราคาปกติ ให้ดึงจากป้ายราคาปกติ
                main_price_el = container.select_one('strong.text-end')
                if main_price_el:
                    price_digits = "".join([c for c in main_price_el.text if c.isdigit()])
                    
            if not price_digits: 
                continue
                
            img_element = container.select_one('img.card.img-fluid')
            img_src = urljoin("https://yuyu-tei.jp", img_element.get('src', '')) if img_element else None
            
            # 🎯 ระบบเช็คสต็อก Yuyutei (Zaiko)
            stock_status = "มีสินค้า"
            zaiko_label = container.select_one('.cart_sell_zaiko')
            if zaiko_label and '×' in zaiko_label.text:
                stock_status = "สินค้าหมด"
            
            item_info = {
                "รูปภาพ": img_src, 
                "ร้านค้า": "Yuyutei", 
                "ชื่อที่แสดง": card_name,
                "สภาพการ์ด": classify_condition(card_name),
                "ราคาเยน (ปัดเศษให้แล้วนะจ๊ะ)": int(price_digits), 
                "สถานะ": stock_status, 
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
    core_name = card_name.split('＜')[0].split('(')[0].replace('～', '').replace('~', '').strip()
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
                        "ราคาเยน (ปัดเศษให้แล้วนะจ๊ะ)": int(price), 
                        "สถานะ": stock_status,
                        "ลิงก์สินค้า": specific_link
                    })
        
        if not raw_results:
             return [{
                 "รูปภาพ": None, "ร้านค้า": "Bigweb", "ชื่อที่แสดง": f"ไม่พบข้อมูล {card_name} ({bw_target_num})", 
                 "สภาพการ์ด": "-", "ราคาเยน (ปัดเศษให้แล้วนะจ๊ะ)": 0, "สถานะ": "สินค้าหมด", "ลิงก์สินค้า": base_search_url
             }]       
        return raw_results
    except Exception as e:
        return [{
            "รูปภาพ": None, "ร้านค้า": "Bigweb (Fallback)", "ชื่อที่แสดง": f"Error: {e}",
            "สภาพการ์ด": "-", "ราคาเยน (ปัดเศษให้แล้วนะจ๊ะ)": 0, "สถานะ": "ดึงข้อมูลล้มเหลว", "ลิงก์สินค้า": base_search_url
        }]

@st.cache_data(ttl=300)
def scrape_dorasuta(card_name, card_num, card_rarity="", box_code=""):
    search_keyword = card_name.strip()
    raw_num = card_num.strip()
    target_rarity = card_rarity.strip().upper()
    
    clean_search_core = search_keyword.split('［')[0].split('[')[0].split('＜')[0].split('<')[0].split('(')[0].split('（')[0].strip()
    
    # 🎯 อัปเดต: เคาะเว้นวรรค 1 ที ระหว่างชื่อและรหัสการ์ด เพื่อให้ระบบค้นหาเว็บญี่ปุ่นทำงานได้ถูกต้อง!
    primary_query = f"{search_keyword} {raw_num}"
    encoded_primary = quote(primary_query, safe='!<>/?()（）') 
    fallback_url = f"https://dorasuta.jp/duelmasters/product-list?kw={encoded_primary}"
    
    if not ZENROWS_API_KEY or ZENROWS_API_KEY == "YOUR_ZENROWS_API_KEY":
        return [{"รูปภาพ": None, "ร้านค้า": "Dorasuta", "ชื่อที่แสดง": "API Key Error", "สภาพการ์ด": "-", "ราคาเยน (ปัดเศษให้แล้วนะจ๊ะ)": 0, "สถานะ": "ตรวจสอบผ่านเว็บ", "ลิงก์สินค้า": fallback_url}]

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
                        "สภาพการ์ด": classify_condition(full_name), "ราคาเยน (ปัดเศษให้แล้วนะจ๊ะ)": price,
                        "สถานะ": stock_status, "ลิงก์สินค้า": link
                    })
            return results
        except Exception as e:
            print(f"Error in Dorasuta query '{query_str}': {e}")
            return []

    # 🚀 [วิธีที่ 1] เสิร์ชด้วย "ชื่อการ์ด + รหัส" แล้วกวาดทุกอย่างที่ขวางหน้า (ไม่ใช้ Rarity)
    raw_results = _scrape_with_query(primary_query, filter_by_rarity=False)
    
    # เสิร์ชก๊อกสอง
    if not raw_results and target_rarity:
        raw_results = _scrape_with_query(clean_search_core, filter_by_rarity=True)
        
    if not raw_results:
        return [{
            "รูปภาพ": None, "ร้านค้า": "Dorasuta", "ชื่อที่แสดง": f"ไม่พบข้อมูล {primary_query}", 
            "สภาพการ์ด": "-", "ราคาเยน (ปัดเศษให้แล้วนะจ๊ะ)": 0, "สถานะ": "สินค้าหมด", 
            "ลิงก์สินค้า": fallback_url
        }]
        
    return raw_results

# ==========================================
# 4. ฟังก์ชันประมวลผลข้อมูล (แยกออกจากหน้า UI)
# ==========================================
def compile_card_prices(box_name, card_name, card_num, card_rarity): 
    clean_target_num = clean_card_number(card_num, box_code=box_name)
    
    # --- เช็คฝั่ง Yuyutei ---
    full_box_yuyutei = scrape_yuyutei_full_box(box_name)
    res_yuyutei = full_box_yuyutei.get(clean_target_num, [])
    if not res_yuyutei:
        res_yuyutei = [{
            "รูปภาพ": None, "ร้านค้า": "Yuyutei", "ชื่อที่แสดง": f"ไม่พบรหัส {clean_target_num}", 
            "สภาพการ์ด": "-", "ราคาเยน (ปัดเศษให้แล้วนะจ๊ะ)": 0, "สถานะ": "สินค้าหมด", "ลิงก์สินค้า": "https://yuyu-tei.jp/"
        }]
        # 🎯 เจอจุดพัง ยิง Report เลย!
        log_bug_to_sheet(box_name, card_name, card_num, "Yuyutei", "ดึงจากกล่องแบบ Full Box ไม่เจอ อาจจะเลขรหัสเพี้ยน")
        
    # --- เช็คฝั่ง Bigweb ---
    res_bigweb = scrape_bigweb(card_name, card_num)
    if not res_bigweb or "ไม่พบข้อมูล" in res_bigweb[0]["ชื่อที่แสดง"]:
        # 🎯 เจอจุดพัง ยิง Report!
        log_bug_to_sheet(box_name, card_name,card_num, "Bigweb", "API JSON ไม่ตอบกลับ หรือหาการ์ดไม่เจอ")
        
    # --- เช็คฝั่ง Dorasuta ---
    res_dorasuta = scrape_dorasuta(card_name, card_num, card_rarity, box_code=box_name)
    if not res_dorasuta or "ไม่พบข้อมูล" in res_dorasuta[0]["ชื่อที่แสดง"]:
        # 🎯 เจอจุดพัง ยิง Report!
        log_bug_to_sheet(box_name, card_name, card_num, "Dorasuta", "ZenRows ไม่เจอข้อมูลทั้ง 2 ก๊อก (เว้นวรรคอาจจะยังไม่พอ)")
    
    # รวมข้อมูลทั้งหมด...
    all_results = res_yuyutei + res_bigweb + res_dorasuta
    
    # 🎯 ยัด "รหัสการ์ด", "ราคาปัดเศษ" และ "คำนวณเงินบาท"
    for res in all_results:
        res["รหัสการ์ด"] = card_num 
        
        # ดึงราคาเยนดั้งเดิมมาเข้าเครื่องปัดเศษ
        original_yen = res['ราคาเยน (ปัดเศษให้แล้วนะจ๊ะ)']
        rounded_yen = calculate_tiered_price(original_yen)
        
        # แทนที่ราคาเยนเดิมด้วยราคาปัดเศษ (หรือถ้านายอยากเก็บไว้เปรียบเทียบ ก็สร้างคอลัมน์ใหม่ได้)
        res['ราคาเยน (ปัดเศษให้แล้วนะจ๊ะ)'] = rounded_yen 
        
        # คำนวณเงินบาทจากราคาที่ปัดเศษแล้ว
        res["ราคาบาทโดยประมาณ"] = f"{int(rounded_yen * EXCHANGE_RATE):,} บาท" if rounded_yen > 0 else "-"

final_df = pd.DataFrame(all_results)
    
    # 🎯 1. ร่ายคาถาเปลี่ยนชื่อคอลัมน์ใน DataFrame ก่อน!
    final_df.rename(columns={"ราคา (เยน)": "ราคาเยน (ปัดเศษแล้วนะจ๊ะ)"}, inplace=True)
    
    # 🎯 2. ตัด "รูปภาพ" ออก และใช้ชื่อคอลัมน์ใหม่ที่เพิ่งตั้งในรายการจัดเรียง
    cols = [
        "ร้านค้า", 
        "รหัสการ์ด", 
        "ชื่อที่แสดง", 
        "สภาพการ์ด", 
        "ราคาเยน (ปัดเศษแล้วนะจ๊ะ)", # ใช้ชื่อใหม่ตรงนี้ได้เลย
        "ราคาบาทโดยประมาณ", 
        "สถานะ", 
        "ลิงก์สินค้า"
    ]
    
    return final_df[[c for c in cols if c in final_df.columns]]
# ==========================================
# 5. ส่วนจัดการ UI
# ==========================================
st.set_page_config(page_title="Card Price Checker", layout="wide")

if 'selected_box' not in st.session_state:
    st.session_state.selected_box = None

def go_back_to_home():
    st.session_state.selected_box = None

db_df = load_database(SHEET_CSV_URL)

if st.session_state.selected_box is None:
    st.title("คลังข้อมูล Duel Masters")
    st.markdown("เลือกชุดการ์ดเพื่อตรวจสอบราคา")
    
    if not db_df.empty and 'Box Name' in db_df.columns:
        boxes = db_df['Box Name'].dropna().unique().tolist()
        
        # ช่องค้นหา Box Set
        search_box = st.text_input("🔍 ค้นหาชื่อ Box Set (เช่น EX3, DM25):")
        if search_box:
            boxes = [b for b in boxes if search_box.lower() in b.lower()]
            
        # จัดกลุ่ม Box ตามปี (เช่น 'DM26', 'DM25')
        year_groups = {}
        for box in boxes:
            year_prefix = box.split('-')[0] 
            if year_prefix not in year_groups:
                year_groups[year_prefix] = []
            year_groups[year_prefix].append(box)
            
        # เรียงปีจากมากไปน้อย (DM26 จะอยู่ซ้ายสุด ตามด้วย DM25)
        sorted_years = sorted(year_groups.keys(), reverse=True)
        
        if sorted_years:
            # สร้างคอลัมน์ตามจำนวนปีที่พบ
            cols = st.columns(len(sorted_years))
            for i, year in enumerate(sorted_years):
                with cols[i]:
                    st.markdown(f"**🗓️ ซีรีส์ {year}**")
                    for box in year_groups[year]:
                        if st.button(f"📦 {box}", use_container_width=True):
                            st.session_state.selected_box = box
                            st.rerun()
        else:
            st.info("ไม่พบ Box Set ที่ค้นหา")
    else:
        st.warning("กำลังโหลดฐานข้อมูล หรือไม่พบคอลัมน์ 'Box Name'")
        
else:
    # โซนแสดงผลเมื่อกดเลือกกล่องแล้ว
    st.button("⬅️ ย้อนกลับ", on_click=go_back_to_home)
    st.title(f"📖 ชุดการ์ด: {st.session_state.selected_box}")
    
    # 🎯 เพิ่มตัวเช็คกันเหนียว ป้องกันการเกิด KeyError
    if 'Box Name' not in db_df.columns:
        st.error("❌ ฐานข้อมูลผิดพลาด: ไม่พบคอลัมน์ 'Box Name' กรุณาตรวจสอบลิงก์ CSV")
    else:
        # 1. กรองข้อมูลจาก Database ให้เหลือแค่กล่องที่เลือก
        box_data = db_df[db_df['Box Name'] == st.session_state.selected_box]
        
        if box_data.empty:
            st.warning("ยังไม่มีข้อมูลการ์ดในชุดนี้ (อาจต้องรันอัปเดต Database ก่อน)")
        else:
            # 🎯 ดันโค้ดเข้ามา 4 เคาะ ให้อยู่ "ข้างใน" else
            search_card = st.text_input(f"🔍 ค้นหาการ์ดใน {st.session_state.selected_box} (พิมพ์ชื่อ หรือ รหัส):")
            
            if search_card:
                box_data = box_data[
                    box_data['Card Name JP'].str.contains(search_card, case=False, regex=False, na=False) |
                    box_data['Card Number'].str.contains(search_card, case=False, regex=False, na=False)
                ]
                
            st.markdown(f"พบการ์ดทั้งหมด **{len(box_data)}** ใบ")
        # 3. วนลูปสร้าง Expander โชว์การ์ดทีละใบ
        for idx, row in box_data.iterrows():
            card_num = str(row.get('Card Number', ''))
            card_name = str(row.get('Card Name JP', ''))
            card_rarity = str(row.get('Rarity', ''))
            img_url = str(row.get('Image URL', ''))
            
            # 🎯 พระเอกของเราอยู่ตรงนี้! สับเปลี่ยนโฟลเดอร์รูปภาพกลางอากาศ
            high_res_img_url = img_url.replace("/100_140/", "/front/")
            
            with st.expander(f"📌 {card_num} | {card_name} [{card_rarity}]"):
                col1, col2 = st.columns([1, 4])
                
                with col1:
                    # 🎯 สั่งให้ Streamlit โชว์ลิงก์ใหม่ที่ชัดกว่าเดิม
                    if high_res_img_url and high_res_img_url.startswith("http"):
                        st.image(high_res_img_url, use_container_width=True)
                    else:
                        st.info("ไม่มีรูปภาพ")
                        
                with col2:
                    if st.button(f"📊 เช็คราคาล่าสุด", key=f"btn_{st.session_state.selected_box}_{card_num}"):
                        with st.spinner("กำลังเจาะระบบดึงราคา..."):
                            df_prices = compile_card_prices(
                                box_name=st.session_state.selected_box, 
                                card_name=card_name, 
                                card_num=card_num, 
                                card_rarity=card_rarity
                            )
                            
                            # โชว์ตารางแบบสวยงาม รองรับการแสดงผลรูปภาพและลิงก์
                            st.dataframe(
                                df_prices,
                                column_config={
                                    "ลิงก์สินค้า": st.column_config.LinkColumn("ลิงก์สินค้า")
                                },
                                hide_index=True,
                                use_container_width=True
                            )
