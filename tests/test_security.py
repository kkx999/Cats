from app.security import hash_login_code, new_login_code, read_session, sign_session


def test_login_code_is_six_digits() -> None:
    code = new_login_code()
    assert len(code) == 6
    assert code.isdigit()


def test_code_is_not_stored_as_plain_text() -> None:
    assert hash_login_code("123456") != "123456"


def test_signed_session_round_trip() -> None:
    assert read_session(sign_session(123456789)) == 123456789


def test_tampered_session_is_rejected() -> None:
    token = sign_session(123456789)
    assert read_session(token[:-1] + ("0" if token[-1] != "0" else "1")) is None
