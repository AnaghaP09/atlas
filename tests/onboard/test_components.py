"""Component maps for any language: folders, owner teams and static import edges."""

from __future__ import annotations

from atlas.components import (Layout, build_component_map, codeowner_teams, js_imports, python_imports, resolve_js,
                              resolve_python, source_paths)


def _entries(paths):
    return [{"path": p, "type": "blob", "size": 100} for p in paths]


def test_layout_opens_containers_and_single_packages():
    files = ["src/pkg/__init__.py", "src/pkg/core.py", "src/pkg/web/__init__.py", "src/pkg/web/views.py",
             "tests/test_core.py", "docs/conf.py", "lib/net/a.js", "lib/util/b.js", "lib/index.js", "setup.py",
             ".github/workflows/ci.yml", "node_modules/x/index.js"]
    layout = Layout(files)
    assert layout.component_of("src/pkg/core.py") == "src/pkg"
    assert layout.component_of("src/pkg/web/views.py") == "src/pkg/web"
    assert layout.component_of("lib/net/a.js") == "lib/net"
    assert layout.component_of("lib/index.js") == "lib"
    assert layout.component_of("tests/test_core.py") == "tests"
    assert layout.component_of("setup.py") == "root"
    assert not any(r.startswith((".github", "node_modules")) for r in layout.roots)


def test_python_imports_absolute_relative_and_from():
    src = "import os\nimport pkg.web as w\nfrom . import core\nfrom .web import views\nfrom ..other import thing\n"
    mods = python_imports(src, "src/pkg/sub/mod.py")
    assert "os" in mods and "pkg.web" in mods
    assert "./src.pkg.sub.core" in mods          # "from . import core" inside src/pkg/sub
    assert "./src.pkg.sub.web.views" in mods
    assert "./src.pkg.other.thing" in mods
    files = {"src/pkg/__init__.py", "src/pkg/web/__init__.py", "src/pkg/web/views.py", "src/pkg/other.py"}
    assert resolve_python("pkg.web", "tests/t.py", files, ["", "src"]) == "src/pkg/web/__init__.py"
    assert resolve_python("pkg.web.views.render", "tests/t.py", files, ["", "src"]) == "src/pkg/web/views.py"
    assert resolve_python("./src.pkg.other.thing", "src/pkg/sub/mod.py", files, ["", "src"]) == "src/pkg/other.py"
    assert resolve_python("requests", "tests/t.py", files, ["", "src"]) is None


def test_python_parser_never_runs_code_and_survives_bad_files():
    assert python_imports("def broken(:\n", "a.py") == []
    # Code that would have an effect if run is only parsed.
    assert python_imports("import os\nos.remove('x')\n__import__('shutil')\n", "a.py") == ["os"]


def test_js_imports_all_forms_and_comments():
    src = """
    import a from './a.js';
    import { b,
             c } from "../lib/b";
    import './side-effect';
    export * from './re-export';
    const d = require('./d');
    const e = await import('./e.mjs');
    import type { T } from './types';
    // import x from './commented-out';
    /* const y = require('./block-commented'); */
    const url = 'http://example.com/x'; import z from 'outside-package';
    """
    specs = js_imports(src)
    for want in ("./a.js", "../lib/b", "./side-effect", "./re-export", "./d", "./e.mjs", "./types", "outside-package"):
        assert want in specs
    assert "./commented-out" not in specs and "./block-commented" not in specs
    files = {"lib/core/a.ts", "lib/b.js", "lib/core/d/index.js"}
    assert resolve_js("./a.js", "lib/core/x.js", files) == "lib/core/a.ts"  # compiled name points at the TS source
    assert resolve_js("../b", "lib/core/x.js", files) == "lib/b.js"
    assert resolve_js("./d", "lib/core/x.js", files) == "lib/core/d/index.js"
    assert resolve_js("outside-package", "lib/core/x.js", files) is None
    assert resolve_js("../../../escape", "lib/x.js", files) is None


def test_component_map_edges_owner_teams_and_unknown_languages():
    files = ["src/pkg/__init__.py", "src/pkg/core.py", "src/pkg/web/__init__.py", "src/pkg/web/views.py",
             "tests/test_web.py", "native/fast.rs", "lib/net/a.js", "lib/util/b.js", "pkg/x/x.go", "pkg/y/y.go", "go.mod"]
    texts = {"src/pkg/web/views.py": "from ..core import helper\n", "tests/test_web.py": "import pkg.web\n",
             "lib/net/a.js": "const b = require('../util/b')\n",
             "pkg/x/x.go": 'package x\nimport (\n  "example.org/mod/pkg/y"\n  "fmt"\n)\n'}
    owners = {".github/CODEOWNERS": "/src/pkg/web/ @example/web-team\n/lib/ @someone\n",
              "pkg/x/OWNERS": "labels:\n- area/example\n"}
    graph, summary = build_component_map("abc123", _entries(files), texts, go_mod_text="module example.org/mod\n",
                                         owners_texts=owners)
    edges = {(s, d, k) for (s, d, k) in graph.edges}
    assert ("src/pkg/web", "src/pkg", "depends_on") in edges
    assert ("tests", "src/pkg/web", "test_depends_on") in edges
    assert ("lib/net", "lib/util", "depends_on") in edges
    assert ("pkg/x", "pkg/y", "depends_on") in edges
    assert graph.components["src/pkg/web"]["owner_team"] == "example/web-team"
    assert graph.components["lib/net"]["owner_team"] is None  # a personal account is never an owner team
    assert graph.components["pkg/x"]["owner_team"] == "area/example"
    assert graph.components["native"]["edges_from"] == "folders only"
    assert "Rust" in summary["other_files"] and "folders only" in summary["note"]


def test_codeowners_keeps_teams_only():
    text = "# comment\n*.js @someone\n/docs/ @example/docs @someone\n/api/ someone@example.com\n"
    assert codeowner_teams(text) == [("/docs/", "example/docs")]


def test_source_paths_prefers_code_over_tests_and_respects_the_limit():
    files = ["tests/test_a.py", "src/a.py", "src/b.ts", "src/types.d.ts", "README.md", "dist/x.js"]
    wanted, left = source_paths(_entries(files), 2)
    assert wanted == ["src/a.py", "src/b.ts"] and left == 1
