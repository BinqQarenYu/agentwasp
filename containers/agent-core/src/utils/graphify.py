import os
import ast
import json
from pathlib import Path

def resolve_import_path(module_name: str, target_path: Path) -> str:
    """
    Attempts to resolve an absolute dot-separated import name (e.g. 'src.utils.graphify')
    to an existing file in the target directory (e.g. 'src/utils/graphify.py').
    If not found locally, returns the original module name.
    """
    if not module_name:
        return ""
        
    parts = module_name.split(".")
    
    # Try as file: e.g. parts = ['src', 'utils', 'graphify'] -> src/utils/graphify.py
    candidate_file = target_path / (os.path.join(*parts) + ".py")
    if candidate_file.exists():
        try:
            return candidate_file.relative_to(target_path).as_posix()
        except ValueError:
            return candidate_file.as_posix()
            
    # Try as package init: e.g. src/utils/graphify/__init__.py
    candidate_dir = target_path / os.path.join(*parts) / "__init__.py"
    if candidate_dir.exists():
        try:
            return (candidate_dir.parent / "__init__.py").relative_to(target_path).as_posix()
        except ValueError:
            return (candidate_dir.parent / "__init__.py").as_posix()
            
    return module_name

def extract_ast_graph(target_dir: str):
    target_path = Path(target_dir).resolve()
    
    nodes = []
    edges = []
    
    # 1. Walk directory and parse Python files
    for root, _, files in os.walk(target_path):
        # Skip output files and virtualenvs to keep visual map clean and high performance
        if "graphify-out" in root or ".git" in root or ".venv" in root or "node_modules" in root:
            continue
            
        for file in files:
            if not file.endswith(".py"):
                continue
            
            filepath = Path(root) / file
            try:
                rel_path = filepath.relative_to(target_path).as_posix()
            except ValueError:
                rel_path = filepath.as_posix()
            
            # Append file node
            nodes.append({
                "id": rel_path,
                "type": "file",
                "name": file,
                "path": rel_path
            })
            
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                tree = ast.parse(content, filename=str(filepath))
            except Exception:
                continue
                
            # Class definition scope tracker to avoid duplicate names and link methods to classes correctly
            class ASTVisitor(ast.NodeVisitor):
                def __init__(self, file_rel_path: str):
                    self.rel_path = file_rel_path
                    self.scope_stack = [file_rel_path] # Stack of parent node IDs
                    
                def visit_ClassDef(self, node):
                    class_id = f"{self.rel_path}:{node.name}"
                    parent_id = self.scope_stack[-1]
                    
                    nodes.append({
                        "id": class_id,
                        "type": "class",
                        "name": node.name,
                        "file": self.rel_path,
                        "line": getattr(node, "lineno", 0)
                    })
                    
                    # class is contained in the parent scope (file or nested class)
                    edges.append({"source": parent_id, "target": class_id, "type": "contains"})
                    
                    self.scope_stack.append(class_id)
                    self.generic_visit(node)
                    self.scope_stack.pop()
                    
                def visit_FunctionDef(self, node):
                    self.visit_Func(node)
                    
                def visit_AsyncFunctionDef(self, node):
                    self.visit_Func(node)
                    
                def visit_Func(self, node):
                    parent_id = self.scope_stack[-1]
                    
                    # Qualify method ID by parent scope class if nested
                    if parent_id != self.rel_path:
                        func_id = f"{parent_id}.{node.name}"
                    else:
                        func_id = f"{self.rel_path}:{node.name}"
                        
                    nodes.append({
                        "id": func_id,
                        "type": "function",
                        "name": node.name,
                        "file": self.rel_path,
                        "line": getattr(node, "lineno", 0)
                    })
                    
                    # Function is contained in the parent scope (file or class)
                    edges.append({"source": parent_id, "target": func_id, "type": "contains"})
                    
                    self.scope_stack.append(func_id)
                    self.generic_visit(node)
                    self.scope_stack.pop()
                    
                def visit_Import(self, node):
                    for alias in node.names:
                        resolved = resolve_import_path(alias.name, target_path)
                        edges.append({"source": self.rel_path, "target": resolved, "type": "imports"})
                        
                def visit_ImportFrom(self, node):
                    target_module = node.module or ""
                    
                    if node.level > 0:
                        # Handle relative imports recursively going up directory levels
                        current_dir = (target_path / self.rel_path).parent
                        for _ in range(node.level - 1):
                            current_dir = current_dir.parent
                            
                        parts = target_module.split(".") if target_module else []
                        candidate_path = current_dir / os.path.join(*parts) if parts else current_dir
                        
                        candidate_file = candidate_path.with_suffix(".py")
                        if candidate_file.exists():
                            try:
                                resolved = candidate_file.relative_to(target_path).as_posix()
                            except ValueError:
                                resolved = candidate_file.as_posix()
                        else:
                            candidate_init = candidate_path / "__init__.py"
                            if candidate_init.exists():
                                try:
                                    resolved = candidate_init.relative_to(target_path).as_posix()
                                except ValueError:
                                    resolved = candidate_init.as_posix()
                            else:
                                resolved = target_module
                    else:
                        # Absolute import
                        resolved = resolve_import_path(target_module, target_path)
                        
                    if resolved:
                        edges.append({"source": self.rel_path, "target": resolved, "type": "imports_from"})
            
            # Start visiting AST tree nodes
            visitor = ASTVisitor(rel_path)
            visitor.visit(tree)

    out_dir = target_path / "graphify-out"
    out_dir.mkdir(exist_ok=True, parents=True)
    
    graph_data = {
        "metadata": {
            "root": str(target_path),
            "node_count": len(nodes),
            "edge_count": len(edges)
        },
        "nodes": nodes,
        "edges": edges
    }
    
    graph_file = out_dir / "graph.json"
    with open(graph_file, "w", encoding="utf-8") as f:
        json.dump(graph_data, f, indent=2)
        
    report = f"# Graphify AST Report\n\n- **Target:** {target_path}\n- **Nodes:** {len(nodes)}\n- **Edges:** {len(edges)}\n"
    with open(out_dir / "GRAPH_REPORT.md", "w", encoding="utf-8") as f:
        f.write(report)
        
    return graph_data

def query_ast_graph(target_dir: str, query: str):
    target_path = Path(target_dir).resolve()
    graph_file = target_path / "graphify-out" / "graph.json"
    
    if not graph_file.exists():
        return "Error: Graph not found. Run extract mode first."
        
    with open(graph_file, "r", encoding="utf-8") as f:
        data = json.load(f)
        
    results = []
    q = query.lower()
    for node in data.get("nodes", []):
        if q in node.get("name", "").lower() or q in node.get("id", "").lower():
            results.append(node)
            
    if not results:
        return f"No nodes found matching '{query}'"
        
    return json.dumps(results, indent=2)
