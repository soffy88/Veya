from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_compose_source_publishes_backend_ports_on_loopback_only():
    source = (ROOT / "deploy/docker-compose.yml").read_text()
    assert '"127.0.0.1:8767:8765"' in source
    assert '"127.0.0.1:9120:8765"' in source
    assert '"127.0.0.1:8768:8768"' in source
    assert '"0.0.0.0:8767:8765"' not in source
    assert '"0.0.0.0:9120:8765"' not in source


def test_systemd_gateway_and_web_default_to_loopback():
    gateway = (ROOT / "deploy/veya-gateway.service").read_text()
    web = (ROOT / "deploy/veya-web.service").read_text()
    assert "--host 127.0.0.1" in gateway
    assert "Environment=HOST=127.0.0.1" in web


def test_hicode_server_listener_requires_authentication():
    source = (ROOT / "deploy/hicode-entrypoint.sh").read_text()
    assert "serve --addr 127.0.0.1:8768 --auth token" in source
    assert "serve --addr 0.0.0.0:8768 --auth none" not in source
