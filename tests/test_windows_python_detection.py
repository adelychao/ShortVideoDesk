from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows startup script")
class WindowsPythonDetectionTests(unittest.TestCase):
    def test_existing_project_python_is_used_when_py_launcher_is_unavailable(self) -> None:
        if sys.version_info[:2] != (3, 11):
            self.skipTest('Python 3.11 is needed to create the test environment')
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        test_root = Path(temp.name)
        subprocess.run([sys.executable, '-m', 'venv', str(test_root / '.venv')], check=True, capture_output=True)
        (test_root / 'scripts').mkdir()
        (test_root / 'scripts' / 'project_lock_guard.py').write_text(
            "import sys\nprint('LOCKED', flush=True)\nsys.stdin.readline()\n", encoding='utf-8'
        )
        script = r'''
$path = Join-Path $env:TEST_PROJECT 'scripts\start_local.ps1'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
foreach ($name in @('Get-ToolOutput', 'Get-PythonLauncher', 'Enter-ProjectRuntimeLock', 'Exit-ProjectRuntimeLock')) {
    $definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    Invoke-Expression ($definition.Extent.Text.Replace('$PSScriptRoot', '$env:TEST_GUARD_ROOT'))
}
$projectRoot = $env:TEST_ROOT
function Get-Command {
    param($Name, $CommandType, [switch]$All, $ErrorAction)
    if ($Name -eq 'py') { return }
    Microsoft.PowerShell.Core\Get-Command -Name $Name -CommandType $CommandType -ErrorAction SilentlyContinue
}
$selected = Get-PythonLauncher
if ($null -eq $selected) { exit 3 }
$guard = Enter-ProjectRuntimeLock
Exit-ProjectRuntimeLock $guard
@{source = $selected.Source; arguments = @($selected.Arguments)} | ConvertTo-Json -Compress
'''
        env = {**os.environ, "TEST_PROJECT": str(PROJECT), "TEST_ROOT": str(test_root), "TEST_GUARD_ROOT": str(test_root / 'scripts')}
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", script],
            env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        selected = json.loads(result.stdout)
        self.assertEqual(Path(selected["source"]), test_root / ".venv" / "Scripts" / "python.exe")
        self.assertEqual(selected["arguments"], [])

    def test_existing_dependencies_can_be_verified_without_installing(self) -> None:
        if not all(shutil.which(name) for name in ('uv', 'node', 'npm.cmd')):
            self.skipTest('uv, Node.js and npm are required')
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        frontend = root / 'frontend'
        package = frontend / 'node_modules' / 'example'
        package.mkdir(parents=True)
        (root / 'requirements.txt').write_text('', encoding='utf-8')
        (package / 'package.json').write_text('{"name":"example","version":"1.0.0"}', encoding='utf-8')
        (frontend / 'package.json').write_text('{"name":"fixture","version":"1.0.0","dependencies":{"example":"1.0.0"}}', encoding='utf-8')
        (frontend / 'package-lock.json').write_text('{"packages":{"node_modules/example":{"version":"1.0.0"}}}', encoding='utf-8')
        (frontend / 'node_modules' / '.package-lock.json').write_text('{"packages":{"node_modules/example":{"version":"1.0.0"}}}', encoding='utf-8')
        script = r'''
$path = Join-Path $env:TEST_PROJECT 'scripts\start_local.ps1'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($path, [ref]$tokens, [ref]$errors)
foreach ($name in @('Get-ToolOutput', 'Test-ExistingPythonRequirements', 'Test-ExistingFrontendDependencies')) {
    $definition = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    if ($null -eq $definition) { exit 3 }
    Invoke-Expression $definition.Extent.Text
}
Set-Location -LiteralPath $env:TEST_ROOT
$pythonReady = Test-ExistingPythonRequirements $env:TEST_PYTHON 'requirements.txt'
$frontendReady = Test-ExistingFrontendDependencies
if (-not $pythonReady -or -not $frontendReady) { exit 4 }
(Set-Content -LiteralPath 'requirements.txt' -Value 'package-that-cannot-exist-shortvideodesk==1.0')
if (Test-ExistingPythonRequirements $env:TEST_PYTHON 'requirements.txt') { exit 5 }
(Set-Content -LiteralPath 'frontend/package-lock.json' -Value '{"packages":{"node_modules/example":{"version":"2.0.0"}}}')
if (Test-ExistingFrontendDependencies) { exit 6 }
'''
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", script],
            env={**os.environ, "TEST_PROJECT": str(PROJECT), "TEST_ROOT": str(root), "TEST_PYTHON": sys.executable},
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
