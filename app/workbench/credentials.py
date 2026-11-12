"""Operating-system credential storage for model API keys."""

from __future__ import annotations

import os


SERVICE_NAME = "ShortVideoDesk"
LEGACY_SERVICE_NAME = "LocalVideoWorkbench"
LEGACY_USERNAME = "model-api-key"
USERNAME_PREFIX = "model-api-key:"
PROFILE_USERNAME_PREFIX = "model-profile-api-key:"
ARTICLE_PLATFORM_USERNAME_PREFIX = "article-platform:"
ARTICLE_IMAGE_USERNAME = "article-image:api-key"
_ARTICLE_PLATFORM_CREDENTIAL_KEYS = {
    "wechat": {"app_secret"},
    "zhihu": {"app_secret"},
}


class CredentialStoreUnavailable(RuntimeError):
    pass


def _keyring():
    try:
        import keyring
    except ImportError as exc:
        raise CredentialStoreUnavailable("系统凭据存储依赖尚未安装。") from exc

    if os.name == "nt":
        try:
            from keyring.backends.Windows import WinVaultKeyring

            backend = WinVaultKeyring()
            keyring.set_keyring(backend)
        except Exception as exc:
            raise CredentialStoreUnavailable("Windows 凭据管理器当前不可用。") from exc
    else:
        backend = keyring.get_keyring()
        if getattr(backend, "priority", 0) <= 0:
            raise CredentialStoreUnavailable("操作系统凭据存储当前不可用。")
    return keyring


def get_model_api_key(provider_id: str = "zhipu") -> str | None:
    try:
        keyring = _keyring()
        value = keyring.get_password(SERVICE_NAME, _username(provider_id))
        if value is None and provider_id == "zhipu":
            # Read old installations without forcing an immediate credential migration.
            return keyring.get_password(SERVICE_NAME, LEGACY_USERNAME)
        return value
    except CredentialStoreUnavailable:
        raise
    except Exception as exc:
        raise CredentialStoreUnavailable("读取系统凭据存储失败。") from exc


def get_legacy_model_api_key(provider_id: str = "zhipu") -> str | None:
    """Read an API key from the previous shared credential namespace."""
    try:
        keyring = _keyring()
        value = keyring.get_password(LEGACY_SERVICE_NAME, _username(provider_id))
        if value is None and provider_id == "zhipu":
            return keyring.get_password(LEGACY_SERVICE_NAME, LEGACY_USERNAME)
        return value
    except CredentialStoreUnavailable:
        raise
    except Exception as exc:
        raise CredentialStoreUnavailable("读取旧版系统凭据失败。") from exc


def set_model_api_key(value: str, provider_id: str = "zhipu") -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError("API Key 必须为 1 到 4096 个字符。")
    try:
        keyring = _keyring()
        keyring.set_password(SERVICE_NAME, _username(provider_id), value.strip())
        if provider_id == "zhipu":
            try:
                keyring.delete_password(SERVICE_NAME, LEGACY_USERNAME)
            except keyring.errors.PasswordDeleteError:
                pass
    except CredentialStoreUnavailable:
        raise
    except Exception as exc:
        raise CredentialStoreUnavailable("写入系统凭据存储失败。") from exc


def delete_model_api_key(provider_id: str = "zhipu") -> None:
    keyring = _keyring()
    try:
        keyring.delete_password(SERVICE_NAME, _username(provider_id))
    except keyring.errors.PasswordDeleteError:
        if provider_id != "zhipu":
            return
    except Exception as exc:
        raise CredentialStoreUnavailable("删除系统凭据失败。") from exc
    if provider_id == "zhipu":
        try:
            keyring.delete_password(SERVICE_NAME, LEGACY_USERNAME)
        except keyring.errors.PasswordDeleteError:
            pass
        except Exception as exc:
            raise CredentialStoreUnavailable("删除系统凭据失败。") from exc


def get_model_profile_api_key(credential_id: str) -> str | None:
    try:
        keyring = _keyring()
        return keyring.get_password(SERVICE_NAME, _profile_username(credential_id))
    except CredentialStoreUnavailable:
        raise
    except Exception as exc:
        raise CredentialStoreUnavailable("读取系统凭据存储失败。") from exc


def get_legacy_model_profile_api_key(credential_id: str) -> str | None:
    """Read one profile-scoped key from the previous shared namespace."""
    try:
        keyring = _keyring()
        return keyring.get_password(LEGACY_SERVICE_NAME, _profile_username(credential_id))
    except CredentialStoreUnavailable:
        raise
    except Exception as exc:
        raise CredentialStoreUnavailable("读取旧版配置凭据失败。") from exc


def set_model_profile_api_key(value: str, credential_id: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError("API Key 必须为 1 到 4096 个字符。")
    try:
        keyring = _keyring()
        keyring.set_password(SERVICE_NAME, _profile_username(credential_id), value.strip())
    except CredentialStoreUnavailable:
        raise
    except Exception as exc:
        raise CredentialStoreUnavailable("写入系统凭据失败。") from exc


def delete_model_profile_api_key(credential_id: str) -> None:
    keyring = _keyring()
    try:
        keyring.delete_password(SERVICE_NAME, _profile_username(credential_id))
    except CredentialStoreUnavailable:
        raise
    except Exception as exc:
        if isinstance(exc, keyring.errors.PasswordDeleteError):
            return
        raise CredentialStoreUnavailable("删除系统凭据失败。") from exc


def get_article_platform_secret(platform_id: str, field: str, account_id: str | None = None) -> str | None:
    keyring = _keyring()
    username = _article_platform_username(platform_id, field, account_id)
    try:
        return keyring.get_password(SERVICE_NAME, username)
    except CredentialStoreUnavailable:
        raise
    except Exception as exc:
        raise CredentialStoreUnavailable("读取平台发布凭据失败。") from exc


def set_article_platform_secret(value: str, platform_id: str, field: str, account_id: str | None = None) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError("平台凭据必须为 1 到 4096 个字符。")
    keyring = _keyring()
    username = _article_platform_username(platform_id, field, account_id)
    try:
        keyring.set_password(SERVICE_NAME, username, value.strip())
    except CredentialStoreUnavailable:
        raise
    except Exception as exc:
        raise CredentialStoreUnavailable("写入平台发布凭据失败。") from exc


def delete_article_platform_secret(platform_id: str, field: str, account_id: str | None = None) -> None:
    keyring = _keyring()
    username = _article_platform_username(platform_id, field, account_id)
    try:
        keyring.delete_password(SERVICE_NAME, username)
    except CredentialStoreUnavailable:
        raise
    except Exception as exc:
        if isinstance(exc, keyring.errors.PasswordDeleteError):
            return
        raise CredentialStoreUnavailable("删除平台发布凭据失败。") from exc


def get_article_image_api_key() -> str | None:
    try:
        keyring = _keyring()
        return keyring.get_password(SERVICE_NAME, ARTICLE_IMAGE_USERNAME)
    except CredentialStoreUnavailable:
        raise
    except Exception as exc:
        raise CredentialStoreUnavailable("读取图片模型凭据失败。") from exc


def set_article_image_api_key(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError("图片模型 API Key 必须为 1 到 4096 个字符。")
    try:
        _keyring().set_password(SERVICE_NAME, ARTICLE_IMAGE_USERNAME, value.strip())
    except CredentialStoreUnavailable:
        raise
    except Exception as exc:
        raise CredentialStoreUnavailable("写入图片模型凭据失败。") from exc


def delete_article_image_api_key() -> None:
    keyring = _keyring()
    try:
        keyring.delete_password(SERVICE_NAME, ARTICLE_IMAGE_USERNAME)
    except CredentialStoreUnavailable:
        raise
    except Exception as exc:
        if isinstance(exc, keyring.errors.PasswordDeleteError):
            return
        raise CredentialStoreUnavailable("删除图片模型凭据失败。") from exc


def _username(provider_id: str) -> str:
    if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 64:
        raise ValueError("模型服务商标识格式无效。")
    if not all(ch.isascii() and (ch.isalnum() or ch in "._-") for ch in provider_id):
        raise ValueError("模型服务商标识格式无效。")
    return f"{USERNAME_PREFIX}{provider_id}"


def _profile_username(credential_id: str) -> str:
    if not isinstance(credential_id, str) or not credential_id or len(credential_id) > 64:
        raise ValueError("模型配置凭据标识格式无效。")
    if not all(ch.isascii() and (ch.isalnum() or ch in "._-") for ch in credential_id):
        raise ValueError("模型配置凭据标识格式无效。")
    return f"{PROFILE_USERNAME_PREFIX}{credential_id}"


def _article_platform_username(platform_id: str, field: str, account_id: str | None = None) -> str:
    if platform_id not in _ARTICLE_PLATFORM_CREDENTIAL_KEYS or field not in _ARTICLE_PLATFORM_CREDENTIAL_KEYS[platform_id]:
        raise ValueError("平台凭据字段无效。")
    if account_id is not None:
        if not isinstance(account_id, str) or not account_id or len(account_id) > 64:
            raise ValueError("平台账号标识无效。")
        if not all(ch.isascii() and (ch.isalnum() or ch in "._-") for ch in account_id):
            raise ValueError("平台账号标识无效。")
        return f"{ARTICLE_PLATFORM_USERNAME_PREFIX}{platform_id}:{account_id}:{field}"
    return f"{ARTICLE_PLATFORM_USERNAME_PREFIX}{platform_id}:{field}"
