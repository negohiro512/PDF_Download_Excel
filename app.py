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
        st.rerun()

    if api_key:
        genai.configure(api_key=api_key)
    st.info("※APIキーがない場合、動作しません。")

# --- 共通関数：AIによる抽出 ---
def extract_data_with_ai(file_path, filename):
    # モデル設定
    try:
        model = genai.GenerativeModel('gemini-2.5-flash')
    except:
        model = genai.GenerativeModel('gemini-flash-latest')

    # ファイルタイプに応じた処理
    file_ext = os.path.splitext(filename)[1].lower()
    
    content_to_send = None
    mime_type = ""

    # 1. PDFの場合
    if file_ext == ".pdf":
        try:
            sample_file = genai.upload_file(path=file_path, display_name=filename)
            timeout_counter = 0
            while sample_file.state.name == "PROCESSING":
                time.sleep(1)
                timeout_counter += 1
                sample_file = genai.get_file(sample_file.name)
                if timeout_counter > 30: return []
            
            if sample_file.state.name == "FAILED": return []
            
            content_to_send = sample_file
            mime_type = "pdf"
        except Exception:
            return []

    # 2. Excelの場合 (.xlsx, .xls)
    elif file_ext in [".xlsx", ".xls"]:
        try:
            xls = pd.read_excel(file_path, sheet_name=None)
            text_buffer = f"ファイル名: {filename}\n\n"
            for sheet_name, df in xls.items():
                text_buffer += f"--- Sheet: {sheet_name} ---\n"
                text_buffer += df.to_csv(index=False)
                text_buffer += "\n\n"
            
            content_to_send = text_buffer
            mime_type = "text"
        except Exception as e:
            return []
    
    else:
        return []

    # プロンプト（指示書）
    prompt_text = """
    あなたはデータ入力の専門家です。提供された資料（産業廃棄物処理計画書・報告書）から、以下の情報を正確に抽出・転記してください。
    資料はPDF、またはExcelから変換されたテキストデータです。

    【最重要ルール】
    表には「①現状（前年度実績）」と「②計画（目標）」の2つの列が並んでいる場合があります。
    **必ず「①現状」または「【前年度実績】」と書かれている列の数値のみ**を抽出してください。
    「②計画」や「【目標】」の列の数値は絶対に抽出しないでください。

    【抽出項目定義】
    1. **提出日**: 表紙の右上などにある日付（例：令和6年5月21日）。
    2. **対象年度**: 「①現状」や「実績」が指している年度。
    3. **文書種類**: 全て「報告書」として出力してください。
    4. **事業の種類**: 「事業の種類」欄から抽出。
    5. **事業場名**: 「事業場の名称」または「工場名・事業所名」を抽出。
    6. **住所**: 「事業場の所在地」を抽出。
    7. **自治体名**: 書類の宛名（例：「福岡市長 殿」）やヘッダーから、提出先の自治体名を抽出してください（例：「福岡市」）。
    8. **廃棄物の種類ごとの行作成**: 産業廃棄物の種類ごとに1行作成。合計行は不要。

    【出力フォーマット】
    JSON形式のリスト（配列）のみ出力。Markdown記法不要。
    
    [
      {
        "提出日": "令和6年5月21日",
        "対象年度": "令和5年度",
        "文書種類": "報告書",
        "排出事業者名": "株式会社〇〇",
        "事業の種類": "総合工事業",
        "事業場名": "福岡支店",
        "住所": "福岡市博多区...",
        "廃棄物の種類": "がれき類",
        "⑩全処理委託量_ton": 1299.99,
        "⑪優良認定処理業者への処理委託量_ton": 0,
        "⑫再生利用業者への処理委託量_ton": 1299.99,
        "⑬熱回収認定業者への処理委託量_ton": 0,
        "⑭熱回収認定業者以外の熱回収を行う業者への処理委託量_ton": 0,
        "自治体名": "福岡市",
        "備考": ""
      }
    ]
    """
    
    try:
        if mime_type == "pdf":
            try:
                model = genai.GenerativeModel('gemini-2.5-flash')
                response = model.generate_content([content_to_send, prompt_text], generation_config={"response_mime_type": "application/json"})
            except:
                model = genai.GenerativeModel('gemini-flash-latest')
                response = model.generate_content([content_to_send, prompt_text], generation_config={"response_mime_type": "application/json"})
        else:
            try:
                model = genai.GenerativeModel('gemini-2.5-flash')
                response = model.generate_content([prompt_text, f"以下はExcelデータの内容です:\n{content_to_send}"], generation_config={"response_mime_type": "application/json"})
            except:
                model = genai.GenerativeModel('gemini-flash-latest')
                response = model.generate_content([prompt_text, f"以下はExcelデータの内容です:\n{content_to_send}"], generation_config={"response_mime_type": "application/json"})

        data_list = json.loads(response.text)
        for item in data_list:
            item['ファイル名'] = filename
        return data_list

    except Exception:
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
# タブ1：手動アップロード機能
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
                        
                        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        st.session_state['history'].append({
                            "time": now,
                            "keyword": "手動アップロード",
                            "count": len(df),
                            "df": df
                        })
                        
                        st.success(f"🎉 分析完了！ {len(df)} 件のデータを抽出しました。下の履歴からダウンロードできます。")
                        time.sleep(1)
                    else:
                        st.warning("データが抽出できませんでした。")
                    
                    gc.collect()

# ------------------------------------------
# タブ2：URL自動収集機能
# ------------------------------------------
with tab2:
    st.subheader("Webサイトから自動収集")
    st.write("対象URLにある PDF および Excelファイル を自動収集します。")
    
    col1, col2 = st.columns([2, 1])
    with col1:
        default_url = "https://www.pref.kagoshima.jp/aq21/kurashi-kankyo/kankyo/sangyo/seibi/r6_public.html"
        target_url = st.text_input("対象のURL", default_url)
    with col2:
        keyword = st.text_input("ファイル名に含む文字", "06")

    batch_size = st.number_input("自動処理のバッチサイズ", min_value=1, value=50, step=10)

    # リンク取得関数（Excel対応版）
    def get_file_links(target_url, keyword):
        headers = {"User-Agent": "Mozilla/5.0"}
        try:
            response = requests.get(target_url, headers=headers, timeout=15)
            response.raise_for_status()
            response.encoding = response.apparent_encoding
            soup = BeautifulSoup(response.content, "html.parser")
            links = soup.find_all("a")
            target_urls = []
            for link in links:
                href = link.get("href")
                # 【修正】PDFだけでなく、Excelファイルも対象にする
                if href:
                    href_lower = href.lower()
                    if href_lower.endswith(".pdf") or href_lower.endswith(".xlsx") or href_lower.endswith(".xls"):
                        full_url = urllib.parse.urljoin(target_url, href)
                        filename = os.path.basename(urllib.parse.urlparse(full_url).path)
                        try: filename = urllib.parse.unquote(filename)
                        except: pass
                        
                        if not keyword or keyword in filename:
                            target_urls.append((filename, full_url))
            return list(set(target_urls))
        except Exception as e:
            st.error(f"エラー: {e}")
            return []

    if target_url:
        # 関数名変更に合わせて呼び出し元も変更
        all_file_links = get_file_links(target_url, keyword)
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
                            
                            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                            st.session_state['history'].append({
                                "time": now, "keyword": keyword, "count": len(df), "df": df
                            })
                
                del downloaded_files
                gc.collect()
                # リスト更新
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

# --- 共通：実行履歴エリア ---
st.markdown("---")
st.subheader("📂 実行履歴 & 統合ダウンロード")

if len(st.session_state['history']) > 0:
    all_dfs = [item['df'] for item in st.session_state['history']]
    merged_df = pd.concat(all_dfs, ignore_index=True)
    
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
