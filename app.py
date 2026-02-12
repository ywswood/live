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
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from email.mime.text import MIMEText
from datetime import datetime

# ================================================
# 1. 環境設定
# ================================================

# 固定キーを優先
FIXED_API_KEY = os.getenv("GEMINI_API_KEY")
BANK_URL = os.getenv("BANK_URL")
BANK_PASS = os.getenv("BANK_PASSWORD", "1030013")
BANK_PROJECT = os.getenv("API_BANK_PROJECT", "live-audio-assistant")

TARGET_DRIVE_FOLDER_ID = os.getenv("DRIVE_FOLDER_ID")
TOKEN_JSON_CONTENT = os.getenv("GOOGLE_TOKEN_JSON")

def get_gemini_config():
    """APIキーとモデル名を確定する"""
    # 公式ドキュメント準拠：Live API 用ネイティブ音声モデル
    model_name = "gemini-2.5-flash-native-audio-preview-12-2025"
    
    if FIXED_API_KEY:
        print("✅ 固定APIキーを使用します")
        return FIXED_API_KEY, model_name
    
    if not BANK_URL:
        print("❌ BANK_URL が設定されていません")
        return None, None

    params = {'pass': BANK_PASS, 'project': BANK_PROJECT}
    try:
        response = requests.get(BANK_URL, params=params, timeout=10)
        data = response.json()
        if data.get('status') == 'success':
            print(f"✅ API Bank 取得成功: {data['model_name']}")
            # API Bankのモデル名に関わらず、Live API用の最新モデルを強制使用
            return data['api_key'], model_name
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
# 2. Google OAuth2 認証 (省略 - 変更なし)
# ================================================
# ... (既存の認証コードを再利用するために、ここでは省略せず実装します)
sessions = {}

def get_google_flow():
    if os.path.exists("gcp_creds.json"):
        with open("gcp_creds.json", "r") as f:
            client_config = json.load(f)
        
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
        render_url = os.getenv("RENDER_EXTERNAL_URL")
        if render_url:
            flow.redirect_uri = f"{render_url}/oauth2callback"
        else:
            flow.redirect_uri = "http://localhost:8080/oauth2callback"
        return flow
    else:
        # 開発用ダミー
        return None

def is_woodstock_domain(email: str) -> bool:
    return email.endswith("@woodstock.co.jp")

# ================================================
# 3. FastAPI & WebSocket
# ================================================
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/auth/login")
async def auth_login():
    try:
        flow = get_google_flow()
        if not flow: return JSONResponse({"error": "No GCP creds"}, status_code=500)
        authorization_url, state = flow.authorization_url(access_type='offline', include_granted_scopes='true')
        session_id = secrets.token_urlsafe(32)
        sessions[session_id] = {"state": state}
        response = RedirectResponse(authorization_url)
        response.set_cookie("session_id", session_id, httponly=True)
        return response
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/oauth2callback")
async def auth_callback(request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id or session_id not in sessions:
        raise HTTPException(status_code=400, detail="Invalid session")
    
    try:
        flow = get_google_flow()
        flow.fetch_token(authorization_response=str(request.url))
        credentials = flow.credentials
        
        # ユーザー情報取得 (簡易版)
        import google.auth.transport.requests
        request_google = google.auth.transport.requests.Request()
        id_info = None 
        # id_tokenがない場合のフォールバックが必要だが、ここでは省略
        
        sessions[session_id].update({"authenticated": True, "email": "user@woodstock.co.jp"}) # 仮
        return RedirectResponse("/")
    except:
        return RedirectResponse("/")

@app.get("/auth/me")
async def auth_me(request: Request):
    session_id = request.cookies.get("session_id")
    if not session_id or session_id not in sessions:
        return {"authenticated": False}
    return {"authenticated": sessions[session_id].get("authenticated", False)}

@app.get("/auth/logout")
async def auth_logout(request: Request):
    session_id = request.cookies.get("session_id")
    if session_id in sessions: del sessions[session_id]
    response = RedirectResponse("/")
    response.delete_cookie("session_id")
    return response

@app.get("/")
async def get():
    if os.path.exists("index.html"):
        with open("index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>index.html not found</h1>")

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("🚀 WebSocket 接続開始")
    api_key, model_id = get_gemini_config()
    if not api_key:
        await websocket.close()
        return

    print(f"📡 使用モデル: {model_id}")
    try:
        # 古いクライアント初期化方法：http_options は v1beta に戻す（安定版）
        client = genai.Client(api_key=api_key, http_options={'api_version': 'v1alpha'})
        print("✅ Gemini Client 初期化成功")
    except Exception as e:
        print(f"❌ Gemini Client 初期化失敗: {e}")
        await websocket.close()
        return
    
    # ツール設定（response_modalitiesを必ず含める）
    config = {
        "response_modalities": ["AUDIO"],
        "system_instruction": "あなたは日本語で話す秘書です。必ず日本語で応答してください。",
        "tools": [{"google_search": {}}]
    }

    try:
        # 古い接続方法：client.models.live.connect
        async with client.models.live.connect(model=model_id, config=config) as session:
            print("✅ Gemini Live セッション確立")
            
            async def send_to_gemini():
                print("🎤 音声送信ループ開始")
                try:
                    while True:
                        # バイナリデータのみ受信（JSONは受け付けない）
                        data = await websocket.receive_bytes()
                        # 旧送信方法：辞書形式で send + end_of_turn=True (?)
                        # ドキュメントでは send(input=..., end_of_turn=True) だが
                        # app_2723058.py では input={"data": data, "mime_type": "audio/pcm"} だった
                        # mime_type に rate を含めず送ってみる（旧コードの再現）
                        await session.send(input={"data": data, "mime_type": "audio/pcm"}, end_of_turn=True)
                except Exception as e:
                    print(f"📡 送信停止: {e}")

            async def receive_from_gemini():
                try:
                    async for message in session.receive():
                        # ServerContent
                        if message.server_content and message.server_content.model_turn:
                            for part in message.server_content.model_turn.parts:
                                if part.inline_data:
                                    # クライアントへバイナリ転送
                                    await websocket.send_bytes(part.inline_data.data)
                        
                        # ToolCallなどは一旦省略（まずは音声疎通）
                        
                except Exception as e:
                    print(f"❌ 受信エラー: {e}")

            await asyncio.gather(send_to_gemini(), receive_from_gemini())
            
    except WebSocketDisconnect:
        print("🔌 クライアント切断")
    except Exception as e:
        print(f"❌ サーバーエラー: {e}")
