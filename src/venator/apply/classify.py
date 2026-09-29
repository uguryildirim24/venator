"""Reserve sensitive form fields before consulting any answer source."""

from __future__ import annotations

import re

from venator.fill.mapping import normalize_label


def reserved(label: str, *, kind: str = "text", page_is_login: bool = False) -> str | None:
    """Return a reserved bucket; no library answer may cross this boundary."""
    text = normalize_label(label)
    if page_is_login or kind == "password" or re.search(r"\b(password|passcode|verification code|one time code|otp)\b", text):
        return "sign_in"
    if kind == "file":
        return "file"
    if re.search(r"\b(consent|attest\w*|certif\w*|acknowledg\w*|privacy|terms and conditions|agree to|i agree|signature)\b", text):
        return "consent"
    if re.search(r"\b(gender|race|ethnic|hispanic|latino|veteran|disability|disabled|self identif\w*|demographic|sexual orientation|pronouns)\b", text):
        return "eeo"
    sponsorship = bool(re.search(r"\b(sponsorship|visa support|immigration support|work visa)\b", text))
    # 'Sponsored research' and sponsoring a colleague are not the candidate's visa.
    if sponsorship and ("research" in text or "colleague" in text):
        sponsorship = False
    authorization = bool(re.search(r"\b(authori[sz]ed to work|authori[sz]ation to work|eligible to work|legal right to work|lawful right to work|work permit|right to work)\b", text))
    if sponsorship and (authorization or "without" in text and "work" in text):
        return "authorization_sponsorship"
    if sponsorship:
        return "sponsorship"
    if authorization:
        return "authorization"
    return None
