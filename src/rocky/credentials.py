"""Provider credentials outside the database, workflow history and HTTP responses.

macOS uses the login Keychain through Security.framework. Other platforms use a
private local file so CLI deployments remain supported; the API reports which
storage mechanism is active. No subprocess ever receives a secret argument.
"""

import ctypes
import json
import os
import stat
import sys
import tempfile
from pathlib import Path


class CredentialError(RuntimeError):
    pass


class CredentialStore:
    service = b"com.rocky.agent.models"

    def __init__(self, path: Path):
        self.path = path
        self.storage = "macos-keychain" if sys.platform == "darwin" else "private-file"

    def _keychain(self, account: str, value: str | None = None, *, write: bool = False):
        # Modern SecItem APIs avoid deprecated keychain search handles and let
        # the OS own encryption and access control. Query values remain alive
        # until the synchronous call returns; no secret enters argv or a log.
        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        pointer = ctypes.c_void_p
        core.CFStringCreateWithCString.argtypes = [pointer, ctypes.c_char_p, ctypes.c_uint32]
        core.CFStringCreateWithCString.restype = pointer
        core.CFDictionaryCreateMutable.argtypes = [pointer, ctypes.c_long, pointer, pointer]
        core.CFDictionaryCreateMutable.restype = pointer
        core.CFDictionarySetValue.argtypes = [pointer, pointer, pointer]
        core.CFDataCreate.argtypes = [pointer, ctypes.c_char_p, ctypes.c_long]
        core.CFDataCreate.restype = pointer
        core.CFDataGetLength.argtypes = [pointer]
        core.CFDataGetLength.restype = ctypes.c_long
        core.CFDataGetBytePtr.argtypes = [pointer]
        core.CFDataGetBytePtr.restype = pointer
        core.CFRelease.argtypes = [pointer]
        security.SecItemCopyMatching.argtypes = [pointer, ctypes.POINTER(pointer)]
        security.SecItemCopyMatching.restype = ctypes.c_int32
        security.SecItemAdd.argtypes = [pointer, pointer]
        security.SecItemAdd.restype = ctypes.c_int32
        security.SecItemUpdate.argtypes = [pointer, pointer]
        security.SecItemUpdate.restype = ctypes.c_int32
        security.SecItemDelete.argtypes = [pointer]
        security.SecItemDelete.restype = ctypes.c_int32
        owned = []

        def constant(name):
            return pointer.in_dll(security, name).value

        def string(text):
            result = core.CFStringCreateWithCString(None, text.encode(), 0x08000100)
            owned.append(result)
            return result

        def dictionary(values):
            result = core.CFDictionaryCreateMutable(None, 0, None, None)
            owned.append(result)
            for name, entry in values.items():
                core.CFDictionarySetValue(result, constant(name), entry)
            return result

        try:
            query_values = {
                "kSecClass": constant("kSecClassGenericPassword"),
                "kSecAttrService": string(self.service.decode()),
                "kSecAttrAccount": string(account),
            }
            if not write:
                query_values["kSecReturnData"] = pointer.in_dll(core, "kCFBooleanTrue").value
            query = dictionary(query_values)
            if not write:
                result = pointer()
                status = security.SecItemCopyMatching(query, ctypes.byref(result))
                if status == -25300:
                    return None
                if status != 0:
                    raise CredentialError("macOS Keychain is unavailable or access was denied")
                owned.append(result.value)
                return ctypes.string_at(
                    core.CFDataGetBytePtr(result), core.CFDataGetLength(result)
                ).decode()
            if value is None:
                status = security.SecItemDelete(query)
                if status not in (0, -25300):
                    raise CredentialError("Could not remove the saved model credential")
                return
            encoded = value.encode()
            data = core.CFDataCreate(None, encoded, len(encoded))
            owned.append(data)
            attributes = dictionary({"kSecValueData": data})
            status = security.SecItemUpdate(query, attributes)
            if status == -25300:
                core.CFDictionarySetValue(query, constant("kSecValueData"), data)
                status = security.SecItemAdd(query, None)
            if status != 0:
                raise CredentialError("Could not save the model credential in macOS Keychain")
        finally:
            for item in reversed(owned):
                if item:
                    core.CFRelease(item)

    def _load(self) -> dict[str, str]:
        try:
            if self.path.is_symlink():
                raise CredentialError("Credential storage must not be a symbolic link")
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "r") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise CredentialError("Credential storage must be a regular file without links")
                if info.st_mode & 0o077:
                    raise CredentialError("Credential file permissions must be 0600")
                content = stream.read(1_000_001)
                if len(content) > 1_000_000:
                    raise CredentialError("Credential storage exceeds its size limit")
                values = json.loads(content)
            if not isinstance(values, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in values.items()
            ):
                raise ValueError
            return values
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise CredentialError("Could not read private credential storage") from None

    def get(self, account: str) -> str | None:
        if self.storage == "macos-keychain":
            return self._keychain(account)
        return self._load().get(account)

    def set(self, account: str, value: str | None) -> None:
        if self.storage == "macos-keychain":
            self._keychain(account, value, write=True)
            return
        values = self._load()
        if value is None:
            values.pop(account, None)
        else:
            values[account] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".rocky-credentials-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(values, stream)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
