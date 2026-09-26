"""Google Drive upload for files too big for Telegram (~2GB cap).

One-time setup (on the Mac, it has a browser):
  1. Google Cloud Console -> new project -> enable "Google Drive API"
  2. APIs & Services -> Credentials -> Create Credentials -> OAuth client ID
     -> Desktop app -> download JSON -> save as client_secret.json next to
     this file
  3. python3 gdrive_auth.py   (opens browser, approve, saves token.json)
  4. copy token.json to the VPS:  scp token.json <vps>:/opt/tg-video-bot/

After that the bot can upload; /drivestatus shows the state.
token.json / client_secret.json are gitignored and never committed.
"""
import os

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_PATH = os.path.join(DATA_DIR, "token.json")
FOLDER_NAME = "tg-video-bot"

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def is_configured() -> bool:
    """A saved OAuth token exists (setup completed)."""
    return os.path.exists(TOKEN_PATH)


def _service():
    if not is_configured():
        return None
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except ImportError:
        return None
    creds = Credentials.from_authorized_user_file(TOKEN_PATH, _SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_PATH, "w") as f:
                f.write(creds.to_json())
        else:
            return None
    return build("drive", "v3", credentials=creds,
                 cache_discovery=False)


def _folder_id(service) -> str:
    """Find (or create) the tg-video-bot folder, return its id."""
    q = (f"name='{FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' "
         "and trashed=false")
    res = service.files().list(q=q, fields="files(id)", pageSize=1).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": FOLDER_NAME,
            "mimeType": "application/vnd.google-apps.folder"}
    return service.files().create(body=meta, fields="id").execute()["id"]


def account_email() -> str | None:
    """Which Google account the token belongs to (for /drivestatus)."""
    service = _service()
    if service is None:
        return None
    try:
        about = service.about().get(fields="user(emailAddress)").execute()
        return about.get("user", {}).get("emailAddress")
    except Exception:
        return None


def upload_file(path: str, name: str,
                progress_cb=None) -> dict:
    """Resumable upload -> {id, link}. progress_cb(done, total) optional."""
    service = _service()
    if service is None:
        raise RuntimeError("Google Drive not configured — run gdrive_auth.py first")
    from googleapiclient.http import MediaFileUpload
    folder = _folder_id(service)
    media = MediaFileUpload(path, resumable=True, chunksize=8 * 1024 * 1024)
    meta = {"name": name, "parents": [folder]}
    req = service.files().create(body=meta, media_body=media,
                                 fields="id, size")
    resp = None
    total = os.path.getsize(path)
    while resp is None:
        status, resp = req.next_chunk()
        if progress_cb and status:
            try:
                progress_cb(int(status.resumable_progress or 0), total)
            except Exception:
                pass
    fid = resp["id"]
    # anyone-with-link reader link (convenient; revoke in Drive any time)
    try:
        service.permissions().create(
            fileId=fid, body={"type": "anyone", "role": "reader"}).execute()
    except Exception:
        pass
    link = service.files().get(fileId=fid,
                               fields="webViewLink").execute()["webViewLink"]
    return {"id": fid, "link": link}
