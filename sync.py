import csv
import datetime as dt
import math
import os
import urllib.parse

import requests

ZOOM_ACCOUNT_ID = os.environ["ZOOM_ACCOUNT_ID"]
ZOOM_CLIENT_ID = os.environ["ZOOM_CLIENT_ID"]
ZOOM_CLIENT_SECRET = os.environ["ZOOM_CLIENT_SECRET"]
NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]

NOTION_VERSION = "2025-09-03"
PART_SIZE = 15 * 1024 * 1024  # 15 MB parts (Notion allows 5-20 MB)
LOOKBACK_DAYS = 7

# ---------- Zoom helpers ----------

def zoom_token():
    r = requests.post(
        "https://zoom.us/oauth/token",
        params={"grant_type": "account_credentials", "account_id": ZOOM_ACCOUNT_ID},
        auth=(ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET),
    )
    r.raise_for_status()
    return r.json()["access_token"]

def zoom_get(token, path, **params):
    r = requests.get(
        f"https://api.zoom.us/v2{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    if r.status_code == 404:
        return None  # e.g. no summary / no registrants for this meeting
    r.raise_for_status()
    return r.json()

def encode_uuid(uuid):
    # Zoom requires double URL-encoding when UUID starts with / or contains //
    if uuid.startswith("/") or "//" in uuid:
        return urllib.parse.quote(urllib.parse.quote(uuid, safe=""), safe="")
    return urllib.parse.quote(uuid, safe="")

def download_file(url, token, dest):
    with requests.get(url, headers={"Authorization": f"Bearer {token}"}, stream=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1024 * 1024):
                f.write(chunk)

def get_participants(token, uuid):
    rows, next_token = [], ""
    while True:
        data = zoom_get(
            token,
            f"/report/meetings/{encode_uuid(uuid)}/participants",
            page_size=300,
            next_page_token=next_token,
        )
        if not data:
            break
        rows += data.get("participants", [])
        next_token = data.get("next_page_token", "")
        if not next_token:
            break
    return rows

def get_registrants(token, meeting_id):
    rows, next_token = [], ""
    while True:
        data = zoom_get(
            token,
            f"/meetings/{meeting_id}/registrants",
            page_size=300,
            status="approved",
            next_page_token=next_token,
        )
        if not data:
            break
        rows += data.get("registrants", [])
        next_token = data.get("next_page_token", "")
        if not next_token:
            break
    return rows

def get_summary_text(token, uuid):
    data = zoom_get(token, f"/meetings/{encode_uuid(uuid)}/meeting_summary")
    if not data:
        return None
    parts = []
    if data.get("summary_overview"):
        parts.append(data["summary_overview"])
    for d in data.get("summary_details", []):
        label = d.get("label", "")
        parts.append((label + "\n" if label else "") + d.get("summary", ""))
    steps = data.get("next_steps") or []
    if steps:
        parts.append("Next steps:\n" + "\n".join(f"- {s}" for s in steps))
    return "\n\n".join(p for p in parts if p) or None

# ---------- CSV helpers ----------

def write_participants_csv(participants, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Name", "Email", "Join Time", "Leave Time", "Duration (min)"])
        for p in participants:
            w.writerow([
                p.get("name", ""),
                p.get("user_email", ""),
                p.get("join_time", ""),
                p.get("leave_time", ""),
                round(p.get("duration", 0) / 60, 1),
            ])

def write_registrants_csv(registrants, path):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["First Name", "Last Name", "Email", "Registered At", "Status"])
        for p in registrants:
            w.writerow([
                p.get("first_name", ""),
                p.get("last_name", ""),
                p.get("email", ""),
                p.get("create_time", ""),
                p.get("status", ""),
            ])

# ---------- Notion helpers ----------

def notion(method, path, **kwargs):
    headers = {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Notion-Version": NOTION_VERSION,
    }
    headers.update(kwargs.pop("headers", {}))
    r = requests.request(method, f"https://api.notion.com/v1{path}", headers=headers, **kwargs)
    if not r.ok:
        print("Notion error:", r.status_code, r.text[:500])
    r.raise_for_status()
    return r.json()

def get_data_source_id():
    db = notion("GET", f"/databases/{NOTION_DATABASE_ID}")
    return db["data_sources"][0]["id"]

def already_synced(ds_id, uuid):
    res = notion("POST", f"/data_sources/{ds_id}/query", json={
        "filter": {"property": "Zoom UUID", "rich_text": {"equals": uuid}}
    })
    return len(res.get("results", [])) > 0

def upload_to_notion(path, content_type):
    """Uploads a file to Notion; returns the file_upload id. Uses multi-part for >20MB."""
    size = os.path.getsize(path)
    filename = os.path.basename(path)
    if size <= 20 * 1024 * 1024:
        fu = notion("POST", "/file_uploads", json={
            "filename": filename, "content_type": content_type,
        })
        with open(path, "rb") as f:
            notion("POST", f"/file_uploads/{fu['id']}/send",
                   files={"file": (filename, f, content_type)})
        return fu["id"]

    parts = math.ceil(size / PART_SIZE)
    fu = notion("POST", "/file_uploads", json={
        "mode": "multi_part", "number_of_parts": parts,
        "filename": filename, "content_type": content_type,
    })
    with open(path, "rb") as f:
        for i in range(1, parts + 1):
            chunk = f.read(PART_SIZE)
            notion("POST", f"/file_uploads/{fu['id']}/send",
                   data={"part_number": str(i)},
                   files={"file": (filename, chunk, content_type)})
            print(f"    uploaded part {i}/{parts}")
    notion("POST", f"/file_uploads/{fu['id']}/complete")
    return fu["id"]

def text_blocks(text, block_type="paragraph"):
    """Split long text into <=2000-char rich text blocks (Notion limit)."""
    blocks = []
    for i in range(0, len(text), 2000):
        blocks.append({
            "object": "block", "type": block_type,
            block_type: {"rich_text": [{"type": "text", "text": {"content": text[i:i + 2000]}}]},
        })
    return blocks

def heading(text):
    return {"object": "block", "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": text}}]}}

def file_block(block_type, upload_id):
    return {"object": "block", "type": block_type,
            block_type: {"type": "file_upload", "file_upload": {"id": upload_id}}}

# ---------- Main ----------

def process_meeting(token, ds_id, meeting):
    uuid = meeting["uuid"]
    topic = meeting.get("topic", "Untitled meeting")
    print(f"Processing: {topic} ({uuid})")

    if already_synced(ds_id, uuid):
        print("  already synced, skipping")
        return

    files = meeting.get("recording_files", [])
    mp4 = next((f for f in files if f.get("file_type") == "MP4" and f.get("status") == "completed"), None)
    vtt = next((f for f in files if f.get("file_type") == "TRANSCRIPT"), None)
    if not mp4:
        print("  no completed MP4 yet, will retry next run")
        return

    # 1. Download from Zoom Cloud
    download_file(mp4["download_url"], token, "recording.mp4")
    print(f"  video downloaded ({os.path.getsize('recording.mp4') / 1e6:.0f} MB)")
    if vtt:
        download_file(vtt["download_url"], token, "transcript.vtt")

    # 2. Attendance + registrants + AI summary
    participants = get_participants(token, uuid)
    registrants = get_registrants(token, meeting["id"])
    summary = get_summary_text(token, uuid)
    write_participants_csv(participants, "attendance.csv")
    write_registrants_csv(registrants, "registrants.csv")

    # 3. Upload files to Notion
    video_id = upload_to_notion("recording.mp4", "video/mp4")
    attendance_id = upload_to_notion("attendance.csv", "text/csv")
    registrants_id = upload_to_notion("registrants.csv", "text/csv")
    transcript_id = upload_to_notion("transcript.vtt", "text/vtt") if vtt else None

    # 4. Build page body
    children = [heading("🎥 Recording"), file_block("video", video_id)]
    if transcript_id:
        children += [heading("📄 Transcript"), file_block("file", transcript_id)]
    children += [heading("🧑‍🤝‍🧑 Attendance"), file_block("file", attendance_id)]
    children += [heading("📝 Registered Users"), file_block("file", registrants_id)]
    children += [heading("🤖 AI Summary")]
    children += text_blocks(summary or "Summary not available yet.")

    # 5. Create the database row
    notion("POST", "/pages", json={
        "parent": {"type": "data_source_id", "data_source_id": ds_id},
        "properties": {
            "Meeting Topic": {"title": [{"text": {"content": topic}}]},
            "Date": {"date": {"start": meeting.get("start_time")}},
            "Attended Count": {"number": len(participants)},
            "Registered Count": {"number": len(registrants)},
            "Duration (min)": {"number": meeting.get("duration")},
            "Zoom UUID": {"rich_text": [{"text": {"content": uuid}}]},
        },
        "children": children,
    })
    print("  ✅ synced to Notion")

    # cleanup runner disk between meetings
    for tmp in ("recording.mp4", "transcript.vtt", "attendance.csv", "registrants.csv"):
        if os.path.exists(tmp):
            os.remove(tmp)

def main():
    token = zoom_token()
    ds_id = get_data_source_id()
    from_date = (dt.date.today() - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()

    next_token = ""
    while True:
        # 'from' is a reserved Python keyword, so build this request manually:
        data = requests.get(
            "https://api.zoom.us/v2/users/me/recordings",
            headers={"Authorization": f"Bearer {token}"},
            params={"from": from_date, "page_size": 30, "next_page_token": next_token},
        ).json()
        for meeting in data.get("meetings", []):
            try:
                process_meeting(token, ds_id, meeting)
            except Exception as e:
                print(f"  ⚠️ failed: {e}")
        next_token = data.get("next_page_token", "")
        if not next_token:
            break

if __name__ == "__main__":
    main()
