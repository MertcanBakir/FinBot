import html
from datetime import date, datetime
from typing import Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.models.portfolio_snapshots import PortfolioSnapshotJobResult

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


def format_date_tr(value: date) -> str:
    return f"{value.day} {MONTH_NAMES_TR[value.month]} {value.year}"


def format_datetime_tr(value: Optional[str]) -> str:
    if not value:
        return "Belirtilmedi"
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(ZoneInfo(settings.TIMEZONE))
        return f"{format_date_tr(parsed.date())} • {parsed.strftime('%H:%M:%S')}"
    except (TypeError, ValueError):
        return value


def mask_portfolio_id(value: Optional[UUID]) -> str:
    if value is None:
        return "Genel job hatası"
    raw = str(value)
    return f"{raw[:8]}…{raw[-4:]}"


def format_portfolio_snapshots_subject(result: PortfolioSnapshotJobResult) -> str:
    snapshot_date = format_date_tr(result.snapshot_date)
    if result.status == "failed":
        return f"❌ FinBot Portföy Snapshot — Oluşturma başarısız — {snapshot_date}"
    if result.status == "partial":
        return f"⚠️ FinBot Portföy Snapshot — Kısmi değerleme — {snapshot_date}"
    return f"✅ FinBot Portföy Snapshot — Başarılı — {snapshot_date}"


def format_portfolio_snapshots_email_text(result: PortfolioSnapshotJobResult) -> str:
    status_label = {
        "success": "BAŞARILI",
        "partial": "KISMİ DEĞERLEME",
        "failed": "BAŞARISIZ",
    }[result.status]
    lines = [
        f"FinBot Günlük Portföy Snapshot Raporu — {status_label}",
        "=" * 56,
        f"Snapshot Tarihi: {format_date_tr(result.snapshot_date)}",
        f"Başlangıç: {format_datetime_tr(result.started_at)}",
        f"Bitiş: {format_datetime_tr(result.finished_at)}",
        f"Süre: {result.duration_seconds:.2f} saniye",
        "",
        "AMAÇ:",
        "Geçmiş günler için o gün kaydedilmiş son portföy değerlemesini doğrular; geçmiş değerleri yeniden hesaplamaz.",
        "",
        "İŞLEM ÖZETİ:",
        f"• İstenen portföy: {result.requested_count}",
        f"• Tamamlanan: {result.completed_count}",
        f"• Kısmi değerlenen: {result.partial_count}",
        f"• Başarısız: {result.failed_count}",
        f"• Mevcut kaydı kullanılan: {result.reused_count}",
        f"• Gün içinde kaydedilmemiş: {result.missing_snapshot_count}",
    ]

    if result.failures:
        lines.extend(["", "BAŞARISIZ PORTFÖYLER:"])
        for failure in result.failures[:20]:
            symbols = (
                f" — Eksik fiyat: {', '.join(failure.missing_symbols)}"
                if failure.missing_symbols
                else ""
            )
            lines.append(
                f"• {mask_portfolio_id(failure.portfolio_id)}: {failure.message}{symbols}"
            )

    if result.warnings:
        lines.extend(["", "DEĞERLEME UYARILARI:"])
        lines.extend(f"• {warning}" for warning in result.warnings[:20])

    lines.extend(
        [
            "",
            "ÖNERİLEN AKSİYON:",
            "Eksik geçmiş kayıtlar güncel fiyatlarla doldurulmaz. Saatlik Supabase snapshot görevinin çalışmasını kontrol edin.",
            "",
            "FinBot Otomatik İzleme Sistemi",
        ]
    )
    return "\n".join(lines)


def format_portfolio_snapshots_email_html(result: PortfolioSnapshotJobResult) -> str:
    is_failed = result.status == "failed"
    status_title = "Oluşturma Başarısız" if is_failed else "Kısmi Değerleme"
    badge_bg = "#FEE2E2" if is_failed else "#FEF3C7"
    badge_color = "#991B1B" if is_failed else "#92400E"
    header_start = "#DC2626" if is_failed else "#D97706"
    header_end = "#7F1D1D" if is_failed else "#78350F"

    failure_rows = ""
    for failure in result.failures[:20]:
        symbols = ""
        if failure.missing_symbols:
            symbols = (
                '<div style="margin-top:4px;font-size:12px;color:#B91C1C;">'
                f"Eksik fiyat: <strong>{html.escape(', '.join(failure.missing_symbols))}</strong>"
                "</div>"
            )
        failure_rows += f"""
        <tr>
          <td style="padding:10px 12px;border-bottom:1px solid #FECACA;font-family:monospace;font-size:12px;color:#7F1D1D;vertical-align:top;">{html.escape(mask_portfolio_id(failure.portfolio_id))}</td>
          <td style="padding:10px 12px;border-bottom:1px solid #FECACA;font-size:13px;color:#7F1D1D;">{html.escape(failure.message)}{symbols}</td>
        </tr>"""

    failures_html = ""
    if failure_rows:
        failures_html = f"""
        <div style="margin:0 0 20px 0;background:#FEF2F2;border:1px solid #FECACA;border-radius:8px;overflow:hidden;">
          <div style="padding:12px 14px;font-size:13px;font-weight:700;color:#991B1B;">❌ Başarısız Portföyler ({result.failed_count})</div>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">{failure_rows}</table>
        </div>"""

    warnings_html = ""
    if result.warnings:
        items = "".join(
            f'<li style="margin-bottom:5px;">{html.escape(item)}</li>'
            for item in result.warnings[:20]
        )
        warnings_html = f"""
        <div style="margin:0 0 20px 0;padding:14px;background:#FFFBEB;border:1px solid #FDE68A;border-radius:8px;color:#78350F;">
          <div style="font-size:13px;font-weight:700;margin-bottom:8px;">⚠️ Değerleme Uyarıları</div>
          <ul style="margin:0;padding-left:20px;font-size:12px;line-height:1.5;">{items}</ul>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="tr">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(format_portfolio_snapshots_subject(result))}</title></head>
<body style="margin:0;padding:24px 8px;background:#F3F4F6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;color:#111827;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0"><tr><td align="center">
    <table role="presentation" width="640" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:640px;background:#FFFFFF;border:1px solid #E5E7EB;border-radius:12px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.06);">
      <tr><td style="padding:24px;background:{header_start};background:linear-gradient(135deg,{header_start},{header_end});color:#FFFFFF;">
        <table role="presentation" width="100%"><tr>
          <td style="font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:#FDE68A;">FinBot Operasyon Raporu</td>
          <td align="right"><span style="display:inline-block;padding:4px 10px;border-radius:999px;background:{badge_bg};color:{badge_color};font-size:11px;font-weight:800;">{status_title.upper()}</span></td>
        </tr></table>
        <h1 style="margin:12px 0 6px;font-size:21px;line-height:1.25;">📊 Günlük Portföy Snapshot</h1>
        <div style="font-size:13px;color:#FDE68A;">{format_date_tr(result.snapshot_date)} gün sonu değerlemesi</div>
      </td></tr>
      <tr><td style="padding:24px;">
        <div style="margin-bottom:20px;padding:14px;background:#EFF6FF;border:1px solid #BFDBFE;border-radius:8px;">
          <div style="font-size:13px;font-weight:700;color:#1E3A8A;margin-bottom:5px;">Bu job ne yapar?</div>
          <div style="font-size:12px;line-height:1.5;color:#1E40AF;">Geçmiş günler için o gün kaydedilmiş son portföy değerlemesini doğrular; geçmiş değerleri yeniden hesaplamaz.</div>
        </div>
        <div style="margin-bottom:20px;padding:14px;background:#F8FAFC;border:1px solid #E2E8F0;border-radius:8px;">
          <table role="presentation" width="100%" style="font-size:12px;color:#475569;">
            <tr><td style="padding:3px 0;"><strong>Başlangıç</strong></td><td align="right">{html.escape(format_datetime_tr(result.started_at))}</td></tr>
            <tr><td style="padding:3px 0;"><strong>Bitiş</strong></td><td align="right">{html.escape(format_datetime_tr(result.finished_at))}</td></tr>
            <tr><td style="padding:3px 0;"><strong>Süre</strong></td><td align="right">{result.duration_seconds:.2f} saniye</td></tr>
          </table>
        </div>
        <table role="presentation" width="100%" cellpadding="0" cellspacing="8" border="0" style="margin:0 0 20px;font-size:12px;text-align:center;">
          <tr>
            <td style="padding:12px 5px;background:#F8FAFC;border:1px solid #E2E8F0;border-radius:8px;"><div style="font-size:22px;font-weight:800;color:#0F172A;">{result.requested_count}</div><div style="color:#64748B;">İstenen</div></td>
            <td style="padding:12px 5px;background:#ECFDF5;border:1px solid #A7F3D0;border-radius:8px;"><div style="font-size:22px;font-weight:800;color:#047857;">{result.completed_count}</div><div style="color:#047857;">Tamamlanan</div></td>
            <td style="padding:12px 5px;background:#FFFBEB;border:1px solid #FDE68A;border-radius:8px;"><div style="font-size:22px;font-weight:800;color:#B45309;">{result.partial_count}</div><div style="color:#B45309;">Kısmi</div></td>
            <td style="padding:12px 5px;background:#FEF2F2;border:1px solid #FECACA;border-radius:8px;"><div style="font-size:22px;font-weight:800;color:#B91C1C;">{result.failed_count}</div><div style="color:#B91C1C;">Başarısız</div></td>
          </tr>
        </table>
        {failures_html}
        {warnings_html}
        <p style="font-size:12px;color:#475569;">Mevcut kaydı kullanılan: {result.reused_count} · Gün içinde kaydedilmemiş: {result.missing_snapshot_count}</p>
        <div style="padding:14px;background:#F8FAFC;border-left:4px solid #2563EB;border-radius:6px;">
          <div style="font-size:13px;font-weight:700;color:#1E3A8A;margin-bottom:4px;">Önerilen aksiyon</div>
          <div style="font-size:12px;line-height:1.5;color:#475569;">Eksik geçmiş kayıtlar güncel fiyatlarla doldurulmaz. Saatlik Supabase snapshot görevinin çalışmasını kontrol edin. Tekrar çalışma mevcut geçmiş kayıtları değiştirmez.</div>
        </div>
      </td></tr>
      <tr><td align="center" style="padding:14px 24px;background:#F9FAFB;border-top:1px solid #E5E7EB;font-size:11px;color:#9CA3AF;">FinBot Otomatik İzleme Sistemi • Bu bildirim operasyonel takip amaçlıdır.</td></tr>
    </table>
  </td></tr></table>
</body></html>"""
