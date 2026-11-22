from unittest.mock import MagicMock, patch

from app.services.email_service import EmailService


def test_send_email_multipart_alternative_when_html_provided(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.EMAIL_USER", "test@example.com")
    monkeypatch.setattr("app.core.config.settings.EMAIL_APP_PASSWORD", "secret")
    monkeypatch.setattr("app.core.config.settings.EMAIL_TO", "recipient@example.com")

    service = EmailService()
    assert service.is_configured() is True

    subject = "✅ FinBot Bilanço Takvimi — Güncel — 17 Ağustos 2026"
    body_text = "FİNBOT BİLANÇO TAKVİMİ\nDurum: Başarılı"
    body_html = "<html><body><h1>Bilanço Takvimi Güncellendi</h1></body></html>"

    with patch("smtplib.SMTP") as mock_smtp_cls:
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_server

        success = service.send_email(
            subject=subject,
            body_text=body_text,
            body_html=body_html
        )

        assert success is True
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("test@example.com", "secret")
        mock_server.send_message.assert_called_once()

        sent_msg = mock_server.send_message.call_args[0][0]
        assert sent_msg.is_multipart() is True
        assert sent_msg.get_content_type() == "multipart/alternative"
        assert sent_msg["Subject"] == subject
        assert sent_msg["From"] == "test@example.com"
        assert sent_msg["To"] == "recipient@example.com"

        payloads = sent_msg.get_payload()
        assert len(payloads) == 2

        # 1. Plain text first
        assert payloads[0].get_content_type() == "text/plain"
        assert payloads[0].get_content_charset() == "utf-8"
        assert "FİNBOT BİLANÇO TAKVİMİ" in payloads[0].get_payload(decode=True).decode("utf-8")

        # 2. HTML second
        assert payloads[1].get_content_type() == "text/html"
        assert payloads[1].get_content_charset() == "utf-8"
        assert "<h1>Bilanço Takvimi Güncellendi</h1>" in payloads[1].get_payload(decode=True).decode("utf-8")


def test_send_email_plain_text_backward_compatibility(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.EMAIL_USER", "test@example.com")
    monkeypatch.setattr("app.core.config.settings.EMAIL_APP_PASSWORD", "secret")
    monkeypatch.setattr("app.core.config.settings.EMAIL_TO", "recipient@example.com")

    service = EmailService()

    subject = "BIST Sabah Özeti — 17.08.2026"
    body_text = "Sabah piyasa bülteni özeti..."
    warnings = ["API gecikmesi yaşandı"]
    raw_summary = '{"bist100": 10500}'
    fetch_time = "17.08.2026 08:30:00"

    with patch("smtplib.SMTP") as mock_smtp_cls:
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_server

        success = service.send_email(
            subject=subject,
            body_text=body_text,
            body_html=None,
            warnings=warnings,
            raw_data_summary=raw_summary,
            fetch_time=fetch_time
        )

        assert success is True
        mock_server.send_message.assert_called_once()
        sent_msg = mock_server.send_message.call_args[0][0]

        payloads = sent_msg.get_payload()
        assert len(payloads) == 1
        assert payloads[0].get_content_type() == "text/plain"
        plain_content = payloads[0].get_payload(decode=True).decode("utf-8")

        assert "Sabah piyasa bülteni özeti..." in plain_content
        assert "Veri Çekilme Saati: 17.08.2026 08:30:00" in plain_content
        assert "API gecikmesi yaşandı" in plain_content
        assert '{"bist100": 10500}' in plain_content


def test_send_email_skipped_when_not_configured(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.EMAIL_USER", None)
    monkeypatch.setattr("app.core.config.settings.EMAIL_APP_PASSWORD", None)
    monkeypatch.setattr("app.core.config.settings.EMAIL_TO", None)

    service = EmailService()
    assert service.is_configured() is False

    success = service.send_email(subject="Test", body_text="Hello")
    assert success is False


def test_send_email_positional_arguments_backward_compatibility(monkeypatch):
    """Verify legacy 5-argument positional call (subject, body, warnings, raw_summary, fetch_time)."""
    monkeypatch.setattr("app.core.config.settings.EMAIL_USER", "test@example.com")
    monkeypatch.setattr("app.core.config.settings.EMAIL_APP_PASSWORD", "secret")
    monkeypatch.setattr("app.core.config.settings.EMAIL_TO", "recipient@example.com")

    service = EmailService()

    subject = "Piyasa Kapanış Raporu"
    body_text = "Kapanış bülteni detayları..."
    warnings = ["API gecikmesi uyarısı"]
    raw_summary = '{"bist30": 11200}'
    fetch_time = "17.08.2026 18:15:00"

    with patch("smtplib.SMTP") as mock_smtp_cls:
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__.return_value = mock_server

        # Positional call: exactly 5 positional args
        success = service.send_email(
            subject,
            body_text,
            warnings,
            raw_summary,
            fetch_time,
        )

        assert success is True
        mock_server.send_message.assert_called_once()
        sent_msg = mock_server.send_message.call_args[0][0]

        # Must not be multipart/alternative
        payloads = sent_msg.get_payload()
        assert len(payloads) == 1
        assert payloads[0].get_content_type() == "text/plain"
        plain_content = payloads[0].get_payload(decode=True).decode("utf-8")

        assert "Kapanış bülteni detayları..." in plain_content
        assert "Veri Çekilme Saati: 17.08.2026 18:15:00" in plain_content
        assert "API gecikmesi uyarısı" in plain_content
        assert '{"bist30": 11200}' in plain_content
        assert "<html" not in plain_content
