"""Audit BinqQarenYu/mue-x repository via GitHub API."""

import os
import sys
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

def get_json(url):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)

repo_url = "https://api.github.com/repos/BinqQarenYu/mue-x"
repo_info = get_json(repo_url)

print("=" * 60)
print("REPOSITORY INFO:")
print("Name:", repo_info.get("full_name"))
print("Description:", repo_info.get("description"))
print("Created At:", repo_info.get("created_at"))
print("Pushed At:", repo_info.get("pushed_at"))
print("Size (KB):", repo_info.get("size"))
print("Default Branch:", repo_info.get("default_branch"))

# Tree structure
tree_url = f"{repo_url}/git/trees/{repo_info.get('default_branch')}?recursive=1"
try:
    tree_data = get_json(tree_url)
    tree = tree_data.get("tree", [])
    print(f"\nTotal Tree Items: {len(tree)}")
    print("\nFILES & DIRECTORIES:")
    for item in tree:
        print(f"[{item['type'][:4]}] {item['path']} ({item.get('size', 0)} bytes)")
except Exception as e:
    print("Tree error:", e)

# Fetch README
try:
    readme_info = get_json(f"{repo_url}/readme")
    content = base64.b64decode(readme_info["content"]).decode("utf-8", errors="replace")
    print("\n" + "=" * 60)
    print("README PREVIEW (First 2000 chars):")
    print(content[:2000])
    with open("mue_x_readme.md", "w", encoding="utf-8") as f:
        f.write(content)
    print("\nFull README saved to mue_x_readme.md")
except Exception as e:
    print("Readme error:", e)
