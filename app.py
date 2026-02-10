import os
import asyncio
import json
import base64
import requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from google import genai
from google.genai import types
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from email.mime.text import MIMEText
#from dotenv import load_dotenv
from datetime import datetime, timedelta

# ================================================
# 1. 環境設定
# ================================================
#load_dotenv()

# 固定キーを優先
FIXED_API_KEY = os.getenv("GEMINI_API_KEY")
BANK_URL = os.getenv("BANK_URL")
BANK_PASS = os.getenv("BANK_PASSWORD", "1030013")
BANK_PROJECT = os.getenv("API_BANK_PROJECT", "live-audio-assistant")

TARGET_DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")
# クラウド環境用：環境変数 "GOOGLE_TOKEN_JSON"
TOKEN_JSON_CONTENT = os.getenv("GOOGLE_TOKEN_JSON")

def get_gemini_config():
    """APIキーとモデル名を確定する"""
    print(f"🔍 config取得試行: FIXED_API_KEY={'あり' if FIXED_API_KEY else 'なし'}, BANK_URL={'あり' if BANK_URL else 'なし'}")
    if FIXED_API_KEY:
        print("✅ 固定APIキーを使用します")
        return FIXED_API_KEY, "gemini-2.0-flash-exp"
    
    if not BANK_URL:
        print("❌ BANK_URL が設定されていません")
        return None, None

    params = {'pass': BANK_PASS, 'project': BANK_PROJECT}
    try:
        print(f"📡 API Bank にリクエスト中... URL: {BANK_URL}")
        response = requests.get(BANK_URL, params=params, timeout=10)
        data = response.json()
        if data.get('status') == 'success':
            print(f"✅ API Bank 取得成功: {data['model_name']}")
            return data['api_key'], data['model_name']
        else:
            print(f"❌ API Bank 取得失敗: {data.get('message', 'Unknown error')}")
    except Exception as e:
        print(f"❌ API Bank 取得エラー: {e}")
    return None, None

def report_api_error(api_key):
    """API エラーを API Bank に報告する"""
    if FIXED_API_KEY and api_key == FIXED_API_KEY:
        return
    try:
        requests.post(BANK_URL, json={'pass': BANK_PASS, 'api_key': api_key}, timeout=5)
    except:
        pass

# ================================================
# 2. Google Services Tools (手足となる関数)
# ================================================

def get_google_creds():
    """token.json ファイルまたは環境変数から認証情報を取得"""
    if TOKEN_JSON_CONTENT:
        creds_data = json.loads(TOKEN_JSON_CONTENT)
        return Credentials.from_authorized_user_info(creds_data)
    elif os.path.exists("token.json"):
        return Credentials.from_authorized_user_file("token.json")
    else:
        raise FileNotFoundError("Google API の認証情報が見つかりません。")

# --- Drive 検索 ---
async def search_drive_files(query: str):
    """Google Drive 内のファイルを検索します。"""
    print(f"🔍 Drive検索開始: {query}")
    try:
        service = build('drive', 'v3', credentials=get_google_creds())
        q = f"name contains '{query}' and trashed = false"
        if TARGET_DRIVE_FOLDER_ID:
            q += f" and '{TARGET_DRIVE_FOLDER_ID}' in parents"
        results = service.files().list(q=q, fields="files(id, name, mimeType)", pageSize=5).execute()
        files = results.get('files', [])
        if not files: return "ファイルは見つかりませんでした。"
        output = [f"・{f['name']} ({'フォルダ' if f['mimeType'] == 'application/vnd.google-apps.folder' else 'ファイル'})" for f in files]
        return "検索結果:\n" + "\n".join(output)
    except Exception as e: return f"Driveエラー: {str(e)}"

# --- Gmail 操作 ---
async def send_gmail_message(to: str, subject: str, body: str):
    """メールを送信します。"""
    print(f"📧 Gmail送信準備: To={to}")
    try:
        service = build('gmail', 'v1', credentials=get_google_creds())
        message = MIMEText(body)
        message['to'] = to
        message['subject'] = subject
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
        service.users().messages().send(userId='me', body={'raw': raw}).execute()
        return f"{to} 宛にメールを送信しました。"
    except Exception as e: return f"Gmail送信失敗: {str(e)}"

async def list_recent_emails(max_results: int = 5):
    """最近のメール件名を取得します。"""
    print(f"📩 メールリスト取得中 (最大{max_results}件)")
    try:
        service = build('gmail', 'v1', credentials=get_google_creds())
        results = service.users().messages().list(userId='me', maxResults=max_results).execute()
        messages = results.get('messages', [])
        if not messages: return "新着メールはありません。"
        output = []
        for msg in messages:
            m = service.users().messages().get(userId='me', id=msg['id']).execute()
            subject = "件名なし"
            headers = m['payload']['headers']
            for h in headers:
                if h['name'].lower() == 'subject':
                    subject = h['value']
                    break
            output.append(f"・{subject}")
        return "最近のメール:\n" + "\n".join(output)
    except Exception as e: return f"Gmail取得エラー: {str(e)}"

# --- Calendar 操作 ---
async def list_calendar_events(days: int = 7):
    """直近の予定を確認します。"""
    print(f"📅 カレンダー確認中...")
    try:
        service = build('calendar', 'v3', credentials=get_google_creds())
        now = datetime.utcnow().isoformat() + 'Z'
        events_result = service.events().list(calendarId='primary', timeMin=now, maxResults=10, singleEvents=True, orderBy='startTime').execute()
        events = events_result.get('items', [])
        if not events: return "予定は入っていません。"
        output = []
        for e in events:
            start = e['start'].get('dateTime', e['start'].get('date'))
            output.append(f"・{start}: {e['summary']}")
        return "直近の予定:\n" + "\n".join(output)
    except Exception as e: return f"カレンダー取得エラー: {str(e)}"

async def add_calendar_event(summary: str, start_iso: str, end_iso: str):
    """カレンダーに予定を登録します。"""
    print(f"📅 予定登録開始: {summary}")
    try:
        service = build('calendar', 'v3', credentials=get_google_creds())
        event = {
            'summary': summary,
            'start': {'dateTime': start_iso, 'timeZone': 'Asia/Tokyo'},
            'end': {'dateTime': end_iso, 'timeZone': 'Asia/Tokyo'},
        }
        service.events().insert(calendarId='primary', body=event).execute()
        return f"予定「{summary}」をカレンダーに登録しました。"
    except Exception as e: return f"カレンダー登録失敗: {str(e)}"

# --- Tasks 操作 ---
async def list_tasks():
    """現在のタスク一覧を取得します。"""
    print(f"📝 タスク一覧を取得中...")
    try:
        service = build('tasks', 'v1', credentials=get_google_creds())
        results = service.tasks().list(tasklist='@default').execute()
        items = results.get('items', [])
        if not items: return "タスクはありません。"
        return "ToDoリスト:\n" + "\n".join([f"・{t['title']}" for t in items])
    except Exception as e: return f"タスク取得失敗: {str(e)}"

async def add_task(title: str):
    """ToDoリストにタスクを追加します。"""
    print(f"📝 タスク追加: {title}")
    try:
        service = build('tasks', 'v1', credentials=get_google_creds())
        service.tasks().insert(tasklist='@default', body={'title': title}).execute()
        return f"タスク「{title}」を追加しました。"
    except Exception as e: return f"タスク追加失敗: {str(e)}"

# ================================================
# 3. FastAPI & WebSocket
# ================================================
app = FastAPI()

@app.get("/")
async def get():
    if os.path.exists("index.html"):
        with open("index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>index.html not found</h1>")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("🚀 WebSocket 接続開始（クライアントが接続を試みています）")
    api_key, model_id = get_gemini_config()
    if not api_key:
        print("❌ APIキーが取得できないため、接続を拒否します")
        await websocket.close()
        return

    print(f"📡 使用モデル: {model_id}")
    try:
        client = genai.Client(api_key=api_key, http_options={'api_version': 'v1alpha'})
        print("✅ Gemini Client 初期化成功")
    except Exception as e:
        print(f"❌ Gemini Client 初期化失敗: {e}")
        await websocket.close()
        return
    
    # Geminiに教えるツール一覧
    config = {"tools": [
        {"google_search": {}},
        {"function_declarations": [
            {"name": "search_drive_files", "description": "Google Drive内のドキュメントやファイルを検索します。", "parameters": {"type": "OBJECT", "properties": {"query": {"type": "STRING"}}, "required": ["query"]}},
            {"name": "send_gmail_message", "description": "メールを送信します。", "parameters": {"type": "OBJECT", "properties": {"to": {"type": "STRING", "description": "宛先アドレス"}, "subject": {"type": "STRING", "description": "件名"}, "body": {"type": "STRING", "description": "本文"}}, "required": ["to", "subject", "body"]}},
            {"name": "list_recent_emails", "description": "最近のメール一覧を取得します。", "parameters": {"type": "OBJECT", "properties": {"max_results": {"type": "INTEGER"}}}},
            {"name": "list_calendar_events", "description": "カレンダーの予定を確認します。", "parameters": {"type": "OBJECT", "properties": {"days": {"type": "INTEGER"}}}},
            {"name": "add_calendar_event", "description": "カレンダーに予定を登録します。時間は ISO 形式です。", "parameters": {"type": "OBJECT", "properties": {"summary": {"type": "STRING"}, "start_iso": {"type": "STRING"}, "end_iso": {"type": "STRING"}}, "required": ["summary", "start_iso", "end_iso"]}},
            {"name": "list_tasks", "description": "ToDoリストを確認します。", "parameters": {"type": "OBJECT", "properties": {}}},
            {"name": "add_task", "description": "ToDoリストにタスクを追加します。", "parameters": {"type": "OBJECT", "properties": {"title": {"type": "STRING"}}, "required": ["title"]}},
        ]}
    ]}

    try:
        async with client.models.live.connect(model=model_id, config=config) as session:
            async def send_to_gemini():
                print("🎤 クライアントからの音声送信ループ開始")
                try:
                    while True:
                        data = await websocket.receive_bytes()
                        # print(f"📤 データ受信: {len(data)} bytes") # ログが多すぎるのでコメントアウト
                        await session.send(input={"data": data, "mime_type": "audio/pcm"}, end_of_turn=True)
                except Exception as e:
                    print(f"📡 クライアント送信停止: {e}")

            async def receive_from_gemini():
                try:
                    async for message in session.receive():
                        # 音声の返却
                        if message.server_content and message.server_content.model_turn:
                            for part in message.server_content.model_turn.parts:
                                if part.inline_data:
                                    await websocket.send_bytes(part.inline_data.data)
                        
                        # ツール実行要求の処理
                        if message.tool_call:
                            for call in message.tool_call.function_calls:
                                res = None
                                if call.name == "search_drive_files": res = await search_drive_files(call.args["query"])
                                elif call.name == "send_gmail_message": res = await send_gmail_message(call.args["to"], call.args["subject"], call.args["body"])
                                elif call.name == "list_recent_emails": res = await list_recent_emails(call.args.get("max_results", 5))
                                elif call.name == "list_calendar_events": res = await list_calendar_events(call.args.get("days", 7))
                                elif call.name == "add_calendar_event": res = await add_calendar_event(call.args["summary"], call.args["start_iso"], call.args["end_iso"])
                                elif call.name == "list_tasks": res = await list_tasks()
                                elif call.name == "add_task": res = await add_task(call.args["title"])
                                
                                if res:
                                    # print(f"🔧 ツール結果送信: {call.name}")
                                    await session.send(input=types.LiveClientToolResponse(
                                        function_responses=[types.LiveClientFunctionResponse(
                                            name=call.name, id=call.id, response={"result": res}
                                        )]
                                    ))
                        
                        if message.server_content and message.server_content.turn_complete:
                            pass # print("✅ Gemini のターンが完了しました")
                except Exception as e:
                    print(f"❌ Gemini 通信エラー: {e}")
                    print("💡 ヒント: APIキーの制限（ウェブサイト制限）が有効なままになっていませんか？ Pythonから使う場合は制限を外す必要があります。")
                    report_api_error(api_key)

            await asyncio.gather(send_to_gemini(), receive_from_gemini())
    except WebSocketDisconnect:
        print("🔌 クライアントが切断しました")
    except Exception as e:
        print(f"❌ サーバーエラー: {e}")

# html_content variable removed. Served from index.html file.

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)