"""
不依赖 ruff 的基本静态检查，防止曾经出现过的问题回归：
- 类中重复定义同名方法（后者静默覆盖前者，曾导致 Pixiv 标签搜索失效）
- 裸 except:（会吞掉 KeyboardInterrupt/SystemExit）
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted(p for d in ("utils", "webapp") for p in (ROOT / d).rglob("*.py"))


def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_no_duplicate_method_definitions():
    problems = []
    for path in SOURCES:
        for node in ast.walk(_parse(path)):
            if not isinstance(node, ast.ClassDef):
                continue
            seen = {}
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # @property / @x.setter 等同名定义是正常用法
                    if item.decorator_list:
                        continue
                    if item.name in seen:
                        problems.append(
                            f"{path.relative_to(ROOT)}:{item.lineno} {node.name}.{item.name} "
                            f"重复定义（首次在第 {seen[item.name]} 行）"
                        )
                    seen[item.name] = item.lineno
    assert not problems, "\n".join(problems)


def test_no_bare_except():
    problems = []
    for path in SOURCES:
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                problems.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    assert not problems, "裸 except: " + ", ".join(problems)


def test_ruff_clean():
    import shutil
    import subprocess
    import sys
    import pytest

    probe = subprocess.run([sys.executable, "-m", "ruff", "--version"], capture_output=True, text=True)
    if probe.returncode == 0:
        ruff = [sys.executable, "-m", "ruff"]
    elif shutil.which("ruff"):
        ruff = [shutil.which("ruff")]
    else:
        pytest.skip("ruff 未安装")
    cmd = [*ruff, "check", str(ROOT), "--output-format", "concise"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
