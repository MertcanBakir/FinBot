from typing import Dict, Optional

TRACKED_EVENTS: Dict[str, Dict[str, str]] = {
    "BLS": {
        "CONSUMER PRICE INDEX": "ABD Tüketici Fiyat Endeksi (CPI)",
        "PRODUCER PRICE INDEX": "ABD Üretici Fiyat Endeksi (PPI)",
        "EMPLOYMENT SITUATION": "ABD İstihdam Raporu (NFP)",
        "JOB OPENINGS AND LABOR TURNOVER": "ABD JOLTS"
    },
    "BEA": {
        "GROSS DOMESTIC PRODUCT": "ABD GSYH",
        "GDP": "ABD GSYH",
        "PERSONAL INCOME AND OUTLAYS": "ABD PCE / Personal Income and Outlays"
    },
    "TÜİK": {
        "TÜKETİCİ FİYAT ENDEKSİ": "Türkiye TÜFE",
        "TÜFE": "Türkiye TÜFE",
        "GAYRİSAFİ YURT İÇİ HASILA": "Türkiye GSYH",
        "GSYH": "Türkiye GSYH",
        "İŞGÜCÜ İSTATİSTİKLERİ": "Türkiye İşgücü",
        "SANAYİ ÜRETİM ENDEKSİ": "Türkiye Sanayi Üretimi"
    },
    "TCMB": {
        "PARA POLİTİKASI KURULU TOPLANTIS": "TCMB Faiz Kararı",
        "PARA POLİTİKASI KURULU TOPLANTISINA İLİŞKİN BASIN DUYURUSU": "TCMB Faiz Kararı",
        "PARA POLİTİKASI KURULU": "TCMB Faiz Kararı",
        "PPK TOPLANTISI": "TCMB Faiz Kararı",
        "ENFLASYON RAPORU": "TCMB Enflasyon Raporu"
    },
    "Federal Reserve": {
        "FOMC": "Fed Faiz Kararı (FOMC)"
    }
}

CANONICAL_EVENT_CODES: Dict[str, str] = {
    "Türkiye TÜFE": "TR_CPI",
    "Türkiye GSYH": "TR_GDP",
    "Türkiye İşgücü": "TR_LABOR",
    "Türkiye Sanayi Üretimi": "TR_INDUSTRIAL_PRODUCTION",
    "TCMB Faiz Kararı": "TR_POLICY_RATE",
    "TCMB Enflasyon Raporu": "TR_INFLATION_REPORT",
    "ABD Tüketici Fiyat Endeksi (CPI)": "US_CPI",
    "ABD Üretici Fiyat Endeksi (PPI)": "US_PPI",
    "ABD İstihdam Raporu (NFP)": "US_NFP",
    "ABD JOLTS": "US_JOLTS",
    "ABD GSYH": "US_GDP",
    "ABD PCE / Personal Income and Outlays": "US_PCE",
    "Fed Faiz Kararı (FOMC)": "US_FOMC",
}

def normalize_text(text: str) -> str:
    if not text:
        return ""
    return text.strip().upper().replace("İ", "I").replace("İ", "I")

def match_tracked_event(source: str, raw_name: str) -> Optional[str]:
    mapping = TRACKED_EVENTS.get(source, {})
    norm_name = normalize_text(raw_name)
    for pattern, std_title in mapping.items():
        norm_pattern = normalize_text(pattern)
        if norm_pattern in norm_name:
            return std_title
    return None
