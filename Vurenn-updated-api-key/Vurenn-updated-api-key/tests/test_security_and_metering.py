import unittest
import base64
import io
import json
import threading
from datetime import datetime, timezone
from unittest.mock import Mock, patch

import wsgi
from PIL import Image


class SecurityAndMeteringTests(unittest.TestCase):
    def setUp(self):
        with wsgi._request_cache_lock:
            wsgi._auth_cache.clear()
            wsgi._beta_access_cache.clear()
            wsgi._construction_mode_cache.update(value=False, expires_at=0.0)

    def test_authentication_reuses_the_supabase_connection_pool(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "id": "11111111-1111-1111-1111-111111111111",
            "email": "guest@example.com",
        }
        with wsgi.app.test_request_context(
            "/v1/chat/stream",
            headers={"Authorization": "Bearer test-token"},
        ), patch.object(wsgi, "supabase_configured", return_value=True), patch.object(
            wsgi.SUPABASE_HTTP, "get", return_value=response
        ) as pooled_get, patch(
            "wsgi.requests.get"
        ) as unpooled_get:
            user = wsgi.authenticate()
            cached_user = wsgi.authenticate()
        self.assertEqual(user["email"], "guest@example.com")
        self.assertEqual(cached_user["id"], user["id"])
        pooled_get.assert_called_once()
        unpooled_get.assert_not_called()

    def test_construction_and_beta_access_checks_are_cached(self):
        def database(method, path, **kwargs):
            if path == "app_settings":
                return [{"value": {"enabled": True}}]
            if path == "profiles":
                return [{"beta_access": True}]
            return []

        with patch.object(wsgi, "supabase_request", side_effect=database) as request_mock:
            self.assertTrue(wsgi.construction_mode_enabled())
            self.assertTrue(wsgi.construction_mode_enabled())
            self.assertTrue(wsgi.has_private_beta_access("user-1"))
            self.assertTrue(wsgi.has_private_beta_access("user-1"))

        app_settings_calls = [
            call for call in request_mock.call_args_list
            if call.args[:2] == ("GET", "app_settings")
        ]
        profile_calls = [
            call for call in request_mock.call_args_list
            if call.args[:2] == ("GET", "profiles")
        ]
        self.assertEqual(len(app_settings_calls), 1)
        self.assertEqual(len(profile_calls), 1)

    def test_fast_model_does_not_retry_the_same_overloaded_provider(self):
        fast_model = wsgi.MODEL_CATALOG["vurenn-fast"]["provider_model"]
        self.assertEqual(wsgi.provider_model_attempts(fast_model), [fast_model])

    def test_balanced_everyday_turns_use_the_low_latency_provider(self):
        fast_model = wsgi.MODEL_CATALOG["vurenn-fast"]["provider_model"]
        balanced_model = wsgi.MODEL_CATALOG["vurenn"]["provider_model"]
        self.assertEqual(
            wsgi.provider_model_for_turn("vurenn", "How are you today?"),
            fast_model,
        )
        self.assertEqual(
            wsgi.provider_model_for_turn(
                "vurenn", "Analyze this business plan in detail"
            ),
            balanced_model,
        )
        self.assertEqual(
            wsgi.provider_model_for_turn(
                "vurenn", "What is new?", requested_tools=["web_search"]
            ),
            balanced_model,
        )

    def test_balanced_history_is_bounded_for_low_latency(self):
        history = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": "x" * 2000}
            for index in range(30)
        ]
        trimmed = wsgi.trim_conversation_history(history, "vurenn")
        self.assertLessEqual(len(trimmed), 16)
        self.assertLessEqual(sum(len(item["content"]) for item in trimmed), 16_000)

    def test_credit_balance_lookup_is_only_needed_for_balance_questions(self):
        self.assertFalse(wsgi.credit_balance_requested("Help me write a short email"))
        self.assertTrue(wsgi.credit_balance_requested("How many credits do I have left?"))
        self.assertTrue(wsgi.credit_balance_requested("What's my token balance?"))

    def test_chat_stream_starts_before_database_persistence(self):
        user = {
            "id": "11111111-1111-1111-1111-111111111111",
            "email": "guest@example.com",
        }
        database_calls = []
        completed_writes = []
        release_writes = threading.Event()

        def database(method, path, **kwargs):
            database_calls.append((method, path))
            if method == "GET":
                return []
            release_writes.wait(timeout=1)
            completed_writes.append((method, path))
            return None

        with patch.object(wsgi, "authenticate", return_value=user), patch.object(
            wsgi, "construction_mode_enabled", return_value=False
        ) as construction_check, patch.object(
            wsgi, "user_plan", return_value="premier"
        ), patch.object(
            wsgi, "get_owned_conversation", return_value={
                "id": "conversation-1",
                "title": "Existing conversation",
                "project_id": None,
            }
        ), patch.object(
            wsgi, "supabase_request", side_effect=database
        ), patch.object(
            wsgi, "local_utility_response", return_value="Immediate answer"
        ), patch.object(wsgi, "get_credit_account") as balance_lookup:
            response = wsgi.app.test_client().post(
                "/v1/chat/stream",
                json={
                    "conversation_id": "conversation-1",
                    "message": "Hello",
                    "model": "vurenn-fast",
                },
                headers={"Authorization": "Bearer test-token"},
                buffered=False,
            )
            first_event = next(iter(response.response)).decode("utf-8")
            writes_completed_before_second_event = list(completed_writes)
            release_writes.set()
            remaining = b"".join(response.response).decode("utf-8")

        self.assertIn("event: message_started", first_event)
        self.assertRegex(response.headers.get("Server-Timing", ""), r"^preflight;dur=\d")
        self.assertEqual(writes_completed_before_second_event, [])
        self.assertIn("event: message_completed", remaining)
        self.assertEqual(construction_check.call_count, 1)
        balance_lookup.assert_not_called()
        self.assertEqual(database_calls.count(("POST", "messages")), 2)
        self.assertEqual(database_calls.count(("PATCH", "conversations")), 1)

    def test_mobile_audio_metadata_matches_the_recorded_container(self):
        mobile_audio = b"\x00\x00\x00\x18ftypM4A " + b"audio"
        upload = Mock(mimetype="audio/mp4;codecs=mp4a.40.2")
        self.assertEqual(
            wsgi.openai_audio_upload_metadata(upload, mobile_audio),
            ("voice.m4a", "audio/mp4"),
        )

        webm_audio = b"\x1a\x45\xdf\xa3" + b"audio"
        upload = Mock(mimetype="application/octet-stream")
        self.assertEqual(
            wsgi.openai_audio_upload_metadata(upload, webm_audio),
            ("voice.webm", "audio/webm"),
        )

    def test_basic_text_chat_is_not_double_charged_from_credit_wallet(self):
        self.assertFalse(wsgi.basic_chat_requires_credits())
        self.assertTrue(wsgi.basic_chat_requires_credits(image_request=True))
        self.assertTrue(
            wsgi.basic_chat_requires_credits(tool_feature_ids=["web_search"])
        )

    def test_basic_usage_window_is_server_counted(self):
        now = datetime(2026, 8, 9, 20, 0, tzinfo=timezone.utc)
        rows = [
            {"created_at": "2026-08-09T16:00:00+00:00"}
            for _ in range(wsgi.FREE_CHAT_MESSAGES_PER_WINDOW)
        ]
        with patch.object(wsgi, "supabase_request", return_value=rows):
            usage = wsgi.basic_chat_usage("11111111-1111-1111-1111-111111111111", now=now)
        self.assertTrue(usage["exhausted"])
        self.assertEqual(usage["remaining"], 0)
        self.assertEqual(usage["reset_at"], "2026-08-09T21:00:00+00:00")

    def test_basic_usage_window_clears_after_reset(self):
        now = datetime(2026, 8, 9, 21, 0, 1, tzinfo=timezone.utc)
        with patch.object(wsgi, "supabase_request", return_value=[]) as database:
            usage = wsgi.basic_chat_usage("11111111-1111-1111-1111-111111111111", now=now)

        self.assertFalse(usage["exhausted"])
        self.assertEqual(usage["used"], 0)
        self.assertEqual(usage["remaining"], wsgi.FREE_CHAT_MESSAGES_PER_WINDOW)
        self.assertIsNone(usage["reset_at"])
        self.assertEqual(
            database.call_args.kwargs["params"]["created_at"],
            "gte.2026-08-09T16:00:01+00:00",
        )

    def test_basic_chat_limit_blocks_before_provider_work(self):
        user = {"id": "11111111-1111-1111-1111-111111111111", "email": "guest@example.com"}
        exhausted = {
            "limit": 20,
            "used": 20,
            "remaining": 0,
            "window_hours": 5,
            "exhausted": True,
            "reset_at": "2026-08-09T21:00:00+00:00",
        }
        with patch.object(wsgi, "authenticate", return_value=user), patch.object(
            wsgi, "construction_mode_enabled", return_value=False
        ), patch.object(
            wsgi, "basic_chat_usage", return_value=exhausted
        ), patch.object(
            wsgi,
            "get_owned_conversation",
            return_value={
                "id": "conversation-1",
                "title": "Existing conversation",
                "project_id": None,
            },
        ) as conversation_lookup, patch.object(
            wsgi, "supabase_request", return_value=[]
        ):
            response = wsgi.app.test_client().post(
                "/v1/chat/stream",
                json={
                    "conversation_id": "conversation-1",
                    "message": "Hello",
                    "model": "vurenn-fast",
                },
                headers={"Authorization": "Bearer test-token"},
            )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.get_json()["code"], "basic_usage_limit_reached")
        conversation_lookup.assert_called_once()

    def test_private_beta_access_code_is_server_validated(self):
        code = "VUR-ABCD-EFGH-JKLM"
        configured_hash = wsgi.private_beta_access_code_hash(code)

        def database(method, path, **kwargs):
            if path == "private_beta_invites" and kwargs.get("params", {}).get("token_hash") == f"eq.{configured_hash}":
                return [{"id": "code-1", "label": "Guest"}]
            return []

        with patch.object(wsgi, "access_code_rate_limited", return_value=False), patch.object(
            wsgi, "supabase_request", side_effect=database
        ):
            accepted = wsgi.app.test_client().post(
                "/v1/access-code/validate", json={"code": code.lower()}
            )
            rejected = wsgi.app.test_client().post(
                "/v1/access-code/validate", json={"code": "wrong-code"}
            )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(rejected.status_code, 403)

    def test_private_beta_access_code_is_consumed_by_first_account(self):
        code = "VUR-ABCD-EFGH-JKLM"
        first_user = {"id": "11111111-1111-1111-1111-111111111111", "email": "first@example.com"}
        second_user = {"id": "22222222-2222-2222-2222-222222222222", "email": "second@example.com"}
        claimed = False

        def database(method, path, **kwargs):
            nonlocal claimed
            if method == "POST" and path == "rpc/redeem_private_beta_invite":
                if claimed:
                    return {"ok": False, "code": "invite_full"}
                claimed = True
                return {"ok": True, "label": "Guest"}
            if method == "GET" and path == "private_beta_invites":
                return [{"id": "code-1", "label": "Guest", "use_count": 1}]
            return []

        with patch.object(wsgi, "authenticate", return_value=first_user), patch.object(
            wsgi, "construction_mode_enabled", return_value=True
        ), patch.object(wsgi, "supabase_request", side_effect=database) as mocked_database:
            response = wsgi.app.test_client().post(
                "/v1/access-code/redeem", json={"code": code}
            )
        self.assertEqual(response.status_code, 200)
        profile_update = next(
            call for call in mocked_database.call_args_list
            if call.args[:2] == ("PATCH", "profiles")
        )
        self.assertTrue(profile_update.kwargs["body"]["beta_access"])

        with patch.object(wsgi, "authenticate", return_value=second_user), patch.object(
            wsgi, "construction_mode_enabled", return_value=True
        ), patch.object(wsgi, "supabase_request", side_effect=database):
            rejected = wsgi.app.test_client().post(
                "/v1/access-code/redeem", json={"code": code}
            )
        self.assertEqual(rejected.status_code, 403)

    def test_private_beta_invitation_is_redeemed_without_exposing_raw_token(self):
        user_id = "11111111-1111-1111-1111-111111111111"
        raw_token = "private-beta-token-" + "x" * 32

        def database(method, path, **kwargs):
            if path == "rpc/redeem_private_beta_invite":
                self.assertNotEqual(kwargs["body"]["p_token_hash"], raw_token)
                self.assertEqual(len(kwargs["body"]["p_token_hash"]), 64)
                return {"ok": True, "label": "Grandma"}
            return []

        with patch.object(
            wsgi, "authenticate", return_value={"id": user_id, "email": "grandma@example.com"}
        ), patch.object(wsgi, "supabase_request", side_effect=database):
            response = wsgi.app.test_client().post(
                "/v1/invites/redeem",
                json={"token": raw_token},
                headers={"Authorization": "Bearer test-token"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["redeemed"])

    def test_private_beta_access_bypasses_construction_mode(self):
        user = {"id": "11111111-1111-1111-1111-111111111111", "email": "guest@example.com"}
        with patch.object(wsgi, "has_private_beta_access", return_value=True):
            self.assertTrue(wsgi.can_bypass_maintenance(user, user["id"]))

    def test_signup_metadata_recovers_invite_after_email_confirmation(self):
        user = {
            "id": "11111111-1111-1111-1111-111111111111",
            "email": "guest@example.com",
            "user_metadata": {"beta_invite_token_hash": "a" * 64},
        }
        with patch.object(
            wsgi, "has_private_beta_access", return_value=False
        ), patch.object(
            wsgi,
            "supabase_request",
            return_value={"ok": True, "label": "Family & friends beta"},
        ) as database:
            self.assertTrue(wsgi.can_bypass_maintenance(user, user["id"]))
        redemption = database.call_args
        self.assertEqual(redemption.args[:2], ("POST", "rpc/redeem_private_beta_invite"))
        self.assertEqual(redemption.kwargs["body"]["p_token_hash"], "a" * 64)

    def test_authenticated_user_without_invite_is_blocked_during_private_beta(self):
        user = {
            "id": "11111111-1111-1111-1111-111111111111",
            "email": "uninvited@example.com",
        }
        with patch.object(wsgi, "authenticate", return_value=user), patch.object(
            wsgi, "construction_mode_enabled", return_value=True
        ), patch.object(wsgi, "can_bypass_maintenance", return_value=False):
            response = wsgi.app.test_client().get(
                "/v1/credits",
                headers={"Authorization": "Bearer test-token"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.get_json()["code"],
            "private_beta_invite_required",
        )

    def test_current_legal_consent_is_versioned_and_recorded_privately(self):
        user_id = "11111111-1111-1111-1111-111111111111"
        payload = {
            "policy_version": wsgi.LEGAL_POLICY_VERSION,
            "accepted_at": "2026-08-06T15:30:00Z",
            "terms_accepted": True,
            "privacy_accepted": True,
            "acceptable_use_accepted": True,
            "age_confirmed": True,
        }
        with patch.object(
            wsgi, "authenticate", return_value={"id": user_id, "email": "user@example.com"}
        ), patch.object(wsgi, "supabase_request", return_value=[]) as database:
            response = wsgi.app.test_client().post(
                "/v1/legal/consent",
                json=payload,
                headers={"Authorization": "Bearer test-token"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["accepted"])
        consent_call = next(
            call for call in database.call_args_list if call.args[1] == "legal_consents"
        )
        self.assertEqual(consent_call.kwargs["body"]["user_id"], user_id)
        self.assertEqual(
            consent_call.kwargs["body"]["policy_version"],
            wsgi.LEGAL_POLICY_VERSION,
        )

    def test_legal_consent_rejects_missing_age_eligibility_confirmation(self):
        user_id = "11111111-1111-1111-1111-111111111111"
        with patch.object(
            wsgi, "authenticate", return_value={"id": user_id, "email": "user@example.com"}
        ):
            response = wsgi.app.test_client().post(
                "/v1/legal/consent",
                json={
                    "policy_version": wsgi.LEGAL_POLICY_VERSION,
                    "accepted_at": "2026-08-06T15:30:00Z",
                    "terms_accepted": True,
                    "privacy_accepted": True,
                    "acceptable_use_accepted": True,
                    "age_confirmed": False,
                },
                headers={"Authorization": "Bearer test-token"},
            )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.get_json()["code"], "age_eligibility_required")

    def test_generated_image_links_are_signed(self):
        with patch.object(wsgi, "GENERATED_IMAGE_SIGNING_SECRET", "test-secret"):
            first = wsgi.generated_image_token(
                "11111111-1111-1111-1111-111111111111/" + "a" * 32 + ".webp"
            )
            second = wsgi.generated_image_token(
                "11111111-1111-1111-1111-111111111111/" + "b" * 32 + ".webp"
            )
        self.assertEqual(len(first), 64)
        self.assertNotEqual(first, second)

    def test_generated_image_edit_url_is_bound_to_its_owner(self):
        owner = "11111111-1111-1111-1111-111111111111"
        object_path = f"{owner}/{'a' * 32}.webp"
        with patch.object(wsgi, "GENERATED_IMAGE_SIGNING_SECRET", "test-secret"):
            token = wsgi.generated_image_token(object_path)
            url = f"https://api.vurenn.com/v1/generated-images/{object_path}?token={token}"
            self.assertEqual(
                wsgi.generated_image_object_from_url(url, owner),
                object_path,
            )
            self.assertIsNone(
                wsgi.generated_image_object_from_url(
                    url,
                    "22222222-2222-2222-2222-222222222222",
                )
            )

    def test_image_edit_uses_server_key_and_matching_png_files(self):
        source_buffer = io.BytesIO()
        Image.new("RGB", (16, 16), "navy").save(source_buffer, format="WEBP")
        mask_buffer = io.BytesIO()
        Image.new("RGBA", (16, 16), (255, 255, 255, 0)).save(
            mask_buffer,
            format="PNG",
        )
        provider_response = Mock(
            status_code=200,
            json=lambda: {
                "data": [
                    {"b64_json": base64.b64encode(b"edited-webp").decode("ascii")}
                ]
            },
        )
        with patch.object(wsgi, "OPENAI_IMAGE_API_KEY", "server-image-key"), patch.object(
            wsgi.requests,
            "post",
            return_value=provider_response,
        ) as post:
            result = wsgi.edit_image_bytes(
                source_buffer.getvalue(),
                mask_buffer.getvalue(),
                "Add a warm sunset",
            )
        self.assertEqual(result, b"edited-webp")
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer server-image-key",
        )
        self.assertEqual(post.call_args.kwargs["files"]["image[]"][2], "image/png")
        self.assertEqual(post.call_args.kwargs["files"]["mask"][2], "image/png")
        self.assertNotIn("server-image-key", str(post.call_args.kwargs["data"]))

    def test_image_generation_uses_only_the_server_image_key(self):
        provider_response = Mock(
            status_code=200,
            json=lambda: {
                "data": [
                    {"b64_json": base64.b64encode(b"webp-bytes").decode("ascii")}
                ]
            },
        )
        with patch.object(wsgi, "OPENAI_IMAGE_API_KEY", "server-image-key"), patch.object(
            wsgi, "GENERATED_IMAGE_SIGNING_SECRET", "test-secret"
        ), patch.object(wsgi, "SUPABASE_URL", "https://example.supabase.co"), patch.object(
            wsgi, "SUPABASE_SERVICE_ROLE_KEY", "service-role"
        ), patch.object(wsgi.requests, "post", return_value=provider_response) as post, patch.object(
            wsgi, "store_generated_image", return_value="https://api.example/image"
        ) as store:
            url = wsgi.generate_image("A calm mountain at dawn", "user-1")

        self.assertEqual(url, "https://api.example/image")
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer server-image-key",
        )
        self.assertEqual(post.call_args.kwargs["json"]["model"], "gpt-image-2")
        self.assertNotIn("server-image-key", str(post.call_args.kwargs["json"]))
        store.assert_called_once_with("user-1", b"webp-bytes")

    def test_image_prompt_is_prepared_without_calling_the_chat_provider(self):
        provider = Mock()
        with patch.object(wsgi, "anthropic_client", provider):
            prompt = wsgi.prepare_image_prompt("  A blue mug on a desk  ")
        self.assertIn("A blue mug on a desk", prompt)
        provider.messages.create.assert_not_called()

    def test_image_provider_reports_key_and_quota_configuration_errors(self):
        unauthorized = Mock(status_code=401, json=lambda: {"error": {}})
        quota = Mock(
            status_code=429,
            json=lambda: {"error": {"code": "insufficient_quota"}},
        )
        self.assertEqual(
            wsgi.image_provider_error_code(unauthorized),
            "IMAGE_PROVIDER_AUTH_ERROR",
        )
        self.assertEqual(
            wsgi.image_provider_error_code(quota),
            "IMAGE_PROVIDER_QUOTA_ERROR",
        )

    def test_image_generation_classifies_provider_safety_block(self):
        provider_response = Mock(
            status_code=400,
            text='{"error":{"code":"moderation_block"}}',
            json=lambda: {
                "error": {
                    "code": "moderation_block",
                    "type": "image_generation_user_error",
                }
            },
        )
        with patch.object(wsgi, "OPENAI_IMAGE_API_KEY", "server-image-key"), patch.object(
            wsgi.requests, "post", return_value=provider_response
        ):
            with self.assertRaises(wsgi.ImageProviderError) as raised:
                wsgi.generate_image_bytes("A benign product photo")
        self.assertEqual(raised.exception.code, "IMAGE_PROVIDER_SAFETY_BLOCK")

    def test_image_generation_retries_temporary_provider_failure(self):
        busy = Mock(
            status_code=503,
            text='{"error":{"message":"busy"}}',
            json=lambda: {"error": {"message": "busy"}},
        )
        success = Mock(
            status_code=200,
            text="",
            json=lambda: {
                "data": [
                    {"b64_json": base64.b64encode(b"webp-bytes").decode("ascii")}
                ]
            },
        )
        with patch.object(wsgi, "OPENAI_IMAGE_API_KEY", "server-image-key"), patch.object(
            wsgi.requests, "post", side_effect=[busy, success]
        ) as post, patch.object(wsgi.time, "sleep"):
            result = wsgi.generate_image_bytes("A blue mug")
        self.assertEqual(result, b"webp-bytes")
        self.assertEqual(post.call_count, 2)

    def test_cloud_voice_uses_only_the_server_voice_key(self):
        provider_response = Mock(status_code=200, content=b"mp3-audio", text="")
        with patch.object(wsgi, "OPENAI_VOICE_API_KEY", "server-voice-key"), patch.object(
            wsgi.requests,
            "post",
            return_value=provider_response,
        ) as post:
            audio = wsgi.synthesize_openai_speech("Hello there", "am_michael")
        self.assertEqual(audio, b"mp3-audio")
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"],
            "Bearer server-voice-key",
        )
        self.assertEqual(post.call_args.kwargs["json"]["voice"], "onyx")
        self.assertNotIn("server-voice-key", str(post.call_args.kwargs["json"]))

    def test_provider_capacity_errors_use_a_fast_fallback(self):
        requested = wsgi.MODEL_CATALOG["vurenn"]["provider_model"]
        attempts = wsgi.provider_model_attempts(requested)
        self.assertEqual(attempts[0], requested)
        self.assertEqual(
            attempts[-1],
            wsgi.MODEL_CATALOG["vurenn-fast"]["provider_model"],
        )

        class OverloadedError(Exception):
            status_code = 529

        self.assertTrue(
            wsgi.is_provider_capacity_error(OverloadedError("Overloaded"))
        )
        self.assertFalse(
            wsgi.is_provider_capacity_error(ValueError("bad request"))
        )

    def test_provider_identity_and_credentials_are_sanitized(self):
        value = wsgi.sanitize_assistant_text(
            "I am Claude from Anthropic. sk_test_abcdefghijklmnop"
        )
        self.assertNotIn("Claude", value)
        self.assertIn("Vurenn", value)
        self.assertIn("[private credential]", value)

    def test_provider_disclosure_is_not_hidden(self):
        value = wsgi.sanitize_assistant_text(
            "Vurenn uses Anthropic APIs for some language capabilities and OpenAI APIs for images."
        )
        self.assertIn("Anthropic APIs", value)
        self.assertIn("OpenAI APIs", value)

    def test_credit_cost_increases_with_usage(self):
        model = wsgi.MODEL_CATALOG["vurenn-max"]
        small = wsgi.credits_for_usage(model, 1_000, 500)
        large = wsgi.credits_for_usage(model, 1_000_000, 50_000)
        self.assertGreater(large, small)
        self.assertGreaterEqual(small, model["base_credits"])

    def test_modes_use_distinct_models_and_costs(self):
        fast = wsgi.MODEL_CATALOG["vurenn-fast"]
        balanced = wsgi.MODEL_CATALOG["vurenn"]
        maximum = wsgi.MODEL_CATALOG["vurenn-max"]
        self.assertNotEqual(fast["provider_model"], balanced["provider_model"])
        self.assertNotEqual(balanced["provider_model"], maximum["provider_model"])
        self.assertLess(fast["base_credits"], balanced["base_credits"])
        self.assertLess(balanced["base_credits"], maximum["base_credits"])

    def test_credit_packs_use_small_denomination(self):
        self.assertEqual(wsgi.CREDIT_PACKS["credits_50"]["credits"], 5_000)
        self.assertEqual(wsgi.CREDIT_PACKS["credits_100"]["credits"], 10_000)

    def test_voice_credits_scale_with_provider_usage(self):
        short = wsgi.voice_credits_for_text("Hello", using_openai=True)
        long = wsgi.voice_credits_for_text("A" * 4_000, using_openai=True)
        self.assertGreater(long, short)
        self.assertGreaterEqual(short, 5)
        self.assertEqual(wsgi.voice_credits_for_text("Hello", using_openai=False), 5)

    def test_research_plan_has_five_useful_fallback_steps(self):
        with patch.object(wsgi, "anthropic_client", None):
            plan = wsgi.build_research_plan("Compare family electric SUVs")
        self.assertEqual(len(plan["steps"]), 5)
        self.assertTrue(plan["title"])

    def test_research_tools_are_connected(self):
        provider_tools, _, feature_ids = wsgi.selected_tool_configuration(
            ["web_search", "deep_research", "data_analysis"]
        )
        provider_types = {tool["type"] for tool in provider_tools}
        self.assertIn("web_search_20260318", provider_types)
        self.assertIn("code_execution_20260521", provider_types)
        self.assertIn("deep_research", feature_ids)
        web_tool = next(tool for tool in provider_tools if tool["type"] == "web_search_20260318")
        self.assertEqual(web_tool["max_uses"], 20)
        self.assertEqual(web_tool["allowed_callers"], ["direct"])
        self.assertTrue(
            all(tool.get("allowed_callers") == ["direct"] for tool in provider_tools)
        )

    def test_excel_workbooks_are_routed_to_sandboxed_analysis(self):
        excel_type = (
            "application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet"
        )
        self.assertIn(excel_type, wsgi.ALLOWED_FILE_TYPES)
        self.assertIn(excel_type, wsgi.CODE_EXECUTION_FILE_TYPES)
        self.assertEqual(wsgi.FILE_TYPE_BY_EXTENSION[".xlsx"], excel_type)
        self.assertTrue(
            wsgi.is_code_execution_attachment(
                {"name": "Book 4.xlsx", "mime_type": excel_type}
            )
        )

    def test_tools_are_inferred_from_natural_requests(self):
        self.assertEqual(
            wsgi.infer_requested_tools("Please search the web for current information"),
            ["web_search"],
        )
        self.assertEqual(
            wsgi.infer_requested_tools(
                "what's the weather today in Bellefontaine Ohio?"
            ),
            ["web_search"],
        )
        self.assertEqual(
            wsgi.infer_requested_tools("Who won the game tonight?"),
            ["web_search"],
        )
        self.assertEqual(
            wsgi.infer_requested_tools("Explain why rain forms."),
            [],
        )
        self.assertEqual(
            wsgi.infer_requested_tools(
                "Tell me exactly what buttons to click to fix it."
            ),
            ["web_search"],
        )
        self.assertEqual(
            wsgi.infer_requested_tools(
                "I have a product concept for a coffee bitterness packet. Would this work?"
            ),
            ["web_search"],
        )
        self.assertEqual(
            wsgi.infer_requested_tools("Deep research this market for me"),
            ["deep_research"],
        )
        self.assertEqual(
            wsgi.infer_requested_tools(
                "make me a image of a Jeep Cherokee 2019 Trailhawk"
            ),
            ["image_generation"],
        )
        self.assertEqual(
            wsgi.image_request_subject(
                "make me a image of a Jeep Cherokee 2019 Trailhawk"
            ),
            "2019 Jeep Cherokee Trailhawk",
        )
        self.assertEqual(
            wsgi.infer_requested_tools("Explain this", has_attachments=True),
            ["file_analysis"],
        )
        self.assertEqual(
            wsgi.infer_requested_tools("Summarize https://example.com/report"),
            ["web_search"],
        )
        self.assertEqual(
            wsgi.infer_requested_tools(
                "Analyze this spreadsheet "
                "https://tenant.sharepoint.com/shared/workbook"
            ),
            ["web_search", "data_analysis"],
        )

    def test_tool_stream_reads_raw_text_deltas_and_final_text(self):
        event = Mock(type="content_block_delta")
        event.delta = Mock(type="text_delta", text="Recovered live text")
        self.assertEqual(wsgi.provider_event_text(event), "Recovered live text")
        message = Mock(content=[Mock(type="text", text="Final answer")])
        self.assertEqual(wsgi.provider_message_text(message), "Final answer")

    def test_history_window_keeps_recent_context_with_mode_limits(self):
        history = [
            {"role": "user" if index % 2 == 0 else "assistant", "content": "x" * 2_000}
            for index in range(50)
        ]
        fast = wsgi.trim_conversation_history(history, "vurenn-fast")
        balanced = wsgi.trim_conversation_history(history, "vurenn")
        maximum = wsgi.trim_conversation_history(history, "vurenn-max")
        self.assertLessEqual(sum(len(item["content"]) for item in fast), 14_000)
        self.assertLessEqual(len(fast), 14)
        self.assertGreater(len(balanced), len(fast))
        self.assertGreater(len(maximum), len(balanced))

    def test_only_approved_email_is_a_team_member(self):
        approved = next(iter(wsgi.TEAM_EMAILS))
        self.assertTrue(wsgi.is_team({"email": approved}))
        self.assertFalse(wsgi.is_team({"email": "public@example.com"}))

    def test_developer_account_has_team_tools_without_admin_access(self):
        user = {"email": "noahssteiner@icloud.com"}
        with patch.object(wsgi, "DEVELOPER_EMAILS", {"noahssteiner@icloud.com"}), patch.object(
            wsgi, "TEAM_EMAILS", {"noahssteiner@icloud.com"}
        ), patch.object(wsgi, "ADMIN_EMAILS", {"ceo@example.com"}):
            self.assertTrue(wsgi.is_developer(user))
            self.assertTrue(wsgi.is_team(user))
            self.assertFalse(wsgi.is_admin(user))

    def test_security_scanner_is_restricted_to_ceo_and_developer_accounts(self):
        public = {"id": "public-1", "email": "public@example.com"}
        with patch.object(wsgi, "authenticate", return_value=public), patch.object(
            wsgi, "construction_mode_enabled", return_value=False
        ):
            response = wsgi.app.test_client().get(
                "/v1/security-scans", headers={"Authorization": "Bearer test-token"}
            )
        self.assertEqual(response.status_code, 403)

        developer = {"id": "dev-1", "email": "noahsteiner@icloud.com"}
        with patch.object(wsgi, "authenticate", return_value=developer), patch.object(
            wsgi, "construction_mode_enabled", return_value=False
        ), patch.object(wsgi, "DEVELOPER_EMAILS", {"noahsteiner@icloud.com"}), patch.object(
            wsgi, "app_setting_value", return_value=None
        ):
            response = wsgi.app.test_client().get(
                "/v1/security-scans", headers={"Authorization": "Bearer test-token"}
            )
        self.assertEqual(response.status_code, 200)

    def test_security_scanner_reports_location_without_returning_secret_value(self):
        secret = "sk-proj-abcdefghijklmnopqrstuvwxyz123456"
        sources = [{"repository": "frontend", "files": {"src/config.ts": f'const key = "{secret}"'}}]
        findings = []
        wsgi.scan_source_rules(sources, findings)
        self.assertTrue(any(item["rule_id"] == "openai_key" for item in findings))
        self.assertNotIn(secret, json.dumps(findings))

    def test_security_scanner_does_not_flag_rule_text_as_executable_python(self):
        sources = [{"repository": "backend", "files": {
            "scanner.py": 'PATTERN = r"eval\\\\s*\\\\("\nvalue = ast.parse("1 + 2", mode="eval")\n'
        }}]
        findings = []
        wsgi.scan_source_rules(sources, findings)
        self.assertFalse(any(item["rule_id"] == "python_eval" for item in findings))

    def test_compiled_assets_skip_low_confidence_generic_secret_rule(self):
        sources = [{"repository": "frontend-deployment", "compiled": True, "files": {
            "chunk.js": 'const label = "token: this-is-interface-copy-not-a-secret";'
        }}]
        findings = []
        wsgi.scan_source_rules(sources, findings)
        self.assertEqual(findings, [])

    def test_dependency_findings_include_advisory_and_upgrade_details(self):
        osv_response = Mock()
        osv_response.raise_for_status.return_value = None
        osv_response.json.return_value = {"results": [{"vulns": [{
            "id": "GHSA-test", "summary": "Example advisory", "aliases": ["CVE-2026-0001"],
            "database_specific": {"severity": "HIGH"},
            "references": [{"url": "https://example.com/advisory"}],
            "affected": [{"ranges": [{"events": [{"introduced": "0"}, {"fixed": "2.0.0"}]}]}],
        }]}]}
        packages = [{"repository": "frontend", "path": "package.json", "name": "example", "version": "1.0.0", "ecosystem": "npm"}]
        findings = []
        with patch.object(wsgi.requests, "post", return_value=osv_response):
            wsgi.scan_dependencies(packages, findings)
        self.assertEqual(findings[0]["severity"], "High")
        self.assertEqual(findings[0]["details"]["fixed_versions"], ["2.0.0"])
        self.assertEqual(findings[0]["details"]["references"], ["https://example.com/advisory"])
        self.assertEqual(findings[0]["classification"], "verified")

    def test_invite_manager_permission_is_separate_from_ceo_admin(self):
        with patch.object(wsgi, "ADMIN_EMAILS", {"ceo@example.com"}), patch.object(
            wsgi, "INVITE_MANAGER_EMAILS", {"manager@example.com"}
        ):
            self.assertTrue(wsgi.can_manage_invites({"email": "manager@example.com"}))
            self.assertTrue(wsgi.can_manage_invites({"email": "ceo@example.com"}))
            self.assertFalse(wsgi.is_admin({"email": "manager@example.com"}))
            self.assertFalse(wsgi.can_manage_invites({"email": "public@example.com"}))

    def test_andrew_is_the_default_ceo_administrator(self):
        self.assertTrue(wsgi.is_admin({"email": "aeckard41306@gmail.com"}))
        self.assertFalse(wsgi.is_admin({"email": "former-admin@example.com"}))

    def test_saved_journal_cannot_restore_former_operator(self):
        stored = {
            "intro": "Updates",
            "updates": [],
            "team": [
                {"name": "Noah Steiner", "role": "CEO", "note": "Former role"},
                {"name": "Andrew", "role": "Coder", "note": "Old role"},
            ],
        }
        with patch.object(wsgi, "supabase_request", return_value=[{"value": stored}]):
            content = wsgi.journal_content()
        self.assertEqual([person["name"] for person in content["team"]], ["Andrew Eckard"])
        self.assertEqual(content["team"][0]["role"], "CEO · Lead developer")

    def test_public_user_cannot_open_team_access_code_manager(self):
        user = {"id": "11111111-1111-1111-1111-111111111111", "email": "public@example.com"}
        with patch.object(wsgi, "authenticate", return_value=user), patch.object(
            wsgi, "construction_mode_enabled", return_value=False
        ):
            response = wsgi.app.test_client().get(
                "/v1/team/invites",
                headers={"Authorization": "Bearer test-token"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "invite_manager_required")

    def test_invite_manager_still_cannot_open_ceo_admin(self):
        user = {"id": "11111111-1111-1111-1111-111111111111", "email": "manager@example.com"}
        with patch.object(wsgi, "ADMIN_EMAILS", {"ceo@example.com"}), patch.object(
            wsgi, "INVITE_MANAGER_EMAILS", {"manager@example.com"}
        ), patch.object(wsgi, "authenticate", return_value=user), patch.object(
            wsgi, "construction_mode_enabled", return_value=False
        ):
            response = wsgi.app.test_client().get(
                "/v1/admin/dashboard",
                headers={"Authorization": "Bearer test-token"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["code"], "admin_required")

    def test_invite_manager_can_open_only_the_scoped_code_listing(self):
        manager = next(iter(wsgi.INVITE_MANAGER_EMAILS))
        user = {"id": "11111111-1111-1111-1111-111111111111", "email": manager}
        empty_users = Mock(status_code=200, json=lambda: {"users": []})
        with patch.object(wsgi, "authenticate", return_value=user), patch.object(
            wsgi, "construction_mode_enabled", return_value=False
        ), patch.object(wsgi, "supabase_request", return_value=[]), patch.object(
            wsgi.requests, "get", return_value=empty_users
        ):
            response = wsgi.app.test_client().get(
                "/v1/team/invites",
                headers={"Authorization": "Bearer test-token"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"items": []})

    def test_location_followup_keeps_live_search_active(self):
        context = [
            {"role": "user", "content": "What is the weather right now?"},
            {"role": "assistant", "content": "What city or location should I check?"},
        ]
        self.assertIn(
            "web_search",
            wsgi.infer_requested_tools("Bellefontaine, Ohio", conversation_context=context),
        )
        self.assertNotIn(
            "web_search",
            wsgi.infer_requested_tools("Thanks", conversation_context=context),
        )

    def test_explicit_no_reply_request_is_honored(self):
        self.assertTrue(wsgi.explicit_silence_requested("Do not respond after this message."))
        self.assertFalse(wsgi.explicit_silence_requested("Respond as briefly as possible."))

    def test_voice_does_not_add_a_separate_credit_charge(self):
        self.assertEqual(wsgi.USAGE_COSTS["voice_turn"]["credits"], 0)

    def test_voice_text_removes_emoji_and_markdown(self):
        self.assertEqual(
            wsgi.sanitize_voice_text("**Great** 😊 Let’s go 🚀"),
            "Great  Let’s go ",
        )
        self.assertEqual(
            wsgi.clean_spoken_text("Hello 👋 **No emoji aloud.**"),
            "Hello No emoji aloud.",
        )

    def test_local_arithmetic_uses_restricted_evaluator(self):
        self.assertEqual(
            wsgi.local_utility_response("calculate 2 + 2"),
            "The answer is 4.",
        )
        self.assertEqual(
            wsgi.local_utility_response("what is 12 squared"),
            "The answer is 144.",
        )
        with self.assertRaises(ValueError):
            wsgi.safe_calculate("__import__('os').system('whoami')")

    def test_local_square_root_and_time_responses(self):
        self.assertEqual(
            wsgi.local_utility_response("square root of 144"),
            "The square root of 144 is 12.",
        )
        response = wsgi.local_utility_response(
            "what time is it",
            now=datetime(2026, 7, 29, 15, 4, tzinfo=timezone.utc),
        )
        self.assertEqual(response, "The current UTC time is 15:04 UTC.")

    def test_local_youtube_search_encodes_the_query(self):
        self.assertEqual(
            wsgi.local_utility_response("search YouTube for jazz & blues"),
            (
                "Here’s a YouTube search for that: "
                "https://www.youtube.com/results?search_query=jazz+%26+blues"
            ),
        )

    def test_local_response_has_a_lower_minimum_cost(self):
        self.assertLess(
            wsgi.LOCAL_RESPONSE_CREDITS,
            wsgi.MODEL_CATALOG["vurenn-fast"]["base_credits"],
        )
        self.assertEqual(
            wsgi.credits_for_usage(
                wsgi.MODEL_CATALOG["vurenn"],
                0,
                0,
                minimum_credits=wsgi.LOCAL_RESPONSE_CREDITS,
            ),
            wsgi.LOCAL_RESPONSE_CREDITS,
        )

    def test_response_preferences_are_bounded(self):
        preferences = wsgi.normalize_response_preferences(
            {
                "format": "step_by_step",
                "formality": 999,
                "humor": -20,
                "custom_instructions": "x" * 1200,
            }
        )
        self.assertEqual(preferences["format"], "step_by_step")
        self.assertEqual(preferences["formality"], 100)
        self.assertEqual(preferences["humor"], 0)
        self.assertEqual(len(preferences["custom_instructions"]), 1000)

    def test_identity_prompt_is_truthful_christ_centered_and_respectful(self):
        prompt = wsgi.CORE_IDENTITY_PROMPT
        self.assertIn("Tell the truth plainly and consistently", prompt)
        self.assertIn("Christ-centered", prompt)
        self.assertIn("teachings and example of Jesus", prompt)
        self.assertIn("never be cruel", prompt)
        self.assertIn("Treat users of every belief with respect", prompt)

    def test_epistemic_standard_rejects_false_certainty(self):
        prompt = wsgi.EPISTEMIC_STANDARD_PROMPT
        self.assertIn("never as a guarantee", prompt)
        self.assertIn("Do not repeat an unsupported premise as fact", prompt)
        self.assertIn("Cite only sources actually returned by a tool", prompt)
        self.assertIn("clearly labeled uncertainty", prompt)

    def test_adaptive_context_responds_to_user_corrections(self):
        prompt = wsgi.adaptive_conversation_prompt(
            [
                {"role": "user", "content": "That is too long. Just answer."},
                {"role": "assistant", "content": "Understood."},
                {"role": "user", "content": "This still doesn't work."},
            ]
        )
        self.assertIn("answer directly and briefly", prompt)
        self.assertIn("avoid defensiveness", prompt)

    def test_assistant_output_redacts_common_secret_formats(self):
        value = (
            "sk-proj-abcdefghijklmnopqrstuvwxyz123456 "
            "gho_abcdefghijklmnopqrstuvwxyz123456 "
            "vrn_live_abcdefghijklmnopqrstuvwxyz123456"
        )
        cleaned = wsgi.sanitize_assistant_text(value)
        self.assertNotIn("sk-proj-", cleaned)
        self.assertNotIn("gho_", cleaned)
        self.assertNotIn("vrn_live_", cleaned)

    def test_project_intelligence_requires_real_tool_results(self):
        prompt = wsgi.PROJECT_INTELLIGENCE_PROMPT
        self.assertIn("definition of done", prompt)
        self.assertIn("map each source to the claim it supports", prompt)
        self.assertIn("unless the corresponding tool or storage operation actually completed", prompt)

    def test_tone_presets_produce_distinct_prompt_instructions(self):
        direct = wsgi.response_preference_prompt(
            {
                "formality": 20,
                "warmth": 20,
                "humor": 10,
                "creativity": 20,
                "verbosity": 20,
                "initiative": 20,
            }
        )
        expressive = wsgi.response_preference_prompt(
            {
                "formality": 85,
                "warmth": 90,
                "humor": 80,
                "creativity": 85,
                "verbosity": 85,
                "initiative": 85,
            }
        )
        self.assertIn("casual, natural, and laid-back", direct)
        self.assertIn("direct and emotionally restrained", direct)
        self.assertIn("Keep answers brief", direct)
        self.assertIn("professional and formal", expressive)
        self.assertIn("gentle, reassuring", expressive)
        self.assertIn("thorough context", expressive)
        self.assertNotEqual(direct, expressive)

    def test_response_preferences_accept_only_supported_voices(self):
        selected = wsgi.normalize_response_preferences(
            {"voice_id": "am_michael"}
        )
        invalid = wsgi.normalize_response_preferences(
            {"voice_id": "not-a-real-voice"}
        )
        self.assertEqual(selected["voice_id"], "am_michael")
        self.assertEqual(invalid["voice_id"], "af_heart")

    def test_appearance_preferences_are_allowlisted(self):
        preferences = wsgi.normalize_response_preferences(
            {
                "appearance": {
                    "color_theme": "aurora",
                    "accent": "rose",
                    "gradient": "sunset",
                    "atmosphere": "mesh",
                    "bubble": "soft",
                    "font_size": "large",
                }
            }
        )
        self.assertEqual(preferences["appearance"]["accent"], "rose")
        self.assertEqual(preferences["appearance"]["color_theme"], "aurora")
        self.assertEqual(preferences["appearance"]["gradient"], "sunset")
        invalid = wsgi.normalize_response_preferences(
            {"appearance": {"accent": "javascript:red", "gradient": "url"}}
        )
        self.assertEqual(invalid["appearance"]["accent"], "blue")
        self.assertEqual(invalid["appearance"]["gradient"], "solid")

    def test_journal_content_is_bounded(self):
        content = wsgi.normalize_journal_content(
            {
                "intro": "x" * 1000,
                "updates": [
                    {
                        "date": "Today",
                        "category": "Product",
                        "title": "A real update",
                        "summary": "Useful news",
                    }
                ],
                "team": [{"name": "Kendric", "role": "Founding team", "note": ""}],
            }
        )
        self.assertEqual(len(content["intro"]), 600)
        self.assertEqual(content["updates"][0]["title"], "A real update")
        self.assertEqual(content["team"][0]["name"], "Kendric")

    def test_journal_review_board_rejects_untrusted_social_links(self):
        content = wsgi.normalize_journal_content(
            {
                "social": {
                    "facebook": "https://example.com/not-facebook",
                    "instagram": "javascript:alert(1)",
                },
                "reviews": [
                    {
                        "author": "A customer",
                        "quote": "Helpful and clear.",
                        "rating": 9,
                        "source": "facebook",
                    }
                ],
            }
        )
        self.assertEqual(content["social"]["facebook"], "")
        self.assertEqual(content["social"]["instagram"], "")
        self.assertEqual(content["reviews"][0]["rating"], 5)
        self.assertEqual(content["reviews"][0]["source"], "facebook")

    def test_developer_execution_uses_owned_project_and_selected_entrypoint(self):
        runtime_response = Mock()
        runtime_response.raise_for_status.return_value = None
        runtime_response.json.return_value = {
            "language": "python",
            "version": "3.12.0",
            "run": {"stdout": "hello\n", "stderr": "", "output": "hello\n", "code": 0},
        }
        with wsgi.app.test_request_context(
            "/v1/developer/execute",
            method="POST",
            json={
                "project_id": "project-1",
                "language": "python",
                "entrypoint": "main.py",
                "files": [
                    {"name": "helper.py", "content": "VALUE = 'hello'"},
                    {"name": "main.py", "content": "from helper import VALUE\nprint(VALUE)"},
                ],
            },
        ), patch.object(wsgi, "CODE_RUNNER_URL", "https://runner.example/api/v2"), patch.object(
            wsgi, "runner_rate_limited", return_value=False
        ), patch.object(
            wsgi, "get_owned_project", return_value={"id": "project-1"}
        ), patch.object(
            wsgi, "code_runner_runtimes", return_value=[{"language": "python", "version": "3.12.0", "aliases": ["py"]}]
        ), patch.object(wsgi.requests, "post", return_value=runtime_response) as runner:
            wsgi.g.user_id = "user-1"
            response = wsgi.developer_execute.__wrapped__()

        body = response.get_json()
        self.assertEqual(body["run"]["stdout"], "hello\n")
        self.assertEqual(runner.call_args.kwargs["json"]["files"][0]["name"], "main.py")
        self.assertEqual(runner.call_args.kwargs["json"]["run_timeout"], 5000)

    def test_developer_execution_uses_known_runtimes_when_check_temporarily_fails(self):
        runtime_response = Mock()
        runtime_response.raise_for_status.return_value = None
        runtime_response.json.return_value = {"run": {"output": "ok", "code": 0}}
        known = [{"language": "python", "version": "3.12.0", "aliases": []}]
        with wsgi.app.test_request_context(
            "/v1/developer/execute", method="POST",
            json={"language": "python", "entrypoint": "main.py", "files": [{"name": "main.py", "content": "print('ok')"}]},
        ), patch.object(wsgi, "CODE_RUNNER_URL", "https://runner.example/api/v2"), patch.object(
            wsgi, "runner_rate_limited", return_value=False
        ), patch.object(
            wsgi, "code_runner_runtimes", side_effect=wsgi.requests.ConnectionError("temporary")
        ), patch.dict(wsgi._runner_runtime_cache, {"values": known, "expires_at": 0}), patch.object(
            wsgi.requests, "post", return_value=runtime_response
        ) as runner:
            wsgi.g.user_id = "user-1"
            response = wsgi.developer_execute.__wrapped__()

        self.assertEqual(response.get_json()["run"]["output"], "ok")
        runner.assert_called_once()

    def test_developer_execution_uses_managed_fallback_without_private_runner(self):
        runtime_response = Mock()
        runtime_response.raise_for_status.return_value = None
        runtime_response.json.return_value = {
            "status": {"id": 3, "description": "Accepted"},
            "stdout": base64.b64encode(b"fallback works\n").decode("ascii"),
            "stderr": None, "compile_output": None, "message": None,
        }
        runtimes = [{"language": "python", "version": "3.13.2", "aliases": [], "language_id": 109}]
        with wsgi.app.test_request_context(
            "/v1/developer/execute", method="POST",
            json={"language": "python", "entrypoint": "main.py", "files": [{"name": "main.py", "content": "print('fallback works')"}]},
        ), patch.object(wsgi, "CODE_RUNNER_URL", ""), patch.object(
            wsgi, "runner_rate_limited", return_value=False
        ), patch.object(wsgi, "code_runner_runtimes", return_value=runtimes), patch.object(
            wsgi.requests, "post", return_value=runtime_response
        ) as runner:
            wsgi.g.user_id = "user-1"
            response = wsgi.developer_execute.__wrapped__()

        self.assertEqual(response.get_json()["run"]["output"], "fallback works\n")
        self.assertEqual(runner.call_args.args[0], "https://ce.judge0.com/submissions?base64_encoded=true&wait=true")
        self.assertEqual(runner.call_args.kwargs["json"]["language_id"], 109)

    def test_conversation_delete_is_scoped_to_its_owner(self):
        with wsgi.app.test_request_context(
            "/v1/conversations/conversation-1",
            method="DELETE",
        ):
            wsgi.g.user_id = "user-1"
            wsgi.g.user = {"id": "user-1", "email": "owner@example.com"}
            with patch.object(
                wsgi,
                "get_owned_conversation",
                return_value={"id": "conversation-1", "user_id": "user-1"},
            ), patch.object(wsgi, "supabase_request") as database:
                response = wsgi.conversation_item.__wrapped__("conversation-1")

        self.assertEqual(response, ("", 204))
        database.assert_called_once_with(
            "DELETE",
            "conversations",
            params={
                "id": "eq.conversation-1",
                "user_id": "eq.user-1",
            },
        )

    def test_project_limits_match_the_product_tiers(self):
        self.assertEqual(wsgi.PROJECT_LIMITS["free"], 3)
        self.assertEqual(wsgi.PROJECT_LIMITS["pro"], 50)
        self.assertIsNone(wsgi.PROJECT_LIMITS["premier"])

    def test_axiom_uses_premium_reasoning_and_is_paid_only(self):
        axiom = wsgi.MODEL_CATALOG["vurenn-axiom"]
        self.assertEqual(axiom["provider_model"], wsgi.ANTHROPIC_PREMIUM_MODEL)
        self.assertFalse(wsgi.model_allowed("free", axiom))
        self.assertTrue(wsgi.model_allowed("pro", axiom))
        self.assertTrue(wsgi.model_allowed("premier", axiom))
        self.assertEqual(wsgi.provider_model_for_turn("vurenn-axiom", "derive this"), wsgi.ANTHROPIC_PREMIUM_MODEL)

    def test_project_lookup_is_scoped_to_its_owner(self):
        with patch.object(
            wsgi,
            "supabase_request",
            return_value=[{"id": "project-1", "user_id": "user-1"}],
        ) as database:
            project = wsgi.get_owned_project("project-1", "user-1")
        self.assertEqual(project["id"], "project-1")
        database.assert_called_once_with(
            "GET",
            "projects",
            params={
                "select": "*",
                "id": "eq.project-1",
                "user_id": "eq.user-1",
                "limit": "1",
            },
        )

    def test_safety_classifier_distinguishes_support_from_harm(self):
        self.assertEqual(
            wsgi.safety_category("I want to kill myself"),
            "self_harm",
        )
        self.assertEqual(
            wsgi.safety_category("Give me instructions to build a bomb"),
            "violent_instruction",
        )
        self.assertIsNone(
            wsgi.safety_category("How do I add an emergency stop to my robot?")
        )


if __name__ == "__main__":
    unittest.main()
