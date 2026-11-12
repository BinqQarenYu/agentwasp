"""Download key docs from BinqQarenYu/mue-x."""

import os
import json
import base64
import urllib.request
from dotenv import load_dotenv

load_dotenv()

gh_token = os.getenv("GITHUB_TOKEN", "")
headers = {
    "User-Agent": "AgentWASP-Auditor",
    "Accept": "application/vnd.github.v3+json"
}
if gh_token:
    headers["Authorization"] = f"Bearer {gh_token}"

def get_file(path, out_name):
    url = f"https://api.github.com/repos/BinqQarenYu/mue-x/contents/{path}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        data = json.load(resp)
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        with open(out_name, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"Downloaded {path} -> {out_name} ({len(content)} chars)")

os.makedirs("scratch/mue_x_audit", exist_ok=True)
get_file("README.md", "scratch/mue_x_audit/README.md")
get_file("TECHNICAL.md", "scratch/mue_x_audit/TECHNICAL.md")
get_file("CLAUDE.md", "scratch/mue_x_audit/CLAUDE.md")
get_file("DEMO.md", "scratch/mue_x_audit/DEMO.md")
get_file("LAUNCH-KIT.md", "scratch/mue_x_audit/LAUNCH-KIT.md")
get_file("paper/paper.tex", "scratch/mue_x_audit/paper.tex")
get_file("mue/MUE.md", "scratch/mue_x_audit/MUE.md")
