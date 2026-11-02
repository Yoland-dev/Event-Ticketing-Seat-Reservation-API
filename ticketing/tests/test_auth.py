import pytest

from ticketing.tests.conftest import PASSWORD
from ticketing.tests.utils import assert_error

pytestmark = pytest.mark.django_db

TOKEN_URL = "/api/auth/token/"


def test_obtain_and_use_jwt(anon_client, standard_user):
    response = anon_client.post(
        TOKEN_URL, {"username": "alice", "password": PASSWORD}, format="json"
    )
    assert response.status_code == 200
    tokens = response.json()
    assert {"access", "refresh"} <= tokens.keys()

    anon_client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
    me = anon_client.get("/api/auth/me/")
    assert me.status_code == 200
    assert me.json()["username"] == "alice"
    assert me.json()["role"] == "standard"


def test_refresh_and_verify(anon_client, standard_user):
    tokens = anon_client.post(
        TOKEN_URL, {"username": "alice", "password": PASSWORD}, format="json"
    ).json()
    refreshed = anon_client.post(
        "/api/auth/token/refresh/", {"refresh": tokens["refresh"]}, format="json"
    )
    assert refreshed.status_code == 200 and "access" in refreshed.json()
    verified = anon_client.post(
        "/api/auth/token/verify/", {"token": tokens["access"]}, format="json"
    )
    assert verified.status_code == 200


def test_wrong_password_returns_standard_error(anon_client, standard_user):
    response = anon_client.post(TOKEN_URL, {"username": "alice", "password": "nope"}, format="json")
    assert_error(response, 401, "no_active_account")


def test_missing_credentials_is_validation_error(anon_client):
    error = assert_error(anon_client.post(TOKEN_URL, {}, format="json"), 400, "validation_error")
    assert {"username", "password"} <= error["details"].keys()


def test_garbage_token_is_rejected(anon_client):
    anon_client.credentials(HTTP_AUTHORIZATION="Bearer not-a-real-token")
    error = assert_error(anon_client.get("/api/events/"), 401, "token_not_valid")
    assert error["details"]  # simplejwt explains which token class failed


def test_unauthenticated_request(anon_client):
    response = anon_client.get("/api/events/")
    assert_error(response, 401, "not_authenticated")
    assert response["WWW-Authenticate"].startswith("Bearer")


def test_register_creates_standard_user(anon_client):
    response = anon_client.post(
        "/api/auth/register/",
        {"username": "carol", "email": "carol@test.example", "password": PASSWORD},
        format="json",
    )
    assert response.status_code == 201
    assert "password" not in response.json()
    login = anon_client.post(TOKEN_URL, {"username": "carol", "password": PASSWORD}, format="json")
    assert login.status_code == 200


def test_register_cannot_escalate_to_admin(anon_client):
    from django.contrib.auth import get_user_model

    anon_client.post(
        "/api/auth/register/",
        {
            "username": "mallory",
            "email": "m@test.example",
            "password": PASSWORD,
            "is_staff": True,
        },
        format="json",
    )
    assert get_user_model().objects.get(username="mallory").is_staff is False


def test_register_rejects_weak_password_and_duplicate(anon_client, standard_user):
    weak = anon_client.post(
        "/api/auth/register/",
        {"username": "dave", "email": "d@test.example", "password": "123"},
        format="json",
    )
    assert "password" in assert_error(weak, 400, "validation_error")["details"]
    dup = anon_client.post(
        "/api/auth/register/",
        {"username": "alice", "email": "a2@test.example", "password": PASSWORD},
        format="json",
    )
    assert "username" in assert_error(dup, 400, "validation_error")["details"]


def test_me_reports_admin_role(admin_client):
    assert admin_client.get("/api/auth/me/").json()["role"] == "admin"
