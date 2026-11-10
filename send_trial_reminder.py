import datetime
import json
import subprocess
import urllib.request

REPO = "yashchawan6-hash/yash-project"
WORKFLOW_FILE = "trial_reminder.yml"

def get_github_token():
    p = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        text=True,
        capture_output=True,
        check=False,
    )
    for line in p.stdout.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1].strip()
    return None

def trigger_cloud_reminder():
    ist_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
    expiry_date = datetime.date(2026, 12, 5)
    days_left = (expiry_date - ist_now.date()).days
    print(f"[IST: {ist_now.strftime('%Y-%m-%d %H:%M:%S')}] Days remaining until Dec 5, 2026 Railway Trial expiry: {days_left}")

    token = get_github_token()
    if not token:
        print("Could not retrieve GitHub token via git credential fill.")
        return False

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
    }
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW_FILE}/dispatches"
    req = urllib.request.Request(
        url,
        data=json.dumps({"ref": "main"}).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(f"Triggered GitHub Actions cloud reminder ({WORKFLOW_FILE}): HTTP {resp.status}")
        return resp.status in (200, 204)

if __name__ == "__main__":
    trigger_cloud_reminder()
