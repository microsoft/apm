"""Transient provider-coordinate behavior for dependency references."""

from abc import ABC, abstractmethod
from dataclasses import replace
from typing import Self


class ProviderCoordinateMixin(ABC):
    """Derive and validate provider details without persisting them."""

    host: str | None
    repo_url: str
    ado_organization: str | None
    ado_project: str | None
    ado_repo: str | None

    @classmethod
    @abstractmethod
    def canonical_ado_coordinates(
        cls,
        host: str | None,
        repo_url: str,
    ) -> tuple[str | None, str | None, str | None]:
        """Return canonical ADO coordinates from the concrete reference owner."""
        raise NotImplementedError

    def validate_provider_coordinates(self) -> None:
        """Reject transient provider coordinates that disagree with canonical identity."""
        supplied = (self.ado_organization, self.ado_project, self.ado_repo)
        canonical = self.canonical_ado_coordinates(self.host, self.repo_url)
        if supplied != canonical:
            raise ValueError(
                f"Incomplete or mismatched Azure DevOps reference coordinates for "
                f"{self.repo_url}. Run `apm install <original-ado-url>` with "
                "the original Azure DevOps URL to regenerate its state."
            )

    @staticmethod
    def is_transient_provider_field(field_name: str) -> bool:
        """Return whether a model field must never be persisted in lock state."""
        return field_name in {"ado_organization", "ado_project", "ado_repo"}

    def with_derived_provider_coordinates(self) -> Self:
        """Return a copy with transient provider coordinates derived from identity."""
        ado_organization, ado_project, ado_repo = self.canonical_ado_coordinates(
            self.host,
            self.repo_url,
        )
        return replace(
            self,
            ado_organization=ado_organization,
            ado_project=ado_project,
            ado_repo=ado_repo,
        )
