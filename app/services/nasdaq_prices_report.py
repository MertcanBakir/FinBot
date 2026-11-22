import html

from app.models.nasdaq_prices import NasdaqPricesJobResult


def format_nasdaq_prices_subject(result: NasdaqPricesJobResult) -> str:
    """Generate subject line for NASDAQ price alerts."""
    if result.status == "partial":
        return (
            f"[FinBot UYARI] NASDAQ100 Güncel Fiyat Senkronizasyonu - "
            f"Kısmi güncelleme ({result.matched_count}/{result.requested_active_count})"
        )
    return "[FinBot HATA] NASDAQ100 Güncel Fiyat Senkronizasyonu Başarısız"


def format_nasdaq_prices_email_text(result: NasdaqPricesJobResult) -> str:
    """Generate plain-text alert email body for NASDAQ price sync."""
    lines = [
        "FINBOT NASDAQ100 GÜNCEL FİYAT SENKRONİZASYON RAPORU",
        "=" * 50,
        f"Durum: {result.status.upper()}",
        f"İşlem Tarihi: {result.trading_date or 'Bilinmiyor'}",
        f"Seans Saatleri: {result.session_open or '-'} / {result.session_close or '-'}",
        f"Çalışma Penceresi: {result.execution_window or '-'}",
        f"Süre: {result.duration_seconds:.2f} saniye",
        "",
        "METRİKLER:",
        f"  • İstenen Aktif Hisse Sayısı: {result.requested_active_count}",
        f"  • Gözlemlenen Yanıt Sayısı: {result.response_count}",
        f"  • Geçerli Güncel Fiyat Sayısı: {result.valid_count}",
        f"  • Geçersiz/Hatalı Ticker Sayısı: {result.invalid_count}",
        f"  • Duplicate Ticker Sayısı: {result.duplicate_count}",
        f"  • Eşleşen Aktif Hisse Sayısı: {result.matched_count}",
        f"  • Eksik Aktif Hisse Sayısı: {result.missing_count}",
        f"  • İstek Dışı (Unmatched) Ticker: {result.unmatched_count}",
        f"  • Önceki Kapanışı Eşleşen: {result.previous_close_matched_count}",
        f"  • Önceki Kapanışı Eksik: {result.previous_close_missing_count}",
        "",
        "VERİTABANI İŞLEM ÖZETİ:",
        f"  • Eklenen / Güncellenen: {result.inserted_or_updated_count}",
        f"  • Değişmeyen (Unchanged): {result.unchanged_count}",
        f"  • Eski Olduğu İçin Atlanan: {result.stale_skipped_count}",
        f"  • Fiyat Gelmediği İçin Stale Yapılan (price_not_published): {result.price_not_published_stale_count}",
        f"  • Pasif Olduğu İçin Stale Yapılan (instrument_inactive): {result.instrument_inactive_stale_count}",
    ]

    if result.error:
        lines.extend(["", f"HATA DETAYI:\n{result.error}"])

    if result.warnings:
        lines.extend(["", f"UYARILAR ({len(result.warnings)}):"])
        for w in result.warnings[:15]:
            lines.append(f"  - {w}")
        if len(result.warnings) > 15:
            lines.append(f"  ... ve {len(result.warnings) - 15} adet daha uyarı.")

    lines.extend(["", "Bu e-posta FinBot otomatik izleme sistemi tarafından üretilmiştir."])
    return "\n".join(lines)


def format_nasdaq_prices_email_html(result: NasdaqPricesJobResult) -> str:
    """Generate Outlook-compatible HTML alert email body for NASDAQ price sync."""
    status_bg = "#d9534f" if result.status == "failed" else "#f0ad4e"
    status_text = "BAŞARISIZ (FAILED)" if result.status == "failed" else "KISMİ BAŞARI (PARTIAL)"

    warnings_html = ""
    if result.warnings:
        warn_items = "".join(f"<li style='margin-bottom:4px;'>{html.escape(w)}</li>" for w in result.warnings[:15])
        more_warn = ""
        if len(result.warnings) > 15:
            more_warn = f"<p style='color:#777;font-size:12px;'>... ve {len(result.warnings) - 15} adet daha uyarı.</p>"
        warnings_html = f"""
        <div style="margin-top:20px;padding:12px;background:#fff8e5;border-left:4px solid #f0ad4e;font-size:13px;">
          <strong style="color:#8a6d3b;">Operasyonel Uyarılar ({len(result.warnings)}):</strong>
          <ul style="margin:8px 0 0 16px;padding:0;">{warn_items}</ul>
          {more_warn}
        </div>
        """

    error_html = ""
    if result.error:
        error_html = f"""
        <div style="margin-top:20px;padding:12px;background:#fde8e8;border-left:4px solid #d9534f;font-size:13px;">
          <strong style="color:#b92c28;">Hata Özeti:</strong>
          <p style="margin:6px 0 0 0;color:#333;">{html.escape(result.error)}</p>
        </div>
        """

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>FinBot NASDAQ100 Fiyat Raporu</title>
</head>
<body style="font-family:Arial,sans-serif;background-color:#f4f6f8;margin:0;padding:20px;color:#333;">
  <table width="100%" border="0" cellspacing="0" cellpadding="0" style="max-width:650px;margin:0 auto;background:#fff;border:1px solid #e1e4e8;border-radius:6px;overflow:hidden;">
    <tr>
      <td style="background:{status_bg};padding:18px 24px;color:#fff;">
        <h2 style="margin:0;font-size:18px;">FinBot NASDAQ100 Fiyat Senkronizasyonu</h2>
        <div style="margin-top:4px;font-size:14px;font-weight:bold;">{status_text}</div>
      </td>
    </tr>
    <tr>
      <td style="padding:24px;">
        <table width="100%" border="0" cellspacing="0" cellpadding="6" style="font-size:13px;border-collapse:collapse;">
          <tr style="background:#f9fafb;">
            <td style="width:40%;color:#666;border-bottom:1px solid #eee;"><strong>İşlem Tarihi (US):</strong></td>
            <td style="border-bottom:1px solid #eee;">{html.escape(result.trading_date or '-')}</td>
          </tr>
          <tr>
            <td style="color:#666;border-bottom:1px solid #eee;"><strong>Seans Saatleri (UTC):</strong></td>
            <td style="border-bottom:1px solid #eee;">{html.escape(result.session_open or '-')} / {html.escape(result.session_close or '-')}</td>
          </tr>
          <tr style="background:#f9fafb;">
            <td style="color:#666;border-bottom:1px solid #eee;"><strong>Çalışma Penceresi:</strong></td>
            <td style="border-bottom:1px solid #eee;">{html.escape(result.execution_window or '-')}</td>
          </tr>
          <tr>
            <td style="color:#666;border-bottom:1px solid #eee;"><strong>İstenen / Yanıt Sayısı:</strong></td>
            <td style="border-bottom:1px solid #eee;">{result.requested_active_count} istenen / {result.response_count} yanıt</td>
          </tr>
          <tr style="background:#f9fafb;">
            <td style="color:#666;border-bottom:1px solid #eee;"><strong>Eşleşen / Eksik Aktif:</strong></td>
            <td style="border-bottom:1px solid #eee;"><strong>{result.matched_count}</strong> eşleşen / <span style="color:{'#d9534f' if result.missing_count else '#333'}"><strong>{result.missing_count}</strong></span> eksik</td>
          </tr>
          <tr>
            <td style="color:#666;border-bottom:1px solid #eee;"><strong>Geçerli / Geçersiz / Duplicate:</strong></td>
            <td style="border-bottom:1px solid #eee;">{result.valid_count} geçerli / {result.invalid_count} geçersiz / {result.duplicate_count} duplicate</td>
          </tr>
          <tr style="background:#f9fafb;">
            <td style="color:#666;border-bottom:1px solid #eee;"><strong>Önceki Kapanış Eşleşen / Eksik:</strong></td>
            <td style="border-bottom:1px solid #eee;">{result.previous_close_matched_count} eşleşen / {result.previous_close_missing_count} eksik</td>
          </tr>
          <tr>
            <td style="color:#666;border-bottom:1px solid #eee;"><strong>DB Eklenen/Güncellenen:</strong></td>
            <td style="border-bottom:1px solid #eee;">{result.inserted_or_updated_count} (Değişmeyen: {result.unchanged_count}, Atlanan Eski: {result.stale_skipped_count})</td>
          </tr>
          <tr style="background:#f9fafb;">
            <td style="color:#666;border-bottom:1px solid #eee;"><strong>Stale İşaretlenen:</strong></td>
            <td style="border-bottom:1px solid #eee;">Fiyat Yok: {result.price_not_published_stale_count} | Pasif: {result.instrument_inactive_stale_count}</td>
          </tr>
        </table>
        {error_html}
        {warnings_html}
      </td>
    </tr>
    <tr>
      <td style="background:#f9fafb;padding:12px 24px;font-size:11px;color:#999;border-top:1px solid #e1e4e8;text-align:center;">
        FinBot Automated Alert & Monitoring System
      </td>
    </tr>
  </table>
</body>
</html>"""
