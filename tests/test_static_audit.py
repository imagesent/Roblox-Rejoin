"""Static check: no self.<name> is used that nothing defines.

Qt swallows exceptions raised inside a signal handler or a QThread, so a
method left behind by a rename shows up only as "nothing happens" (or as a
log line, once the engine's crash guard is in place). This test catches that
class of bug at test time instead.

It also guards the two things the engine's startup path touches, because a
single missing helper there stops every launch.
"""
import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def class_members(cls: ast.ClassDef) -> set:
    """Every name a class defines: methods and instance attributes."""
    names = set()
    for node in ast.walk(cls):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if (isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"):
                    names.add(target.attr)
                elif isinstance(target, ast.Name):
                    names.add(target.id)
        if isinstance(node, ast.AnnAssign):
            target = node.target
            if (isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"):
                names.add(target.attr)
            elif isinstance(target, ast.Name):
                names.add(target.id)
        if isinstance(node, ast.For):
            target = node.target
            if (isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"):
                names.add(target.attr)
    return names


def missing_attrs(path: Path) -> list:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    module_level = {n.name for n in tree.body
                    if isinstance(n, ast.FunctionDef)}
    classes = {c.name: c for c in ast.walk(tree)
               if isinstance(c, ast.ClassDef)}
    out = []
    for cname, cls in classes.items():
        known = class_members(cls) | module_level
        for base in cls.bases:
            if isinstance(base, ast.Name) and base.id in classes:
                known |= class_members(classes[base.id])
        for node in ast.walk(cls):
            if not (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "self"):
                continue
            name = node.attr
            # Qt's own API is camelCase or public; this project's helpers are
            # all underscore-prefixed, so only those are checked.
            if name in known or not name.startswith("_"):
                continue
            out.append(f"{path.name}:{node.lineno} {cname}.self.{name}")
    return out


class StaticAuditTests(unittest.TestCase):
    def test_no_missing_self_attributes(self):
        problems = []
        for path in sorted(SRC.glob("*.py")):
            problems += missing_attrs(path)
        self.assertEqual(
            problems, [],
            "self-attribute used but never defined:\n  "
            + "\n  ".join(problems))

    def test_no_class_defines_a_method_twice(self):
        """A duplicate silently shadows the newer body, so the patch appears
        to do nothing at all."""
        problems = []
        for path in sorted(SRC.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for cls in [n for n in ast.walk(tree)
                        if isinstance(n, ast.ClassDef)]:
                seen = {}
                for node in cls.body:
                    if not isinstance(node, (ast.FunctionDef,
                                             ast.AsyncFunctionDef)):
                        continue
                    if node.name in seen:
                        problems.append(
                            f"{path.name}:{node.lineno} {cls.name}.{node.name}"
                            f" redefined (first at {seen[node.name]})")
                    seen[node.name] = node.lineno
        self.assertEqual(problems, [], "duplicate method definitions:\n  "
                         + "\n  ".join(problems))

    def test_engine_startup_helpers_exist(self):
        """The exact names the startup path calls, one by one."""
        import sys
        sys.path.insert(0, str(SRC))
        import multi_account_monitor_engine as engine
        for name in ("_run_inner", "_join_target_summary", "_resolve_join",
                     "_apply_target", "_retry_join", "_launch_one",
                     "_server_reference", "_note_server", "_watch_running",
                     "_check_collisions", "_emit_state", "_begin_leave",
                     "_remove_from_game", "_handle_launch_failure",
                     "_launcher_loop", "_enqueue", "_status_text", "_notify",
                     "_load_icon"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(engine.MonitorEngine, name),
                                f"MonitorEngine.{name} is missing")


if __name__ == "__main__":
    unittest.main()
