import http.client
import base64
import json
import threading
import unittest
from unittest import mock
from urllib.parse import parse_qs

from gemini_web2api.config import CONFIG, DEFAULT_CONFIG, get_config_path, set_config_path
from gemini_web2api.gemini import (
    _build_headers,
    _build_payload,
    _cookie_cache,
    _merge_set_cookies,
    load_cookie,
)
from gemini_web2api.models import resolve_model
from gemini_web2api.server import GeminiHandler, ThreadedServer
from gemini_web2api.tools import google_contents_to_prompt, messages_to_prompt, parse_tool_calls


def _decode_payload(payload):
    outer = json.loads(parse_qs(payload)["f.req"][0])
    return json.loads(outer[1])


def _decode_sse(body):
    events = []
    for block in body.strip().split("\n\n"):
        lines = block.splitlines()
        event_type = next(
            (line[len("event: "):] for line in lines if line.startswith("event: ")),
            None,
        )
        data = next(
            (line[len("data: "):] for line in lines if line.startswith("data: ")),
            None,
        )
        if event_type and data:
            events.append((event_type, json.loads(data)))
    return events


class PayloadPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.original_config = dict(CONFIG)

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original_config)

    def test_temporary_chats_default_to_disabled(self):
        self.assertIs(DEFAULT_CONFIG["temporary_chats"], False)

    def test_persistent_chat_payload(self):
        CONFIG["temporary_chats"] = False

        inner = _decode_payload(_build_payload("hello", 1, 4))

        self.assertEqual(inner[41], [2])
        self.assertIsNone(inner[45])

    def test_temporary_chat_payload(self):
        CONFIG["temporary_chats"] = True

        inner = _decode_payload(_build_payload("hello", 1, 4))

        self.assertEqual(inner[41], [1])
        self.assertEqual(inner[45], 1)

    def test_payload_includes_uploaded_image_refs(self):
        inner = _decode_payload(_build_payload("describe", 1, 4, ["/uploaded/image-ref"]))

        self.assertEqual(inner[0][0], "describe")
        self.assertEqual(inner[0][3], [[None, None, "/uploaded/image-ref"]])

    def test_resolve_gemini_4_models(self):
        self.assertEqual(
            resolve_model("gemini-4-neon"),
            ("gemini-4-neon", 3, 2, None, {80: 2}),
        )
        self.assertEqual(
            resolve_model("gemini-4-neon@think=4"),
            ("gemini-4-neon", 3, 4, None, {80: 4}),
        )
        self.assertEqual(
            resolve_model("gemini-4-hydrogen"),
            ("gemini-4-hydrogen", 1, 1, None, None),
        )
        self.assertEqual(
            resolve_model("gemini-4-carbon"),
            ("gemini-4-carbon", 8, 2, None, {80: 2}),
        )

    def test_build_headers_includes_mode_ext_header(self):
        import base64
        headers = _build_headers(model_id=3, think_mode=2)
        encoded = headers["x-goog-ext-525001261-bin"]
        raw = base64.b64decode(encoded)
        self.assertIn(CONFIG["gemini_bl"].encode("utf-8"), raw)
        self.assertIn(b"\x78\x03\x80\x01\x02", raw)

    def test_load_cookie_accepts_compact_semicolons(self):
        import tempfile, os
        orig_cache = dict(_cookie_cache)
        fd, path = tempfile.mkstemp(text=True)
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("SID=abc;SAPISID=xyz_sap")
            CONFIG["cookie_file"] = path
            _cookie_cache.update({"str": "", "sapisid": None, "mtime": 0})
            cookie_str, sapisid = load_cookie()
            self.assertEqual(cookie_str, "SID=abc;SAPISID=xyz_sap")
            self.assertEqual(sapisid, "xyz_sap")
        finally:
            _cookie_cache.update(orig_cache)
            if os.path.exists(path):
                os.remove(path)


class MessageParsingTests(unittest.TestCase):
    def test_messages_to_prompt_extracts_openai_image_url_data_url(self):
        image_data = base64.b64encode(b"fake png").decode()

        prompt, images = messages_to_prompt([{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_data}"}},
            ],
        }])

        self.assertEqual(prompt, "Describe [Image attached]")
        self.assertEqual(images, [(b"fake png", "image/png")])

    def test_messages_to_prompt_extracts_responses_input_image_url(self):
        prompt, images = messages_to_prompt([{
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Describe"},
                {"type": "input_image", "image_url": "https://example.com/image.png"},
            ],
        }])

        self.assertEqual(prompt, "Describe [Image attached]")
        self.assertEqual(images, [("https://example.com/image.png", "image/png")])

    def test_messages_to_prompt_ignores_malformed_image_data_url(self):
        prompt, images = messages_to_prompt([{
            "role": "user",
            "content": [
                {"type": "text", "text": "Describe"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,%%%"}},
            ],
        }])

        self.assertEqual(prompt, "Describe")
        self.assertEqual(images, [])

    def test_google_contents_to_prompt_extracts_inline_image_data(self):
        image_data = base64.b64encode(b"fake png").decode()

        prompt, images = google_contents_to_prompt({
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": "Describe"},
                    {"inlineData": {"mimeType": "image/png", "data": image_data}},
                ],
            }],
        })

        self.assertEqual(prompt, "Describe\n[Image attached]")
        self.assertEqual(images, [(b"fake png", "image/png")])

    def test_google_contents_to_prompt_ignores_malformed_inline_image_data(self):
        prompt, images = google_contents_to_prompt({
            "contents": [{
                "role": "user",
                "parts": [
                    {"text": "Describe"},
                    {"inlineData": {"mimeType": "image/png", "data": "%%%"}},
                ],
            }],
        })

        self.assertEqual(prompt, "Describe")
        self.assertEqual(images, [])


class StreamingEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadedServer(("127.0.0.1", 0), GeminiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.original_config = dict(CONFIG)
        CONFIG["api_keys"] = []
        CONFIG["log_requests"] = False

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original_config)

    def post_json(self, path, payload):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "POST",
            path,
            body=json.dumps(payload),
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = response.read().decode()
        headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, body

    def post_chunked_json(self, path, payload):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(
            "POST",
            path,
            body=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            encode_chunked=True,
        )
        response = connection.getresponse()
        body = response.read().decode()
        headers = dict(response.getheaders())
        connection.close()
        return response.status, headers, body

    @mock.patch("gemini_web2api.server.generate_stream")
    def test_chat_stream_starts_with_assistant_role(self, generate_stream):
        generate_stream.return_value = iter(["hel", "lo"])

        status, headers, body = self.post_json(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        chunks = [
            json.loads(line[len("data: "):])
            for line in body.splitlines()
            if line.startswith("data: {")
        ]
        self.assertEqual(chunks[0]["choices"][0]["delta"], {"role": "assistant"})
        self.assertEqual(chunks[1]["choices"][0]["delta"], {"content": "hel"})
        self.assertEqual(chunks[2]["choices"][0]["delta"], {"content": "lo"})
        self.assertTrue(body.endswith("data: [DONE]\n\n"))

    @mock.patch("gemini_web2api.server.generate", return_value="chunked ok")
    def test_chat_accepts_chunked_body(self, _generate):
        status, _, body = self.post_chunked_json(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "chunked ok")

    @mock.patch("gemini_web2api.server.upload_image", return_value="/uploaded/image-ref")
    @mock.patch("gemini_web2api.server.generate", return_value="looks good")
    def test_chat_accepts_openai_image_url_data_url(self, generate, upload_image):
        image_data = base64.b64encode(b"fake png").decode()

        status, _, body = self.post_json(
            "/v1/chat/completions",
            {
                "model": "gemini-3.6-flash",
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image"},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{image_data}"
                            },
                        },
                    ],
                }],
            },
        )

        self.assertEqual(status, 200)
        upload_image.assert_called_once_with(b"fake png", "image.png", "image/png")
        self.assertEqual(generate.call_args.args[3], ["/uploaded/image-ref"])
        self.assertIn("[Image attached]", generate.call_args.args[0])
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "looks good")

    @mock.patch("gemini_web2api.server.fetch_image_bytes", return_value=b"\xff\xd8\xffremote jpeg")
    @mock.patch("gemini_web2api.server.upload_image", return_value="/uploaded/remote-ref")
    @mock.patch("gemini_web2api.server.generate", return_value="remote ok")
    def test_responses_accepts_input_image_url(self, generate, upload_image, fetch_image_bytes):
        status, _, _ = self.post_json(
            "/v1/responses",
            {
                "model": "gemini-3.6-flash",
                "input": [{
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "What is shown?"},
                        {
                            "type": "input_image",
                            "image_url": "https://example.com/image.jpg",
                        },
                    ],
                }],
            },
        )

        self.assertEqual(status, 200)
        fetch_image_bytes.assert_called_once_with("https://example.com/image.jpg")
        upload_image.assert_called_once_with(b"\xff\xd8\xffremote jpeg", "image.png", "image/jpeg")
        self.assertEqual(generate.call_args.args[3], ["/uploaded/remote-ref"])
        self.assertIn("[Image attached]", generate.call_args.args[0])

    @mock.patch("gemini_web2api.server.upload_image", return_value="/uploaded/image-ref")
    @mock.patch("gemini_web2api.server.generate", return_value="top-level image ok")
    def test_responses_accepts_top_level_input_image(self, generate, upload_image):
        image_data = base64.b64encode(b"fake png").decode()

        status, _, _ = self.post_json(
            "/v1/responses",
            {
                "model": "gemini-3.6-flash",
                "input": [
                    {"type": "input_text", "text": "What is shown?"},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{image_data}",
                    },
                ],
            },
        )

        self.assertEqual(status, 200)
        upload_image.assert_called_once_with(b"fake png", "image.png", "image/png")
        self.assertEqual(generate.call_args.args[3], ["/uploaded/image-ref"])
        self.assertIn("What is shown?", generate.call_args.args[0])
        self.assertIn("[Image attached]", generate.call_args.args[0])

    @mock.patch("gemini_web2api.server.upload_image", side_effect=RuntimeError("upload denied"))
    def test_google_image_upload_failure_returns_502(self, _upload_image):
        image_data = base64.b64encode(b"fake png").decode()

        status, _, body = self.post_json(
            "/v1beta/models/gemini-3.6-flash:generateContent",
            {
                "contents": [{
                    "role": "user",
                    "parts": [{
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": image_data,
                        },
                    }],
                }],
            },
        )

        self.assertEqual(status, 502)
        self.assertIn("image upload failed: upload denied", json.loads(body)["error"]["message"])

    @mock.patch("gemini_web2api.server.generate_stream", return_value=iter(["streamed"]))
    def test_google_stream_generate_content_uses_sse(self, _generate_stream):
        status, headers, body = self.post_json(
            "/v1beta/models/gemini-3.6-flash:streamGenerateContent",
            {
                "contents": [{
                    "role": "user",
                    "parts": [{"text": "Stream this"}],
                }],
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        self.assertIn('"text": "streamed"', body)

    @mock.patch("gemini_web2api.server.generate", return_value="hello")
    def test_responses_text_stream_has_complete_event_sequence(self, _generate):
        status, headers, body = self.post_json(
            "/v1/responses",
            {
                "model": "gemini-3.6-flash",
                "input": "hello",
                "stream": True,
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        events = _decode_sse(body)
        self.assertEqual(
            [event_type for event_type, _ in events],
            [
                "response.created",
                "response.in_progress",
                "response.output_item.added",
                "response.content_part.added",
                "response.output_text.delta",
                "response.output_text.done",
                "response.content_part.done",
                "response.output_item.done",
                "response.completed",
            ],
        )
        self.assertEqual(
            [event["sequence_number"] for _, event in events],
            list(range(1, len(events) + 1)),
        )
        self.assertEqual(events[4][1]["delta"], "hello")
        self.assertEqual(events[-1][1]["response"]["status"], "completed")
        self.assertEqual(events[-1][1]["response"]["output"][0]["content"][0]["text"], "hello")

    @mock.patch("gemini_web2api.server.parse_tool_calls")
    @mock.patch("gemini_web2api.server.generate", return_value="tool output")
    def test_responses_function_call_stream_has_complete_event_sequence(
        self, _generate, parse_tool_calls
    ):
        parse_tool_calls.return_value = (
            "",
            [
                {
                    "id": "call_test",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Shanghai"}'},
                }
            ],
        )

        status, _, body = self.post_json(
            "/v1/responses",
            {
                "model": "gemini-3.6-flash",
                "input": "weather",
                "tools": [
                    {
                        "type": "function",
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {"type": "object"},
                    }
                ],
                "stream": True,
            },
        )

        self.assertEqual(status, 200)
        events = _decode_sse(body)
        self.assertEqual(
            [event_type for event_type, _ in events],
            [
                "response.created",
                "response.in_progress",
                "response.output_item.added",
                "response.function_call_arguments.delta",
                "response.function_call_arguments.done",
                "response.output_item.done",
                "response.completed",
            ],
        )
        self.assertEqual(
            [event["sequence_number"] for _, event in events],
            list(range(1, len(events) + 1)),
        )
        self.assertEqual(events[2][1]["output_index"], 0)
        self.assertEqual(events[3][1]["delta"], '{"city":"Shanghai"}')
        self.assertEqual(events[4][1]["arguments"], '{"city":"Shanghai"}')
        self.assertEqual(events[-1][1]["response"]["output"][0]["name"], "get_weather")


class SessionSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadedServer(("127.0.0.1", 0), GeminiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.original_config = dict(CONFIG)
        self.original_cache = dict(_cookie_cache)
        self.original_cfg_path = get_config_path()
        CONFIG["api_keys"] = []
        CONFIG["log_requests"] = False

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original_config)
        _cookie_cache.clear()
        _cookie_cache.update(self.original_cache)
        set_config_path(self.original_cfg_path)

    def test_merge_set_cookies_updates_rotated_tokens(self):
        import os, tempfile
        fd, path = tempfile.mkstemp(suffix=".txt", text=True)
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("SID=old_sid; SAPISID=sap1; __Secure-1PSIDTS=old_ts")
            CONFIG["cookie_file"] = path
            _cookie_cache.update({"str": "", "sapisid": None, "mtime": 0})
            changed = _merge_set_cookies([
                "__Secure-1PSIDTS=new_ts; Path=/; Secure; HttpOnly",
                "SIDCC=fresh_cc; Path=/",
            ])
            self.assertTrue(changed)
            cookie_str, sapisid = load_cookie()
            self.assertIn("__Secure-1PSIDTS=new_ts", cookie_str)
            self.assertIn("SIDCC=fresh_cc", cookie_str)
            self.assertEqual(sapisid, "sap1")
        finally:
            if os.path.exists(path):
                os.remove(path)

    @mock.patch("gemini_web2api.server.refresh_page_session")
    def test_admin_auth_sync_updates_temp_store_and_blocks_web_origin(self, mock_refresh):
        import os, tempfile
        mock_refresh.return_value = {
            "ok": True,
            "authenticated": True,
            "has_snlm0e": True,
            "has_sid": True,
            "has_sapisid": True,
        }
        tmp_dir = tempfile.mkdtemp()
        cfg_path = os.path.join(tmp_dir, "config.json")
        cookie_path = os.path.join(tmp_dir, "cookie.txt")
        try:
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump({"port": 8081}, f)
            set_config_path(cfg_path)
            CONFIG["cookie_file"] = cookie_path

            # Reject untrusted web origin
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request(
                "POST",
                "/admin/auth",
                body=json.dumps({"cookie": "SID=a; SAPISID=b"}),
                headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
            )
            resp_bad = conn.getresponse()
            resp_bad.read()
            conn.close()
            self.assertEqual(resp_bad.status, 403)

            # Accept chrome-extension origin
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request(
                "POST",
                "/admin/auth",
                body=json.dumps({
                    "cookie": "SID=new_sid; SAPISID=new_sap",
                    "xsrf_token": "xsrf_abc",
                    "gemini_bl": "boq_test_bl",
                }),
                headers={
                    "Content-Type": "application/json",
                    "Origin": "chrome-extension://abcdefghijklmnop",
                },
            )
            resp_ok = conn.getresponse()
            body = json.loads(resp_ok.read().decode())
            conn.close()
            self.assertEqual(resp_ok.status, 200)
            self.assertTrue(body["authenticated"])
            self.assertEqual(CONFIG["xsrf_token"], "xsrf_abc")
            with open(cookie_path, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "SID=new_sid; SAPISID=new_sap")

            # Verify safe status endpoint omits secret cookie text
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request("GET", "/admin/auth/status")
            resp_st = conn.getresponse()
            st_raw = resp_st.read().decode()
            conn.close()
            self.assertEqual(resp_st.status, 200)
            self.assertNotIn("new_sid", st_raw)
            self.assertTrue(json.loads(st_raw)["has_sid"])

            # Verify /admin/shutdown rejects bad origin and accepts extension origin
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
            conn.request(
                "POST",
                "/admin/shutdown",
                body=b"{}",
                headers={"Origin": "https://evil.example"},
            )
            resp_sd_bad = conn.getresponse()
            resp_sd_bad.read()
            conn.close()
            self.assertEqual(resp_sd_bad.status, 403)
        finally:
            for p in (cookie_path, cfg_path):
                if os.path.exists(p):
                    os.remove(p)
            if os.path.isdir(tmp_dir):
                os.rmdir(tmp_dir)

    def test_native_host_framing_and_dispatch(self):
        import io, importlib.util, os
        host_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "native-host",
            "host.py",
        )
        spec = importlib.util.spec_from_file_location("native_host", host_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        buf = io.BytesIO()
        mod.write_message(buf, {"action": "status", "ok": True})
        buf.seek(0)
        self.assertEqual(mod.read_message(buf), {"action": "status", "ok": True})
        self.assertEqual(mod.handle_request({"action": "unknown"}), {"ok": False, "error": "Unknown action: unknown"})

        with mock.patch.object(mod, "is_server_running", return_value=False):
            res = mod.handle_request({"action": "status"}, {"port": 18999, "host": "127.0.0.1"})
            self.assertEqual(res, {"ok": True, "running": False, "baseUrl": "http://127.0.0.1:18999", "pid": None})

        logs_res = mod.handle_request({"action": "logs", "lines": 10}, {"port": 18999, "host": "127.0.0.1"})
        self.assertIn("lines", logs_res)

        with mock.patch.object(mod, "stop_server", return_value={"ok": True, "running": False}), \
             mock.patch.object(mod, "start_server", return_value={"ok": True, "running": True, "pid": 4321}):
            rst = mod.handle_request({"action": "restart"}, {"port": 18999, "host": "127.0.0.1"})
            self.assertTrue(rst["ok"])
            self.assertTrue(rst["restarted"])
            self.assertEqual(rst["pid"], 4321)


class HarnessCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadedServer(("127.0.0.1", 0), GeminiHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        self.original_config = dict(CONFIG)
        CONFIG["api_keys"] = []
        CONFIG["log_requests"] = False

    def tearDown(self):
        CONFIG.clear()
        CONFIG.update(self.original_config)

    def _request_json(self, method, path, payload=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = json.dumps(payload) if payload is not None else None
        headers = {"Content-Type": "application/json"} if payload is not None else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read().decode()
        headers_out = dict(response.getheaders())
        connection.close()
        return response.status, headers_out, raw

    def test_developer_role_and_tool_call_id_name_recovery(self):
        prompt, _ = messages_to_prompt([
            {"role": "developer", "content": "Follow rules strictly."},
            {"role": "user", "content": "Read config.json"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_abc123",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"config.json"}'},
                }],
            },
            {"role": "tool", "tool_call_id": "call_abc123", "content": '{"port": 8081}'},
        ])
        self.assertIn("[System instruction]: Follow rules strictly.", prompt)
        self.assertIn("[Tool result for read_file (call_abc123)]: {\"port\": 8081}", prompt)

    def test_tool_protocol_is_placed_after_messages_and_compacted(self):
        prompt, _ = messages_to_prompt(
            [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Inspect config.json"},
            ],
            tools=[{
                "type": "function",
                "function": {"name": "read", "description": "Read a file", "parameters": {"type": "object"}},
            }],
        )
        self.assertLess(prompt.index("Inspect config.json"), prompt.index("[External OpenAI Tool Harness Protocol]"))
        self.assertIn('{"name":"read"', prompt)

    def test_parse_tool_calls_fallback_respects_allowed_names(self):
        raw_json_reply = '```json\n{"name": "read_file", "arguments": {"path": "README.md"}}\n```'
        # Without allowed_names, plain ```json fences stay text.
        clean_strict, calls_strict = parse_tool_calls(raw_json_reply)
        self.assertEqual(calls_strict, [])
        self.assertIn("read_file", clean_strict)

        # With allowed_names matching declared tools, recover cleanly.
        clean_ok, calls_ok = parse_tool_calls(raw_json_reply, allowed_names={"read_file"})
        self.assertEqual(clean_ok, "")
        self.assertEqual(len(calls_ok), 1)
        self.assertEqual(calls_ok[0]["function"]["name"], "read_file")
        self.assertEqual(json.loads(calls_ok[0]["function"]["arguments"]), {"path": "README.md"})

        # Unregistered tool names are never turned into tool calls.
        _, calls_unknown = parse_tool_calls(
            '{"name": "unknown_fn", "arguments": {"x": 1}}',
            allowed_names={"read_file"},
        )
        self.assertEqual(calls_unknown, [])

    def test_parse_tool_calls_multi_json_inside_single_fence(self):
        text = (
            '```tool_call\n'
            '{"name": "read_file", "arguments": {"path": "a.txt"}}\n'
            '{"name": "glob", "arguments": {"pattern": "*.py"}}\n'
            '```'
        )
        clean, calls = parse_tool_calls(text)
        self.assertEqual(clean, "")
        self.assertEqual([c["function"]["name"] for c in calls], ["read_file", "glob"])

    @mock.patch(
        "gemini_web2api.server.generate",
        return_value='```tool_call\n{"name": "read_file", "arguments": {"path": "README.md"}}\n```',
    )
    def test_chat_stream_tool_calls_include_index_role_and_usage(self, _mock_generate):
        status, headers, body = self._request_json(
            "POST",
            "/v1/chat/completions",
            {
                "model": "gemini-4-neon",
                "stream": True,
                "stream_options": {"include_usage": True},
                "messages": [{"role": "user", "content": "Read README.md"}],
                "tools": [{
                    "type": "function",
                    "function": {"name": "read_file", "parameters": {"type": "object"}},
                }],
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/event-stream")
        chunks = [
            json.loads(line[len("data: "):])
            for line in body.splitlines()
            if line.startswith("data: {")
        ]
        self.assertEqual(chunks[0]["choices"][0]["delta"], {"role": "assistant"})
        tc_chunk = next(c for c in chunks if c["choices"] and "tool_calls" in c["choices"][0]["delta"])
        self.assertEqual(tc_chunk["choices"][0]["delta"]["tool_calls"][0]["index"], 0)
        self.assertEqual(tc_chunk["choices"][0]["delta"]["tool_calls"][0]["function"]["name"], "read_file")
        finish_chunk = next(c for c in chunks if c["choices"] and c["choices"][0]["finish_reason"] is not None)
        self.assertEqual(finish_chunk["choices"][0]["finish_reason"], "tool_calls")
        usage_chunk = next(c for c in chunks if "usage" in c)
        self.assertIn("total_tokens", usage_chunk["usage"])
        self.assertTrue(body.endswith("data: [DONE]\n\n"))

    def test_models_and_admin_telemetry_logs_endpoints(self):
        st_m, _, raw_m = self._request_json("GET", "/v1/models")
        self.assertEqual(st_m, 200)
        models_data = json.loads(raw_m)["data"]
        neon = next(m for m in models_data if m["id"] == "gemini-4-neon")
        self.assertIn("context_window", neon)
        self.assertIn("max_tokens", neon)

        st_s, _, raw_s = self._request_json("GET", "/admin/auth/status")
        self.assertEqual(st_s, 200)
        status_json = json.loads(raw_s)
        self.assertIn("telemetry", status_json)
        self.assertIn("models", status_json)

        st_l, _, raw_l = self._request_json("GET", "/admin/logs?lines=20")
        self.assertEqual(st_l, 200)
        self.assertTrue(json.loads(raw_l)["ok"])

        st_c, _, _ = self._request_json("POST", "/admin/telemetry/clear", {})
        self.assertEqual(st_c, 200)

        st_bad_cfg, _, _ = self._request_json("POST", "/admin/config", {"default_model": "non-existent-model"})
        self.assertEqual(st_bad_cfg, 400)


if __name__ == "__main__":
    unittest.main()
