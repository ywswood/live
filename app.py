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
from dotenv import load_dotenv
from datetime import datetime, timedelta

# ================================================
# 1. 環境設定
# ================================================
load_dotenv()

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
    if FIXED_API_KEY:
        return FIXED_API_KEY, "gemini-2.0-flash-exp"
    
    params = {'pass': BANK_PASS, 'project': BANK_PROJECT}
    try:
        response = requests.get(BANK_URL, params=params, timeout=10)
        data = response.json()
        if data.get('status') == 'success':
            return data['api_key'], data['model_name']
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
    return HTMLResponse(html_content)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("🚀 WebSocket 接続開始")
    api_key, model_id = get_gemini_config()
    if not api_key:
        await websocket.close()
        return

    print(f"📡 使用モデル: {model_id}")
    client = genai.Client(api_key=api_key, http_options={'api_version': 'v1alpha'})
    
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
                try:
                    while True:
                        data = await websocket.receive_bytes()
                        await session.send(input={"data": data, "mime_type": "audio/pcm"}, end_of_turn=True)
                except: pass

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
                                    await session.send(input=types.LiveClientToolResponse(
                                        function_responses=[types.LiveClientFunctionResponse(
                                            name=call.name, id=call.id, response={"result": res}
                                        )]
                                    ))
                except Exception as e:
                    print(f"⚠️ Gemini 通信エラー: {e}")
                    report_api_error(api_key)

            await asyncio.gather(send_to_gemini(), receive_from_gemini())
    except WebSocketDisconnect:
        print("🔌 クライアントが切断しました")
    except Exception as e:
        print(f"❌ サーバーエラー: {e}")

# ================================================
# 4. UI (TikTokRec スタイル)
# ================================================
html_content = """
<!DOCTYPE html>
<html lang="ja">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Voice Assistant Pro</title>
    <style>
        :root { 
            --bg: #000; 
            --cyan: #00f2ea; 
            --pink: #ff0050; 
            --grad: linear-gradient(135deg, #ff0050, #00f2ea); 
            --text: #fff;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
        body { 
            background: var(--bg); 
            color: var(--text); 
            height: 100vh; 
            display: flex; 
            flex-direction: column; 
            align-items: center; 
            justify-content: center; 
            overflow: hidden; 
        }
        .container { 
            width: 100%; 
            max-width: 420px; 
            height: 100%; 
            display: flex; 
            flex-direction: column; 
            align-items: center; 
            justify-content: space-between; 
            padding: 70px 30px; 
            text-align: center; 
        }
        header h1 { 
            font-size: 2.2rem; 
            font-weight: 800; 
            letter-spacing: -0.02em; 
            line-height: 1.2; 
        }
        header h1 span { color: var(--cyan); text-shadow: 0 0 15px rgba(0,242,234,0.4); }
        .main-area { flex: 1; display: flex; align-items: center; justify-content: center; width: 100%; }
        
        /* TikTokRec 譲りのアニメーションリング */
        .btn-wrapper { position: relative; width: 220px; height: 220px; cursor: pointer; transition: transform 0.1s; }
        .btn-wrapper:active { transform: scale(0.96); }
        .ring { 
            position: absolute; 
            inset: 0; 
            border-radius: 50%; 
            background: var(--grad); 
            mask: radial-gradient(transparent 71px, #000 73px); 
            -webkit-mask: radial-gradient(transparent 71px, #000 73px); 
            animation: spin 12s linear infinite; 
        }
        .fill { 
            position: absolute; 
            inset: 12px; 
            background: radial-gradient(circle at center, #151515, #000); 
            border-radius: 50%; 
            display: flex; 
            flex-direction: column; 
            align-items: center; 
            justify-content: center; 
            border: 1.5px solid #222; 
            box-shadow: inset 0 0 30px #000; 
        }
        .recording .ring { 
            animation: pulse 0.8s ease-in-out infinite; 
            mask: none; 
            -webkit-mask: none; 
            background: var(--pink); 
            opacity: 0.45; 
            transform: scale(1.18); 
            box-shadow: 0 0 50px var(--pink); 
        }
        .recording .fill { border-color: var(--pink); box-shadow: 0 0 20px rgba(255,0,80,0.2); }
        
        @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
        @keyframes pulse { 0%, 100% { transform: scale(1.05); opacity: 0.35; } 50% { transform: scale(1.25); opacity: 0.55; } }
        
        .status-box { width: 100%; min-height: 70px; }
        #status { color: #999; font-size: 0.95rem; font-weight: 500; }
        #txt { font-size: 0.85rem; font-weight: 800; color: #555; letter-spacing: 0.15em; margin-top: 10px; }
        .recording #txt { color: var(--pink); text-shadow: 0 0 8px var(--pink); }
    </style>
</head>
<body>
    <div class="container">
        <header><h1><span>声</span>で、<br>すべてを操る。</h1></header>
        <div class="main-area">
            <div id="btn" class="btn-wrapper">
                <div class="ring"></div>
                <div class="fill">
                    <div style="font-size:3.8rem">🎙️</div>
                    <div id="txt">READY</div>
                </div>
            </div>
        </div>
        <div class="status-box">
            <div id="status">ボタンを長押しして話しかけてください</div>
        </div>
    </div>
    <script>
        let ws, audioCtx, proc, isRec = false;
        const btn = document.getElementById('btn'), st = document.getElementById('status'), txt = document.getElementById('txt');
        
        async function init() {
            try {
                audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
                const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
                ws = new WebSocket(`${proto}//${location.host}/ws`);
                ws.binaryType = 'arraybuffer';
                
                ws.onmessage = async (e) => {
                    const buffer = await audioCtx.decodeAudioData(e.data);
                    const src = audioCtx.createBufferSource();
                    src.buffer = buffer;
                    src.connect(audioCtx.destination);
                    src.start();
                };
                
                const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
                const src = audioCtx.createMediaStreamSource(stream);
                proc = audioCtx.createScriptProcessor(2048, 1, 1);
                src.connect(proc);
                proc.connect(audioCtx.destination);
                
                proc.onaudioprocess = (e) => {
                    if (!isRec || ws.readyState !== 1) return;
                    const input = e.inputBuffer.getChannelData(0);
                    const pcm = new Int16Array(input.length);
                    for (let i=0; i<input.length; i++) {
                        pcm[i] = Math.max(-1, Math.min(1, input[i])) * 0x7FFF;
                    }
                    ws.send(pcm.buffer);
                };
                st.innerText = '接続されました。どうぞ！';
            } catch (err) { 
                st.innerText = 'マイクが起動できません。設定を確認してください。'; 
                console.error(err); 
            }
        }
        
        function start() { 
            if(!audioCtx) init(); 
            isRec = true; 
            btn.classList.add('recording'); 
            txt.innerText = 'LISTENING'; 
            st.innerText = '聴いています...'; 
        }
        
        function stop() { 
            isRec = false; 
            btn.classList.remove('recording'); 
            txt.innerText = 'READY'; 
            st.innerText = '考えています...'; 
        }
        
        btn.onmousedown = start; btn.onmouseup = stop;
        btn.ontouchstart = (e) => { e.preventDefault(); start(); }; btn.ontouchend = stop;
    </script>
</body>
</html>
"""

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)