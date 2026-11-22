import html
from datetime import date, datetime
from typing import List, Optional, Set

from app.models.economic_calendar_refresh import (
    EconomicCalendarRefreshJobResult,
)

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
    12: "Aralık"
}

def format_date_tr(d: date) -> str:
    return f"{d.day} {MONTH_NAMES_TR.get(d.month, '')} {d.year}"

def format_datetime_tr(iso_str: Optional[str]) -> str:
    if not iso_str:
        return "Belirtilmedi"
    try:
        dt = datetime.fromisoformat(iso_str)
        month_name = MONTH_NAMES_TR.get(dt.month, "")
        # If time is 00:00:00 with no subsecond, check if time part is meaningful
        if dt.hour == 0 and dt.minute == 0 and dt.second == 0:
            return f"{dt.day} {month_name} {dt.year}"
        return f"{dt.day} {month_name} {dt.year} • {dt.strftime('%H:%M')}"
    except Exception:
        return iso_str

def format_economic_calendar_refresh_subject(result: EconomicCalendarRefreshJobResult) -> str:
    try:
        gen_dt = datetime.fromisoformat(result.generated_at)
        date_str = format_date_tr(gen_dt.date())
    except Exception:
        date_str = result.generated_at

    meaningful_changes = result.diff.added_count + result.diff.changed_count + result.diff.removed_count
    effective_status = "failed" if result.database_status == "failed" else result.status

    if effective_status == "failed":
        return f"❌ FinBot Ekonomik Takvim — Güncelleme başarısız — {date_str}"
    elif effective_status == "partial":
        if meaningful_changes > 0:
            return f"⚠️ FinBot Ekonomik Takvim — Kısmi başarı ({meaningful_changes} değişiklik) — {date_str}"
        return f"⚠️ FinBot Ekonomik Takvim — Kısmi başarı — {date_str}"
    else:
        if meaningful_changes == 0:
            return f"✅ FinBot Ekonomik Takvim — Güncel — {date_str}"
        elif meaningful_changes == 1:
            return f"✅ FinBot Ekonomik Takvim — 1 değişiklik — {date_str}"
        else:
            return f"✅ FinBot Ekonomik Takvim — {meaningful_changes} değişiklik — {date_str}"

def _collect_unique_warnings(result: EconomicCalendarRefreshJobResult) -> List[str]:
    seen: Set[str] = set()
    unique: List[str] = []
    
    for w in result.warnings:
        cleaned = w.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            unique.append(cleaned)

    for p in result.provider_outcomes:
        for w in p.warnings:
            cleaned = w.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                unique.append(cleaned)
        if p.error:
            cleaned = p.error.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                unique.append(cleaned)

    if result.database_status == "failed":
        db_warn = f"Supabase veritabanı senkronizasyonu başarısız: {result.database_error}" if result.database_error else "Supabase veritabanı senkronizasyonu başarısız."
        if db_warn not in seen:
            seen.add(db_warn)
            unique.append(db_warn)

    return unique

def format_economic_calendar_refresh_email_text(result: EconomicCalendarRefreshJobResult) -> str:
    try:
        gen_dt = datetime.fromisoformat(result.generated_at)
        gen_str = f"{format_date_tr(gen_dt.date())} {gen_dt.strftime('%H:%M:%S')}"
    except Exception:
        gen_str = result.generated_at

    try:
        c_start = format_date_tr(date.fromisoformat(result.coverage_start))
        c_end = format_date_tr(date.fromisoformat(result.coverage_end))
        cov_str = f"{c_start} — {c_end}"
    except Exception:
        cov_str = f"{result.coverage_start} — {result.coverage_end}"

    effective_status = "failed" if result.database_status == "failed" else result.status

    status_labels = {
        "success": "Başarılı",
        "partial": "Kısmi Başarı",
        "failed": "Hata"
    }
    status_label = status_labels.get(effective_status, effective_status)

    lines = [
        "=" * 60,
        f"FinBot Günlük Ekonomik Takvim Raporu — {status_label}",
        "=" * 60,
        f"Rapor Tarihi   : {gen_str}",
        f"Kapsam (91 gün): {cov_str}",
        f"Genel Durum    : {status_label}",
        f"Senkronizasyon Aksiyonu : {result.sync_action}",
        "",
        "ÖZET METRİKLER:",
        f"  • Toplam Takip Edilen Olay: {result.diff.final_event_count}",
        f"  • Yeni Eklenen Olay       : {result.diff.added_count}",
        f"  • Değişen Olay            : {result.diff.changed_count}",
        f"  • Kaldırılan Olay         : {result.diff.removed_count}",
        f"  • Taşınan (Carried) Olay  : {result.diff.carried_forward_count}",
        f"  • Kapsam Dışı (Aged Out)  : {result.diff.aged_out_count}",
        "",
        "VERİ SAĞLAYICILARI:",
    ]

    for pr in result.provider_outcomes:
        p_status = status_labels.get(pr.status, pr.status)
        p_detail = f"{pr.event_count} olay"
        if pr.carried_forward_count > 0:
            p_detail += f" ({pr.carried_forward_count} taşınan)"
        lines.append(f"  • {pr.provider_name:<32} : [{p_status}] {p_detail}")

    lines.append("")
    lines.append("DEĞİŞİKLİKLER VE GÜNCELLEMELER:")

    meaningful_changes = result.diff.added_count + result.diff.changed_count + result.diff.removed_count + result.diff.carried_forward_count
    if meaningful_changes == 0 and effective_status == "success":
        lines.append("  Tüm ekonomik takvim kaynakları başarıyla kontrol edildi.")
        lines.append("  Yeni, değişen veya kaldırılan etkinlik bulunamadı.")
    elif len(result.diff.details) == 0:
        lines.append("  Gösterilebilecek etkinlik değişikliği bulunmuyor.")
    else:
        displayed_details = result.diff.details[:20]
        for item in displayed_details:
            if item.change_type == "added":
                lines.append(f"  + {item.name}")
                lines.append(f"    {format_datetime_tr(item.new_scheduled_at_istanbul)}")
            elif item.change_type == "changed":
                lines.append(f"  ↻ {item.name}")
                lines.append(f"    {format_datetime_tr(item.old_scheduled_at_istanbul)} → {format_datetime_tr(item.new_scheduled_at_istanbul)}")
            elif item.change_type == "removed":
                lines.append(f"  ✕ {item.name}")
                lines.append(f"    {format_datetime_tr(item.old_scheduled_at_istanbul)}")
            elif item.change_type == "carried_forward":
                lines.append(f"  ⟳ {item.name} (Önceki takvimden korundu)")
                lines.append(f"    {format_datetime_tr(item.new_scheduled_at_istanbul)}")
            lines.append("")

        if len(result.diff.details) > 20:
            lines.append(f"  ... ve {len(result.diff.details) - 20} kayıt daha")

    unique_warnings = _collect_unique_warnings(result)
    if unique_warnings:
        lines.append("")
        lines.append("UYARILAR:")
        for w in unique_warnings:
            lines.append(f"  ⚠️ {w}")

    lines.append("")
    lines.append("-" * 60)
    lines.append("FinBot Otomasyon Sistemi • Bu e-posta bilgilendirme amaçlıdır.")
    lines.append("-" * 60)

    return "\n".join(lines)


def format_economic_calendar_refresh_email_html(result: EconomicCalendarRefreshJobResult) -> str:
    try:
        gen_dt = datetime.fromisoformat(result.generated_at)
        gen_str = f"{format_date_tr(gen_dt.date())} {gen_dt.strftime('%H:%M:%S')}"
    except Exception:
        gen_str = result.generated_at

    try:
        c_start = format_date_tr(date.fromisoformat(result.coverage_start))
        c_end = format_date_tr(date.fromisoformat(result.coverage_end))
        cov_str = f"{c_start} — {c_end}"
    except Exception:
        cov_str = f"{result.coverage_start} — {result.coverage_end}"

    effective_status = "failed" if result.database_status == "failed" else result.status

    status_styles = {
        "success": {"text": "Başarılı", "color": "#166534", "bg": "#DCFCE7"},
        "partial": {"text": "Kısmi Başarı", "color": "#B45309", "bg": "#FEF3C7"},
        "failed": {"text": "Hata", "color": "#B91C1C", "bg": "#FEE2E2"},
    }
    s_style = status_styles.get(effective_status, {"text": effective_status, "color": "#6B7280", "bg": "#F3F4F6"})

    provider_rows = []
    for pr in result.provider_outcomes:
        pr_style = status_styles.get(pr.status, {"text": pr.status, "color": "#6B7280", "bg": "#F3F4F6"})
        count_text = f"{pr.event_count} olay"
        if pr.carried_forward_count > 0:
            count_text += f" <span style=\"color: #B45309; font-size: 11px;\">({pr.carried_forward_count} taşınan)</span>"

        provider_rows.append(f"""
        <tr>
          <td style="padding: 10px 12px; font-size: 13px; font-weight: 500; color: #1F2937; border-bottom: 1px solid #E5E7EB;">
            {html.escape(pr.provider_name)}
          </td>
          <td style="padding: 10px 12px; font-size: 13px; text-align: center; border-bottom: 1px solid #E5E7EB;">
            <span style="display: inline-block; padding: 2px 8px; font-size: 11px; font-weight: 600; border-radius: 9999px; color: {pr_style['color']}; background-color: {pr_style['bg']};">
              {pr_style['text']}
            </span>
          </td>
          <td style="padding: 10px 12px; font-size: 13px; font-weight: 600; color: #4B5563; text-align: right; border-bottom: 1px solid #E5E7EB;">
            {count_text}
          </td>
        </tr>
        """)

    # Changes section HTML
    meaningful_changes = result.diff.added_count + result.diff.changed_count + result.diff.removed_count + result.diff.carried_forward_count
    if meaningful_changes == 0 and effective_status == "success":
        changes_html = """
        <div style="background-color: #F0FDF4; border: 1px solid #BBF7D0; border-radius: 8px; padding: 14px; text-align: center;">
          <p style="margin: 0; font-size: 13px; font-weight: 600; color: #166534;">Tüm ekonomik takvim kaynakları güncel</p>
          <p style="margin: 4px 0 0 0; font-size: 12px; color: #15803D;">Yeni, değişen veya kaldırılan etkinlik bulunamadı.</p>
        </div>
        """
    elif len(result.diff.details) == 0:
        changes_html = """
        <div style="background-color: #F9FAFB; border: 1px solid #E5E7EB; border-radius: 8px; padding: 14px; text-align: center;">
          <p style="margin: 0; font-size: 13px; color: #6B7280;">Gösterilebilecek etkinlik değişikliği bulunmuyor.</p>
        </div>
        """
    else:
        change_items = []
        displayed_details = result.diff.details[:20]
        for item in displayed_details:
            name_esc = html.escape(item.name)
            if item.change_type == "added":
                dt_str = html.escape(format_datetime_tr(item.new_scheduled_at_istanbul))
                change_items.append(f"""
                <div style="padding: 10px; margin-bottom: 8px; background-color: #F0FDF4; border-left: 3px solid #16A34A; border-radius: 4px;">
                  <div style="font-size: 13px; font-weight: 600; color: #166534;">+ {name_esc}</div>
                  <div style="font-size: 12px; color: #15803D; margin-top: 2px;">{dt_str}</div>
                </div>
                """)
            elif item.change_type == "changed":
                old_str = html.escape(format_datetime_tr(item.old_scheduled_at_istanbul))
                new_str = html.escape(format_datetime_tr(item.new_scheduled_at_istanbul))
                change_items.append(f"""
                <div style="padding: 10px; margin-bottom: 8px; background-color: #EFF6FF; border-left: 3px solid #2563EB; border-radius: 4px;">
                  <div style="font-size: 13px; font-weight: 600; color: #1E40AF;">↻ {name_esc}</div>
                  <div style="font-size: 12px; color: #6B7280; text-decoration: line-through; margin-top: 2px;">{old_str}</div>
                  <div style="font-size: 12px; font-weight: 600; color: #1E40AF; margin-top: 1px;">→ {new_str}</div>
                </div>
                """)
            elif item.change_type == "removed":
                dt_str = html.escape(format_datetime_tr(item.old_scheduled_at_istanbul))
                change_items.append(f"""
                <div style="padding: 10px; margin-bottom: 8px; background-color: #FEF2F2; border-left: 3px solid #DC2626; border-radius: 4px;">
                  <div style="font-size: 13px; font-weight: 600; color: #991B1B;">✕ {name_esc}</div>
                  <div style="font-size: 12px; color: #B91C1C; margin-top: 2px;">{dt_str}</div>
                </div>
                """)
            elif item.change_type == "carried_forward":
                dt_str = html.escape(format_datetime_tr(item.new_scheduled_at_istanbul))
                change_items.append(f"""
                <div style="padding: 10px; margin-bottom: 8px; background-color: #FFFBEB; border-left: 3px solid #D97706; border-radius: 4px;">
                  <div style="font-size: 13px; font-weight: 600; color: #92400E;">⟳ {name_esc} <span style="font-size: 11px; font-weight: normal; color: #B45309;">(Önceki takvimden korundu)</span></div>
                  <div style="font-size: 12px; color: #B45309; margin-top: 2px;">{dt_str}</div>
                </div>
                """)

        if len(result.diff.details) > 20:
            rem = len(result.diff.details) - 20
            change_items.append(f"""
            <div style="text-align: center; padding: 8px; font-size: 12px; color: #6B7280; font-style: italic;">
              ... ve {rem} kayıt daha
            </div>
            """)

        changes_html = "".join(change_items)

    # Warnings section HTML
    unique_warnings = _collect_unique_warnings(result)
    warnings_html = ""
    if unique_warnings:
        warn_items = "".join([
            f"<li style=\"margin-bottom: 4px; font-size: 12px; color: #991B1B;\">{html.escape(w)}</li>"
            for w in unique_warnings
        ])
        warnings_html = f"""
        <div style="margin-top: 20px; background-color: #FEF2F2; border: 1px solid #FECACA; border-radius: 8px; padding: 14px;">
          <div style="font-size: 13px; font-weight: 600; color: #991B1B; margin-bottom: 8px;">⚠️ Uyarılar ve Notlar</div>
          <ul style="margin: 0; padding-left: 18px;">
            {warn_items}
          </ul>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="tr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>FinBot Günlük Ekonomik Takvim</title>
</head>
<body style="margin: 0; padding: 20px 0; background-color: #F3F4F6; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; -webkit-font-smoothing: antialiased;">
  <div style="max-width: 640px; margin: 0 auto; background-color: #FFFFFF; border: 1px solid #E5E7EB; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
    
    <!-- HEADER -->
    <div style="padding: 24px; background: linear-gradient(135deg, #1E3A8A 0%, #0F172A 100%); color: #FFFFFF;">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
        <span style="font-size: 12px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; color: #93C5FD;">FinBot Operasyonel Rapor</span>
        <span style="display: inline-block; padding: 3px 10px; font-size: 11px; font-weight: 700; border-radius: 9999px; color: {s_style['color']}; background-color: {s_style['bg']};">
          {s_style['text']}
        </span>
      </div>
      <h1 style="margin: 0 0 6px 0; font-size: 20px; font-weight: 700; color: #FFFFFF;">Günlük Ekonomik Takvim</h1>
      <div style="font-size: 13px; color: #CBD5E1;">
        Kapsam: <strong>{html.escape(cov_str)}</strong> (91 gün)
      </div>
    </div>

    <div style="padding: 24px;">
      
      <!-- METRICS GRID -->
      <table style="width: 100%; border-collapse: separate; border-spacing: 8px; margin-bottom: 20px;">
        <tr>
          <td style="width: 33.33%; padding: 12px; background-color: #F9FAFB; border: 1px solid #E5E7EB; border-radius: 8px; text-align: center;">
            <div style="font-size: 11px; font-weight: 600; color: #6B7280; text-transform: uppercase;">Toplam Olay</div>
            <div style="font-size: 20px; font-weight: 700; color: #111827; margin-top: 4px;">{result.diff.final_event_count}</div>
          </td>
          <td style="width: 33.33%; padding: 12px; background-color: #F0FDF4; border: 1px solid #BBF7D0; border-radius: 8px; text-align: center;">
            <div style="font-size: 11px; font-weight: 600; color: #166534; text-transform: uppercase;">Yeni Eklenen</div>
            <div style="font-size: 20px; font-weight: 700; color: #15803D; margin-top: 4px;">+{result.diff.added_count}</div>
          </td>
          <td style="width: 33.33%; padding: 12px; background-color: #EFF6FF; border: 1px solid #BFDBFE; border-radius: 8px; text-align: center;">
            <div style="font-size: 11px; font-weight: 600; color: #1E40AF; text-transform: uppercase;">Değişen</div>
            <div style="font-size: 20px; font-weight: 700; color: #2563EB; margin-top: 4px;">{result.diff.changed_count}</div>
          </td>
        </tr>
        <tr>
          <td style="padding: 12px; background-color: #FEF2F2; border: 1px solid #FECACA; border-radius: 8px; text-align: center;">
            <div style="font-size: 11px; font-weight: 600; color: #991B1B; text-transform: uppercase;">Kaldırılan</div>
            <div style="font-size: 18px; font-weight: 700; color: #DC2626; margin-top: 4px;">{result.diff.removed_count}</div>
          </td>
          <td style="padding: 12px; background-color: #FFFBEB; border: 1px solid #FDE68A; border-radius: 8px; text-align: center;">
            <div style="font-size: 11px; font-weight: 600; color: #92400E; text-transform: uppercase;">Taşınan</div>
            <div style="font-size: 18px; font-weight: 700; color: #D97706; margin-top: 4px;">{result.diff.carried_forward_count}</div>
          </td>
          <td style="padding: 12px; background-color: #F9FAFB; border: 1px solid #E5E7EB; border-radius: 8px; text-align: center;">
            <div style="font-size: 11px; font-weight: 600; color: #6B7280; text-transform: uppercase;">Aged Out</div>
            <div style="font-size: 18px; font-weight: 700; color: #4B5563; margin-top: 4px;">{result.diff.aged_out_count}</div>
          </td>
        </tr>
      </table>

      <!-- PROVIDERS TABLE -->
      <div style="margin-bottom: 24px;">
        <div style="font-size: 14px; font-weight: 700; color: #111827; margin-bottom: 10px;">Veri Kaynakları Durumu</div>
        <table style="width: 100%; border-collapse: collapse; border: 1px solid #E5E7EB; border-radius: 8px; overflow: hidden;">
          <thead>
            <tr style="background-color: #F9FAFB;">
              <th style="padding: 8px 12px; font-size: 11px; font-weight: 600; color: #6B7280; text-align: left; text-transform: uppercase; border-bottom: 1px solid #E5E7EB;">Kaynak</th>
              <th style="padding: 8px 12px; font-size: 11px; font-weight: 600; color: #6B7280; text-align: center; text-transform: uppercase; border-bottom: 1px solid #E5E7EB;">Durum</th>
              <th style="padding: 8px 12px; font-size: 11px; font-weight: 600; color: #6B7280; text-align: right; text-transform: uppercase; border-bottom: 1px solid #E5E7EB;">Olay Sayısı</th>
            </tr>
          </thead>
          <tbody>
            {''.join(provider_rows)}
          </tbody>
        </table>
      </div>

      <!-- CHANGES -->
      <div style="margin-bottom: 20px;">
        <div style="font-size: 14px; font-weight: 700; color: #111827; margin-bottom: 10px;">Önemli Değişiklikler</div>
        {changes_html}
      </div>

      <!-- WARNINGS -->
      {warnings_html}

    </div>

    <!-- FOOTER -->
    <div style="padding: 16px 24px; background-color: #F9FAFB; border-top: 1px solid #E5E7EB; text-align: center; font-size: 11px; color: #9CA3AF;">
      Oluşturulma Saati: {html.escape(gen_str)} • FinBot Otomasyon Sistemi
    </div>

  </div>
</body>
</html>
"""
