"""GitHub Absorption Engine — Mines GitHub for code patterns, concepts, and evolution ideas.

The agent searches for repos related to self-evolving AI, extracts code snippets,
identifies valuable patterns, and absorbs them as "Atouts" (evolvable capabilities).

Unlike simple cloning, the agent UNDERSTANDS what it finds and selectively
integrates only what improves its fitness. No bloat, no thousands of files.
"""

import json
import hashlib
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .code_assessor import (
    DOMAIN_KEYWORDS,
    assess_code,
    extract_key_pattern,
    is_noise_file,
    filename_matches_domain,
)


@dataclass
class AbsorbedPattern:
    """A code pattern or concept absorbed from GitHub."""
    source_url: str
    source_repo: str
    pattern_type: str  # "function", "architecture", "algorithm", "concept", "trick"
    code: str
    description: str
    value_assessment: float  # 0-1 how useful for the agent
    fingerprint: str  # unique hash for dedup
    absorbed_at: float = field(default_factory=time.time)
    usage_count: int = 0
    success_count: int = 0

    @property
    def success_rate(self) -> float:
        return self.success_count / max(self.usage_count, 1)


class GitHubMiner:
    """Searches GitHub for evolutionary material and absorbs the best patterns."""

    # Pre-loaded high-value repos for reference (domain → repos)
    REFERENCE_REPOS = {
        "general": [
            "gpt-engineer-org/gpt-engineer", "Aider-AI/aider", "microsoft/autogen",
            "crewAIInc/crewAI", "langchain-ai/langchain", "joaomdmoura/crewAI",
            "Significant-Gravitas/AutoGPT", "run-llama/llama_index",
            "BerriAI/litellm", "TransformerOptimus/SuperAGI",
        ],
        "trading": [
            "vnpy/vnpy", "jesse-ai/jesse", "freqtrade/freqtrade",
            "quantopian/zipline", "mementum/backtrader", "quantconnect/Lean",
            "ccxt/ccxt", "ta-lib/ta-lib-python", "ranaroussi/quantstats",
        ],
        "coding": [
            "psf/black", "astral-sh/ruff", "pytest-dev/pytest",
            "python/mypy", "aws/jsii", "numpy/numpy",
        ],
        "research": [
            "huggingface/transformers", "pytorch/pytorch", "tensorflow/tensorflow",
            "openai/openai-python", "anthropics/anthropic-sdk-python",
        ],
    }

    def __init__(self, atouts_dir: Path, memory_lattice, genome, project_root: Path = None):
        self.atouts_dir = Path(atouts_dir)
        self.atouts_dir.mkdir(parents=True, exist_ok=True)
        self.memory = memory_lattice
        self.genome = genome
        # project_root is the actual project directory (e.g., free-claude-code/)
        self.project_root = project_root or atouts_dir.parent.parent
        self.absorbed: dict[str, AbsorbedPattern] = {}
        self._load_absorbed()
        self._mining_cycle_count = 0
        self.auto_mine_interval = 2  # Auto-mine every N evolution cycles (high frequency)
        self._clones_dir = self.atouts_dir / "_clones"
        self._clones_dir.mkdir(parents=True, exist_ok=True)
        # M9: Rate limiting for GitHub API
        self._rate_limit_remaining = 60
        self._rate_limit_reset = time.time()

    def _load_absorbed(self):
        """Load previously absorbed atouts. Reconciles manifest with on-disk files."""
        manifest = self.atouts_dir / "_manifest.json"
        loaded_fps = set()

        # Load from manifest
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text("utf-8"))
                for entry in data:
                    loaded_fps.add(entry["fingerprint"])
                    atout_path = self.atouts_dir / f"{entry['fingerprint']}.py"
                    if atout_path.exists():
                        pattern = AbsorbedPattern(
                            source_url=entry.get("source_url", ""),
                            source_repo=entry.get("source_repo", ""),
                            pattern_type=entry.get("pattern_type", ""),
                            code=atout_path.read_text("utf-8"),
                            description=entry.get("description", ""),
                            value_assessment=entry.get("value_assessment", 0.5),
                            fingerprint=entry["fingerprint"],
                            usage_count=entry.get("usage_count", 0),
                            success_count=entry.get("success_count", 0),
                        )
                        self.absorbed[pattern.fingerprint] = pattern
            except (json.JSONDecodeError, KeyError):
                pass

        # S10 FIX: Reconcile orphan atout files not in manifest
        manifest_changed = False
        for py_file in self.atouts_dir.glob("*.py"):
            fp = py_file.stem
            if fp not in loaded_fps and fp != "_manifest":
                try:
                    code = py_file.read_text("utf-8")
                    # Extract header metadata if available
                    lines = code.split("\n")
                    source_url = ""
                    source_repo = ""
                    pattern_type = ""
                    description = ""
                    value = 0.4
                    for line in lines[:6]:
                        if line.startswith("# Atout absorbed from:"):
                            source_url = line.replace("# Atout absorbed from:", "").strip()
                        elif line.startswith("# Type:"):
                            pattern_type = line.replace("# Type:", "").strip()
                        elif line.startswith("# Value:"):
                            try:
                                value = float(line.replace("# Value:", "").strip())
                            except ValueError:
                                pass
                        elif not line.startswith("#") and not line.strip() == "":
                            if not description:
                                description = line.strip()[:200]

                    body = "\n".join(lines[6:]) if len(lines) > 6 else code
                    pattern = AbsorbedPattern(
                        source_url=source_url,
                        source_repo=source_repo,
                        pattern_type=pattern_type or "unknown",
                        code=body.strip(),
                        description=description or f"Orphan atout recovered: {fp}",
                        value_assessment=value,
                        fingerprint=fp,
                    )
                    self.absorbed[fp] = pattern
                    loaded_fps.add(fp)
                    manifest_changed = True
                except Exception:
                    pass

        # S10 FIX: Prune manifest entries pointing to deleted files
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text("utf-8"))
                pruned = [e for e in data if (self.atouts_dir / f"{e['fingerprint']}.py").exists()]
                if len(pruned) != len(data):
                    (self.atouts_dir / "_manifest.json").write_text(
                        json.dumps(pruned, indent=2), encoding="utf-8"
                    )
            except Exception:
                pass

        if manifest_changed:
            self._save_manifest()

    def _save_manifest(self):
        manifest = []
        for fp, p in self.absorbed.items():
            manifest.append({
                "fingerprint": fp,
                "source_url": p.source_url,
                "source_repo": p.source_repo,
                "pattern_type": p.pattern_type,
                "description": p.description,
                "value_assessment": p.value_assessment,
                "usage_count": p.usage_count,
                "success_count": p.success_count,
            })
        (self.atouts_dir / "_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

    def mine(self, query: Optional[str] = None, domain: str = "trading") -> list[AbsorbedPattern]:
        """Search GitHub and absorb patterns. Domain drives scoring + filtering.

        M8 FIX: When using REFERENCE_REPOS (known repos), extract directly
        via _extract_from_repo instead of searching for the repo name as text.
        """
        newly_absorbed = []
        if query:
            # User query: search GitHub for repos matching the text
            try:
                patterns = self._search_and_extract(query, domain=domain)
                for pattern in patterns:
                    if pattern.fingerprint not in self.absorbed:
                        self._absorb(pattern)
                        self.absorbed[pattern.fingerprint] = pattern
                        newly_absorbed.append(pattern)
            except Exception:
                pass
        else:
            # REFERENCE_REPOS: known high-value repos — extract directly
            repos = self.REFERENCE_REPOS.get(domain, self.REFERENCE_REPOS.get("general", []))
            for repo_full_name in repos[:3]:
                try:
                    repo_dict = {
                        "nameWithOwner": repo_full_name,
                        "url": f"https://github.com/{repo_full_name}",
                    }
                    extracted = self._extract_from_repo(repo_dict, domain=domain)
                    for pattern in extracted:
                        if pattern.fingerprint not in self.absorbed:
                            self._absorb(pattern)
                            self.absorbed[pattern.fingerprint] = pattern
                            newly_absorbed.append(pattern)
                except Exception:
                    continue

        self._save_manifest()
        return newly_absorbed

    def _rate_limit_wait(self):
        """M9: Wait if GitHub API rate limit is exhausted."""
        now = time.time()
        if self._rate_limit_remaining <= 2:
            wait = max(0, self._rate_limit_reset - now)
            if wait > 0:
                time.sleep(min(wait, 60.0))
            self._rate_limit_remaining = 60
            self._rate_limit_reset = now + 3600

    def _search_and_extract(self, query: str, domain: str = "general") -> list[AbsorbedPattern]:
        """Search GitHub and extract patterns from top results."""
        patterns = []

        try:
            result = subprocess.run(
                ["gh", "search", "repos", query, "--sort=stars", "--limit=5", "--json=nameWithOwner,url,description"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                repos = json.loads(result.stdout)
                for repo in repos[:3]:
                    extracted = self._extract_from_repo(repo, domain=domain)
                    patterns.extend(extracted)
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            pass

        if not patterns:
            patterns.extend(self._web_fallback(query, domain=domain))

        return patterns

    def _extract_from_repo(self, repo: dict, domain: str = "general") -> list[AbsorbedPattern]:
        """Extract code patterns by cloning the repo and reading all source files.

        Falls back to API-based extraction only when git clone is impossible.
        Domain-aware: skips files irrelevant to the current domain.
        """
        name = repo.get("nameWithOwner", "")
        if not name:
            return []

        patterns = self._clone_and_extract(name, domain=domain)
        if patterns:
            return patterns

        return self._extract_via_api(name, domain=domain)


    def _clone_and_extract(self, full_name: str, domain: str = "general") -> list[AbsorbedPattern]:
        """Shallow-clone a repo, walk relevant Python files, extract best patterns.

        Domain-aware: files matching domain keywords get priority scoring.
        Irrelevant files (serialization, web boilerplate, etc.) are skipped.
        """
        patterns = []
        repo_name = full_name.replace("/", "_")
        clone_path = self._clones_dir / repo_name

        if clone_path.exists():
            try:
                shutil.rmtree(clone_path)
            except Exception:
                pass

        clone_url = f"https://github.com/{full_name}.git"

        try:
            result = subprocess.run(
                ["git", "clone", "--depth", "1", "--single-branch",
                 "--filter=blob:limit=1m", clone_url, str(clone_path)],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0 or not clone_path.exists():
                return patterns
        except (subprocess.TimeoutExpired, Exception):
            return patterns

        # Walk Python files, skipping noise directories
        py_files = []
        skip_dirs = {"test", "tests", "example", "examples", "vendor", "venv",
                     ".venv", "node_modules", "__pycache__", "docs", "benchmarks"}
        for py_file in clone_path.rglob("*.py"):
            parts = set(py_file.parent.parts)
            if parts & skip_dirs:
                continue
            if py_file.stat().st_size < 100 or py_file.stat().st_size > 200_000:
                continue
            if py_file.name in ("setup.py", "conf.py", "conftest.py"):
                continue
            # Domain-based file name filtering
            if self._is_noise_file(py_file.name, domain):
                continue
            py_files.append(py_file)

        # Prioritize domain-relevant files first, then by size
        py_files.sort(key=lambda f: (
            0 if self._filename_matches_domain(f.name, domain) else 1,
            abs(f.stat().st_size - 8000)
        ))
        selected = py_files[:50]

        for py_file in selected:
            try:
                code = py_file.read_text(encoding="utf-8", errors="replace")
                assessment = self._assess_code(code, py_file.name, domain=domain)
                if assessment["value"] >= 0.4:
                    fingerprint = hashlib.sha256(
                        (full_name + str(py_file.relative_to(clone_path))).encode()
                    ).hexdigest()[:16]
                    if assessment["extracted"] and len(assessment["extracted"]) > 50:
                        patterns.append(AbsorbedPattern(
                            source_url=f"https://github.com/{full_name}",
                            source_repo=full_name,
                            pattern_type=assessment["type"],
                            code=assessment["extracted"],
                            description=assessment["description"],
                            value_assessment=assessment["value"],
                            fingerprint=fingerprint,
                        ))
            except Exception:
                continue

        try:
            shutil.rmtree(clone_path)
        except Exception:
            pass

        return patterns


    def _extract_via_api(self, full_name: str, domain: str = "general") -> list[AbsorbedPattern]:
        """Legacy API-based extraction — kept as fallback when git is unavailable."""
        patterns = []

        try:
            result = subprocess.run(
                ["gh", "api", f"repos/{full_name}/git/trees/HEAD?recursive=1",
                 "--jq", ".tree[] | select(.type==\"blob\") | select(.path | endswith(\".py\")) | .path"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return patterns

            py_paths = [p for p in result.stdout.strip().split("\n") if p
                       and "test" not in p.lower() and "example" not in p.lower()
                       and not p.endswith("setup.py")
                       and not self._is_noise_file(p.split("/")[-1], domain)][:20]

            for path in py_paths:
                try:
                    result2 = subprocess.run(
                        ["gh", "api", f"repos/{full_name}/contents/{path}",
                         "--jq", ".content"],
                        capture_output=True, text=True, timeout=15,
                    )
                    if result2.returncode != 0:
                        continue

                    import base64
                    content = base64.b64decode(result2.stdout).decode("utf-8", errors="replace")

                    assessment = self._assess_code(content, path, domain=domain)
                    if assessment["value"] >= 0.4 and assessment["extracted"] and len(assessment["extracted"]) > 50:
                        patterns.append(AbsorbedPattern(
                            source_url=f"https://github.com/{full_name}",
                            source_repo=full_name,
                            pattern_type=assessment["type"],
                            code=assessment["extracted"],
                            description=assessment["description"],
                            value_assessment=assessment["value"],
                            fingerprint=hashlib.sha256(
                                (full_name + path).encode()
                            ).hexdigest()[:16],
                        ))
                except Exception:
                    continue
        except Exception:
            pass

        return patterns

    def _web_fallback(self, query: str, domain: str = "general") -> list[AbsorbedPattern]:
        """When gh CLI not available, mine GitHub via API without auth.

        Uses Repository Search (no auth required) to find repos,
        then Git Trees API (recursive) to find Python files.
        """
        import urllib.request
        import urllib.parse

        patterns = []
        try:
            # Step 1: Search repositories (works without auth)
            search_url = (
                f"https://api.github.com/search/repositories"
                f"?q={urllib.parse.quote(query)}"
                f"&sort=stars&per_page=3"
            )
            req = urllib.request.Request(search_url, headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "MUE"
            })
            self._rate_limit_wait()
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            self._rate_limit_remaining -= 1

            for item in data.get("items", [])[:3]:
                full_name = item.get("full_name", "")
                default_branch = item.get("default_branch", "main")
                if not full_name:
                    continue

                # Step 2: Get recursive git tree to find ALL .py files
                try:
                    tree_url = (
                        f"https://api.github.com/repos/{full_name}"
                        f"/git/trees/{default_branch}?recursive=1"
                    )
                    req2 = urllib.request.Request(tree_url, headers={
                        "Accept": "application/vnd.github.v3+json",
                        "User-Agent": "MUE"
                    })
                    self._rate_limit_wait()
                    with urllib.request.urlopen(req2, timeout=15) as resp2:
                        tree_data = json.loads(resp2.read().decode("utf-8"))
                    self._rate_limit_remaining -= 1

                    py_entries = [
                        e for e in tree_data.get("tree", [])
                        if e.get("path", "").endswith(".py")
                        and e.get("type") == "blob"
                        and e.get("size", 0) < 50000
                        and "test" not in e.get("path", "").lower()
                        and "example" not in e.get("path", "").lower()
                    ]

                    # Pick up to 5 Python files, prefer non-init files
                    py_entries.sort(key=lambda e: (
                        0 if e["path"].endswith("__init__.py") else 1,
                        -(e.get("size", 0))
                    ))
                    selected = py_entries[:5]

                    for entry in selected:
                        path = entry["path"]
                        # Step 3: Download raw file
                        raw_url = (
                            f"https://raw.githubusercontent.com"
                            f"/{full_name}/{default_branch}/{path}"
                        )
                        try:
                            req3 = urllib.request.Request(raw_url, headers={
                                "User-Agent": "MUE"
                            })
                            self._rate_limit_wait()
                            with urllib.request.urlopen(req3, timeout=10) as resp3:
                                code = resp3.read().decode("utf-8", errors="replace")
                            self._rate_limit_remaining -= 1
                        except Exception:
                            continue

                        if len(code) < 100:
                            continue

                        assessment = self._assess_code(code, path, domain=domain)
                        if assessment["value"] >= 0.4 and assessment["extracted"] and len(assessment["extracted"]) > 50:
                            patterns.append(AbsorbedPattern(
                                source_url=f"https://github.com/{full_name}",
                                source_repo=full_name,
                                pattern_type=assessment["type"],
                                code=assessment["extracted"],
                                description=assessment["description"],
                                value_assessment=assessment["value"],
                                fingerprint=hashlib.sha256(
                                    (full_name + path).encode()
                                ).hexdigest()[:16],
                            ))
                except Exception:
                    continue
        except Exception:
            pass

        return patterns

    # ═══════════════════════════════════════════════════════════════
    # Domain-specific keyword sets — see code_assessor.py
    # ═══════════════════════════════════════════════════════════════

    def _is_noise_file(self, filename: str, domain: str = "general") -> bool:
        """Reject files that are clearly irrelevant to the domain."""
        return is_noise_file(filename, domain)

    def _filename_matches_domain(self, filename: str, domain: str = "general") -> bool:
        """Check if filename suggests domain relevance."""
        return filename_matches_domain(filename, domain)

    def _assess_code(self, code: str, filename: str, domain: str = "general") -> dict:
        """Domain-aware code quality assessment."""
        return assess_code(code, filename, domain)

    def _extract_key_pattern(self, code: str) -> str:
        """Extract full function/class bodies using AST."""
        return extract_key_pattern(code)

    def _absorb(self, pattern: AbsorbedPattern):
        """Absorb a pattern — save as atout and register in genome."""
        # Save as atout
        atout_path = self.atouts_dir / f"{pattern.fingerprint}.py"
        header = (
            f"# Atout absorbed from: {pattern.source_url}\n"
            f"# Type: {pattern.pattern_type}\n"
            f"# Value: {pattern.value_assessment:.2f}\n"
            f"# Absorbed: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        )
        atout_path.write_text(header + pattern.code, encoding="utf-8")

        # Register as gene
        atout_name = f"atout_{pattern.fingerprint}"
        self.genome.add_gene(atout_name, pattern.code)
        if atout_name in self.genome.genes:
            self.genome.genes[atout_name].tags = ["atout", pattern.pattern_type, pattern.source_repo]

        # Store in memory L3
        from ..memory.lattice import MemoryEntry
        entry = MemoryEntry(
            layer=3,
            key=f"atout:{pattern.fingerprint}",
            content=f"Atout from {pattern.source_repo}: {pattern.description}\n\n{pattern.code}",
            tags=["atout", pattern.pattern_type, pattern.source_repo],
        )
        self.memory.store(entry)

    def reinforce(self, fingerprint: str, success: bool):
        """Reinforce an atout based on usage outcome."""
        if fingerprint in self.absorbed:
            self.absorbed[fingerprint].usage_count += 1
            if success:
                self.absorbed[fingerprint].success_count += 1
            self._save_manifest()

    def auto_mine_tick(self, cycle_count: int, domain: str = "general") -> list[AbsorbedPattern]:
        """Called by evolution loop — auto-mines every N cycles."""
        self._mining_cycle_count += 1
        if self._mining_cycle_count % self.auto_mine_interval != 0:
            return []
        # Use domain-specific queries if available
        domain_queries = self.REFERENCE_REPOS.get(domain, [])
        query = None
        if domain_queries:
            import random
            query = random.choice(domain_queries)
        return self.mine(query)

    def local_absorb(self, domain: str = "trading") -> list[AbsorbedPattern]:
        """Scan project directories for domain-relevant code patterns to absorb."""
        patterns = []
        scan_dirs = []
        if self.project_root and self.project_root.exists():
            actual_root = self.project_root.parent if self.project_root.name in ("mue", "mue-x") else self.project_root
            scan_dirs = [
                actual_root / "TradingAgents",
                actual_root / "Vibe-Trading-HKUDS",
                actual_root / "Claude-Trading-Skills",
                actual_root / "Trading_Skills",
                actual_root / "autonomous_trader",
                actual_root / "ai-trader-platform",
                actual_root / "kronos",
                actual_root / "Athena",
            ]

        for scan_dir in scan_dirs:
            if not scan_dir.exists():
                continue
            for py_file in list(scan_dir.rglob("*.py"))[:20]:
                try:
                    code = py_file.read_text(encoding="utf-8")
                    if len(code) < 100 or len(code) > 50000:
                        continue
                    if self._is_noise_file(py_file.name, domain):
                        continue
                    assessment = self._assess_code(code, py_file.name, domain=domain)
                    if assessment["value"] >= 0.4 and assessment["extracted"] and len(assessment["extracted"]) > 50:
                        fingerprint = hashlib.sha256(
                            (str(py_file)).encode()
                        ).hexdigest()[:16]
                        if fingerprint not in self.absorbed:
                            patterns.append(AbsorbedPattern(
                                source_url=str(py_file),
                                source_repo=scan_dir.name,
                                pattern_type=assessment["type"],
                                code=assessment["extracted"],
                                description=f"Local absorption from {py_file.relative_to(self.project_root)}",
                                value_assessment=assessment["value"],
                                fingerprint=fingerprint,
                            ))
                except Exception:
                    continue

        newly_absorbed = []
        for pattern in patterns[:5]:  # Max 5 per local absorption
            if pattern.fingerprint not in self.absorbed:
                self._absorb(pattern)
                self.absorbed[pattern.fingerprint] = pattern
                newly_absorbed.append(pattern)

        self._save_manifest()
        return newly_absorbed

    def list_atouts(self) -> list[dict]:
        """List all absorbed atouts with stats."""
        return [
            {
                "fingerprint": fp,
                "source": p.source_repo,
                "type": p.pattern_type,
                "description": p.description,
                "value": p.value_assessment,
                "usage": p.usage_count,
                "success_rate": p.success_rate,
            }
            for fp, p in sorted(
                self.absorbed.items(),
                key=lambda x: -x[1].value_assessment
            )
        ]

    @property
    def stats(self) -> dict:
        return {
            "total_atouts": len(self.absorbed),
            "avg_value": sum(p.value_assessment for p in self.absorbed.values()) / max(len(self.absorbed), 1),
            "most_used": max(
                self.absorbed.values(),
                key=lambda p: p.usage_count,
                default=None
            ).source_repo if self.absorbed else None,
            "recent_absorptions": [
                p.source_repo for p in sorted(
                    self.absorbed.values(),
                    key=lambda p: -p.absorbed_at
                )[:5]
            ],
        }
