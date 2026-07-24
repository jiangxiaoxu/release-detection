from __future__ import annotations

import http.client
import io
import json
import unittest
from email.message import Message
from unittest import mock
from urllib import error

from scripts import release_detection


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode("utf-8")
        self.headers = Message()

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


class RawResponse(FakeResponse):
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.headers = Message()


class IncompleteResponse(FakeResponse):
    def read(self) -> bytes:
        raise http.client.IncompleteRead(b'{"partial":')


class FailingResponse(FakeResponse):
    def __init__(self, read_error: BaseException) -> None:
        super().__init__({})
        self.read_error = read_error

    def read(self) -> bytes:
        raise self.read_error


class FailingErrorBody:
    def __init__(self, read_error: BaseException) -> None:
        self.read_error = read_error

    def read(self, *_args: object, **_kwargs: object) -> bytes:
        raise self.read_error

    def close(self) -> None:
        return None


def make_http_error(
    status: int,
    retry_after: str | None = None,
    *,
    payload: bytes = b'{"message":"temporary failure"}',
    body_error: BaseException | None = None,
) -> error.HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    error_body = FailingErrorBody(body_error) if body_error else io.BytesIO(payload)
    return error.HTTPError(
        "https://example.test/api",
        status,
        "temporary failure",
        headers,
        error_body,
    )


class HttpRetryTests(unittest.TestCase):
    @mock.patch.object(release_detection.time, "sleep")
    @mock.patch.object(release_detection.request, "urlopen")
    def test_retries_transient_statuses_and_respects_retry_after(
        self, urlopen: mock.Mock, sleep: mock.Mock
    ) -> None:
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                urlopen.reset_mock(side_effect=True)
                sleep.reset_mock()
                retry_after = "7" if status == 503 else None
                urlopen.side_effect = [
                    make_http_error(status, retry_after),
                    FakeResponse({"ok": True}),
                ]

                result = release_detection.http_json(
                    "GET", "https://example.test/api"
                )

                self.assertEqual(result, {"ok": True})
                self.assertEqual(urlopen.call_count, 2)
                urlopen.assert_called_with(
                    mock.ANY, timeout=release_detection.HTTP_TIMEOUT_SECONDS
                )
                sleep.assert_called_once_with(7.0 if status == 503 else 1.0)

    @mock.patch.object(release_detection.time, "sleep")
    @mock.patch.object(release_detection.request, "urlopen")
    def test_retries_incomplete_response(
        self, urlopen: mock.Mock, sleep: mock.Mock
    ) -> None:
        urlopen.side_effect = [
            IncompleteResponse({}),
            FakeResponse({"complete": True}),
        ]

        result = release_detection.http_json("GET", "https://example.test/api")

        self.assertEqual(result, {"complete": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1.0)

    @mock.patch.object(release_detection.time, "sleep")
    @mock.patch.object(release_detection.request, "urlopen")
    def test_retries_connection_reset_while_reading_response(
        self, urlopen: mock.Mock, sleep: mock.Mock
    ) -> None:
        urlopen.side_effect = [
            FailingResponse(ConnectionResetError("peer reset")),
            FakeResponse({"complete": True}),
        ]

        result = release_detection.http_json("GET", "https://example.test/api")

        self.assertEqual(result, {"complete": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1.0)

    @mock.patch.object(release_detection.time, "sleep")
    @mock.patch.object(release_detection.request, "urlopen")
    def test_http_error_body_network_failure_preserves_status_and_retries(
        self, urlopen: mock.Mock, sleep: mock.Mock
    ) -> None:
        for body_error in (
            TimeoutError("body timed out"),
            ConnectionResetError("body reset"),
        ):
            with self.subTest(body_error=type(body_error).__name__):
                urlopen.reset_mock(side_effect=True)
                sleep.reset_mock()
                urlopen.side_effect = [
                    make_http_error(503, body_error=body_error),
                    FakeResponse({"ok": True}),
                ]

                result = release_detection.http_json(
                    "GET", "https://example.test/api"
                )

                self.assertEqual(result, {"ok": True})
                self.assertEqual(urlopen.call_count, 2)
                sleep.assert_called_once_with(1.0)

    @mock.patch.object(release_detection.time, "sleep")
    @mock.patch.object(release_detection.request, "urlopen")
    def test_http_error_body_failure_has_clear_non_retryable_error(
        self, urlopen: mock.Mock, sleep: mock.Mock
    ) -> None:
        urlopen.side_effect = make_http_error(
            404,
            body_error=ConnectionResetError("body reset"),
        )

        with self.assertRaisesRegex(
            RuntimeError,
            r"HTTP 404 .*response body unavailable: ConnectionResetError",
        ):
            release_detection.http_json("GET", "https://example.test/api")

        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    @mock.patch.object(release_detection.time, "sleep")
    @mock.patch.object(release_detection.request, "urlopen")
    def test_json_decode_error_is_not_retried(
        self, urlopen: mock.Mock, sleep: mock.Mock
    ) -> None:
        urlopen.return_value = RawResponse(b"not-json")

        with self.assertRaises(json.JSONDecodeError):
            release_detection.http_json("GET", "https://example.test/api")

        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    @mock.patch.object(release_detection.time, "sleep")
    @mock.patch.object(release_detection.request, "urlopen")
    def test_url_error_retries_are_bounded(
        self, urlopen: mock.Mock, sleep: mock.Mock
    ) -> None:
        urlopen.side_effect = error.URLError("connection reset")

        with self.assertRaisesRegex(RuntimeError, "after 4 attempt"):
            release_detection.http_json("GET", "https://example.test/api")

        self.assertEqual(urlopen.call_count, release_detection.HTTP_MAX_ATTEMPTS)
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [1.0, 2.0, 4.0],
        )

    @mock.patch.object(release_detection.time, "sleep")
    @mock.patch.object(release_detection.request, "urlopen")
    def test_non_retryable_status_fails_immediately(
        self, urlopen: mock.Mock, sleep: mock.Mock
    ) -> None:
        urlopen.side_effect = make_http_error(404)

        with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
            release_detection.http_json("GET", "https://example.test/api")

        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()

    @mock.patch.object(release_detection.time, "sleep")
    @mock.patch.object(release_detection.request, "urlopen")
    def test_mutating_issue_methods_are_not_retried_by_default(
        self, urlopen: mock.Mock, sleep: mock.Mock
    ) -> None:
        for method in ("POST", "PATCH"):
            with self.subTest(method=method):
                urlopen.reset_mock(side_effect=True)
                sleep.reset_mock()
                urlopen.side_effect = make_http_error(503)

                with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                    release_detection.http_json(
                        method,
                        "https://example.test/issues",
                        body={"title": "do not duplicate"},
                    )

                self.assertEqual(urlopen.call_count, 1)
                sleep.assert_not_called()

    @mock.patch.object(release_detection.time, "sleep")
    @mock.patch.object(release_detection.request, "urlopen")
    def test_post_can_opt_in_to_transient_retry(
        self, urlopen: mock.Mock, sleep: mock.Mock
    ) -> None:
        urlopen.side_effect = [make_http_error(503), FakeResponse({"ok": True})]

        result = release_detection.http_json(
            "POST",
            "https://example.test/api",
            body={"query": "safe"},
            retry_transient=True,
        )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_malformed_retry_after_is_ignored(self) -> None:
        self.assertIsNone(release_detection.parse_retry_after("not-a-date"))
        self.assertIsNone(release_detection.parse_retry_after("9" * 4000))


class LabelTests(unittest.TestCase):
    @mock.patch.object(release_detection, "http_json", return_value={})
    def test_labels_are_only_ensured_once_per_process(
        self, http_json: mock.Mock
    ) -> None:
        ensured_labels: set[str] = set()

        release_detection.ensure_labels(
            "owner", "repo", "token", ["release-detection", "automated"], ensured_labels
        )
        release_detection.ensure_labels(
            "owner", "repo", "token", ["release-detection", "automated"], ensured_labels
        )

        self.assertEqual(http_json.call_count, 2)
        self.assertTrue(
            all(
                call.kwargs["retry_transient"]
                for call in http_json.call_args_list
            )
        )
        self.assertEqual(ensured_labels, {"release-detection", "automated"})

    @mock.patch.object(release_detection.time, "sleep")
    @mock.patch.object(release_detection.request, "urlopen")
    def test_label_503_then_already_exists_is_cached(
        self, urlopen: mock.Mock, sleep: mock.Mock
    ) -> None:
        urlopen.side_effect = [
            make_http_error(503),
            make_http_error(
                422,
                payload=b'{"errors":[{"code":"already_exists"}]}',
            ),
        ]
        ensured_labels: set[str] = set()

        release_detection.ensure_labels(
            "owner", "repo", "token", ["release-detection"], ensured_labels
        )
        release_detection.ensure_labels(
            "owner", "repo", "token", ["release-detection"], ensured_labels
        )

        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(1.0)
        self.assertEqual(ensured_labels, {"release-detection"})


if __name__ == "__main__":
    unittest.main()
