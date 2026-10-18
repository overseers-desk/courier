"""The Google token endpoint is asked with a timeout on every call, so a
refresh or a code exchange cannot hang a connect() for ever."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from courier import browser_auth, oauth2


def _response(payload):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


def _config() -> SimpleNamespace:
    """The attributes get_access_token and exchange_code_for_tokens read."""
    return SimpleNamespace(
        client_id="id",
        client_secret="secret",
        refresh_token="refresh",
        access_token=None,
        token_expiry=0,
    )


def test_token_refresh_carries_a_timeout():
    tokens = _response({"access_token": "t", "expires_in": 3600})
    with patch("courier.oauth2.requests.post", return_value=tokens) as post:
        oauth2.get_access_token(_config())
    assert post.call_args.kwargs["timeout"] == oauth2.TOKEN_REQUEST_TIMEOUT


def test_code_exchange_carries_a_timeout():
    tokens = _response({"access_token": "t", "refresh_token": "r", "expires_in": 1})
    with patch("courier.oauth2.requests.post", return_value=tokens) as post:
        oauth2.exchange_code_for_tokens(_config(), "code")
    assert post.call_args.kwargs["timeout"] == oauth2.TOKEN_REQUEST_TIMEOUT


def test_browser_callback_exchange_carries_a_timeout():
    app = browser_auth.create_oauth_app()
    app.config.update(client_id="id", client_secret="secret", redirect_uri="r")
    tokens = _response({"access_token": "t", "refresh_token": "r", "expires_in": 1})
    with patch("requests.post", return_value=tokens) as post:
        reply = app.test_client().get(f"{browser_auth.CALLBACK_PATH}?code=abc")
    assert reply.status_code == 302
    assert post.call_args.kwargs["timeout"] == oauth2.TOKEN_REQUEST_TIMEOUT
