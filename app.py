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
st.set_page_config(page_title="PDF一括DL & AI抽出", layout="wide")

st.title("📄 PDF一括ダウンローダー & AI台帳作成（全自動版）")
st.markdown("""
指定URLからPDFを収集し、**前年度実績（報告書情報）**を抽出します。
**「全自動実行」**ボタンを押すと、完了するまで自動で分割処理（バッチ処理）を継続します。
※処理中はブラウザを閉じないでください。
""")

# --- セッションステート初期化 ---
if 'history' not in st.session_state:
    st.session_state['history'] = []
if 'processed_urls' not in st.session_state:
    st.session_state['processed_urls'] = set()
if 'is_running' not in st.session_state:
    st.session_state['is_running'] = False # 実行中フラグ

# --- サイドバー：設定 ---
with st.sidebar:
    st.header("設定")
    
    if "GEMINI_API_KEY" in st.secrets:
        api_key = st.secrets["GEMINI_API_KEY"]
        st.success("🔑 APIキーを自動で読み込みました")
    else:
        api_key = st.text_input("Gemini APIキー", type="password", help="Google AI Studioで取得したキーを入力してください")

    st.markdown("---")
    st.subheader("処理設定")
    batch_size = st.number_input(
        "1回の処理単位（バッチサイズ）", 
        min_value=1, 
        value=50, 
        step=10, 
        help="メモリ不足を防ぐため、50件程度ごとにメモリ解放を行います。"
    )

    st.markdown("---")
    # 強制停止ボタン
    if st.session_state['is_running']:
        if st.button("🛑 処理を中断する"):
            st.session_state['is_running'] = False
            st.warning("中断命令を出しました。現在のバッチが終わり次第停止します。")

    if st.button("🗑️ 履歴と記憶を全クリア"):
        st.session_state['history'] = []
        st.session_state['processed_urls'] = set()
        st.session_state['is_running'] = False
        st.rerun()

    if api_key:
        genai.configure(api_key=api_key)
    st.info("※APIキーがない場合、ダウンロードのみ実行されます。")

# --- ユーザー入力欄 ---
col1, col2 = st.columns([2, 1])
with col1:
    default_url = "https://www.pref.kagoshima.jp/aq21/kurashi-kankyo/kankyo/sangyo/seibi/r6_public.html"
    target_url = st.text_input("対象のURL", default_url)
with col2:
    keyword = st.text_input("ファイル名に含む文字", "06")

# --- 関数群 ---
def get_pdf_links(target_url, keyword):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    try:
        response = requests.get(target_url, headers=headers, timeout=15)
        response.raise_for_status()
    except Exception as e:
        st.error(f"サイトへの接続に失敗しました: {e}")
        return []
    
    response.encoding = response.apparent_encoding
    soup = BeautifulSoup(response.content, "html.parser")
    links = soup.find_all("a")
    
    target_urls = []
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
                target_urls.append((filename, full_url))
                
    return list(set(target_urls))

def extract_data_with_ai(pdf_path, filename):
    # モデル設定
    try:
        model = genai.GenerativeModel('gemini-2.5-flash')
    except:
        model = genai.GenerativeModel('gemini-flash-latest')

    # アップロード
    try:
        sample_file = genai.upload_file(path=pdf_path, display_name=filename)
        # 待機
        timeout_counter = 0
        while sample_file.state.name == "PROCESSING":
            time.sleep(1)
            timeout_counter += 1
            sample_file = genai.get_file(sample_file.name)
            if timeout_counter > 30: # 30秒以上かかったらタイムアウト
                return []
        
        if sample_file.state.name == "FAILED":
            return []
    except Exception:
        return []

    prompt = """
    あなたはデータ入力の専門家です。PDFから以下の情報を正確に抽出・転記してください。

    【最重要ルール】
    表には「①現状（前年度実績）」と「②計画（目標）」の2つの列が並んでいる場合があります。
    **必ず「①現状」または「【前年度実績】」と書かれている列の数値のみ**を抽出してください。
    「②計画」や「【目標】」の列の数値は絶対に抽出しないでください。

    【抽出項目定義】
    1. **提出日**: 表紙の右上にある日付（例：令和6年5月21日）。
    2. **対象年度**: 「①現状」や「実績」が指している年度。
    3. **文書種類**: 全て「報告書」として出力してください。
    4. **事業の種類**: 「事業の種類」欄から抽出。
    5. **事業場名**: 「事業場の名称」または「工場名・事業所名」を抽出。
    6. **住所**: 「事業場の所在地」を抽出。
    7. **廃棄物の種類ごとの行作成**: 産業廃棄物の種類ごとに1行作成。合計行は不要。

    【出力フォーマット】
    JSON形式のリスト（配列）のみ出力。
    
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
        # 生成実行
        try:
            model = genai.GenerativeModel('gemini-2.5-flash')
            response = model.generate_content([sample_file, prompt], generation_config={"response_mime_type": "application/json"})
        except Exception:
            model = genai.GenerativeModel('gemini-flash-latest')
            response = model.generate_content([sample_file, prompt], generation_config={"response_mime_type": "application/json"})
        
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

# --- 事前情報取得エリア ---
st.markdown("---")
st.subheader("📊 実行ステータス")

if target_url:
    # リンク全取得
    all_pdf_links = get_pdf_links(target_url, keyword)
    total_count = len(all_pdf_links)
    
    # 処理済み計算
    processed_set = st.session_state['processed_urls']
    unprocessed_links = [link for link in all_pdf_links if link[1] not in processed_set]
    remaining_count = len(unprocessed_links)
    processed_count = total_count - remaining_count
    
    # 画面表示
    col_a, col_b, col_c = st.columns(3)
    col_a.metric("対象PDF総数", f"{total_count} 件")
    col_b.metric("完了", f"{processed_count} 件")
    col_c.metric("残り", f"{remaining_count} 件")
    
    # 全体進捗バー
    overall_progress = st.progress(0)
    if total_count > 0:
        overall_progress.progress(processed_count / total_count)
    
    # 実行ボタン
    if remaining_count > 0:
        if not st.session_state['is_running']:
            if st.button("🚀 全自動実行を開始する", type="primary"):
                if not api_key:
                    st.error("APIキーを設定してください")
                else:
                    st.session_state['is_running'] = True
                    st.rerun()
    else:
        st.success("✅ すべての処理が完了しています！")

# --- 自動ループ処理ロジック ---
if st.session_state['is_running']:
    # プレースホルダー（進捗表示用）
    status_box = st.empty()
    batch_progress = st.progress(0)
    
    while remaining_count > 0:
        # 中断チェック
        if not st.session_state['is_running']:
            status_box.warning("処理を中断しました。")
            break

        # 今回のバッチを作成
        next_batch = unprocessed_links[:int(batch_size)]
        
        status_box.info(f"🔄 自動処理中... 残り {remaining_count} 件中、今回のバッチ {len(next_batch)} 件を実行します。")
        
        # --- バッチ処理開始 ---
        with tempfile.TemporaryDirectory() as temp_dir:
            save_dir = os.path.join(temp_dir, "pdfs")
            os.makedirs(save_dir, exist_ok=True)
            
            downloaded_files = []
            headers = {"User-Agent": "Mozilla/5.0"}
            
            # 1. ダウンロード
            for i, (fname, furl) in enumerate(next_batch):
                try:
                    res = requests.get(furl, headers=headers, timeout=10)
                    fpath = os.path.join(save_dir, fname)
                    with open(fpath, "wb") as f:
                        f.write(res.content)
                    downloaded_files.append(fpath)
                    st.session_state['processed_urls'].add(furl) # 処理済みに登録
                except Exception:
                    pass # エラーでも止まらず次へ
                
                # バッチ内進捗更新
                batch_progress.progress((i + 1) / len(next_batch) * 0.5) # 前半50%
            
            # 2. AI解析
            if downloaded_files:
                batch_data = []
                for i, fpath in enumerate(downloaded_files):
                    fname = os.path.basename(fpath)
                    extracted = extract_data_with_ai(fpath, fname)
                    if extracted:
                        batch_data.extend(extracted)
                    
                    # バッチ内進捗更新
                    batch_progress.progress(0.5 + (i + 1) / len(downloaded_files) * 0.5) # 後半50%
                
                # 3. 結果保存
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
                    
                    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    st.session_state['history'].append({
                        "time": now,
                        "keyword": keyword,
                        "count": len(df),
                        "df": df
                    })
        
        # --- メモリ解放 ---
        del downloaded_files
        del batch_data
        gc.collect()
        
        # 残り件数を再計算
        unprocessed_links = [link for link in all_pdf_links if link[1] not in st.session_state['processed_urls']]
        remaining_count = len(unprocessed_links)
        
        # 全体進捗バー更新
        processed_count = total_count - remaining_count
        if total_count > 0:
            overall_progress.progress(processed_count / total_count)
            
        # 完了チェック
        if remaining_count == 0:
            st.session_state['is_running'] = False
            status_box.success("🎉 全件の処理が完了しました！")
            st.rerun()
            break
        else:
            # サーバー負荷軽減のため少し待機してから次へ
            time.sleep(1)

# --- 実行履歴エリア ---
st.markdown("---")
st.subheader("📂 実行履歴 & 統合ダウンロード")

if len(st.session_state['history']) > 0:
    all_dfs = [item['df'] for item in st.session_state['history']]
    merged_df = pd.concat(all_dfs, ignore_index=True)
    
    st.info(f"現在、合計 **{len(merged_df)} 行** のデータが抽出されています。")
    
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
            st.write(f"**{item['time']}** - {item['count']}件")
            st.dataframe(item['df'])
else:
    st.write("履歴はありません。")
