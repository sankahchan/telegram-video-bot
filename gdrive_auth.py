"""One-time Google Drive OAuth setup. Run on a computer with a browser:

    python3 gdrive_auth.py

Needs client_secret.json (OAuth Desktop client) in this directory first —
see gdrive.py docstring for the Google Cloud Console steps.
Saves token.json next to this file; copy it to the VPS afterwards:

    scp token.json <vps>:/opt/tg-video-bot/
"""
import os

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_SECRET = os.path.join(DATA_DIR, "client_secret.json")
TOKEN_PATH = os.path.join(DATA_DIR, "token.json")
SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def main():
    if not os.path.exists(CLIENT_SECRET):
        print("❌ client_secret.json not found next to this script.")
        print("   Google Cloud Console -> APIs & Services -> Credentials ->")
        print("   Create Credentials -> OAuth client ID -> Desktop app,")
        print("   download the JSON and save it as client_secret.json here.")
        raise SystemExit(1)
    from google_auth_oauthlib.flow import InstalledAppFlow
    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET, SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True,
                                  prompt="consent")
    with open(TOKEN_PATH, "w") as f:
        f.write(creds.to_json())
    print(f"✅ saved {TOKEN_PATH}")
    print("   Now copy it to the VPS:  scp token.json <vps>:/opt/tg-video-bot/")
    print("   then:  sudo systemctl restart tg-video-bot")


if __name__ == "__main__":
    main()
