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

# --- 画面設定 ---
st.set_page_config(page_title="PDF一括DL & AI抽出", layout="wide")

st.title("📄 PDF一括ダウンローダー & AI台帳作成")
st.markdown("""
指定URLからPDFを収集し、**前年度実績（報告書情報）**の数値を抽出してExcel化します。
実行結果は画面下の「実行履歴」に保存され、**まとめて結合ダウンロード**も可能です。
""")

# --- セッションステート（履歴保存用）の初期化 ---
if 'history' not in st.session_state:
    st.session_state['history'] = []

# --- サイドバー：設定 ---
with st.sidebar:
    st.header("設定")
    
    # 1. まずSecrets（安全な保管場所）からキーを探す
    if "GEMINI_API_KEY" in st.secrets:
        api_key = st.secrets["GEMINI_API_KEY"]
        st.success("🔑 APIキーを自動で読み込みました")
    # 2. なければ入力欄を表示する（ローカル環境や未設定時用）
    else:
        api_key = st.text_input("Gemini APIキー", type="password", help="Google AI Studioで取得したキーを入力してください")

    debug_mode = st.checkbox("デバッグモード（エラー詳細を表示）")
    
    # 履歴クリアボタン
    if st.button("🗑️ 履歴をクリア"):
        st.session_state['history'] = []
        st.rerun()

    if api_key:
        genai.configure(api_key=api_key)
    st.info("※APIキーがない場合、ダウンロードのみ実行されます。")
# --- ユーザー入力欄 ---
col1, col2 = st.columns([2, 1])
with col1:
    default_url = "https://www.city.fukuoka.lg.jp/kankyo/sanhai/hp/sangyouhaikibutu/haisyutujigyousya/taryoukouhyou.html"
    target_url = st.text_input("対象のURL", default_url)
with col2:
    keyword = st.text_input("ファイル名に含む文字", "06")

# --- 関数：PDFダウンロード ---
def download_pdfs(target_url, keyword, save_dir, status_text, progress_bar):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Referer": "https://www.google.com/"
    }
    
    status_text.text("サイトの情報を取得中...")
    try:
        response = requests.get(target_url, headers=headers, timeout=10)
        response.raise_for_status()
    except Exception as e:
        st.error(f"接続エラー: {e}")
        return []
    
    response.encoding = response.apparent_encoding
    soup = BeautifulSoup(response.content, "html.parser")
    links = soup.find_all("a")
    
    download_targets = []
    for link in links:
        href = link.get("href")
        if href and href.lower().endswith(".pdf"):
            full_url = urllib.parse.urljoin(target_url, href)
            filename = os.path.basename(urllib.parse.urlparse(full_url).path)
            try:
                filename = urllib.parse.unquote(filename)
            except:
                pass
            
            if not keyword or keyword in filename:
                download_targets.append((filename, full_url))
    
    download_targets = list(set(download_targets))
    if not download_targets:
        return []
    
    downloaded_files = []
    status_text.text(f"{len(download_targets)} 件のPDFが見つかりました。ダウンロード中...")
    
    for i, (filename, url) in enumerate(download_targets):
        try:
            file_res = requests.get(url, headers=headers, timeout=10)
            file_path = os.path.join(save_dir, filename)
            with open(file_path, "wb") as f:
                f.write(file_res.content)
            downloaded_files.append(file_path)
            progress_bar.progress((i + 1) / len(download_targets))
            time.sleep(1)
        except Exception as e:
            st.warning(f"{filename} の取得失敗: {e}")
            
    return downloaded_files

# --- 関数：AIによる抽出（ご指定のモデル名を使用） ---
def extract_data_with_ai(pdf_path, filename, debug_mode=False):
    # Gemini 2.5 Flash (Experimental) を優先
    try:
        model = genai.GenerativeModel('gemini-2.5-flash')
    except:
        model = genai.GenerativeModel('gemini-flash-latest')

    try:
        sample_file = genai.upload_file(path=pdf_path, display_name=filename)
        while sample_file.state.name == "PROCESSING":
            time.sleep(1)
            sample_file = genai.get_file(sample_file.name)
        
        if sample_file.state.name == "FAILED":
            if debug_mode: st.error("ファイルのアップロード処理に失敗しました。")
            return []
            
    except Exception as e:
        if debug_mode: st.error(f"アップロードエラー: {e}")
        return []

    # プロンプト（指示書）
    prompt = """
    あなたはデータ入力の専門家です。このPDF（産業廃棄物処理計画書・報告書）の「別紙」にある表から、数値を正確に転記してください。

    【最重要ルール】
    表には「①現状（前年度実績）」と「②計画（目標）」の2つの列が並んでいる場合があります。
    **必ず「①現状」または「【前年度実績】」と書かれている列の数値のみ**を抽出してください。
    「②計画」や「【目標】」の列の数値は絶対に抽出しないでください。

    【抽出項目定義】
    1. **提出日**: 表紙の右上にある日付（例：令和6年5月21日）。
    2. **対象年度**: 「①現状」や「実績」が指している年度。通常は提出日の前年度（例：令和5年度）。
    3. **文書種類**: 全て「報告書」として出力してください。
    4. **廃棄物の種類ごとの行作成**: 表にある全ての「産業廃棄物の種類」について、1種類につき1つのデータ（行）を作成してください。合計行は不要です。

    【出力フォーマット】
    以下のJSON形式のリスト（配列）のみを出力してください。Markdown記法（```json）は不要です。
    
    [
      {
        "提出日": "令和6年5月21日",
        "対象年度": "令和5年度",
        "文書種類": "報告書",
        "排出事業者名": "株式会社〇〇",
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
        # 生成実行
        try:
            model = genai.GenerativeModel('gemini-2.5-flash')
            response = model.generate_content(
                [sample_file, prompt],
                generation_config={"response_mime_type": "application/json"}
            )
        except Exception:
            if debug_mode: st.warning("gemini-2.5-flash が利用できないため、gemini-flash-latest を使用します。")
            model = genai.GenerativeModel('gemini-flash-latest')
            response = model.generate_content(
                [sample_file, prompt],
                generation_config={"response_mime_type": "application/json"}
            )
        
        if debug_mode:
            st.text(f"--- {filename} のAI生回答 ---")
            st.text(response.text)

        data_list = json.loads(response.text)
        
        for item in data_list:
            item['ファイル名'] = filename
            
        return data_list
    except Exception as e:
        if debug_mode:
            st.error(f"データ解析エラー: {e}")
        return []

# --- データ変換関数（Excel用） ---
def convert_df_to_excel(df):
    # バイトストリームを使うと複雑になるため、一時ファイルを作成して読み込む方式
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
        df.to_excel(tmp.name, index=False)
        with open(tmp.name, "rb") as f:
            data = f.read()
    return data

# --- メイン処理 ---
if st.button("🚀 ダウンロード & データ抽出を開始"):
    if not api_key:
        st.error("AI抽出を行うには、サイドバーでAPIキーを設定してください。")
    else:
        status_text = st.empty()
        progress_bar = st.progress(0)

        with tempfile.TemporaryDirectory() as temp_dir:
            save_dir = os.path.join(temp_dir, "pdfs")
            os.makedirs(save_dir, exist_ok=True)
            
            # 1. ダウンロード
            downloaded_files = download_pdfs(target_url, keyword, save_dir, status_text, progress_bar)
            
            if not downloaded_files:
                st.warning("条件に合うPDFが見つかりませんでした。")
            else:
                status_text.text("AIによるデータ抽出を開始します...")
                progress_bar.progress(0)
                
                all_extracted_data = []
                
                # 2. AI抽出ループ
                for i, pdf_path in enumerate(downloaded_files):
                    filename = os.path.basename(pdf_path)
                    status_text.text(f"分析中 ({i+1}/{len(downloaded_files)}): {filename}")
                    
                    extracted_list = extract_data_with_ai(pdf_path, filename, debug_mode)
                    
                    if extracted_list:
                        all_extracted_data.extend(extracted_list)
                    
                    progress_bar.progress((i + 1) / len(downloaded_files))
                
                # 3. データ整形と保存
                if all_extracted_data:
                    df = pd.DataFrame(all_extracted_data)
                    
                    # 列の並び順指定
                    column_mapping = {
                        'ファイル名': 'ファイル名',
                        '自治体名': '自治体名',
                        '提出日': '提出日',
                        '対象年度': '対象年度',
                        '文書種類': '種類',
                        '排出事業者名': '排出事業者名',
                        '廃棄物の種類': '廃棄物の種類',
                        '⑩全処理委託量_ton': '⑩全処理委託量(t)',
                        '⑪優良認定処理業者への処理委託量_ton': '⑪優良認定(t)',
                        '⑫再生利用業者への処理委託量_ton': '⑫再生利用(t)',
                        '⑬熱回収認定業者への処理委託量_ton': '⑬熱回収認定(t)',
                        '⑭熱回収認定業者以外の熱回収を行う業者への処理委託量_ton': '⑭熱回収その他(t)',
                        '備考': '備考'
                    }
                    
                    target_cols = [c for c in column_mapping.keys() if c in df.columns]
                    df = df[target_cols]
                    df = df.rename(columns=column_mapping)
                    
                    # 履歴に保存
                    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    history_item = {
                        "time": now,
                        "keyword": keyword,
                        "count": len(df),
                        "df": df
                    }
                    st.session_state['history'].append(history_item)
                    
                    st.success(f"🎉 処理完了！ {len(df)} 件の実績データを抽出しました。")
                else:
                    st.error("データの抽出に失敗しました。")

# --- 実行履歴エリア ---
st.markdown("---")
st.subheader("📂 実行履歴")

if len(st.session_state['history']) == 0:
    st.write("履歴はまだありません。")
else:
    # ---------------------------------------------------------
    # 【追加機能】履歴が複数ある場合、まとめてダウンロードするボタンを表示
    # ---------------------------------------------------------
    if len(st.session_state['history']) > 1:
        st.info("💡 複数の抽出結果があります。これらを1つのファイルにまとめてダウンロードできます。")
        
        # 全てのDataFrameを結合 (pd.concat)
        all_dfs = [item['df'] for item in st.session_state['history']]
        merged_df = pd.concat(all_dfs, ignore_index=True)
        
        # 結合データのダウンロード
        merged_excel = convert_df_to_excel(merged_df)
        now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        
        st.download_button(
            label="📦 履歴をすべて結合してダウンロード (Merge All)",
            data=merged_excel,
            file_name=f"waste_report_merged_{now_str}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key="download_all_btn"
        )
        st.markdown("---")

    # 個別の履歴表示
    for i, item in enumerate(reversed(st.session_state['history'])):
        with st.expander(f"【{item['time']}】キーワード: {item['keyword']} (抽出数: {item['count']}件)"):
            st.dataframe(item['df'])
            
            # Excelダウンロードボタン
            excel_data = convert_df_to_excel(item['df'])
            st.download_button(
                label=f"📥 このExcelをダウンロード",
                data=excel_data,
                file_name=f"waste_report_{item['time'].replace(':','-')}.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key=f"dl_btn_{i}"
            )
        
