"""Secure credentials and native Codex settings for custom model providers.

Secrets are intentionally absent from project configuration.  Environment
variables take precedence; on macOS a credential can instead be shared across
OrchestraKit processes through the user's Keychain.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Mapping, Protocol
from urllib.parse import urlsplit, urlunsplit

from .config import ENV_PATTERN, NAME_PATTERN, ProviderConfig, validate_provider_url, validate_provider_env


_KEYCHAIN_SERVICE = "dev.openai.orchestrakit.provider-credentials.v1"
_UTF8 = 0x08000100
_ITEM_NOT_FOUND = -25300


class CredentialStoreError(RuntimeError):
    """A credential backend could not safely complete an operation."""


class ProviderCredentialUnavailable(RuntimeError):
    """A provider requires a credential but none is available."""


class CredentialBackend(Protocol):
    def get(self, account: str) -> str | None: ...

    def contains(self, account: str) -> bool: ...

    def set(self, account: str, secret: str) -> None: ...

    def delete(self, account: str) -> None: ...


@dataclass(frozen=True)
class ResolvedCredential:
    secret: str = field(repr=False)
    source: str


@dataclass(frozen=True)
class CredentialStatus:
    available: bool
    source: str | None


@dataclass(frozen=True)
class ProviderRuntime:
    argv_config: tuple[str, ...]
    environment: dict[str, str] = field(repr=False)


def _normalized_base_url(value: str) -> str:
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    port = parsed.port
    if port is not None and not (
        (scheme == "https" and port == 443) or (scheme == "http" and port == 80)
    ):
        hostname = f"{hostname}:{port}"
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((scheme, hostname, path, parsed.query, parsed.fragment))


def credential_account(provider: ProviderConfig) -> str:
    """Return an opaque endpoint-bound Keychain account identifier."""
    if provider.env_key is None:
        raise ValueError("provider does not declare an environment credential")
    identity = json.dumps(
        [_normalized_base_url(provider.base_url), provider.env_key],
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "provider-v1-" + hashlib.sha256(identity).hexdigest()


class MacOSKeychainBackend:
    """Generic-password storage backed directly by Security.framework."""

    def __init__(self, service: str = _KEYCHAIN_SERVICE) -> None:
        if sys.platform != "darwin":
            raise CredentialStoreError("credential store is unavailable")
        try:
            security_path = ctypes.util.find_library("Security")
            core_path = ctypes.util.find_library("CoreFoundation")
            if not security_path or not core_path:
                raise OSError
            self._security = ctypes.CDLL(security_path)
            self._core = ctypes.CDLL(core_path)
            self._configure_functions()
            self._constants = {
                name: ctypes.c_void_p.in_dll(self._security, name).value
                for name in (
                    "kSecClass",
                    "kSecClassGenericPassword",
                    "kSecAttrService",
                    "kSecAttrAccount",
                    "kSecValueData",
                    "kSecReturnData",
                    "kSecReturnAttributes",
                    "kSecMatchLimit",
                    "kSecMatchLimitOne",
                )
            }
            self._true = ctypes.c_void_p.in_dll(self._core, "kCFBooleanTrue").value
            self._key_callbacks = ctypes.addressof(
                ctypes.c_byte.in_dll(self._core, "kCFTypeDictionaryKeyCallBacks")
            )
            self._value_callbacks = ctypes.addressof(
                ctypes.c_byte.in_dll(self._core, "kCFTypeDictionaryValueCallBacks")
            )
            self._service = service
        except Exception as exc:
            raise CredentialStoreError("credential store is unavailable") from exc

    def _configure_functions(self) -> None:
        self._core.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        self._core.CFStringCreateWithCString.restype = ctypes.c_void_p
        self._core.CFDataCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint8), ctypes.c_long]
        self._core.CFDataCreate.restype = ctypes.c_void_p
        self._core.CFDataGetLength.argtypes = [ctypes.c_void_p]
        self._core.CFDataGetLength.restype = ctypes.c_long
        self._core.CFDataGetBytePtr.argtypes = [ctypes.c_void_p]
        self._core.CFDataGetBytePtr.restype = ctypes.POINTER(ctypes.c_uint8)
        self._core.CFDictionaryCreateMutable.argtypes = [
            ctypes.c_void_p,
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        self._core.CFDictionaryCreateMutable.restype = ctypes.c_void_p
        self._core.CFDictionarySetValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        self._core.CFRelease.argtypes = [ctypes.c_void_p]
        self._security.SecItemCopyMatching.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        self._security.SecItemCopyMatching.restype = ctypes.c_int32
        self._security.SecItemAdd.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        self._security.SecItemAdd.restype = ctypes.c_int32
        self._security.SecItemUpdate.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._security.SecItemUpdate.restype = ctypes.c_int32
        self._security.SecItemDelete.argtypes = [ctypes.c_void_p]
        self._security.SecItemDelete.restype = ctypes.c_int32

    def _dictionary(self) -> int:
        value = self._core.CFDictionaryCreateMutable(
            None, 0, self._key_callbacks, self._value_callbacks
        )
        if not value:
            raise CredentialStoreError("credential store is unavailable")
        return value

    def _string(self, value: str) -> int:
        result = self._core.CFStringCreateWithCString(None, value.encode("utf-8"), _UTF8)
        if not result:
            raise CredentialStoreError("credential store is unavailable")
        return result

    def _set_value(self, dictionary: int, key: str, value: int) -> None:
        self._core.CFDictionarySetValue(dictionary, self._constants[key], value)

    def _query(
        self, account: str, *, return_data: bool = False,
        return_attributes: bool = False,
    ) -> int:
        query = self._dictionary()
        service = account_value = None
        try:
            service = self._string(self._service)
            account_value = self._string(account)
            self._set_value(query, "kSecClass", self._constants["kSecClassGenericPassword"])
            self._set_value(query, "kSecAttrService", service)
            self._set_value(query, "kSecAttrAccount", account_value)
            if return_data:
                self._set_value(query, "kSecReturnData", self._true)
                self._set_value(query, "kSecMatchLimit", self._constants["kSecMatchLimitOne"])
            if return_attributes:
                self._set_value(query, "kSecReturnAttributes", self._true)
                self._set_value(query, "kSecMatchLimit", self._constants["kSecMatchLimitOne"])
            return query
        except Exception:
            self._core.CFRelease(query)
            raise
        finally:
            if service:
                self._core.CFRelease(service)
            if account_value:
                self._core.CFRelease(account_value)

    def _data(self, secret: str) -> int:
        encoded = secret.encode("utf-8")
        buffer = (ctypes.c_uint8 * len(encoded)).from_buffer_copy(encoded)
        value = self._core.CFDataCreate(None, buffer, len(encoded))
        if not value:
            raise CredentialStoreError("credential store is unavailable")
        return value

    @staticmethod
    def _check(status: int, *, allow_missing: bool = False) -> bool:
        if status == 0:
            return True
        if allow_missing and status == _ITEM_NOT_FOUND:
            return False
        raise CredentialStoreError("credential store is unavailable")

    def get(self, account: str) -> str | None:
        query = self._query(account, return_data=True)
        result = ctypes.c_void_p()
        try:
            if not self._check(
                self._security.SecItemCopyMatching(query, ctypes.byref(result)),
                allow_missing=True,
            ):
                return None
            length = self._core.CFDataGetLength(result)
            pointer = self._core.CFDataGetBytePtr(result)
            return ctypes.string_at(pointer, length).decode("utf-8")
        except (UnicodeError, ValueError) as exc:
            raise CredentialStoreError("credential store is unavailable") from exc
        finally:
            self._core.CFRelease(query)
            if result.value:
                self._core.CFRelease(result)

    def contains(self, account: str) -> bool:
        # Attribute-only lookup checks metadata without fetching the secret bytes.
        query = self._query(account, return_attributes=True)
        result = ctypes.c_void_p()
        try:
            return self._check(
                self._security.SecItemCopyMatching(query, ctypes.byref(result)),
                allow_missing=True,
            )
        finally:
            self._core.CFRelease(query)
            if result.value:
                self._core.CFRelease(result)

    def set(self, account: str, secret: str) -> None:
        query = self._query(account)
        attributes = self._dictionary()
        data = self._data(secret)
        try:
            self._set_value(attributes, "kSecValueData", data)
            status = self._security.SecItemUpdate(query, attributes)
            if status == _ITEM_NOT_FOUND:
                self._set_value(query, "kSecValueData", data)
                status = self._security.SecItemAdd(query, None)
            self._check(status)
        finally:
            self._core.CFRelease(data)
            self._core.CFRelease(attributes)
            self._core.CFRelease(query)

    def delete(self, account: str) -> None:
        query = self._query(account)
        try:
            self._check(self._security.SecItemDelete(query), allow_missing=True)
        finally:
            self._core.CFRelease(query)


class ProviderCredentialService:
    def __init__(
        self,
        *,
        environment: Mapping[str, str] | None = None,
        backend: CredentialBackend | None = None,
    ) -> None:
        self._environment = os.environ if environment is None else environment
        self._credential_backend = backend

    def _backend(self) -> CredentialBackend:
        if self._credential_backend is None:
            self._credential_backend = MacOSKeychainBackend()
        return self._credential_backend

    @staticmethod
    def _generic_error(exc: Exception) -> CredentialStoreError:
        return CredentialStoreError("credential store is unavailable")

    def resolve(self, provider: ProviderConfig) -> ResolvedCredential | None:
        if provider.env_key is None:
            return None
        environment_value = self._environment.get(provider.env_key)
        if environment_value:
            return ResolvedCredential(environment_value, "environment")
        try:
            value = self._backend().get(credential_account(provider))
        except Exception as exc:
            raise self._generic_error(exc) from None
        return ResolvedCredential(value, "keychain") if value else None

    def status(self, provider: ProviderConfig) -> CredentialStatus:
        if provider.env_key is None:
            return CredentialStatus(True, None)
        if self._environment.get(provider.env_key):
            return CredentialStatus(True, "environment")
        try:
            available = self._backend().contains(credential_account(provider))
        except Exception as exc:
            raise self._generic_error(exc) from None
        return CredentialStatus(available, "keychain" if available else None)

    def set(self, provider: ProviderConfig, secret: str) -> None:
        if provider.env_key is None:
            raise ValueError("provider does not declare an environment credential")
        if not isinstance(secret, str) or not secret or "\0" in secret or len(secret) > 65536:
            raise ValueError("credential must be nonempty bounded text")
        try:
            self._backend().set(credential_account(provider), secret)
        except Exception as exc:
            raise self._generic_error(exc) from None

    def delete(self, provider: ProviderConfig) -> None:
        if provider.env_key is None:
            return
        try:
            self._backend().delete(credential_account(provider))
        except Exception as exc:
            raise self._generic_error(exc) from None


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def resolve_provider_runtime(
    provider: ProviderConfig,
    *,
    credential_service: ProviderCredentialService | None = None,
) -> ProviderRuntime:
    """Compile one Responses-compatible provider for a child Codex process."""
    if not NAME_PATTERN.fullmatch(provider.name):
        raise ValueError("invalid provider identifier")
    if provider.wire_api != "responses":
        raise ValueError("native provider execution requires the Responses protocol")
    if provider.env_key is not None and not ENV_PATTERN.fullmatch(provider.env_key):
        raise ValueError("invalid provider credential environment name")
    validate_provider_url(provider.base_url)
    validate_provider_env(provider.env_key)
    service = credential_service or ProviderCredentialService()
    credential = service.resolve(provider)
    if provider.env_key is not None and credential is None:
        raise ProviderCredentialUnavailable("provider credential is unavailable")
    prefix = f"model_providers.{provider.name}"
    settings = [
        f"model_provider={_toml_string(provider.name)}",
        f"{prefix}.name={_toml_string(provider.display_name)}",
        f"{prefix}.base_url={_toml_string(provider.base_url)}",
        f"{prefix}.wire_api={_toml_string(provider.wire_api)}",
    ]
    environment: dict[str, str] = {}
    if provider.env_key is not None:
        settings.append(f"{prefix}.env_key={_toml_string(provider.env_key)}")
        environment[provider.env_key] = credential.secret  # type: ignore[union-attr]
    if provider.supports_standalone_web_search:
        settings.append(f"{prefix}.supports_standalone_web_search=true")
    return ProviderRuntime(tuple(settings), environment)
