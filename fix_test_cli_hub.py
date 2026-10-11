import re

with open("tests/test_nano_w8_cli_hub.py", "r") as f:
    content = f.read()

# Fix search_cli
content = re.sub(
    r'with patch\("veya\.oprim\.cli_hub\.httpx\.Client"\) as mock_client:\n\s*mock_client\.return_value\.__enter__\.return_value\.get\.return_value = MagicMock\(\n\s*status_code=200,\n\s*json=lambda: \{"results": \[\{"name": "test-tool", "description": "A test tool"\}\]\},\n\s*\)\n\s*result = search_cli\(query="test"\)',
    r'http_get = MagicMock()\n        http_get.return_value.json = lambda: {"results": [{"name": "test-tool", "description": "A test tool"}]}\n        result = search_cli(query="test", http_get=http_get)',
    content
)

# Fix read_cli_skill
content = re.sub(
    r'with patch\("veya\.oprim\.cli_hub\.httpx\.Client"\) as mock_client:\n\s*mock_client\.return_value\.__enter__\.return_value\.get\.return_value = MagicMock\(\n\s*status_code=200,\n\s*text="# Test Skill\\n\\nDescription\.\.\.",\n\s*\)\n\s*result = read_cli_skill\(skill_id="test-skill"\)',
    r'http_get = MagicMock()\n        http_get.return_value.text = "# Test Skill\\n\\nDescription..."\n        result = read_cli_skill(skill_id="test-skill", http_get=http_get)',
    content
)

# Fix install_cli_on_runner
content = re.sub(
    r'with patch\("veya\.oprim\.cli_hub\.subprocess\.run"\) as mock_run:\n\s*mock_run\.return_value = MagicMock\(returncode=0, stdout="installed", stderr=""\)\n\s*result = install_cli_on_runner\(package="test-package", version="1\.0\.0"\)',
    r'run_cmd = MagicMock()\n        run_cmd.return_value = MagicMock(returncode=0, stdout="installed", stderr="")\n        result = install_cli_on_runner(package="test-package", version="1.0.0", run_cmd=run_cmd)',
    content
)

# Fix invoke_cli
content = re.sub(
    r'with patch\("veya\.oprim\.cli_hub\.subprocess\.run"\) as mock_run:\n\s*mock_run\.return_value = MagicMock\(\n\s*returncode=0,\n\s*stdout=\'\{"result": "success"\}\',\n\s*stderr=""\n\s*\)\n\s*result = invoke_cli\(command=\["test-cli", "run"\], args=\{"input": "data"\}\)',
    r'run_cmd = MagicMock()\n        run_cmd.return_value = MagicMock(returncode=0, stdout=\'{"result": "success"}\', stderr="")\n        result = invoke_cli(command=["test-cli", "run"], args={"input": "data"}, run_cmd=run_cmd)',
    content
)

# Also fix TestCLIHubRoutes flag_required
content = content.replace(
    '        response = client.get("/api/v1/cli-hub/search", params={"q": "test"})',
    '        response = client_fixture.get("/api/v1/cli-hub/search", params={"q": "test"})'
)
content = content.replace(
    '    def test_flag_required(self):',
    '    def test_flag_required(self, client):'
)
content = content.replace(
    '        response = client.get',
    '        response = client.get'
)


with open("tests/test_nano_w8_cli_hub.py", "w") as f:
    f.write(content)
