# Example Django Project

Run the example from an `abxpkg` checkout. `uv` uses the checkout's locked
development environment, while the copied project keeps the example database
out of the source tree.

```bash
ABXPKG_REPO_ROOT="$PWD"
DJANGO_EXAMPLE_TEST="$(mktemp -d)"
trap 'rm -rf "$DJANGO_EXAMPLE_TEST"' EXIT
cp -R django_example_project "$DJANGO_EXAMPLE_TEST/project"
cd "$DJANGO_EXAMPLE_TEST/project"
```
<!--pytest-codeblocks:cont-->
```bash
uv run --project "$ABXPKG_REPO_ROOT" ./manage.py check
uv run --project "$ABXPKG_REPO_ROOT" ./manage.py makemigrations --check --dry-run
uv run --project "$ABXPKG_REPO_ROOT" ./manage.py migrate --noinput

DJANGO_SUPERUSER_USERNAME=admin \
DJANGO_SUPERUSER_EMAIL=admin@example.com \
DJANGO_SUPERUSER_PASSWORD=archivebox-test-password \
uv run --project "$ABXPKG_REPO_ROOT" ./manage.py createsuperuser --noinput

uv run --project "$ABXPKG_REPO_ROOT" ./manage.py shell -c '
from django.contrib.auth import get_user_model
from django.test import Client
assert get_user_model().objects.filter(username="admin", is_superuser=True).count() == 1
response = Client(HTTP_HOST="localhost").get("/admin/")
assert response.status_code == 302
assert response.headers["Location"].startswith("/admin/login/")
'
cd "$ABXPKG_REPO_ROOT"
```

For interactive use, run
`uv run --project .. ./manage.py runserver 127.0.0.1:8000` from
`django_example_project/`, then open <http://127.0.0.1:8000/admin/>.
