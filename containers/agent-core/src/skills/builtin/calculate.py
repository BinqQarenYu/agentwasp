import ast
import math
import operator

from ..base import SkillBase
from ..types import ParamType, SkillDefinition, SkillParam, SkillResult

SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

SAFE_FUNCS = {
    "abs": abs, "round": round, "min": min, "max": max,
    "sqrt": math.sqrt, "log": math.log, "log10": math.log10,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "pi": math.pi, "e": math.e,
}


def safe_eval(expr: str):
    tree = ast.parse(expr, mode="eval")

    def _eval(node):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        elif isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError(f"Unsupported constant: {node.value}")
        elif isinstance(node, ast.BinOp):
            op = SAFE_OPS.get(type(node.op))
            if not op:
                raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
            left = _eval(node.left)
            right = _eval(node.right)
            if isinstance(node.op, ast.Pow) and right > 1000:
                raise ValueError("Exponent too large (max 1000)")
            return op(left, right)
        elif isinstance(node, ast.UnaryOp):
            op = SAFE_OPS.get(type(node.op))
            if not op:
                raise ValueError("Unsupported unary operator")
            return op(_eval(node.operand))
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in SAFE_FUNCS:
                func = SAFE_FUNCS[node.func.id]
                if callable(func):
                    args = [_eval(a) for a in node.args]
                    return func(*args)
                return func
            raise ValueError(f"Unsupported function")
        elif isinstance(node, ast.Name):
            if node.id in SAFE_FUNCS:
                val = SAFE_FUNCS[node.id]
                if not callable(val):
                    return val
            raise ValueError(f"Unsupported name: {node.id}")
        raise ValueError(f"Unsupported expression")

    return _eval(tree)


class CalculateSkill(SkillBase):
    def definition(self) -> SkillDefinition:
        return SkillDefinition(
            name="calculate",
            description="Evaluate a math expression. Supports +, -, *, /, **, sqrt, log, sin, cos, tan, pi, e.",
            params=[
                SkillParam(name="expression", param_type=ParamType.STRING, description="Math expression"),
            ],
            category="utility",
            timeout_seconds=5.0,
        )

    async def execute(self, expression: str, **kwargs) -> SkillResult:
        try:
            result = safe_eval(expression)
            return SkillResult(
                skill_name="calculate",
                success=True,
                output=f"{expression} = {result}",
            )
        except Exception as e:
            return SkillResult(skill_name="calculate", success=False, output="", error=f"Calculation error: {e}")
