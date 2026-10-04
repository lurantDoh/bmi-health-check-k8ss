from app.bmi import calculate_bmi, categorize_bmi
from fastapi.testclient import TestClient
import pytest

from app import auth, db, metrics
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def fresh_storage(monkeypatch):
    """Every test starts with an empty client table and a stable signing key."""
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    # The TestClient keeps a cookie jar for the whole module, so a session from
    # an earlier test would otherwise leak into the next one.
    client.cookies.clear()
    db.set_repository(db.InMemoryClientRepository())
    yield
    db.set_repository(None)


@pytest.fixture
def signed_in():
    """A client session for a registered user."""
    response = client.post(
        "/signup",
        data={
            "full_name": "Ada Lovelace",
            "email": "Ada@Example.com ",
            "password": "analytical-engine",
            "confirm_password": "analytical-engine",
        },
    )
    assert response.status_code == 200
    return client


def test_normal_bmi():
    result = calculate_bmi(175, 70)
    assert result.bmi == 22.9
    assert result.category == "normal"
    assert result.needs_attention is False
    assert result.health_advice
    assert result.exercises


def test_underweight():
    result = calculate_bmi(180, 50)
    assert result.category == "underweight"
    assert result.needs_attention is True
    assert any("Strength training" in tip for tip in result.exercises)


def test_overweight():
    result = calculate_bmi(170, 80)
    assert result.category == "overweight"
    assert result.needs_attention is True
    assert any("calorie" in tip.lower() for tip in result.health_advice)


def test_obese():
    result = calculate_bmi(160, 90)
    assert result.category == "obese"
    assert result.needs_attention is True
    assert any("low-impact" in tip.lower() for tip in result.exercises)


def test_rejects_non_positive():
    with pytest.raises(ValueError):
        calculate_bmi(0, 70)
    with pytest.raises(ValueError):
        calculate_bmi(170, -1)


def test_categorize_boundaries():
    assert categorize_bmi(18.4) == "underweight"
    assert categorize_bmi(18.5) == "normal"
    assert categorize_bmi(24.9) == "normal"
    assert categorize_bmi(25.0) == "overweight"
    assert categorize_bmi(29.9) == "overweight"
    assert categorize_bmi(30.0) == "obese"


def test_ui_home_requires_sign_in():
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/signin"


def test_ui_home_for_signed_in_client(signed_in):
    response = signed_in.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "BMI Health Check" in response.text
    assert "/static/nextgenlogo.jpg" in response.text
    assert "Ada Lovelace" in response.text


def test_health_is_public():
    assert client.get("/health").json()["status"] == "ok"


def test_signup_page_renders():
    response = client.get("/signup")
    assert response.status_code == 200
    assert "Create your account" in response.text


def test_signup_persists_normalized_email(signed_in):
    stored = db.get_repository().find("ada@example.com")
    assert stored is not None
    assert stored.full_name == "Ada Lovelace"
    # The raw password is never stored.
    assert "analytical-engine" not in stored.password_hash
    assert auth.verify_password("analytical-engine", stored.password_hash)


def test_signup_rejects_duplicate_email(signed_in):
    response = client.post(
        "/signup",
        data={
            "full_name": "Someone Else",
            "email": "ada@example.com",
            "password": "another-password",
            "confirm_password": "another-password",
        },
    )
    assert response.status_code == 400
    assert "already registered" in response.text
    assert db.get_repository().count() == 1


@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"full_name": "  "}, "full name"),
        ({"email": "not-an-email"}, "valid email"),
        ({"password": "short", "confirm_password": "short"}, "at least 8"),
        ({"confirm_password": "mismatched-value"}, "do not match"),
    ],
)
def test_signup_validation(overrides, expected):
    payload = {
        "full_name": "Grace Hopper",
        "email": "grace@example.com",
        "password": "compiler-1952",
        "confirm_password": "compiler-1952",
    }
    payload.update(overrides)

    response = client.post("/signup", data=payload)
    assert response.status_code == 400
    assert expected.lower() in response.text.lower()
    assert db.get_repository().count() == 0


def test_signin_flow_and_logout(signed_in):
    signed_in.post("/logout")
    assert signed_in.get("/", follow_redirects=False).status_code == 303

    response = signed_in.post(
        "/signin", data={"email": "ada@example.com", "password": "analytical-engine"}
    )
    assert response.status_code == 200
    assert "Signed in as" in response.text
    assert db.get_repository().find("ada@example.com").last_login_at is not None


def test_signin_rejects_wrong_password(signed_in):
    signed_in.post("/logout")
    response = signed_in.post(
        "/signin", data={"email": "ada@example.com", "password": "wrong-password"}
    )
    assert response.status_code == 401
    assert "incorrect" in response.text


def test_signin_does_not_reveal_unknown_emails():
    unknown = client.post(
        "/signin", data={"email": "nobody@example.com", "password": "whatever-1234"}
    )
    assert unknown.status_code == 401
    assert "incorrect" in unknown.text


def test_session_token_rejects_tampering():
    token = auth.issue_session("ada@example.com")
    assert auth.read_session(token) == "ada@example.com"
    assert auth.read_session(token[:-2] + "xx") is None
    assert auth.read_session("garbage") is None
    assert auth.read_session(None) is None


def test_session_token_expires():
    token = auth.issue_session("ada@example.com", now=0)
    assert auth.read_session(token, now=0) == "ada@example.com"
    assert auth.read_session(token, now=auth.SESSION_TTL_SECONDS + 1) is None


def test_metrics_disabled_by_default(monkeypatch):
    monkeypatch.delenv("METRICS_ENABLED", raising=False)
    assert metrics.metrics_enabled() is False


def test_region_falls_back_to_aws_region(monkeypatch):
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-2")
    assert metrics.resolve_region() == "us-east-2"

    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    assert metrics.resolve_region() == "us-west-2"

    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    assert metrics.resolve_region() is None


def test_metrics_collector_aggregates():
    collector = metrics.MetricsCollector()
    collector.record_request(12.5, 200)
    collector.record_request(30.0, 500)
    collector.record_bmi_calculation()

    window = collector.drain()
    assert window.requests == 2
    assert window.errors == 1
    assert window.bmi_calculations == 1

    data = {item["MetricName"]: item for item in metrics.build_metric_data(window)}
    assert data["RequestCount"]["Value"] == 2
    assert data["ErrorCount"]["Value"] == 1
    assert data["LatencyMs"]["StatisticValues"]["SampleCount"] == 2
    assert data["LatencyMs"]["StatisticValues"]["Maximum"] == 30.0

    # Draining resets the window so counts are never double-reported.
    assert collector.drain().is_empty()


def test_requests_are_recorded_by_middleware():
    before = metrics.collector.drain()
    assert before.is_empty() or before.requests >= 0

    client.get("/health")
    window = metrics.collector.drain()
    assert window.requests >= 1


def test_brand_assets_served():
    for path in ("/static/nextgenlogo.jpg", "/static/nextgenmark.jpg"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("image/"), path


def test_bmi_api_requires_session():
    response = client.post("/bmi", json={"height_cm": 175, "weight_kg": 70})
    assert response.status_code == 401
    assert client.get("/bmi?height_cm=175&weight_kg=70").status_code == 401


def test_bmi_api_includes_guidance(signed_in):
    response = signed_in.post("/bmi", json={"height_cm": 175, "weight_kg": 70})
    assert response.status_code == 200
    data = response.json()
    assert data["category"] == "normal"
    assert data["summary"]
    assert len(data["health_advice"]) >= 1
    assert len(data["exercises"]) >= 1
    assert data["needs_attention"] is False


def test_me_returns_the_signed_in_client(signed_in):
    data = signed_in.get("/me").json()
    assert data["email"] == "ada@example.com"
    assert data["full_name"] == "Ada Lovelace"
    assert data["created_at"]
