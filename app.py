import streamlit as st
import os
import time
import urllib.parse
import requests
import shutil
import tempfile
import json
import pandas as pd
from bs4 import BeautifulSoup
import google.generativeai as genai
import datetime
import gc  # メモリ解放用
import re   # JSONクリーニング用

# --- 画面設定 ---
st.set_page_config(page_title="産廃報告書AI抽出アプリ", layout="wide")

st.title("📄 産廃報告書データ抽出・台帳作成アプリ")
st.markdown("""
**「Web自動収集」** または **「手動アップロード」** で、報告書データを抽出して一覧化します。
**PDFファイル** と **Excelファイル** の両方に対応しています。
""")

# --- セッションステート初期化 ---
if 'history' not in st.session_state:
    st.session_state['history'] = []
if 'processed_urls' not in st.session_state:
    st.session_state['processed_urls'] = set()
if 'is_running' not in st.session_state:
    st.session_state['is_running'] = False
# 監査用：Web上の全ファイルリストを保持（順序保持リスト）
if 'all_target_files' not in st.session_state:
    st.session_state['all_target_files'] = []

# --- サイドバー：設定 ---
with st.sidebar:
    st.header("設定")
    
    if "GEMINI_API_KEY" in st.secrets:
        api_key = st.secrets["GEMINI_API_KEY"]
        st.success("🔑 APIキーを自動で読み込みました")
    else:
        api_key = st.text_input("Gemini APIキー", type="password", help="Google AI Studioで取得したキーを入力してください")

    st.markdown("---")
    if st.button("🗑️ 履歴と記憶を全クリア"):
        st.session_state['history'] = []
        st.session_state['processed_urls'] = set()
        st.session_state['is_running'] = False
        st.session_state['all_target_files'] = []
        st.rerun()

    if api_key:
        genai.configure(api_key=api_key)
    st.info("※APIキーがない場合、動作しません。")

# ==========================================
# ロジック関数群
# ==========================================

# --- ヘルパー：日本時間取得 ---
def get_jst_now_str():
    """現在時刻を日本時間(JST)の文字列で返す"""
    jst = datetime.timezone(datetime.timedelta(hours=9))
    return datetime.datetime.now(jst).strftime("%Y-%m-%d %H:%M:%S")

# --- ヘルパー：JSONクリーニング関数 ---
def clean_json_response(text):
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    match = re.search(r'\[.*\]', text, re.DOTALL)
    if match:
        return match.group(0)
    return text

# --- Excel強力読み取り関数 (Pythonロジック) ---
def read_excel_robust(file_path):
    extracted_data = []
    try:
        xls = pd.ExcelFile(file_path)
        for sheet_name in xls.sheet_names:
            try:
                df = pd.read_excel(xls, sheet_name=sheet_name, header=None)
            except Exception:
                continue
            
            target_row_idx = -1
            col_mapping = {} 
            for r_idx, row in df.iterrows():
                row_str = row.astype(str).values
                if any("廃棄物の種類" in s for s in row_str) or any("産業廃棄物の種類" in s for s in row_str):
                    target_row_idx = r_idx
                    for c_idx, cell_val in enumerate(row_str):
                        val = str(cell_val).replace("\n", "").replace(" ", "")
                        if "種類" in val:
                            col_mapping["kind"] = c_idx
                        elif "全処理委託量" in val or "委託量" in val:
                            col_mapping["amount"] = c_idx
                    break 
            
            if target_row_idx != -1 and "kind" in col_mapping and "amount" in col_mapping:
                start_row = target_row_idx + 1
                for i in range(start_row, len(df)):
                    if col_mapping["kind"] >= len(df.columns) or col_mapping["amount"] >= len(df.columns):
                        continue
                    kind_val = df.iloc[i, col_mapping["kind"]]
                    amount_val = df.iloc[i, col_mapping["amount"]]
                    if pd.notna(kind_val) and pd.notna(amount_val):
                        try:
                            amt_str = str(amount_val).replace(",", "").strip()
                            amt = float(amt_str)
                            waste_type = str(kind_val).strip()
                            if "合計" in waste_type or waste_type == "" or waste_type == "nan":
                                continue
                            extracted_data.append({
                                "提出日": "", "対象年度": "", "文書種類": "報告書", "排出事業者名": "",
                                "事業の種類": "", "事業場名": "", "住所": "", "自治体名": "",
                                "廃棄物の種類": waste_type, "⑩全処理委託量_ton": amt, "備考": ""
                            })
                        except ValueError:
                            continue 
    except Exception:
        return []
    return extracted_data

# --- 共通関数：データ抽出（ハイブリッド・完全版） ---
def extract_data_with_ai(file_path, filename):
    file_ext = os.path.splitext(filename)[1].lower()
    
    # 【修正】備考欄の「AI抽出」を削除し、空欄にする例に変更
    STRICT_PROMPT = """
    あなたはデータ入力の専門家です。資料から産業廃棄物処理の実績データを抽出してください。
    
    【重要規則】
    1. 出力は必ず **以下のJSONフォーマット** に従ってください。キー名は絶対に変更しないでください。
    2. 「実績」の数値を抽出してください。「計画」や「目標」のみの場合は、それを抽出して備考に「計画値」と明記してください。
    3. Markdown記法（```json）は含めないでください。

    【JSON出力例】
    [
      {
        "提出日": "令和6年6月30日",
        "対象年度": "令和5年度",
        "文書種類": "報告書",
        "排出事業者名": "有限会社〇〇",
        "事業の種類": "建設業",
        "事業場名": "〇〇工事現場",
        "住所": "徳島県...",
        "自治体名": "徳島県",
        "廃棄物の種類": "汚泥",
        "⑩全処理委託量_ton": 100.5,
        "⑪優良認定処理業者への処理委託量_ton": 0,
        "⑫再生利用業者への処理委託量_ton": 100.5,
        "⑬熱回収認定業者への処理委託量_ton": 0,
        "⑭熱回収認定業者以外の熱回収を行う業者への処理委託量_ton": 0,
        "備考": ""
      }
    ]
    """

    if file_ext in [".xlsx", ".xls"]:
        data_list = read_excel_robust(file_path)
        if len(data_list) > 0:
            for item in data_list:
                item['ファイル名'] = filename
                if "排出事業者名" in item and not item["排出事業者名"]:
                    item["排出事業者名"] = filename
            return data_list
        
        try:
            xls = pd.read_excel(file_path, sheet_name=None)
            text_buffer = f"ファイル名: {filename}\n\n"
            for sheet_name, df in xls.items():
                text_buffer += f"--- Sheet: {sheet_name} ---\n"
                text_buffer += df.fillna("").to_csv(index=False)
                text_buffer += "\n\n"
            if len(text_buffer) > 30000:
                text_buffer = text_buffer[:30000]

            try:
                model = genai.GenerativeModel('gemini-2.5-flash')
                response = model.generate_content([STRICT_PROMPT, text_buffer], generation_config={"response_mime_type": "application/json"})
            except:
                model = genai.GenerativeModel('gemini-flash-latest')
                response = model.generate_content([STRICT_PROMPT, text_buffer], generation_config={"response_mime_type": "application/json"})

            json_str = clean_json_response(response.text)
            ai_data_list = json.loads(json_str)
            for item in ai_data_list:
                item['ファイル名'] = filename
                if "⑩全処理委託量_ton" not in item: item["⑩全処理委託量_ton"] = 0
            return ai_data_list
        except Exception:
            return []

    elif file_ext == ".pdf":
        try:
            try:
                model = genai.GenerativeModel('gemini-2.5-flash')
            except:
                model = genai.GenerativeModel('gemini-flash-latest')

            sample_file = genai.upload_file(path=file_path, display_name=filename)
            timeout_counter = 0
            while sample_file.state.name == "PROCESSING":
                time.sleep(1)
                timeout_counter += 1
                sample_file = genai.get_file(sample_file.name)
                if timeout_counter > 600: return [] 
            
            if sample_file.state.name == "FAILED": return []
            
            try:
                response = model.generate_content([sample_file, STRICT_PROMPT], generation_config={"response_mime_type": "application/json"})
            except:
                time.sleep(2)
                response = model.generate_content([sample_file, STRICT_PROMPT], generation_config={"response_mime_type": "application/json"})
            
            json_str = clean_json_response(response.text)
            data_list = json.loads(json_str)
            for item in data_list:
                item['ファイル名'] = filename
            return data_list
        except Exception:
            return []
    else:
        return []

def convert_df_to_excel(df):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        df.to_excel(tmp.name, index=False)
        with open(tmp.name, "rb") as f:
            data = f.read()
    return data

# ==========================================
# タブで機能を切り替え
# ==========================================
tab1, tab2 = st.tabs(["📂 ファイルアップロード分析", "🌐 URLから自動収集"])

# ------------------------------------------
# タブ1：手動アップロード
# ------------------------------------------
with tab1:
    st.subheader("手持ちのファイルを分析")
    st.write("PDF または Excelファイル(.xlsx, .xls) をドラッグ＆ドロップしてください。")
    uploaded_files = st.file_uploader("ファイルを選択", type=["pdf", "xlsx", "xls"], accept_multiple_files=True)
    if uploaded_files:
        st.info(f"{len(uploaded_files)} 件のファイルが選択されています。")
        if st.button("🚀 アップロードしたファイルを分析開始", type="primary"):
            if not api_key:
                st.error("APIキーを設定してください")
            else:
                progress_bar = st.progress(0)
                status_text = st.empty()
                with tempfile.TemporaryDirectory() as temp_dir:
                    save_dir = os.path.join(temp_dir, "uploads")
                    os.makedirs(save_dir, exist_ok=True)
                    batch_data = []
                    status_text.text("AIによる分析を開始します...")
                    for i, uploaded_file in enumerate(uploaded_files):
                        file_path = os.path.join(save_dir, uploaded_file.name)
                        with open(file_path, "wb") as f:
                            f.write(uploaded_file.getbuffer())
                        status_text.text(f"分析中 ({i+1}/{len(uploaded_files)}): {uploaded_file.name}")
                        extracted = extract_data_with_ai(file_path, uploaded_file.name)
                        if extracted:
                            batch_data.extend(extracted)
                        progress_bar.progress((i + 1) / len(uploaded_files))
                    
                    if batch_data:
                        df = pd.DataFrame(batch_data)
                        # 列の整理
                        column_mapping = {
                            'ファイル名': 'ファイル名', '自治体名': '自治体名', '提出日': '提出日',
                            '対象年度': '対象年度', '文書種類': '種類', '事業の種類': '事業の種類',
                            '排出事業者名': '排出事業者名', '事業場名': '事業場名', '住所': '住所',
                            '廃棄物の種類': '廃棄物の種類',
                            '⑩全処理委託量_ton': '⑩全処理委託量(t)',
                            '⑪優良認定処理業者への処理委託量_ton': '⑪優良認定(t)',
                            '⑫再生利用業者への処理委託量_ton': '⑫再生利用(t)',
                            '⑬熱回収認定業者への処理委託量_ton': '⑬熱回収認定(t)',
                            '⑭熱回収認定業者以外の熱回収を行う業者への処理委託量_ton': '⑭熱回収その他(t)',
                            '備考': '備考'
                        }
                        target_cols = [c for c in column_mapping.keys() if c in df.columns]
                        df = df[target_cols].rename(columns=column_mapping)
                        
                        # 【修正】日本時間を使用
                        now = get_jst_now_str()
                        st.session_state['history'].append({
                            "time": now, "keyword": "手動アップロード", "count": len(df), "df": df
                        })
                        st.success(f"🎉 分析完了！ {len(df)} 件のデータを抽出しました。")
                        time.sleep(1)
                    else:
                        st.warning("データが抽出できませんでした。")
                    gc.collect()

# ------------------------------------------
# タブ2：URL自動収集 & レポート
# ------------------------------------------
with tab2:
    st.subheader("Webサイトから自動収集")
    st.write("対象URLにある PDF および Excelファイル を自動収集します。")
    
    col1, col2 = st.columns([2, 1])
    with col1:
        default_url = "https://www.pref.tokushima.lg.jp/jigyoshanokata/kurashi/recycling/7300999"
        target_url = st.text_input("対象のURL", default_url)
    with col2:
        keyword = st.text_input("ファイル名に含む文字", "")

    batch_size = st.number_input("自動処理のバッチサイズ", min_value=1, value=50, step=10)

    # 【修正】リンク取得関数（出現順を保持）
    def get_file_links(target_url, keyword):
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            response = requests.get(target_url, headers=headers, timeout=15)
            response.raise_for_status()
            response.encoding = response.apparent_encoding
            soup = BeautifulSoup(response.content, "html.parser")
            links = soup.find_all("a")
            
            target_urls = []
            seen_urls = set() # 重複防止用セット
            
            for link in links:
                href = link.get("href")
                if href:
                    href_lower = href.lower()
                    if href_lower.endswith(".pdf") or href_lower.endswith(".xlsx") or href_lower.endswith(".xls"):
                        full_url = urllib.parse.urljoin(target_url, href)
                        filename = os.path.basename(urllib.parse.urlparse(full_url).path)
                        try: filename = urllib.parse.unquote(filename)
                        except: pass
                        
                        if not keyword or keyword in filename:
                            # 順序を保持しつつ重複排除
                            if full_url not in seen_urls:
                                target_urls.append((filename, full_url))
                                seen_urls.add(full_url)
                                
            return target_urls
        except Exception as e:
            st.error(f"エラー: {e}")
            return []

    if target_url:
        all_file_links = get_file_links(target_url, keyword)
        # セッションステートに全ファイルリストを保存（順序保持）
        st.session_state['all_target_files'] = [f[0] for f in all_file_links]

        processed_set = st.session_state['processed_urls']
        unprocessed_links = [link for link in all_file_links if link[1] not in processed_set]
        remaining_count = len(unprocessed_links)
        
        st.caption(f"対象ファイル総数: {len(all_file_links)}件 / 完了: {len(all_file_links)-remaining_count}件 / 残り: {remaining_count}件")

        if remaining_count > 0:
            if not st.session_state['is_running']:
                if st.button("🚀 URLからの自動実行を開始", type="primary"):
                    st.session_state['is_running'] = True
                    st.rerun()
        
        if st.session_state['is_running']:
            status_box = st.empty()
            batch_progress = st.progress(0)
            
            while remaining_count > 0:
                if not st.session_state['is_running']: break
                next_batch = unprocessed_links[:int(batch_size)]
                status_box.info(f"🔄 自動処理中... 残り {remaining_count} 件")
                
                with tempfile.TemporaryDirectory() as temp_dir:
                    save_dir = os.path.join(temp_dir, "downloads")
                    os.makedirs(save_dir, exist_ok=True)
                    downloaded_files = []
                    headers = {"User-Agent": "Mozilla/5.0"}
                    
                    for i, (fname, furl) in enumerate(next_batch):
                        try:
                            res = requests.get(furl, headers=headers, timeout=10)
                            fpath = os.path.join(save_dir, fname)
                            with open(fpath, "wb") as f: f.write(res.content)
                            downloaded_files.append(fpath)
                            st.session_state['processed_urls'].add(furl)
                        except: pass
                        batch_progress.progress((i + 1) / len(next_batch) * 0.5)
                    
                    if downloaded_files:
                        batch_data = []
                        for i, fpath in enumerate(downloaded_files):
                            fname = os.path.basename(fpath)
                            extracted = extract_data_with_ai(fpath, fname)
                            if extracted: batch_data.extend(extracted)
                            batch_progress.progress(0.5 + (i + 1) / len(downloaded_files) * 0.5)
                        
                        if batch_data:
                            df = pd.DataFrame(batch_data)
                            # 列整理
                            column_mapping = {
                                'ファイル名': 'ファイル名', '自治体名': '自治体名', '提出日': '提出日',
                                '対象年度': '対象年度', '文書種類': '種類', '事業の種類': '事業の種類',
                                '排出事業者名': '排出事業者名', '事業場名': '事業場名', '住所': '住所',
                                '廃棄物の種類': '廃棄物の種類',
                                '⑩全処理委託量_ton': '⑩全処理委託量(t)',
                                '⑪優良認定処理業者への処理委託量_ton': '⑪優良認定(t)',
                                '⑫再生利用業者への処理委託量_ton': '⑫再生利用(t)',
                                '⑬熱回収認定業者への処理委託量_ton': '⑬熱回収認定(t)',
                                '⑭熱回収認定業者以外の熱回収を行う業者への処理委託量_ton': '⑭熱回収その他(t)',
                                '備考': '備考'
                            }
                            target_cols = [c for c in column_mapping.keys() if c in df.columns]
                            df = df[target_cols].rename(columns=column_mapping)
                            
                            # 【修正】日本時間を使用
                            now = get_jst_now_str()
                            st.session_state['history'].append({
                                "time": now, "keyword": keyword, "count": len(df), "df": df
                            })
                
                del downloaded_files
                gc.collect()
                unprocessed_links = [link for link in all_file_links if link[1] not in st.session_state['processed_urls']]
                remaining_count = len(unprocessed_links)
                
                if remaining_count == 0:
                    st.session_state['is_running'] = False
                    status_box.success("完了！")
                    st.rerun()
                else:
                    time.sleep(1)

            if st.button("🛑 中断"):
                st.session_state['is_running'] = False
                st.rerun()

# --- 共通：実行履歴 & 監査レポートエリア ---
st.markdown("---")
st.subheader("📂 実行履歴 & 統合ダウンロード")

if len(st.session_state['history']) > 0:
    all_dfs = [item['df'] for item in st.session_state['history']]
    merged_df = pd.concat(all_dfs, ignore_index=True)
    
    # -----------------------------------------------------
    # 【機能修正】取得状況のレポート（Gap Analysis）
    # -----------------------------------------------------
    st.subheader("📊 取得状況のレポート")
    
    if st.session_state['all_target_files']:
        # 1. Web上の全ファイルリスト（保存された出現順リストを使用）
        # ※ setを使わず、リストの順序をそのまま使う
        all_targets_ordered = st.session_state['all_target_files']
        
        # 2. 抽出できたファイルリスト（ユニーク）
        extracted_files = set(merged_df['ファイル名'].unique())
        
        # 3. 照合
        audit_data = []
        # ここで出現順にループさせることで、レポートの並び順を保証する
        for fname in all_targets_ordered:
            if fname in extracted_files:
                row_count = len(merged_df[merged_df['ファイル名'] == fname])
                status = "✅ 成功"
                note = ""
            else:
                row_count = 0
                status = "⚠️ 未取得"
                note = "要確認"
            
            audit_data.append({
                "ファイル名": fname,
                "ステータス": status,
                "抽出行数": row_count,
                "備考": note
            })
        
        audit_df = pd.DataFrame(audit_data)
        
        # 統計
        success_count = len(audit_df[audit_df['ステータス'] == "✅ 成功"])
        fail_count = len(audit_df[audit_df['ステータス'] == "⚠️ 未取得"])
        
        col_m1, col_m2 = st.columns(2)
        with col_m1:
            st.metric("取得成功ファイル", f"{success_count} / {len(all_targets_ordered)}")
        with col_m2:
            st.metric("未取得（要確認）", f"{fail_count} 件", delta=-fail_count)
            
        if fail_count > 0:
            st.error(f"注意: {fail_count} 件のファイルからデータが取れていません。リストを確認してください。")
        else:
            st.success("素晴らしい！すべての対象ファイルからデータを取得しました。")
            
        # テーブル表示（ソートせずにそのまま表示＝出現順）
        st.dataframe(audit_df, use_container_width=True)
        
    else:
        st.info("Web自動収集を実行すると、ここにレポートが表示されます。")

    st.markdown("---")
    st.info(f"💡 現在合計 **{len(merged_df)} 行** のデータがあります。")
    
    merged_excel = convert_df_to_excel(merged_df)
    now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    
    st.download_button(
        label="📦 すべての結果を結合してExcelダウンロード",
        data=merged_excel,
        file_name=f"waste_report_TOTAL_{now_str}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key="download_total_btn",
        type="primary"
    )
    
    with st.expander("個別の履歴を見る"):
        for i, item in enumerate(reversed(st.session_state['history'])):
            st.write(f"**{item['time']}** - [{item['keyword']}] {item['count']}件")
            st.dataframe(item['df'])
else:
    st.write("履歴はありません。")
