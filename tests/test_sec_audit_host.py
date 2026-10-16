from fastapi.testclient import TestClient

from server.app import app


def test_unlisted_host_header_is_rejected():
    with TestClient(app) as client:
        response = client.get("/route-that-does-not-exist", headers={"host": "attacker.invalid"})
    assert response.status_code == 400


def test_default_cors_origins_are_exact_and_no_regex():
    middlewares = {mw.cls.__name__: mw.kwargs for mw in app.user_middleware}
    cors = middlewares["CORSMiddleware"]
    assert cors.get("allow_origin_regex") is None
    assert "http://172.23.229.195:3000" not in cors["allow_origins"]
    with TestClient(app) as client:
        response = client.options(
            "/route-that-does-not-exist",
            headers={
                "Origin": "http://localhost:9999",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert response.headers.get("access-control-allow-origin") != "http://localhost:9999"
