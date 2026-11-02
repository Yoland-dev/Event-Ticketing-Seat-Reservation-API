import pytest
from django.contrib.auth import get_user_model

from ticketing.tests.conftest import PASSWORD
from ticketing.tests.utils import assert_error

pytestmark = pytest.mark.django_db
URL = "/api/users/"
User = get_user_model()


def test_admin_user_crud(admin_client):
    created = admin_client.post(
        URL,
        {"username": "erin", "email": "erin@test.example", "password": PASSWORD, "is_staff": True},
        format="json",
    )
    assert created.status_code == 201, created.content
    assert created.json()["role"] == "admin" and "password" not in created.json()
    user = User.objects.get(username="erin")
    assert user.check_password(PASSWORD)

    detail = f"{URL}{user.pk}/"
    demoted = admin_client.patch(
        detail, {"is_staff": False, "password": "An0ther-Str0ng-Pass!"}, format="json"
    )
    assert demoted.json()["role"] == "standard"
    user.refresh_from_db()
    assert user.check_password("An0ther-Str0ng-Pass!")

    assert admin_client.get(URL, {"search": "erin"}).json()["count"] == 1
    assert admin_client.delete(detail).status_code == 204


def test_create_requires_strong_password(admin_client):
    missing = admin_client.post(URL, {"username": "x1", "email": "x@test.example"}, format="json")
    assert "password" in assert_error(missing, 400)["details"]
    weak = admin_client.post(URL, {"username": "x2", "password": "password"}, format="json")
    assert "password" in assert_error(weak, 400)["details"]


def test_standard_user_forbidden(user_client):
    assert_error(user_client.get(URL), 403, "permission_denied")
