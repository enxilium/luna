import asyncio
import copy
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from src.integrations.auth import AuthFailure, AuthRequired, KeychainTokenStorage


class KeychainTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        names = (
            "kSecClass", "kSecClassGenericPassword", "kSecAttrService", "kSecAttrAccount",
            "kSecReturnData",
            "kSecMatchLimit", "kSecMatchLimitOne", "kSecValueData",
        )
        self.security = SimpleNamespace(
            **{name: name for name in names}, errSecSuccess=0, errSecItemNotFound=-25300,
            errSecInteractionNotAllowed=-25308, errSecAuthFailed=-25293,
            SecItemCopyMatching=Mock(), SecItemUpdate=Mock(return_value=0), SecItemAdd=Mock(),
            SecKeychainGetUserInteractionAllowed=Mock(return_value=(0, True)),
            SecKeychainSetUserInteractionAllowed=Mock(return_value=0),
        )
        modules = patch.dict(sys.modules, {
            "Security": self.security,
            "Foundation": SimpleNamespace(NSData=SimpleNamespace(dataWithBytes_length_=lambda data, _: data)),
        })
        modules.start()
        self.addCleanup(modules.stop)
        self.storage = KeychainTokenStorage("https://example.com/mcp")
        self.record = {
            "server_url": self.storage.server_url,
            "client_info": {"client_id": "client"},
            "tokens": {"access_token": "old", "token_type": "Bearer"},
            "expires_at": 123, "authorization_server": "https://issuer.example",
        }
        self.security.SecItemCopyMatching.side_effect = lambda *_: (0, json.dumps(self.record).encode())

    async def test_reads_share_record_and_updates_preserve_client_and_metadata(self):
        token, client = await asyncio.gather(self.storage.get_tokens(), self.storage.get_client_info())
        self.assertEqual(token.access_token, "old")
        self.assertEqual(client.client_id, "client")
        await self.storage.set_tokens(OAuthToken(access_token="new", token_type="Bearer", expires_in=3600))
        self.assertEqual((await self.storage.get_tokens()).access_token, "new")
        await self.storage.clear_tokens()
        self.assertIsNone(await self.storage.get_tokens())
        self.security.SecItemCopyMatching.assert_called_once()
        payload = json.loads(self.security.SecItemUpdate.call_args.args[1]["kSecValueData"])
        self.assertEqual(payload["client_info"], self.record["client_info"])
        self.assertEqual(payload["authorization_server"], self.record["authorization_server"])
        self.assertIsNone(payload["expires_at"])
        self.assertEqual(payload["server_url"], self.storage.server_url)

    async def test_background_access_requires_explicit_approval_without_caching_failure(self):
        self.security.SecItemCopyMatching.return_value = (-25308, None)
        self.security.SecItemCopyMatching.side_effect = None
        with self.assertRaisesRegex(AuthRequired, "keychain_access_required"):
            await self.storage.get_tokens()
        self.assertEqual(
            [call.args[0] for call in self.security.SecKeychainSetUserInteractionAllowed.call_args_list],
            [False, True],
        )
        self.security.SecItemUpdate.assert_not_called()
        self.security.SecItemAdd.assert_not_called()
        self.security.SecItemCopyMatching.return_value = (0, json.dumps(self.record).encode())
        interaction = self.storage._interactive.set(True)
        try:
            self.assertEqual((await self.storage.get_client_info()).client_id, "client")
        finally:
            self.storage._interactive.reset(interaction)
        self.assertEqual(
            [call.args[0] for call in self.security.SecKeychainSetUserInteractionAllowed.call_args_list],
            [False, True, True, True],
        )
        self.assertFalse(self.storage._interactive.get())

    async def test_failed_write_keeps_previous_record(self):
        await self.storage.get_tokens()
        before = copy.deepcopy(self.storage._record)
        self.security.SecItemUpdate.return_value = -25308
        with self.assertRaises(AuthRequired):
            await self.storage.clear_tokens()
        self.assertEqual(self.storage._record, before)
        self.assertEqual((await self.storage.get_tokens()).access_token, "old")

    async def test_legacy_auth_failure_requires_approval_only_for_background_access(self):
        self.security.SecItemCopyMatching.side_effect = None
        self.security.SecItemCopyMatching.return_value = (-25293, None)
        with self.assertRaises(AuthRequired):
            await self.storage.get_tokens()
        interaction = self.storage._interactive.set(True)
        try:
            with self.assertRaises(AuthFailure):
                await self.storage.get_tokens()
        finally:
            self.storage._interactive.reset(interaction)
        self.assertIsNone(self.storage._record)

    async def test_native_exception_restores_previous_interaction_setting(self):
        self.security.SecKeychainGetUserInteractionAllowed.return_value = (0, False)
        self.security.SecItemCopyMatching.side_effect = RuntimeError("native failure")
        with self.assertRaises(RuntimeError):
            await self.storage.get_tokens()
        self.assertEqual(
            [call.args[0] for call in self.security.SecKeychainSetUserInteractionAllowed.call_args_list],
            [False, False],
        )

    async def test_missing_record_is_cached_and_created_without_duplicate_read(self):
        self.security.SecItemCopyMatching.side_effect = None
        self.security.SecItemCopyMatching.return_value = (-25300, None)
        self.security.SecItemUpdate.return_value = -25300
        self.security.SecItemAdd.return_value = (0, None)
        self.assertIsNone(await self.storage.get_tokens())
        self.assertIsNone(await self.storage.get_client_info())
        await self.storage.set_client_info(OAuthClientInformationFull(client_id="new-client"))
        self.assertEqual((await self.storage.get_client_info()).client_id, "new-client")
        self.security.SecItemCopyMatching.assert_called_once()
        self.security.SecItemAdd.assert_called_once()

    async def test_binding_mismatch_is_not_cached_or_overwritten(self):
        self.record["server_url"] = "https://wrong.example/mcp"
        with self.assertRaisesRegex(AuthFailure, "credential_binding_mismatch"):
            await self.storage.get_tokens()
        self.assertIsNone(self.storage._record)
        self.security.SecItemUpdate.assert_not_called()

