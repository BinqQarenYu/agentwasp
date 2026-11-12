import os
import subprocess
from pathlib import Path

import structlog

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

logger = structlog.get_logger()

class GraphifySkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="graphify",
            description=(
                "Parse a raw repository or directory using Graphify to build a Knowledge Graph. "
                "This creates a compressed, highly-efficient map of the codebase (AST + Semantic). "
                "Use this when you need to deeply understand a large, unfamiliar repository without "
                "reading every file. It outputs a GRAPH_REPORT.md and a wiki/ folder."
            ),
            params=[
                SkillParam(
                    name="target_dir", 
                    param_type=ParamType.STRING, 
                    description="The absolute path to the directory or repository you want to graphify."
                ),
                SkillParam(
                    name="mode", 
                    param_type=ParamType.STRING, 
                    description="Set to 'wiki' to generate agent-crawlable markdown files, or 'watch' for auto-sync.",
                    required=False,
                    default="wiki"
                ),
            ],
            category="code_analysis",
            timeout_seconds=300.0, # Building a graph can take time
        )

    async def execute(self, target_dir: str, mode: str = "wiki", query_text: str = "", **kwargs) -> SkillResult:
        from src.utils.graphify import extract_ast_graph, query_ast_graph
        try:
            target_path = Path(target_dir).resolve()
            if not target_path.exists() or not target_path.is_dir():
                return SkillResult(
                    skill_name="graphify", 
                    success=False, 
                    output="", 
                    error=f"Directory not found: {target_dir}"
                )

            logger.info("graphify.starting", target=str(target_path), mode=mode)
            
            if mode == "query":
                if not query_text:
                    return SkillResult(skill_name="graphify", success=False, output="", error="query_text is required for query mode.")
                
                # Execute native query
                results = query_ast_graph(str(target_path), query_text)
                return SkillResult(skill_name="graphify", success=True, output=f"Graph Query Results:\n{results}")

            else:
                # Execute native extraction
                graph_data = extract_ast_graph(str(target_path))
                nodes_count = graph_data["metadata"]["node_count"]
                edges_count = graph_data["metadata"]["edge_count"]
                
                result_msg = (
                    f"Graphify complete.\n"
                    f"Nodes: {nodes_count}\n"
                    f"Edges: {edges_count}\n"
                    f"Graph saved to {target_path / 'graphify-out' / 'graph.json'}"
                )
                
                return SkillResult(
                    skill_name="graphify", 
                    success=True, 
                    output=result_msg
                )

        except Exception as e:
            logger.exception("graphify.error")
            return SkillResult(
                skill_name="graphify", 
                success=False, 
                output="", 
                error=str(e)
            )
