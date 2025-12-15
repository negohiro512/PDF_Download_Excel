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

# --- 画面設定 ---
st.set_page_config(page_title="PDF一括DL & AI抽出", layout="wide")

st.title("📄 PDF一括ダウンローダー & AI台帳作成")
st.markdown("""
指定URLからPDFを収集し、**前年度実績（報告書情報）のみ**を抽出してExcel化します。
計画値（目標）は除外されます。
""")

# --- サイドバー：設定 ---
with st.sidebar:
    st.header("設定")
    api_key = st.text_input("Gemini APIキー", type="password", help="Google AI Studioで取得したキーを入力してください")
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

# --- 関数：AIによる抽出（実績のみに限定） ---
def extract_data_with_ai(pdf_path, filename):
    # Gemini 2.5 Flash (Experimental) を優先
    try:
        model = genai.GenerativeModel('gemini-2.5-flash-exp')
    except:
        model = genai.GenerativeModel('gemini-2.5-flash')
    
    try:
        sample_file = genai.upload_file(path=pdf_path, display_name=filename)
        while sample_file.state.name == "PROCESSING":
            time.sleep(1)
            sample_file = genai.get_file(sample_file.name)
        if sample_file.state.name == "FAILED":
            return []
    except Exception as e:
        st.error(f"アップロードエラー: {e}")
        return []

    # プロンプト（指示書）：実績のみに限定
    prompt = """
    このPDFは産業廃棄物の処理計画書・報告書です。
    PDF内の表（特に別紙の内訳表）から、**「前年度実績（現状）」**のデータのみを抽出してください。
    
    【重要：抽出ルール】
    1. **実績のみ抽出**: 「計画」や「目標」の数値は**全て無視**してください。抽出対象は「実績」や「現状」と書かれた欄の数値のみです。
    2. **対象年度**: 実績値の対象となっている年度（例：提出日が令和6年5月なら、対象年度は「令和5年度」）を抽出してください。
    3. **種類ごとの分割**: 合計行ではなく、廃棄物の種類ごとに1行ずつデータを作成してください。
    4. **文書種類**: 全て「報告書」として出力してください（実績値を扱うため）。
    5. **提出日**: 表紙の提出日を正確に抽出してください。

    以下のJSON形式のリスト（配列）で出力してください。該当する実績データがない場合は空リスト [] を返してください。
    
    [
      {
        "提出日": "令和6年5月21日",
        "対象年度": "令和5年度",
        "文書種類": "報告書",
        "排出事業者名": "株式会社〇〇",
        "廃棄物の種類": "がれき類",
        "⑩全処理委託量_ton": 100.5,
        "⑪優良認定処理業者への処理委託量_ton": 0,
        "⑫再生利用業者への処理委託量_ton": 100.5,
        "⑬熱回収認定業者への処理委託量_ton": 0,
        "⑭熱回収認定業者以外の熱回収を行う業者への処理委託量_ton": 0,
        "自治体名": "福岡市",
        "備考": ""
      }
    ]
    """
    
    try:
        response = model.generate_content(
            [sample_file, prompt],
            generation_config={"response_mime_type": "application/json"}
        )
        data_list = json.loads(response.text)
        
        # ファイル名を各データに追加
        for item in data_list:
            item['ファイル名'] = filename
            
        return data_list
    except Exception as e:
        return []

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
                    
                    extracted_list = extract_data_with_ai(pdf_path, filename)
                    if extracted_list:
                        all_extracted_data.extend(extracted_list)
                    
                    progress_bar.progress((i + 1) / len(downloaded_files))
                
                # 3. データ整形とExcel化
                if all_extracted_data:
                    df = pd.DataFrame(all_extracted_data)
                    
                    # カラム順序とリネーム
                    column_mapping = {
                        'ファイル名': 'ファイル名',
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
                        '自治体名': '自治体名',
                        '備考': '備考'
                    }
                    
                    target_cols = [c for c in column_mapping.keys() if c in df.columns]
                    df = df[target_cols]
                    df = df.rename(columns=column_mapping)

                    st.success(f"🎉 処理完了！ {len(df)} 件の実績データを抽出しました。")
                    st.dataframe(df)
                    
                    excel_path = os.path.join(temp_dir, "waste_report_results_only.xlsx")
                    df.to_excel(excel_path, index=False)
                    
                    with open(excel_path, "rb") as f:
                        st.download_button(
                            label="📥 実績データのみのExcelをダウンロード",
                            data=f,
                            file_name="waste_report_results_only.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        )
                else:
                    st.error("データの抽出に失敗しました。条件に合う実績データが見つからなかった可能性があります。")
