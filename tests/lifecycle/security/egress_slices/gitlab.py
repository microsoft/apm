"""Egress rows for HOST GitLab: gitlab.com."""

from __future__ import annotations

from tests.lifecycle.security.egress_slices import EgressRow, HostSlice

SLICE = HostSlice(
    host="GitLab",
    git_host="gitlab.com",
    family=("gitlab.com",),
    base="https://gitlab.com/lifecycle/group",
    spec="https://gitlab.com/lifecycle/group/p-skill#v1.0.0",
    pat_env="GITLAB_APM_PAT",
    rows=(
        EgressRow("EP|CRED=PAT|HOST=GitLab", "PAT", "clone", smoke=True),
        EgressRow("EP|CRED=helper|HOST=GitLab", "helper", "clone"),
        EgressRow("EP|CRED=none|HOST=GitLab", "none", "clone"),
    ),
)
