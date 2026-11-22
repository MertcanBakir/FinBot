import html
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.models.crypto_prices import CryptoPricesJobResult

MONTH_NAMES_TR = {
    1: "Ocak",
    2: "Şubat",
    3: "Mart",
    4: "Nisan",
    5: "Mayıs",
    6: "Haziran",
    7: "Temmuz",
    8: "Ağustos",
    9: "Eylül",
    10: "Ekim",
    11: "Kasım",
    12: "Aralık",
}


def format_date_tr(d: date) -> str:
    """Format date to Turkish localized string: 21 Ağustos 2026."""
    return f"{d.day} {MONTH_NAMES_TR.get(d.month, '')} {d.year}"


def format_datetime_tr(iso_str: Optional[str], timezone_name: str = settings.TIMEZONE) -> str:
    """Format ISO datetime string to Turkish localized string in Istanbul timezone."""
    if not iso_str:
        return "Belirtilmedi"
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is not None:
            dt = dt.astimezone(ZoneInfo(timezone_name))
        month_name = MONTH_NAMES_TR.get(dt.month, "")
        return f"{dt.day} {month_name} {dt.year} • {dt.strftime('%H:%M:%S')}"
    except Exception:
        return iso_str


def format_crypto_prices_subject(result: CryptoPricesJobResult) -> str:
    """Generate subject line for Binance crypto prices alert email."""
    try:
        dt = datetime.fromisoformat(result.started_at)
        if dt.tzinfo is not None:
            dt = dt.astimezone(ZoneInfo(settings.TIMEZONE))
        date_str = format_date_tr(dt.date())
        time_str = dt.strftime("%H:%M")
    except Exception:
        date_str = result.started_at
        time_str = ""

    time_part = f" • {time_str}" if time_str else ""

    if result.status == "failed":
        return f"❌ FinBot Binance Kripto Fiyatları — Güncelleme başarısız — {date_str}{time_part}"
    elif result.status == "partial":
        return f"⚠️ FinBot Binance Kripto Fiyatları — Kısmi güncelleme — {date_str}{time_part}"
    return f"✅ FinBot Binance Kripto Fiyatları — Başarılı — {date_str}{time_part}"


def format_crypto_prices_email_text(result: CryptoPricesJobResult) -> str:
    """Generate plain text email body for Binance crypto prices alerts."""
    status_label = "KISMI BAŞARI" if result.status == "partial" else "BAŞARISIZ"

    lines = [
        f"FinBot Binance Kripto Fiyat Güncelleme Raporu — {status_label}",
        "=" * 50,
        f"Durum: {status_label}",
        f"Başlangıç: {format_datetime_tr(result.started_at)}",
        f"Bitiş: {format_datetime_tr(result.finished_at)}",
        f"Süre: {result.duration_seconds:.2f} saniye",
        "",
        "BİNANCE VE FİYAT METRİKLERİ:",
        "-" * 30,
        f"İstenen Aktif Kripto: {result.requested_active_count}",
        f"Binance'ten Dönen Ticker: {result.response_count}",
        f"Geçerli Fiyat: {result.valid_count}",
        f"Geçersiz Fiyat / Hata: {result.invalid_count}",
        f"Duplicate Ticker: {result.duplicate_count}",
        f"Eşleşen Kripto: {result.matched_count}",
        f"Eşleşmeyen Ticker: {result.unmatched_count}",
        f"Fiyatı Gelmeyen Aktif Kripto: {result.missing_count}",
        "",
        "VERİTABANI METRİKLERİ:",
        "-" * 30,
        f"Yazılan / Güncellenen Fiyat: {result.inserted_or_updated_count}",
        f"Değişmeden Kalan Fiyat: {result.unchanged_count}",
        f"Eski Tarihli Atlanan Fiyat: {result.stale_skipped_count}",
        f"Stale İşaretlenen (price_not_published): {result.price_not_published_stale_count}",
        f"Stale İşaretlenen (instrument_inactive): {result.instrument_inactive_stale_count}",
    ]

    if result.error:
        lines.extend([
            "",
            "HATA DETAYI:",
            "-" * 30,
            f"  • {result.error}",
        ])

    if result.warnings:
        lines.extend([
            "",
            "UYARILAR:",
            "-" * 30,
        ])
        for w in result.warnings[:15]:
            lines.append(f"  • {w}")
        if len(result.warnings) > 15:
            lines.append(f"  ... ve {len(result.warnings) - 15} uyarı daha")

    lines.extend([
        "",
        "-" * 50,
        "FinBot Otomasyon Sistemi • Bu e-posta bilgilendirme amaçlıdır.",
    ])

    return "\n".join(lines)


def format_crypto_prices_email_html(result: CryptoPricesJobResult) -> str:
    """Generate Outlook-compatible HTML email body for Binance crypto prices alerts."""
    is_partial = result.status == "partial"
    status_title = "Kısmi Güncelleme Bildirimi" if is_partial else "Güncelleme Başarısızlığı Bildirimi"
    status_badge_text = "KISMI BAŞARI (%80+)" if is_partial else "BAŞARISIZ"
    status_badge_bg = "#FEF3C7" if is_partial else "#FEE2E2"
    status_badge_color = "#92400E" if is_partial else "#991B1B"
    header_gradient_start = "#D97706" if is_partial else "#DC2626"
    header_gradient_end = "#78350F" if is_partial else "#7F1D1D"

    subject = format_crypto_prices_subject(result)

    # Warnings items
    warnings_rows = []
    if result.warnings:
        for w in result.warnings[:10]:
            warnings_rows.append(
                f'<tr><td style="padding: 4px 8px; font-size: 12px; color: #B45309;">⚠️ {html.escape(w)}</td></tr>'
            )
        if len(result.warnings) > 10:
            warnings_rows.append(
                f'<tr><td style="padding: 4px 8px; font-size: 12px; color: #92400E; font-style: italic;">... ve {len(result.warnings) - 10} uyarı daha</td></tr>'
            )

    warnings_section_html = ""
    if warnings_rows:
        warnings_section_html = f"""
        <div style="margin-bottom: 20px; padding: 14px; background-color: #FFFBEB; border: 1px solid #FDE68A; border-radius: 8px;">
          <div style="font-size: 13px; font-weight: 700; color: #92400E; margin-bottom: 8px;">Sistem Uyarıları ({len(result.warnings)})</div>
          <table role="presentation" style="width: 100%; border-collapse: collapse;">
            {''.join(warnings_rows)}
          </table>
        </div>
        """

    # Error box
    error_box_html = ""
    if result.error:
        error_box_html = f"""
        <div style="margin-bottom: 20px; padding: 14px; background-color: #FEF2F2; border: 1px solid #FECACA; border-radius: 8px;">
          <div style="font-size: 13px; font-weight: 700; color: #991B1B; margin-bottom: 4px;">Kritik Hata Detayı</div>
          <div style="font-size: 13px; color: #B91C1C;">{html.escape(result.error)}</div>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(subject)}</title>
</head>
<body style="margin: 0; padding: 20px 0; background-color: #F3F4F6; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased;">
  <div style="max-width: 640px; margin: 0 auto; background-color: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">

    <!-- HEADER -->
    <table role="presentation" width="100%" border="0" cellspacing="0" cellpadding="0" style="width: 100%; background: linear-gradient(135deg, {header_gradient_start} 0%, {header_gradient_end} 100%); background-color: {header_gradient_start};">
      <tr>
        <td style="padding: 24px; color: #FFFFFF;">
          <table role="presentation" style="width: 100%; border-collapse: collapse; margin-bottom: 10px;">
            <tr>
              <td style="font-size: 12px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; color: #FEF3C7;">FinBot Operasyon Raporu</td>
              <td style="text-align: right;">
                <span style="display: inline-block; padding: 3px 10px; font-size: 11px; font-weight: 700; border-radius: 9999px; color: {status_badge_color}; background-color: {status_badge_bg};">
                  {status_badge_text}
                </span>
              </td>
            </tr>
          </table>
          <h1 style="margin: 0 0 6px 0; font-size: 20px; font-weight: 700; color: #FFFFFF;">🪙 Binance Kripto Fiyat Senkronizasyonu</h1>
          <div style="font-size: 13px; color: #FDE68A;">
            {status_title}
          </div>
        </td>
      </tr>
    </table>

    <div style="padding: 24px;">

      {error_box_html}

      <!-- EXECUTION SUMMARY CARD -->
      <div style="margin-bottom: 20px; padding: 14px; background-color: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 8px;">
        <div style="font-size: 13px; font-weight: 700; color: #0F172A; margin-bottom: 10px;">⏱️ Çalışma Zamanı ve Süre</div>
        <table role="presentation" style="width: 100%; border-collapse: collapse; font-size: 12px; color: #475569;">
          <tr>
            <td style="padding: 3px 0;"><strong>Başlangıç:</strong></td>
            <td style="text-align: right; padding: 3px 0;">{format_datetime_tr(result.started_at)}</td>
          </tr>
          <tr>
            <td style="padding: 3px 0;"><strong>Bitiş:</strong></td>
            <td style="text-align: right; padding: 3px 0;">{format_datetime_tr(result.finished_at)}</td>
          </tr>
          <tr>
            <td style="padding: 3px 0;"><strong>Toplam Süre:</strong></td>
            <td style="text-align: right; padding: 3px 0;">{result.duration_seconds:.2f} saniye</td>
          </tr>
        </table>
      </div>

      <!-- BINANCE & PRICING METRICS -->
      <div style="margin-bottom: 20px;">
        <div style="font-size: 14px; font-weight: 700; color: #111827; margin-bottom: 8px;">📊 Binance ve Fiyat Metrikleri</div>
        <table role="presentation" style="width: 100%; border-collapse: collapse; border: 1px solid #E5E7EB; border-radius: 8px; overflow: hidden; font-size: 13px;">
          <tr style="background-color: #F9FAFB; border-bottom: 1px solid #E5E7EB;">
            <td style="padding: 8px 12px; color: #374151;">İstenen Aktif Kripto:</td>
            <td style="padding: 8px 12px; font-weight: 700; text-align: right; color: #111827;">{result.requested_active_count}</td>
          </tr>
          <tr style="border-bottom: 1px solid #F1F5F9;">
            <td style="padding: 8px 12px; color: #374151;">Binance'ten Dönen Ticker:</td>
            <td style="padding: 8px 12px; font-weight: 700; text-align: right; color: #111827;">{result.response_count}</td>
          </tr>
          <tr style="background-color: #F9FAFB; border-bottom: 1px solid #E5E7EB;">
            <td style="padding: 8px 12px; color: #374151;">Geçerli ve Eşleşen Fiyat:</td>
            <td style="padding: 8px 12px; font-weight: 700; text-align: right; color: #16A34A;">{result.matched_count}</td>
          </tr>
          <tr style="border-bottom: 1px solid #F1F5F9;">
            <td style="padding: 8px 12px; color: #374151;">Fiyatı Gelmeyen Aktif Kripto:</td>
            <td style="padding: 8px 12px; font-weight: 700; text-align: right; color: {'#DC2626' if result.missing_count > 0 else '#475569'};">{result.missing_count}</td>
          </tr>
          <tr style="background-color: #F9FAFB;">
            <td style="padding: 8px 12px; color: #374151;">Geçersiz / Duplicate / Hatalı:</td>
            <td style="padding: 8px 12px; font-weight: 700; text-align: right; color: {'#DC2626' if (result.invalid_count + result.duplicate_count) > 0 else '#475569'};">{result.invalid_count + result.duplicate_count}</td>
          </tr>
        </table>
      </div>

      <!-- DATABASE METRICS -->
      <div style="margin-bottom: 20px;">
        <div style="font-size: 14px; font-weight: 700; color: #111827; margin-bottom: 8px;">💾 Veritabanı Değişiklikleri</div>
        <table role="presentation" style="width: 100%; border-collapse: collapse; border: 1px solid #E5E7EB; border-radius: 8px; overflow: hidden; font-size: 13px;">
          <tr style="background-color: #F9FAFB; border-bottom: 1px solid #E5E7EB;">
            <td style="padding: 8px 12px; color: #374151;">Yazılan / Güncellenen Fiyat:</td>
            <td style="padding: 8px 12px; font-weight: 700; text-align: right; color: #16A34A;">{result.inserted_or_updated_count}</td>
          </tr>
          <tr style="border-bottom: 1px solid #F1F5F9;">
            <td style="padding: 8px 12px; color: #374151;">Değişmeden Kalan (No-op):</td>
            <td style="padding: 8px 12px; font-weight: 700; text-align: right; color: #475569;">{result.unchanged_count}</td>
          </tr>
          <tr style="background-color: #F9FAFB; border-bottom: 1px solid #E5E7EB;">
            <td style="padding: 8px 12px; color: #374151;">Stale İşaretlenen (price_not_published):</td>
            <td style="padding: 8px 12px; font-weight: 700; text-align: right; color: {'#D97706' if result.price_not_published_stale_count > 0 else '#475569'};">{result.price_not_published_stale_count}</td>
          </tr>
          <tr style="border-bottom: 1px solid #F1F5F9;">
            <td style="padding: 8px 12px; color: #374151;">Stale İşaretlenen (instrument_inactive):</td>
            <td style="padding: 8px 12px; font-weight: 700; text-align: right; color: {'#D97706' if result.instrument_inactive_stale_count > 0 else '#475569'};">{result.instrument_inactive_stale_count}</td>
          </tr>
          <tr style="background-color: #F9FAFB;">
            <td style="padding: 8px 12px; color: #374151;">Eski Tarihli Atlanan:</td>
            <td style="padding: 8px 12px; font-weight: 700; text-align: right; color: #475569;">{result.stale_skipped_count}</td>
          </tr>
        </table>
      </div>

      {warnings_section_html}

    </div>

    <!-- FOOTER -->
    <div style="padding: 16px 24px; background-color: #F9FAFB; border-top: 1px solid #E5E7EB; text-align: center; font-size: 11px; color: #9CA3AF;">
      FinBot Otomasyon Sistemi • Bu e-posta bilgilendirme amaçlıdır.
    </div>

  </div>
</body>
</html>
"""
