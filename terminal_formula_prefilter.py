"""Conservative last-bar rejection for expensive full historical formula scans."""
import ast
from functools import lru_cache
from terminal_formula import compile_formula, ALIASES, BINARY, COMPARE


@lru_cache(maxsize=64)
def _trees(formula):
    statements = compile_formula(formula)
    return dict((name, tree) for name, tree in statements if name), statements[-1][1]


def might_match(formula, bars):
    definitions, output = _trees(formula)
    def value(node, index):
        if index < 0:
            return None
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            name = node.id.upper()
            if name in ALIASES:
                return bars[index].get(ALIASES[name])
            return value(definitions[name], index) if name in definitions else None
        if isinstance(node, ast.BoolOp):
            values = [value(item, index) for item in node.values]
            if isinstance(node.op, ast.And):
                if any(item is not None and not bool(item) for item in values):
                    return False
                return True if all(item is not None for item in values) else None
            if any(item is not None and bool(item) for item in values):
                return True
            return False if all(item is not None for item in values) else None
        if isinstance(node, ast.BinOp):
            left, right = value(node.left, index), value(node.right, index)
            if left is None or right is None:
                return None
            try:
                return BINARY[type(node.op)](left, right)
            except (ArithmeticError, TypeError):
                return None
        if isinstance(node, ast.UnaryOp):
            operand = value(node.operand, index)
            if operand is None:
                return None
            if isinstance(node.op, ast.Not):
                return not operand
            return -operand if isinstance(node.op, ast.USub) else operand
        if isinstance(node, ast.Compare):
            left = value(node.left, index)
            for op, right_node in zip(node.ops, node.comparators):
                right = value(right_node, index)
                if left is None or right is None:
                    return None
                if not COMPARE[type(op)](left, right):
                    return False
                left = right
            return True
        if isinstance(node, ast.Call):
            name = node.func.id.upper()
            if name == 'REF' and isinstance(node.args[1], ast.Constant):
                n = node.args[1].value
                if type(n) is int and 1 <= n <= 250:
                    return value(node.args[0], index-n)
            # All rolling functions remain unknown. The full interpreter decides.
            return None
        return None
    result = value(output, len(bars)-1)
    return result is None or bool(result)
