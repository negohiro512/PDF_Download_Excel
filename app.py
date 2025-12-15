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
指定したURLからPDFを収集し、**AI (Gemini)** を使って中身を自動で読み取り、
指定の項目をExcel一覧表にして出力します。
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

# --- 関数：PDFダウンロード（修正版） ---
def download_pdfs(target_url, keyword, save_dir, status_text, progress_bar):
    # 【修正1】ヘッダーを強化して、普通のブラウザからのアクセスに見せかける
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",  # 日本語環境であることを伝える
        "Referer": "https://www.google.com/"             # Google検索から来たふりをする
    }
    
    status_text.text("サイトの情報を取得中...")
    
    try:
        # 【修正2】タイムアウト設定を追加（ずっと待機してエラーになるのを防ぐ）
        response = requests.get(target_url, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        # 具体的なエラーコード（403や404など）を表示する
        st.error(f"サイトへのアクセスが拒否されました。ステータスコード: {e.response.status_code}")
        st.write("考えられる原因: Streamlit Cloudのサーバー（海外IP）からのアクセスがブロックされている可能性があります。")
        return []
    except Exception as e:
        st.error(f"接続エラーが発生しました: {e}")
        return []
    
    # --- 以下は変更なし（文字化け対策を追加して安定させています） ---
    response.encoding = response.apparent_encoding  # 文字化け防止
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
            # ダウンロード時も同じヘッダーを使う
            file_res = requests.get(url, headers=headers, timeout=10)
            file_path = os.path.join(save_dir, filename)
            with open(file_path, "wb") as f:
                f.write(file_res.content)
            downloaded_files.append(file_path)
            
            progress_bar.progress((i + 1) / len(download_targets))
            time.sleep(1) # 【修正3】アクセス間隔を少し長めに（1秒）してブロックを防ぐ
        except Exception as e:
            st.warning(f"{filename} の取得失敗: {e}")
            
    return downloaded_files

# --- 関数：AIによる抽出 ---
def extract_data_with_ai(pdf_path, filename):
    # Gemini 2.5 Flashモデルを使用（高速・安価）
    model = genai.GenerativeModel('gemini-2.5-flash')
    
    # PDFをアップロード
    sample_file = genai.upload_file(path=pdf_path, display_name=filename)
    
    # ファイルの処理完了を待機
    while sample_file.state.name == "PROCESSING":
        time.sleep(1)
        sample_file = genai.get_file(sample_file.name)
        
    if sample_file.state.name == "FAILED":
        return None

# プロンプト（指示書）
    prompt = """
    このPDFは産業廃棄物の処理計画書または報告書です。
    以下の項目を抽出し、JSON形式で出力してください。
    値が見つからない場合は null を入れてください。
    
    【抽出項目】
    - 対象年度: 文書のタイトルや対象期間から、この報告が「何年度」のものか抽出（例：「令和6年度」「2024年度」）。不明な場合は日付を記載。
    - 文書種類: 文書のタイトルに基づき、「計画書」または「報告書」のいずれかを出力。「処理計画書」なら「計画書」、「実施状況報告書」なら「報告書」と短く記載すること。
    - 自治体名(県): 文書内の提出先や住所から都道府県名を推測または抽出
    - 自治体名: 文書内の提出先から市町村名を抽出（例：福岡市長殿なら福岡市）
    - 事業の種類: 事業の内容や業種
    - 排出事業者名: 会社名、氏名、または提出者名
    - 事業場名: 工場名や事業所名（なければ「同上」など記載通りに）
    - 住所: 事業場の所在地
    - 産業廃棄物の種類: 記載されている主な廃棄物の種類（複数ある場合はカンマ区切り）
    - 全処理委託量_ton: 「⑩全処理委託量」に相当する数値
    - 優良認定処理業者への処理委託量_ton: 「⑪優良認定処理業者への処理委託量」に相当する数値
    - 再生利用業者への処理委託量_ton: 「⑫再生利用業者への処理委託量」に相当する数値
    - 熱回収認定業者への処理委託量_ton: 「⑬熱回収認定業者への処理委託量」に相当する数値
    - 熱回収認定業者以外の熱回収を行う業者への処理委託量_ton: 「⑭熱回収認定業者以外の熱回収を行う業者への処理委託量」に相当する数値
    - 備考: 特記事項があれば
    """
    
    # JSON形式での回答を強制
    response = model.generate_content(
        [sample_file, prompt],
        generation_config={"response_mime_type": "application/json"}
    )
    
    # データを解析して辞書型で返す
    try:
        data = json.loads(response.text)
        data['ファイル名'] = filename # ファイル名もデータに追加
        return data
    except:
        return None

# --- メイン処理 ---
if st.button("🚀 ダウンロード & データ抽出を開始"):
    if not api_key:
        st.error("AI抽出を行うには、サイドバーでAPIキーを設定してください。")
    else:
        # 表示用コンテナ
        status_text = st.empty()
        progress_bar = st.progress(0)
        result_area = st.container()

        with tempfile.TemporaryDirectory() as temp_dir:
            save_dir = os.path.join(temp_dir, "pdfs")
            os.makedirs(save_dir, exist_ok=True)
            
            # 1. ダウンロード実行
            downloaded_files = download_pdfs(target_url, keyword, save_dir, status_text, progress_bar)
            
            if not downloaded_files:
                st.warning("条件に合うPDFが見つかりませんでした。")
            else:
                status_text.text("AIによるデータ抽出を開始します...（これには時間がかかります）")
                progress_bar.progress(0)
                
                extracted_data_list = []
                
                # 2. AI抽出ループ
                for i, pdf_path in enumerate(downloaded_files):
                    filename = os.path.basename(pdf_path)
                    status_text.text(f"分析中 ({i+1}/{len(downloaded_files)}): {filename}")
                    
                    try:
                        data = extract_data_with_ai(pdf_path, filename)
                        if data:
                            extracted_data_list.append(data)
                    except Exception as e:
                        st.error(f"{filename} のAI解析でエラー: {e}")
                    
                    progress_bar.progress((i + 1) / len(downloaded_files))
                
                # 3. データ整形とExcel化
                if extracted_data_list:
                    df = pd.DataFrame(extracted_data_list)
                    
                    # カラムの並び替えと日本語リネーム
                    column_mapping = {
                        'ファイル名': 'ファイル名',
                        '対象年度': '対象年度',
                        '文書種類': '種類',        # 【追加】ここに計画書/報告書が入ります
                        '自治体名(県)': '自治体名(県)',
                        '自治体名': '自治体名',
                        '事業の種類': '事業の種類',
                        '排出事業者名': '排出事業者名（＝会社名）',
                        '事業場名': '事業場名（＝工場名or事業所名）',
                        '住所': '住所',
                        '産業廃棄物の種類': '産業廃棄物の種類',
                        '全処理委託量_ton': '⑩全処理委託量（ton）',
                        '優良認定処理業者への処理委託量_ton': '⑪優良認定処理業者への処理委託量（ton）',
                        '再生利用業者への処理委託量_ton': '⑫再生利用業者への処理委託量（ton）',
                        '熱回収認定業者への処理委託量_ton': '⑬熱回収認定業者への処理委託量（ton）',
                        '熱回収認定業者以外の熱回収を行う業者への処理委託量_ton': '⑭熱回収認定業者以外の熱回収を行う業者への処理委託量（ton）',
                        '備考': '備考'
                    }
                    
                    # 存在しないカラムは無視してリネーム
                    df = df.rename(columns=column_mapping)
                    
                    # ユーザー指定の順番に並べ替え
                    target_columns = list(column_mapping.values())
                    existing_cols = [c for c in target_columns if c in df.columns]
                    df = df[existing_cols]

                    # 結果表示
                    st.success("🎉 全ての処理が完了しました！")
                    st.dataframe(df)
                    
                    # Excelダウンロードボタン
                    excel_path = os.path.join(temp_dir, "summary_list.xlsx")
                    df.to_excel(excel_path, index=False)
                    
                    with open(excel_path, "rb") as f:
                        st.download_button(
                            label="📥 Excel一覧表をダウンロード",
                            data=f,
                            file_name="waste_report_summary.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        )
                else:
                    st.error("データの抽出に失敗しました。")
