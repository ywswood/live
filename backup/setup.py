import os.path
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# 今回の拡張に必要なスコープ（Gmail送信、Drive参照）
SCOPES = [
    'https://www.googleapis.com/auth/drive.readonly',
    'https://www.googleapis.com/auth/gmail.send',
    'https://www.googleapis.com/auth/gmail.readonly'
]

def main():
    """gcp_creds.json から token.json を生成する"""
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                'gcp_creds.json', SCOPES)
            # 8080ポートで待機
            creds = flow.run_local_server(port=8080)
            
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
            print("✅ token.json を保存しました")

if __name__ == '__main__':
    main()