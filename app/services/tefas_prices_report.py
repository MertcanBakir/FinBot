import html
from datetime import date, datetime
from typing import Optional
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.models.tefas_prices import TefasPricesJobResult

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


def format_tefas_prices_subject(result: TefasPricesJobResult) -> str:
    """Generate subject line for TEFAS prices alert email."""
    try:
        if result.tefas_data_date:
            d = date.fromisoformat(result.tefas_data_date)
            date_str = format_date_tr(d)
        else:
            dt = datetime.fromisoformat(result.started_at)
            if dt.tzinfo is not None:
                dt = dt.astimezone(ZoneInfo(settings.TIMEZONE))
            date_str = format_date_tr(dt.date())
    except Exception:
        date_str = result.tefas_data_date or result.started_at

    if result.status == "failed":
        return f"❌ FinBot TEFAS Fiyatları — Güncelleme başarısız — {date_str}"
    elif result.status == "partial":
        return f"⚠️ FinBot TEFAS Fiyatları — Kısmi güncelleme — {date_str}"
    return f"✅ FinBot TEFAS Fiyatları — Başarılı — {date_str}"


def format_tefas_prices_email_text(result: TefasPricesJobResult) -> str:
    """Generate plain text email body for TEFAS latest prices alerts."""
    status_label = "KISMI BAŞARI" if result.status == "partial" else "BAŞARISIZ"

    lines = [
        f"FinBot TEFAS Fiyat Güncelleme Raporu — {status_label}",
        "=" * 50,
        f"Durum: {status_label}",
        f"Başlangıç: {format_datetime_tr(result.started_at)}",
        f"Bitiş: {format_datetime_tr(result.finished_at)}",
        f"Süre: {result.duration_seconds:.2f} saniye",
        f"Güncel TEFAS Veri Tarihi: {result.tefas_data_date or 'Belirtilmedi'}",
        f"Önceki TEFAS Veri Tarihi: {result.previous_data_date or 'Bulunamadı'}",
        "",
        "KATALOG MUTABAKAT METRİKLERİ:",
        "-" * 30,
        f"Gözlemlenen TEFAS Fonu: {result.catalog_observed_count}",
        f"Yeni Eklenen Fon: {result.catalog_added_count} (Aktif: {result.catalog_active_added_count}, Pasif/Fiyatsız: {result.catalog_unpriced_added_count})",
        f"İsmi Güncellenen Fon: {result.catalog_name_updated_count}",
        f"Yeniden Aktifleştirilen Fon: {result.catalog_reactivated_count}",
        f"Pasifleştirilen Fon: {result.catalog_deactivated_count} (Fiyat Geçmişi Olmayan: {result.catalog_unpriced_deactivated_count}, Katalogdan Kaybolan: {result.catalog_disappeared_deactivated_count})",
    ]

    if result.catalog_identity_invalid_count > 0 or result.catalog_disappearance_reconciliation_skipped:
        lines.append(f"Kimliği Belirlenemeyen Katalog Satırı: {result.catalog_identity_invalid_count}")
        lines.append(f"Kayıp Fon Mutabakatı: ATLANDI (Gerekçe: {result.catalog_disappearance_skip_reason or 'Kimliği belirsiz katalog satırları'})")

    lines.extend([
        "",
        "FİYAT VE DEĞİŞİM METRİKLERİ:",
        "-" * 30,
        f"Çekilen Toplam Satır: {result.fetched_count}",
        f"Geçerli Güncel Fiyat: {result.valid_count}",
        f"Geçersiz Satır / Fiyat: {result.invalid_count}",
        f"Duplicate Fon Kodu: {result.duplicate_count}",
        f"Eşleşen Aktif Fon: {result.matched_count}",
        f"Önceki Fiyatı Eşleşen: {result.previous_price_matched_count}",
        f"Önceki Fiyatı Bulunamayan: {result.previous_price_missing_count}",
        f"Eşleşmeyen TEFAS Fonu: {result.unmatched_count}",
        f"Fiyatı Gelmeyen Aktif Fon: {result.missing_price_count}",
        f"Yazılan / Güncellenen Fiyat: {result.inserted_or_updated_count}",
        f"Değişmeden Kalan Fiyat: {result.unchanged_count}",
        f"Eski Tarihli Atlanan Fiyat: {result.stale_skipped_count}",
        f"Stale Yapılan (Yayımlanmayan Fiyat): {result.price_not_published_stale_count}",
        f"Stale Yapılan (Pasif Enstrüman): {result.instrument_inactive_stale_count}",
    ])

    if result.error:
        lines.extend(["", "HATA:", "-" * 30, result.error])

    if result.warnings:
        lines.extend(["", f"UYARILAR ({len(result.warnings)} toplam):", "-" * 30])
        for w in result.warnings[:10]:
            lines.append(f"• {w}")
        if len(result.warnings) > 10:
            lines.append(f"… ve {len(result.warnings) - 10} kayıt daha")

    return "\n".join(lines)


def format_tefas_prices_email_html(result: TefasPricesJobResult) -> str:
    """Generate Outlook-compatible HTML email body for TEFAS latest prices alerts."""
    is_partial = result.status == "partial"
    status_title = "Kısmi Güncelleme" if is_partial else "Güncelleme Başarısız"
    badge_bg = "#FEF3C7" if is_partial else "#FEE2E2"
    badge_text = "#B45309" if is_partial else "#B91C1C"
    badge_border = "#FDE68A" if is_partial else "#FECACA"

    warnings_html = ""
    if result.warnings:
        items_html = "".join(f"<li style='margin-bottom:4px;'>{html.escape(w)}</li>" for w in result.warnings[:10])
        more_html = (
            f"<p style='margin-top:8px;margin-bottom:0;font-size:12px;color:#6B7280;font-style:italic;'>… ve {len(result.warnings) - 10} kayıt daha</p>"
            if len(result.warnings) > 10
            else ""
        )
        warnings_html = f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:20px;background-color:#FFFBEB;border:1px solid #FDE68A;border-radius:8px;">
            <tr>
                <td style="padding:16px;">
                    <h3 style="margin:0 0 10px 0;font-size:14px;color:#92400E;">⚠️ Uyarılar ve Atlanan Kayıtlar ({len(result.warnings)} adet)</h3>
                    <ul style="margin:0;padding-left:20px;font-size:13px;color:#78350F;line-height:1.5;">
                        {items_html}
                    </ul>
                    {more_html}
                </td>
            </tr>
        </table>
        """

    error_html = ""
    if result.error:
        error_html = f"""
        <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="margin-top:20px;background-color:#FEF2F2;border:1px solid #FECACA;border-radius:8px;">
            <tr>
                <td style="padding:16px;">
                    <h3 style="margin:0 0 8px 0;font-size:14px;color:#991B1B;">❌ Kritik Hata</h3>
                    <p style="margin:0;font-size:13px;color:#7F1D1D;font-family:monospace;">{html.escape(result.error)}</p>
                </td>
            </tr>
        </table>
        """

    start_formatted = format_datetime_tr(result.started_at)
    date_formatted = html.escape(result.tefas_data_date or 'Belirtilmedi')
    prev_date_formatted = html.escape(result.previous_data_date or 'Bulunamadı')

    deact_details = []
    if result.catalog_unpriced_deactivated_count > 0:
        deact_details.append(f"{result.catalog_unpriced_deactivated_count} fiyatsız")
    if result.catalog_disappeared_deactivated_count > 0:
        deact_details.append(f"{result.catalog_disappeared_deactivated_count} kayıp")
    deact_str = f"{result.catalog_deactivated_count} ({', '.join(deact_details)})" if deact_details else str(result.catalog_deactivated_count)

    identity_warning_row = ""
    if result.catalog_identity_invalid_count > 0 or result.catalog_disappearance_reconciliation_skipped:
        identity_warning_row = f"""
        <tr style="border-bottom:1px solid #F3F4F6;background-color:#FEF3C7;">
            <td style="padding:6px 8px;color:#92400E;font-weight:600;">Kimliği Belirsiz Katalog Satırı</td>
            <td align="right" style="padding:6px 8px;font-weight:700;color:#B45309;">{result.catalog_identity_invalid_count} (Kayıp Mutabakatı Atlandı)</td>
        </tr>
        """

    return f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{status_title}</title>
</head>
<body style="margin:0;padding:24px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background-color:#F9FAFB;color:#111827;">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
        <tr>
            <td align="center">
                <table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="max-width:600px;background-color:#FFFFFF;border:1px solid #E5E7EB;border-radius:12px;overflow:hidden;">
                    <!-- Header -->
                    <tr>
                        <td style="padding:24px;border-bottom:1px solid #E5E7EB;">
                            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                                <tr>
                                    <td align="left" style="font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;color:#6B7280;">
                                        FinBot TEFAS Fiyat Senkronizasyonu
                                    </td>
                                    <td align="right">
                                        <span style="display:inline-block;padding:4px 10px;font-size:12px;font-weight:600;border-radius:9999px;background-color:{badge_bg};color:{badge_text};border:1px solid {badge_border};">
                                            {status_title}
                                        </span>
                                    </td>
                                </tr>
                            </table>
                            <h1 style="margin:12px 0 0 0;font-size:20px;font-weight:700;color:#111827;">TEFAS Fon Fiyatları Raporu</h1>
                            <p style="margin:6px 0 0 0;font-size:13px;color:#6B7280;">Güncel Tarih: <strong>{date_formatted}</strong> • Önceki Tarih: <strong>{prev_date_formatted}</strong><br>Başlangıç: {start_formatted} • Süre: {result.duration_seconds:.2f}s</p>
                        </td>
                    </tr>

                    <!-- Content -->
                    <tr>
                        <td style="padding:20px 24px;">
                            <!-- Catalog Section -->
                            <h2 style="margin:0 0 12px 0;font-size:15px;font-weight:600;color:#374151;">Dinamik Katalog Mutabakatı</h2>
                            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="font-size:13px;margin-bottom:20px;">
                                {identity_warning_row}
                                <tr style="border-bottom:1px solid #F3F4F6;">
                                    <td style="padding:6px 0;color:#4B5563;">Gözlemlenen TEFAS Fonu</td>
                                    <td align="right" style="padding:6px 0;font-weight:600;color:#111827;">{result.catalog_observed_count}</td>
                                </tr>
                                <tr style="border-bottom:1px solid #F3F4F6;">
                                    <td style="padding:6px 0;color:#4B5563;">Yeni Eklenen Fon</td>
                                    <td align="right" style="padding:6px 0;font-weight:600;color:#2563EB;">{result.catalog_added_count}</td>
                                </tr>
                                <tr style="border-bottom:1px solid #F3F4F6;">
                                    <td style="padding:6px 0;color:#4B5563;">İsmi Güncellenen Fon</td>
                                    <td align="right" style="padding:6px 0;font-weight:600;color:#4B5563;">{result.catalog_name_updated_count}</td>
                                </tr>
                                <tr style="border-bottom:1px solid #F3F4F6;">
                                    <td style="padding:6px 0;color:#4B5563;">Yeniden Aktifleştirilen Fon</td>
                                    <td align="right" style="padding:6px 0;font-weight:600;color:#059669;">{result.catalog_reactivated_count}</td>
                                </tr>
                                <tr>
                                    <td style="padding:6px 0;color:#4B5563;">Pasifleştirilen Fon</td>
                                    <td align="right" style="padding:6px 0;font-weight:600;color:{'#D97706' if result.catalog_deactivated_count > 0 else '#6B7280'};">{deact_str}</td>
                                </tr>
                            </table>

                            <!-- Pricing & Changes Section -->
                            <h2 style="margin:0 0 12px 0;font-size:15px;font-weight:600;color:#374151;">Fiyat ve Değişim Senkronizasyonu</h2>
                            <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="font-size:13px;">
                                <tr style="border-bottom:1px solid #F3F4F6;">
                                    <td style="padding:6px 0;color:#4B5563;">Çekilen Toplam Satır</td>
                                    <td align="right" style="padding:6px 0;font-weight:600;color:#111827;">{result.fetched_count}</td>
                                </tr>
                                <tr style="border-bottom:1px solid #F3F4F6;">
                                    <td style="padding:6px 0;color:#4B5563;">Geçerli Güncel Fiyat</td>
                                    <td align="right" style="padding:6px 0;font-weight:600;color:#059669;">{result.valid_count}</td>
                                </tr>
                                <tr style="border-bottom:1px solid #F3F4F6;">
                                    <td style="padding:6px 0;color:#4B5563;">Geçersiz Satır / Fiyat</td>
                                    <td align="right" style="padding:6px 0;font-weight:600;color:{'#DC2626' if result.invalid_count > 0 else '#6B7280'};">{result.invalid_count}</td>
                                </tr>
                                <tr style="border-bottom:1px solid #F3F4F6;">
                                    <td style="padding:6px 0;color:#4B5563;">Eşleşen Aktif Enstrüman</td>
                                    <td align="right" style="padding:6px 0;font-weight:600;color:#111827;">{result.matched_count}</td>
                                </tr>
                                <tr style="border-bottom:1px solid #F3F4F6;">
                                    <td style="padding:6px 0;color:#4B5563;">Önceki Fiyatı Eşleşen</td>
                                    <td align="right" style="padding:6px 0;font-weight:600;color:#059669;">{result.previous_price_matched_count}</td>
                                </tr>
                                <tr style="border-bottom:1px solid #F3F4F6;">
                                    <td style="padding:6px 0;color:#4B5563;">Önceki Fiyatı Bulunamayan</td>
                                    <td align="right" style="padding:6px 0;font-weight:600;color:{'#D97706' if result.previous_price_missing_count > 0 else '#6B7280'};">{result.previous_price_missing_count}</td>
                                </tr>
                                <tr style="border-bottom:1px solid #F3F4F6;">
                                    <td style="padding:6px 0;color:#4B5563;">Yazılan / Güncellenen Fiyat</td>
                                    <td align="right" style="padding:6px 0;font-weight:600;color:#2563EB;">{result.inserted_or_updated_count}</td>
                                </tr>
                                <tr style="border-bottom:1px solid #F3F4F6;">
                                    <td style="padding:6px 0;color:#4B5563;">Değişmeden Kalan Fiyat</td>
                                    <td align="right" style="padding:6px 0;font-weight:600;color:#6B7280;">{result.unchanged_count}</td>
                                </tr>
                                <tr style="border-bottom:1px solid #F3F4F6;">
                                    <td style="padding:6px 0;color:#4B5563;">Stale Yapılan (Yayımlanmayan Fiyat)</td>
                                    <td align="right" style="padding:6px 0;font-weight:600;color:{'#D97706' if result.price_not_published_stale_count > 0 else '#6B7280'};">{result.price_not_published_stale_count}</td>
                                </tr>
                                <tr>
                                    <td style="padding:6px 0;color:#4B5563;">Stale Yapılan (Pasif Enstrüman)</td>
                                    <td align="right" style="padding:6px 0;font-weight:600;color:{'#D97706' if result.instrument_inactive_stale_count > 0 else '#6B7280'};">{result.instrument_inactive_stale_count}</td>
                                </tr>
                            </table>

                            {error_html}
                            {warnings_html}
                        </td>
                    </tr>

                    <!-- Footer -->
                    <tr>
                        <td align="center" style="padding:16px 24px;background-color:#F9FAFB;border-top:1px solid #E5E7EB;font-size:12px;color:#9CA3AF;">
                            FinBot Otomatik Bildirim Sistemi • {datetime.now(ZoneInfo(settings.TIMEZONE)).strftime('%Y')}
                        </td>
                    </tr>
                </table>
            </td>
        </tr>
    </table>
</body>
</html>"""
