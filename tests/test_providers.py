from __future__ import annotations

import unittest

from orchestra_kit.config import ProviderConfig
from orchestra_kit.providers import (
    CredentialStoreError,
    ProviderCredentialService,
    resolve_provider_runtime,
)


class FakeCredentialBackend:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.fail = False

    def get(self, account: str) -> str | None:
        if self.fail:
            raise CredentialStoreError("backend detail must not escape")
        return self.values.get(account)

    def contains(self, account: str) -> bool:
        if self.fail:
            raise CredentialStoreError("backend detail must not escape")
        return account in self.values

    def set(self, account: str, secret: str) -> None:
        if self.fail:
            raise CredentialStoreError("backend detail must not escape")
        self.values[account] = secret

    def delete(self, account: str) -> None:
        self.values.pop(account, None)


def provider() -> ProviderConfig:
    return ProviderConfig(
        name="deepseek",
        display_name="DeepSeek",
        base_url="https://api.deepseek.example/v1",
        env_key="DEEPSEEK_API_KEY",
        wire_api="responses",
        model_catalog_json=None,
        supports_standalone_web_search=True,
        capabilities=("function",),
    )


class ProviderCredentialServiceTests(unittest.TestCase):
    def test_persisted_credential_is_shared_without_appearing_in_repr(self) -> None:
        backend = FakeCredentialBackend()
        writer = ProviderCredentialService(environment={}, backend=backend)
        reader = ProviderCredentialService(environment={}, backend=backend)

        writer.set(provider(), "top-secret-value")
        resolved = reader.resolve(provider())

        self.assertEqual(resolved.secret, "top-secret-value")
        self.assertEqual(resolved.source, "keychain")
        self.assertNotIn("top-secret-value", repr(resolved))
        writer.delete(provider())
        self.assertIsNone(reader.resolve(provider()))

    def test_environment_wins_and_is_never_persisted(self) -> None:
        backend = FakeCredentialBackend()
        ProviderCredentialService(environment={}, backend=backend).set(provider(), "persisted")
        service = ProviderCredentialService(
            environment={"DEEPSEEK_API_KEY": "from-environment"}, backend=backend
        )

        resolved = service.resolve(provider())

        self.assertEqual(resolved.secret, "from-environment")
        self.assertEqual(resolved.source, "environment")
        self.assertIn("persisted", backend.values.values())

    def test_status_does_not_read_secret_and_endpoint_change_has_distinct_identity(self) -> None:
        class StatusBackend(FakeCredentialBackend):
            def get(self, account: str) -> str | None:
                raise AssertionError("status must not fetch credential data")

        backend = StatusBackend()
        service = ProviderCredentialService(environment={}, backend=backend)
        service.set(provider(), "persisted")
        changed = ProviderConfig(**{
            **provider().__dict__, "base_url": "https://other.example/v1"
        })

        self.assertEqual(service.status(provider()).source, "keychain")
        self.assertFalse(service.status(changed).available)

    def test_backend_errors_are_replaced_with_generic_message(self) -> None:
        backend = FakeCredentialBackend()
        backend.fail = True
        service = ProviderCredentialService(environment={}, backend=backend)

        with self.assertRaisesRegex(CredentialStoreError, "credential store is unavailable") as caught:
            service.resolve(provider())

        self.assertNotIn("backend detail", str(caught.exception))

    def test_runtime_contains_only_codex_provider_settings_and_child_environment(self) -> None:
        backend = FakeCredentialBackend()
        service = ProviderCredentialService(environment={}, backend=backend)
        service.set(provider(), "top-secret-value")
        runtime = resolve_provider_runtime(
            provider(),
            credential_service=service,
        )

        self.assertEqual(runtime.environment, {"DEEPSEEK_API_KEY": "top-secret-value"})
        self.assertNotIn("top-secret-value", repr(runtime))
        self.assertEqual(
            runtime.argv_config,
            (
                'model_provider="deepseek"',
                'model_providers.deepseek.name="DeepSeek"',
                'model_providers.deepseek.base_url="https://api.deepseek.example/v1"',
                'model_providers.deepseek.wire_api="responses"',
                'model_providers.deepseek.env_key="DEEPSEEK_API_KEY"',
                "model_providers.deepseek.supports_standalone_web_search=true",
            ),
        )
        self.assertNotIn("top-secret-value", " ".join(runtime.argv_config))


if __name__ == "__main__":
    unittest.main()
