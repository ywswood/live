import os
import asyncio
import json
import base64
import requests
import secrets
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from google.genai import types
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from google_auth_oauthlib.flow import Flow
from email.mime.text import MIMEText
from datetime import datetime, timedelta
from urllib.parse import urlencode

# ================================================
# 1. 環境設定
# ================================================

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

def get_user_credentials(request: Request):
    """現在のユーザーのGoogle認証情報を取得"""
    session_id = request.cookies.get("session_id")
    if not session_id or session_id not in sessions:
        return None
    
    session = sessions[session_id]
    if not session.get("authenticated"):
        return None
    
    return dict_to_credentials(session["credentials"])

# --- Drive 検索 ---
async def search_drive_files(query: str, request: Request = None):
    """Google Drive 内のファイルを検索します。"""
    print(f"🔍 Drive検索開始: {query}")
    try:
        # 認証情報を取得
        if request:
            creds = get_user_credentials(request)
            if not creds:
                return "Drive検索にはログインが必要です。"
        else:
            creds = get_google_creds()
        
        service = build('drive', 'v3', credentials=creds)
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
# 3. Google OAuth2 認証
# ================================================

# セッション情報を保存（本番環境はRedisなどを使用）
sessions = {}

def get_google_flow():
    """Google OAuth2フローを初期化"""
    # 環境変数から認証情報を取得
    gcp_creds_content = os.getenv("GCP_CREDS_JSON")
    
    if gcp_creds_content:
        client_config = json.loads(gcp_creds_content)
    elif os.path.exists("gcp_creds.json"):
        with open("gcp_creds.json", "r") as f:
            client_config = json.load(f)
    else:
        raise FileNotFoundError("Google認証情報が見つかりません。環境変数GCP_CREDS_JSONを設定するか、gcp_creds.jsonファイルを配置してください。")
    
    flow = Flow.from_client_config(
        client_config,
        scopes=[
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
            "https://www.googleapis.com/auth/drive",
            "https://www.googleapis.com/auth/gmail.send",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar",
            "https://www.googleapis.com/auth/tasks"
        ]
    )
    
    # コールバックURLを設定
    # 環境に応じてコールバックURLを動的に設定
    render_url = os.getenv("RENDER_EXTERNAL_URL")
    if render_url:
        # Render環境
        flow.redirect_uri = f"{render_url}/oauth2callback"
    else:
        # ローカル環境
        flow.redirect_uri = "http://localhost:8080/oauth2callback"
    return flow

def is_woodstock_domain(email: str) -> bool:
    """ドメインがwoodstock.co.jpかチェック"""
    return email.endswith("@woodstock.co.jp")

# ================================================
# 4. FastAPI & WebSocket
# ================================================
app = FastAPI()

# CORSミドルウェアを追加
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.post("/simple-auth")
async def simple_auth(request: Request):
    """社内ツール用簡易ログイン"""
    try:
        data = await request.json()
        email = data.get("email", "")
        password = data.get("password", "")
        
        if not email or not password:
            raise HTTPException(status_code=400, detail="メールアドレスとパスワードが必要です")
        
        if not is_woodstock_domain(email):
            raise HTTPException(status_code=403, detail="woodstock.co.jpドメインのメールアドレスが必要です")
        
        # 簡易認証（デモ用：パスワードチェックは省略）
        session_id = secrets.token_urlsafe(32)
        
        # Gmail機能のためにダミー認証情報を設定
        dummy_credentials = {
            "token": "dummy_token",
            "refresh_token": None,
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "dummy_client_id",
            "client_secret": "dummy_client_secret",
            "scopes": [
                "https://www.googleapis.com/auth/userinfo.email",
                "https://www.googleapis.com/auth/userinfo.profile",
                "https://www.googleapis.com/auth/drive",
                "https://www.googleapis.com/auth/gmail.send",
                "https://www.googleapis.com/auth/gmail.readonly",
                "https://www.googleapis.com/auth/calendar",
                "https://www.googleapis.com/auth/tasks"
            ]
        }
        
        sessions[session_id] = {
            "authenticated": True,
            "email": email,
            "name": email.split("@")[0],  # メールアドレスの前半分を名前として使用
            "credentials": dummy_credentials,
            "simple_auth": True  # 簡易認証フラグ
        }
        
        response = JSONResponse({"success": True})
        response.set_cookie("session_id", session_id, httponly=True)
        return response
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ログインエラー: {str(e)}")

@app.get("/gmail/unread")
async def get_unread_emails(request: Request):
    """未読の重要メールを取得"""
    session_id = request.cookies.get("session_id")
    if not session_id or session_id not in sessions:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    session = sessions[session_id]
    if not session.get("authenticated"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    # 簡易認証の場合はダミーデータを返す
    if session.get("simple_auth"):
        # デモ用のダミーメールデータ
        dummy_emails = [
            {
                'id': 'dummy1',
                'from': 'boss@woodstock.co.jp',
                'subject': '明日の会議について',
                'date': '2025-02-11',
                'snippet': '明日の午前10時から重要な会議があります...'
            },
            {
                'id': 'dummy2', 
                'from': 'client@external.com',
                'subject': 'プロジェクト進捗報告',
                'date': '2025-02-11',
                'snippet': '今週のプロジェクト進捗についてご報告します...'
            }
        ]
        return {"emails": dummy_emails}
    
    try:
        # ユーザーの認証情報を復元
        credentials = build_credentials(session.get("credentials", {}))
        
        # Gmail APIサービスを構築
        gmail_service = build('gmail', 'v1', credentials=credentials)
        
        # 未読の重要メールを取得
        results = gmail_service.users().messages().list(
            userId='me',
            q='is:unread important',
            maxResults=10
        ).execute()
        
        messages = results.get('messages', [])
        emails = []
        
        for message in messages:
            msg = gmail_service.users().messages().get(
                userId='me',
                id=message['id'],
                format='metadata',
                metadataHeaders=['From', 'Subject', 'Date']
            ).execute()
            
            # メール情報を抽出
            headers = {h['name']: h['value'] for h in msg['payload'].get('headers', [])}
            
            emails.append({
                'id': message['id'],
                'from': headers.get('From', ''),
                'subject': headers.get('Subject', ''),
                'date': headers.get('Date', ''),
                'snippet': msg.get('snippet', '')
            })
        
        return {"emails": emails}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gmail API error: {str(e)}")

@app.post("/gmail/send")
async def send_email(request: Request):
    """メールを送信"""
    session_id = request.cookies.get("session_id")
    if not session_id or session_id not in sessions:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    session = sessions[session_id]
    if not session.get("authenticated"):
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    try:
        data = await request.json()
        to = data.get("to")
        subject = data.get("subject")
        body = data.get("body")
        
        if not to or not subject or not body:
            raise HTTPException(status_code=400, detail="Missing required fields")
        
        # ユーザーの認証情報を復元
        credentials = build_credentials(session.get("credentials", {}))
        
        # Gmail APIサービスを構築
        gmail_service = build('gmail', 'v1', credentials=credentials)
        
        # メールメッセージを作成
        message = f"From: me\r\nTo: {to}\r\nSubject: {subject}\r\n\r\n{body}"
        raw_message = base64.urlsafe_b64encode(message.encode()).decode()
        
        # メールを送信
        result = gmail_service.users().messages().send(
            userId='me',
            body={'raw': raw_message}
        ).execute()
        
        return {"success": True, "message_id": result['id']}
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gmail API error: {str(e)}")

def build_credentials(creds_dict):
    """認証情報を復元"""
    from google.oauth2.credentials import Credentials
    return Credentials(
        token=creds_dict.get('token'),
        refresh_token=creds_dict.get('refresh_token'),
        token_uri=creds_dict.get('token_uri'),
        client_id=creds_dict.get('client_id'),
        client_secret=creds_dict.get('client_secret'),
        scopes=creds_dict.get('scopes')
    )

@app.get("/auth/login")
async def auth_login():
    """Google認証開始"""
    flow = get_google_flow()
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true'
    )
    # stateをセッションに保存
    session_id = secrets.token_urlsafe(32)
    sessions[session_id] = {"state": state}
    
    response = RedirectResponse(authorization_url)
    response.set_cookie("session_id", session_id, httponly=True)
    return response

@app.get("/oauth2callback")
async def auth_callback(request: Request):
    """OAuth2コールバック処理"""
    session_id = request.cookies.get("session_id")
    if not session_id or session_id not in sessions:
        raise HTTPException(status_code=400, detail="Invalid session")
    
    # URLからstateとcodeを取得
    from urllib.parse import parse_qs
    query_params = parse_qs(str(request.url).split('?')[1] if '?' in str(request.url) else '')
    state = query_params.get('state', [None])[0]
    code = query_params.get('code', [None])[0]
    
    if not state or not code:
        raise HTTPException(status_code=400, detail="Missing authorization code or state")
    
    # state検証
    if sessions[session_id].get("state") != state:
        raise HTTPException(status_code=400, detail="Invalid state")
    
    try:
        flow = get_google_flow()
        flow.fetch_token(code=code)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Token exchange failed: {str(e)}")
    
    # ユーザー情報取得
    credentials = flow.credentials
    userinfo_service = build('oauth2', 'v2', credentials=credentials)
    userinfo = userinfo_service.userinfo().get().execute()
    
    # ドメインチェック
    email = userinfo.get('email', '')
    if not is_woodstock_domain(email):
        raise HTTPException(status_code=403, detail="Access denied: @woodstock.co.jp domain required")
    
    # 認証成功、セッションを更新
    sessions[session_id].update({
        "authenticated": True,
        "email": email,
        "name": userinfo.get('name', ''),
        "credentials": credentials_to_dict(credentials)
    })
    
    return RedirectResponse("/")

@app.get("/auth/logout")
async def auth_logout(request: Request):
    """ログアウト"""
    session_id = request.cookies.get("session_id")
    if session_id and session_id in sessions:
        del sessions[session_id]
    
    response = RedirectResponse("/")
    response.delete_cookie("session_id")
    return response

@app.get("/auth/me")
async def auth_me(request: Request):
    """現在のユーザー情報を返す"""
    session_id = request.cookies.get("session_id")
    if not session_id or session_id not in sessions:
        return {"authenticated": False}
    
    session = sessions[session_id]
    if not session.get("authenticated"):
        return {"authenticated": False}
    
    return {
        "authenticated": True,
        "email": session.get("email"),
        "name": session.get("name")
    }

def credentials_to_dict(credentials):
    """Credentialsオブジェクトを辞書に変換"""
    return {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes
    }

def dict_to_credentials(creds_dict):
    """辞書をCredentialsオブジェクトに変換"""
    return Credentials(
        token=creds_dict['token'],
        refresh_token=creds_dict['refresh_token'],
        token_uri=creds_dict['token_uri'],
        client_id=creds_dict['client_id'],
        client_secret=creds_dict['client_secret'],
        scopes=creds_dict['scopes']
    )

@app.get("/api/gemini-websocket-url")
async def get_gemini_websocket_url():
    """Gemini WebSocket URLをAPIキー付きで返す"""
    api_key, model_id = get_gemini_config()
    if not api_key:
        raise HTTPException(status_code=500, detail="APIキーが取得できません")
    
    # ここでは直接Gemini APIに接続するURLを返す
    # 将来的にはプロキシWebSocketを実装する予定
    url = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent?key={api_key}"
    return {"url": url, "model": model_id}

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
    config = {
        "system_instruction": "あなたは日本語で話す秘書です。ユーザーがどんな言語で話しても、必ず日本語で応答してください。英語は絶対に使わないでください。ツールの結果もすべて日本語で説明してください。自然で丁寧な日本語でお願いします。",
        "tools": [
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
        ]
    }

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

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
