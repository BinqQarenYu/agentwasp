# -*- coding: utf-8 -*-
"""T-Zero Deterministic AST Token Compression Engine for Agent WASP.

Ported from T-Zero Algorithm (https://github.com/BinqQarenYu/TZeroAlgorithm).
Acts as pre-flight middleware on outgoing chat prompts and multi-turn messages
to slash prompt tokens (50% to 95%) and protect free-tier rate limits (TPM/RPM).
"""

from __future__ import annotations

import ast
import re
from typing import List, Tuple

import structlog

from .types import Message

logger = structlog.get_logger()


class TZeroCompressor:
    """Deterministic AST Token Compressor for outgoing LLM prompts."""

    # Matches standard markdown code blocks with language identifiers
    CODE_BLOCK_RE = re.compile(r"```([a-zA-Z0-9_\-\+]*)\r?\n(.*?)```", re.DOTALL)

    def __init__(self, line_threshold: int = 12, enabled: bool = True):
        self.line_threshold = line_threshold
        self.enabled = enabled

    def compress_messages(self, messages: List[Message]) -> Tuple[List[Message], int]:
        """Scans and compresses code blocks in message history.
        
        Returns:
            Tuple of (compressed_messages, tokens_saved_estimate).
        """
        try:
            from ..config import settings
            if not getattr(settings, "tzero_compression_enabled", True):
                return messages, 0
            self.line_threshold = getattr(settings, "tzero_line_threshold", self.line_threshold)
        except Exception:
            pass

        if not self.enabled or not messages:
            return messages, 0

        total_saved_chars = 0
        new_messages: List[Message] = []

        for msg in messages:
            content = msg.content
            if not isinstance(content, str) or "```" not in content:
                new_messages.append(msg)
                continue

            compressed_content, saved_chars = self.compress_text(content)
            total_saved_chars += saved_chars
            new_messages.append(Message(role=msg.role, content=compressed_content, meta=dict(msg.meta)))

        tokens_saved = max(0, total_saved_chars // 4)
        return new_messages, tokens_saved

    def compress_text(self, text: str) -> Tuple[str, int]:
        """Compresses code blocks within arbitrary text.
        
        Returns:
            Tuple of (compressed_text, saved_characters).
        """
        if not text or "```" not in text:
            return text, 0

        saved_chars = 0

        def _replace_block(match: re.Match) -> str:
            nonlocal saved_chars
            lang = (match.group(1) or "python").lower().strip()
            code = match.group(2)
            lines = [ln for ln in code.strip().splitlines() if ln.strip()]

            # Only compress blocks meeting the threshold
            if len(lines) >= self.line_threshold:
                try:
                    reduced = self.reduce_code(code, lang=lang)
                    if reduced and len(reduced) < len(code):
                        delta = len(code) - len(reduced)
                        saved_chars += delta
                        return f"```{lang}\n{reduced}\n```"
                except Exception:
                    pass
            return match.group(0)

        compressed_text = self.CODE_BLOCK_RE.sub(_replace_block, text)
        return compressed_text, saved_chars

    def reduce_code(self, code: str, lang: str = "python") -> str:
        """Applies deterministic AST signature pruning based on language."""
        clean_lang = (lang or "python").lower().strip()

        # 1. Python AST Reduction
        if clean_lang in ("python", "py"):
            return self._reduce_python(code)

        # 2. TypeScript / JavaScript / Web
        if clean_lang in ("javascript", "js", "typescript", "ts", "jsx", "tsx"):
            return self._reduce_c_family(code, lang="ts")

        # 3. Rust, Go, C/C++, Java, C#, Solidity
        if clean_lang in ("rust", "rs", "go", "golang", "c", "cpp", "cxx", "java", "cs", "solidity", "sol"):
            return self._reduce_c_family(code, lang="c_family")

        # 4. Git diffs / Patches
        if clean_lang in ("diff", "patch"):
            return self._reduce_diff(code)

        return code

    def _reduce_python(self, code: str) -> str:
        """Prunes Python function/method bodies while preserving class & function contracts."""
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # Check for existing docstring
                    doc_node = None
                    if (
                        node.body
                        and isinstance(node.body[0], ast.Expr)
                        and isinstance(node.body[0].value, ast.Constant)
                        and isinstance(node.body[0].value.value, str)
                    ):
                        doc_node = node.body[0]

                    ellipsis_node = ast.Expr(value=ast.Constant(value=...))
                    if doc_node:
                        node.body = [doc_node, ellipsis_node]
                    else:
                        node.body = [ellipsis_node]

            unparsed = ast.unparse(tree)
            return unparsed
        except Exception:
            # Fallback to lexical skeleton if partial syntax
            return code

    def _reduce_c_family(self, code: str, lang: str = "ts") -> str:
        """Prunes function bodies in brace-delimited languages down to signatures."""
        lines = code.splitlines()
        reduced_lines: List[str] = []
        brace_depth = 0
        in_pruned_body = False
        body_start_depth = 0

        # Pattern for function / method signatures
        fn_pattern = re.compile(
            r"^\s*(export\s+)?(async\s+)?(public\s+|private\s+|protected\s+|static\s+)*"
            r"(function\s+\w+|\w+\s*\([^)]*\)\s*(:\s*[^={]+)?|fn\s+\w+|func\s+(\([^)]*\)\s*)?\w+)"
        )

        for line in lines:
            stripped = line.strip()
            open_braces = line.count("{") - line.count("\\{")
            close_braces = line.count("}") - line.count("\\}")

            # Check if this line begins a function/method signature
            if not in_pruned_body and fn_pattern.search(line) and "{" in line:
                indent = line[: len(line) - len(line.lstrip())]
                header = line[: line.rfind("{") + 1]
                reduced_lines.append(f"{header} ... }}")
                brace_depth += (open_braces - close_braces)
                if open_braces > close_braces:
                    in_pruned_body = True
                    body_start_depth = brace_depth - (open_braces - close_braces)
                continue

            if in_pruned_body:
                brace_depth += (open_braces - close_braces)
                if brace_depth <= body_start_depth:
                    in_pruned_body = False
                continue

            reduced_lines.append(line)
            brace_depth += (open_braces - close_braces)

        result = "\n".join(reduced_lines)
        return result if len(result) < len(code) else code

    def _reduce_diff(self, code: str) -> str:
        """Condenses git diffs by retaining hunk headers and changes while pruning large unmodified spans."""
        lines = code.splitlines()
        output: List[str] = []
        context_count = 0

        for line in lines:
            if line.startswith(("+", "-", "@@", "diff ", "index ", "---", "+++")):
                context_count = 0
                output.append(line)
            else:
                context_count += 1
                if context_count <= 2:
                    output.append(line)
                elif context_count == 3:
                    output.append(" ... [unmodified context pruned]")

        return "\n".join(output)


# Singleton instance for system-wide reuse
tzero_compressor = TZeroCompressor()
