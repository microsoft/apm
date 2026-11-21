"""The lifecycle driver places the user-scope flag where the CLI accepts it."""

from __future__ import annotations

import pytest

from tests.lifecycle import driver

pytestmark = [pytest.mark.unit, pytest.mark.promise("P-INV-I5")]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("apm install x", ("install", "--global", "x")),
        ("apm deps list", ("deps", "list", "--global")),
        ("apm lock export -o out.json", ("lock", "export", "--global", "-o", "out.json")),
        ("apm lock", ("lock", "--global")),
        ("apm lock --frozen", ("lock", "--global", "--frozen")),
        ("apm deps list -g", ("deps", "list", "-g")),
        ("apm approve p", ("approve", "--user", "p")),
    ],
)
def test_user_scope_flag_goes_on_the_command_that_reads_it(line, expected):
    assert driver.command_args(line, scope="user") == expected
