import pytest

from opskit.approvals.resume import InvalidResumeUrl, internal_resume_target
from opskit.config import Settings

SIG = "a27da6752fbd2b9b5fd6362899eb51ca38c2a384c93f77400b29a10389750bac"


@pytest.fixture
def settings() -> Settings:
    return Settings(n8n_public_url="http://localhost:4300", n8n_internal_url="http://n8n:5678")


def test_public_url_is_rewritten_to_internal(settings: Settings) -> None:
    url = f"http://localhost:4300/webhook-waiting/42?signature={SIG}"
    assert (
        internal_resume_target(url, settings)
        == f"http://n8n:5678/webhook-waiting/42?signature={SIG}"
    )


def test_internal_url_is_accepted(settings: Settings) -> None:
    url = f"http://n8n:5678/webhook-waiting/7?signature={SIG}"
    assert internal_resume_target(url, settings) == url


@pytest.mark.parametrize(
    "url",
    [
        f"https://evil.example/webhook-waiting/42?signature={SIG}",
        f"http://localhost:4301/webhook-waiting/42?signature={SIG}",
        f"http://localhost:4300/webhook/42?signature={SIG}",
        "http://localhost:4300/webhook-waiting/42",
        "http://localhost:4300/webhook-waiting/42?signature=",
        f"http://localhost:4300/webhook-waiting/42?signature={SIG}&signature={SIG}",
        f"http://localhost:4300/webhook-waiting/42?signature={SIG}&next=http://evil.example",
        f"http://a@localhost:4300/webhook-waiting/42?signature={SIG}",
        f"http://localhost:4300/webhook-waiting/..%2F..%2Frest?signature={SIG}",
        f"http://localhost:4300/webhook-waiting/abc?signature={SIG}",
        f"http://localhost:4300/webhook-waiting/42?signature={SIG}#frag",
        "http://localhost:4300/webhook-waiting/42?signature=zz-not-hex",
    ],
)
def test_anything_else_is_rejected(settings: Settings, url: str) -> None:
    with pytest.raises(InvalidResumeUrl):
        internal_resume_target(url, settings)
