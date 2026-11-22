import pytest

from unified_agent.audit.engine import run_audit
from unified_agent.audit.rules import check_py_ast, check_secrets


def test_check_py_ast_eval_exec():
    # Test eval and exec detection
    code = """
import openai
def run_stuff(user_input):
    eval(user_input)
    exec("print('hello')")
"""
    findings = list(check_py_ast("test_file.py", code))
    assert len(findings) == 2

    assert findings[0].rule == "LLM-EVAL"
    assert findings[0].line == 4
    assert "eval()" in findings[0].message

    assert findings[1].rule == "LLM-EVAL"
    assert findings[1].line == 5
    assert "exec()" in findings[1].message


def test_check_py_ast_shell_execution():
    # Test os.system, os.popen and subprocess(shell=True)
    code = """
import openai
import os
import subprocess

def run_cmds():
    os.system("ls -la")
    os.popen("whoami")
    subprocess.run("echo hello", shell=True)
    # Safe call should NOT be flagged
    subprocess.run(["echo", "hello"], shell=False)
"""
    findings = list(check_py_ast("test_file.py", code))
    assert len(findings) == 3

    rules = [f.rule for f in findings]
    assert all(r == "LLM-SHELL-TRUE" for r in rules)

    lines = [f.line for f in findings]
    assert lines == [7, 8, 9]  # os.system, os.popen, subprocess(shell=True)


@pytest.mark.parametrize(
    "module, call",
    [
        ("os", 'os.system("echo hello")'),
        ("os", 'os.popen("echo hello")'),
        ("subprocess", 'subprocess.run("echo hello", shell=True)'),
        ("subprocess", 'subprocess.Popen("echo hello", shell=True)'),
        ("subprocess", 'subprocess.call("echo hello", shell=True)'),
        ("subprocess", 'subprocess.check_call("echo hello", shell=True)'),
        ("subprocess", 'subprocess.check_output("echo hello", shell=True)'),
    ],
)
def test_check_py_ast_local_import_cannot_hide_other_function_shell_call(module, call):
    code = (
        f"import openai\nimport {module}\n"
        "def unrelated():\n"
        f"    from some_module import {module}\n"
        f"    return {module}\n"
        "def run_cmd():\n"
        f"    {call}\n"
    )
    findings = list(check_py_ast("app.py", code))
    assert [(f.rule, f.line) for f in findings] == [("LLM-SHELL-TRUE", 7)]


@pytest.mark.parametrize(
    "binding, call",
    [
        ("from builtins import eval as render", "render('ok')"),
        ("from builtins import exec as render", "render('ok')"),
        ("from os import system as render", "render('ok')"),
        ("from os import popen as render", "render('ok')"),
        ("from subprocess import run as render", "render('ok', shell=True)"),
        ("import subprocess as render", "render.run('ok', shell=True)"),
        ("import os as render", "render.system('ok')"),
    ],
)
def test_check_py_ast_import_alias_does_not_escape_function(binding, call):
    code = (
        "import openai\n"
        "def f():\n"
        f"    {binding}\n"
        "def g():\n"
        "    render = lambda value, **kwargs: value\n"
        f"    {call}\n"
    )
    assert list(check_py_ast("app.py", code)) == []


@pytest.mark.parametrize(
    "body",
    [
        "def g(render):\n    render('ok')\n",
        "def g():\n    render = lambda value: value\n    render('ok')\n",
        "def g():\n    def render(value):\n        return value\n    render('ok')\n",
        "g = lambda render: render('ok')\n",
        "g = [render('ok') for render in callbacks]\n",
    ],
)
def test_check_py_ast_local_binding_shadows_outer_alias(body):
    code = "import openai\nfrom builtins import eval as render\n" + body
    assert list(check_py_ast("app.py", code)) == []


@pytest.mark.parametrize(
    "body, line",
    [
        ("def f():\n    from builtins import eval as render\n    render(x)\n", 4),
        ("from builtins import eval as render\ndef f():\n    render(x)\n", 4),
        ("from builtins import eval as render\ndef f(x=render(y)):\n    pass\n", 3),
        ("from builtins import eval as render\ndef f(x: render(y)):\n    pass\n", 3),
        (
            "class C:\n    from builtins import eval as render\n    render(x)\n",
            4,
        ),
        (
            "def f():\n    from os import system as render\n    def g():\n        render(x)\n",
            5,
        ),
        (
            "def f():\n    import subprocess as render\n    render.run(x, shell=True)\n",
            4,
        ),
    ],
)
def test_check_py_ast_alias_remains_visible_in_lexical_scope(body, line):
    findings = list(check_py_ast("app.py", "import openai\n" + body))
    assert [f.line for f in findings] == [line]


def test_hardcoded_secrets_owned_by_check_secrets():
    # Secrets in assignments are check_secrets' job (it scans every line with
    # SECRET_PATTERNS); check_py_ast must not double-report them.
    code = """
def connect():
    my_api_key = "sk-proj-12345678901234567890123456789012"  # ua:allow
    self.secret_token = "ghp_123456789012345678901234567890123456"  # ua:allow
    # Placeholder should not be flagged
    dummy_key = "your-api-key-placeholder"
    # Unrelated assignment should not be flagged
    other_val = "some-long-string-value-that-is-not-secret"
"""
    assert list(check_py_ast("test_file.py", code)) == []

    findings = list(check_secrets("test_file.py", code))
    assert [f.line for f in findings] == [3, 4]
    assert all(f.rule == "SECRETS-KEY" for f in findings)


def test_check_py_ast_syntax_error():
    # Syntax error without LLM imports: handled gracefully, no findings
    code = """
def invalid_syntax:
    print "missing parenthesis"
"""
    findings = list(check_py_ast("test_file.py", code))
    assert findings == []


def test_check_py_ast_syntax_error_regex_fallback():
    # Unparseable LLM-adjacent files still get the line-regex scan
    code = """
import openai
def broken(:
    eval(user_input)
    subprocess.run(cmd, shell=True)
"""
    findings = list(check_py_ast("test_file.py", code))
    assert [(f.rule, f.line) for f in findings] == [
        ("LLM-EVAL", 4),
        ("LLM-SHELL-TRUE", 5),
    ]


def test_audit_reports_each_finding_once(tmp_path):
    # Regression: regex + AST rules double-reported eval/exec and shell=True
    (tmp_path / "app.py").write_text(
        "import openai\n"
        "import subprocess\n"
        "def run(user_input):\n"
        "    eval(user_input)\n"
        "    subprocess.run(user_input, shell=True)\n"
    )
    report = run_audit(tmp_path)
    keys = [(f["rule"], f["line"]) for f in report["findings"] if f["path"] == "app.py"]
    assert len(keys) == len(set(keys))
    assert keys.count(("LLM-EVAL", 4)) == 1
    assert keys.count(("LLM-SHELL-TRUE", 5)) == 1


@pytest.mark.parametrize(
    "code, expected",
    [
        # LLM import not first on the line still enables the checks
        ("import json, anthropic\nimport subprocess\nsubprocess.run(x, shell=True)\n", 3),
        ("from google import genai\nimport os\nos.system(x)\n", 3),
        # Aliased modules and from-imports resolve to the real API
        ("import anthropic\nimport subprocess as sp\nsp.run(x, shell=True)\n", 3),
        ("import anthropic\nfrom subprocess import run as r\nr(x, shell=True)\n", 3),
        ("import anthropic\nfrom os import system as s\ns(x)\n", 3),
        ("import anthropic\nimport builtins\nbuiltins.eval(x)\n", 3),
        ("import anthropic\nfrom builtins import exec as ex\nex(x)\n", 3),
        # Any truthy constant, or shell passed through a literal **kwargs dict
        ("import anthropic\nimport subprocess\nsubprocess.run(x, shell=1)\n", 3),
        ("import anthropic\nimport subprocess\nsubprocess.run(x, **{'shell': True})\n", 3),
        # APIs that always run a shell
        ("import anthropic\nimport subprocess\nsubprocess.getoutput(x)\n", 3),
        ("import anthropic\nimport subprocess\nsubprocess.getstatusoutput(x)\n", 3),
        ("import anthropic, asyncio\nasyncio.create_subprocess_shell(x)\n", 2),
    ],
)
def test_check_py_ast_resolves_bypass_shapes(code, expected):
    findings = list(check_py_ast("app.py", code))
    assert [f.line for f in findings] == [expected]
    assert findings[0].rule in ("LLM-EVAL", "LLM-SHELL-TRUE")


@pytest.mark.parametrize(
    "code",
    [
        # Falsy shell values and argv calls stay clean
        "import anthropic\nimport subprocess as sp\nsp.run(['ls'], shell=0)\n",
        "import anthropic\nimport subprocess\nsubprocess.run(['ls'], **{'shell': False})\n",
        # LLM SDK named only in a string, not imported: no LLM gate
        's = """\nimport openai\n"""\nimport subprocess\nsubprocess.run(x, shell=True)\n',
        # Look-alike module names are not LLM SDKs
        "import openai_tools\nimport subprocess\nsubprocess.run(x, shell=True)\n",
    ],
)
def test_check_py_ast_resolution_does_not_overflag(code):
    assert list(check_py_ast("app.py", code)) == []


@pytest.mark.parametrize(
    "values, expected",
    [("False, 'shell': True", 1), ("True, 'shell': False", 0)],
)
def test_check_py_ast_literal_shell_dict_uses_last_duplicate_key(values, expected):
    code = f"import openai, subprocess\nsubprocess.run(cmd, **{{'shell': {values}}})\n"
    findings = list(check_py_ast("app.py", code))
    assert [(f.rule, f.line) for f in findings] == [("LLM-SHELL-TRUE", 2)] * expected


@pytest.mark.parametrize("llm", [False, True])
def test_check_py_ast_deep_expression_does_not_abort_audit(llm):
    prefix = "import openai\n" if llm else ""
    code = prefix + "x = " + " + ".join(['"a"'] * 600) + "\neval(value)\n"
    findings = list(check_py_ast("app.py", code))
    assert [(f.rule, f.line) for f in findings] == ([("LLM-EVAL", 3)] if llm else [])
