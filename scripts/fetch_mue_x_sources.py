"""Fetch key Python source files from BinqQarenYu/mue-x for code-level audit."""

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

files_to_fetch = [
    "mue/evo/core.py",
    "mue/evo/dna/mutator.py",
    "mue/evo/dna/sandbox.py",
    "mue/evo/dna/genome.py",
    "mue/evo/absorption/github_miner.py",
    "mue/evo/autonomy/autonomous_signals.py",
    "mue/evo/evolution/loop.py",
    "mue/evo/evolution/rl_optimizer.py",
    "mue/evo/security/guard.py",
    "mue/evo/memory/lattice.py",
    "mue/evo/self_modification/pipeline.py",
    "mue/evo/skills/crystallizer.py",
]

for fpath in files_to_fetch:
    url = f"https://api.github.com/repos/BinqQarenYu/mue-x/contents/{fpath}"
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as resp:
            data = json.load(resp)
            content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
            local_name = fpath.replace("/", "_")
            out_path = os.path.join("scratch/mue_x_audit", local_name)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"Fetched {fpath} -> {out_path} ({len(content.splitlines())} lines)")
    except Exception as e:
        print(f"Failed {fpath}: {e}")
