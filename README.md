# FinBot

FinBot; finansal verileri ücretsiz veya kamuya açık kaynaklardan toplayan, doğrulayan ve Supabase üzerinde güncel tutan Python tabanlı bir finans servisidir.

Projenin iki temel görevi vardır:

1. BIST, ABD hisseleri, kripto, TEFAS fonları ve USD/TRY için güncel fiyat üretmek.
2. Ekonomik takvim, bilanço takvimi ve portföy hesaplamaları için güvenilir backend altyapısı sağlamak.

Mobil uygulama enstrüman, fiyat ve portföy verilerini doğrudan Supabase üzerinden okur. FinBot içindeki HTTP GET API'leri korunmaktadır; ancak mobil portföy akışının ana bağlantı noktası değildir.

## Sistem Özeti

```text
Harici veri kaynakları
  ├── Yahoo Finance
  ├── Binance Public Market Data
  ├── TEFAS / pytefas
  ├── KAP ve şirketlerin resmî yatırımcı ilişkileri sayfaları
  └── TÜİK, TCMB, BLS, BEA ve Federal Reserve
           │
           ▼
Provider -> Service -> Repository -> Supabase
           │                         │
           │                         ├── Enstrüman ve son fiyatlar
           │                         ├── Ekonomik/bilanço takvimleri
           │                         ├── Portföy, FIFO ve snapshot verileri
           │                         └── Job çalışma kayıtları
           ▼
GitHub Actions + merkezî e-posta bildirimi
```

Temel tasarım ilkeleri:

- `providers/`: Dış kaynak erişimini izole eder.
- `services/`: Normalizasyon, doğrulama ve iş kurallarını uygular.
- `repositories/`: PostgreSQL/Supabase erişimini yönetir.
- `jobs/`: Zamanlanmış veya manuel çalıştırılan giriş noktalarıdır.
- `supabase/migrations/`: Şema ve RPC fonksiyonlarının sürümlü kaynağıdır.
- Production takvim state'i ve enstrüman katalogları Supabase'dedir. Rolling JSON dosyaları production veri kaynağı değildir.
- Yalnız BIST mali dönem arşivi kalıcı JSON olarak korunur.

## Desteklenen Veri Akışları

### Güncel fiyatlar

| Kapsam | Kaynak | Para birimi | Güncelleme yaklaşımı |
|---|---|---:|---|
| BIST hisseleri | Yahoo Finance | TRY | XIST işlem günlerinde gün içi ve kapanış sonrası |
| ABD/NASDAQ hisseleri | Yahoo Finance | USD | NASDAQ takvimine göre geniş çalışma penceresi |
| Kripto kataloğu | Binance Spot/USDT | USD | Sabit Supabase kataloğu, 1 USDT = 1 USD varsayımı |
| TEFAS yatırım fonları | TEFAS (`pytefas`) | TRY | İş günlerinde son ve önceki geçerli fon fiyatı |
| USD/TRY | Yahoo Finance (`TRY=X`) | TRY | Karma para birimli portföylerin değerlemesi için |

Fiyat job'ları `public.instrument_latest_prices` tablosunda yalnız son fiyatı tutar. Tarihsel piyasa fiyat serisi saklanmaz. Portföyün tarihsel görünümü günlük `portfolio_snapshots` ve `portfolio_snapshot_positions` kayıtlarıyla ileriye dönük oluşur.

Ortak fiyat güvenliği:

- Katalog çalışma anında `public.instruments` tablosundan okunur.
- Geçerli veri atomik ve idempotent biçimde upsert edilir.
- Güncel fiyat geldiğinde `is_stale=false` ve `stale_reason=NULL` yapılır.
- Geçici fiyat eksikliğinde mevcut satır korunur ve gerekirse `price_not_published` olarak işaretlenir.
- Pasif enstrümanların mevcut fiyatı `instrument_inactive` olarak işaretlenebilir.
- Tamlık oranı `%80` altındaysa job fail-closed davranır ve fiyat yazmaz.
- `partial` ve `failed` sonuçları merkezî bildirim politikasıyla e-posta üretir.

### Ekonomik takvim

Takip edilen 91 günlük ekonomik takvim şu resmî kaynaklardan oluşturulur:

- TÜİK: Türkiye veri takvimi
- TCMB: PPK ve Enflasyon Raporu takvimi
- BLS: CPI, PPI, NFP ve JOLTS
- BEA: ABD GSYH ve PCE yayınları
- Federal Reserve: FOMC toplantıları

Takvim yalnız tanımlı `TRACKED_EVENTS` listesindeki olayları içerir. Sağlayıcılardan biri geçici olarak hata verirse sağlıklı sağlayıcıların verisi işlenmeye devam eder; güvenilir eski kayıtlar kapsam içinde `carried_forward=true` ve `stale=true` olarak korunabilir.

Production state `public.economic_events` tablosundadır. API ve sabah raporu canlı sağlayıcı çağrısı yapmadan Supabase state'ini okur.

### Bilanço takvimi

- BIST: KAP Finansal Takvim bildirimleri ve desteklenen şirketlerin resmî yatırımcı ilişkileri sayfaları
- ABD/NASDAQ: Yahoo Finance earnings takvimi
- Kapsam: Bugün ile bugün + 90 gün arasında toplam 91 takvim günü

BIST tarafında yalnız şirket tarafından açıklanan planlı ilan tarihi kullanılır; mevzuat son gönderim tarihleri bilanço tarihi sayılmaz. KAP ve resmî IR sonuçları sembol bazında mutabakata tabi tutulur.

Production state `public.earnings_events` tablosundadır. BIST mali dönem arşivi ayrıca `data/generated/earnings/bist100/` altında dönemsel JSON partition dosyaları olarak korunur.

### Portföy altyapısı

Supabase portföy altyapısı şunları destekler:

- TRY veya USD bazlı birden fazla portföy
- BUY ve SELL işlemleri
- DEPOSIT ve WITHDRAWAL nakit hareketleri
- İstemci isteği bazında idempotency
- FIFO lot eşleştirmesi
- Gerçekleşmiş kâr/zarar
- Kalan pozisyon ve maliyet
- Günlük portföy snapshot'ları
- Modified Dietz günlük getiri ve zincirlenmiş TWR
- İşlem ve nakit hareketi reversal akışları
- Kullanıcının yatırım verilerini tek işlemde sıfırlama

Mobil istemci portföy mutation tablolarına doğrudan yazmamalı; Supabase RPC fonksiyonlarını kullanmalıdır.

## Proje Klasör Yapısı

```text
FinBot/
├── .github/workflows/             # Zamanlanmış ve manuel GitHub Actions işleri
├── app/
│   ├── api/                       # Salt-okunur FastAPI endpoint'leri
│   ├── core/                      # Ayarlar, DB bağlantısı ve seans yardımcıları
│   ├── jobs/                      # CLI ve zamanlanmış job giriş noktaları
│   ├── models/                    # Pydantic veri sözleşmeleri
│   ├── providers/
│   │   ├── bist/                  # Yahoo BIST fiyat sağlayıcısı
│   │   ├── calendar/              # TÜİK, TCMB, BLS, BEA, Fed
│   │   ├── crypto/                # Binance fiyat sağlayıcısı
│   │   ├── earnings/              # KAP, Yahoo ve resmî IR sağlayıcıları
│   │   ├── fx/                    # USDTRY sağlayıcısı
│   │   ├── nasdaq/                # Yahoo ABD hissesi sağlayıcısı
│   │   └── tefas/                 # pytefas fiyat sağlayıcısı
│   ├── repositories/              # Supabase/PostgreSQL erişim katmanı
│   └── services/
│       └── job_tracking/          # Runner, adapter ve bildirim politikası
├── data/generated/earnings/bist100/
│                                    # Kalıcı BIST mali dönem arşivi
├── requirements/jobs/             # Job'a özel kilitli minimal bağımlılıklar
├── scripts/
│   └── export_reference_seed.py   # Referans verilerini seed.sql'e aktarır
├── supabase/
│   ├── migrations/                # Şema, RLS, trigger ve RPC geçmişi
│   ├── tests/                     # Supabase SQL testleri
│   ├── config.toml                # Yerel Supabase yapılandırması
│   └── seed.sql                   # İdempotent referans verisi
├── tests/
│   ├── integration/               # DB eşzamanlılık kontrolleri
│   └── sql/                       # Portföy RPC entegrasyon testleri
├── requirements.txt              # Tam geliştirme/runtime ortamı
└── README.md
```

## Kurulum

Python 3.11 production workflow'larında kullanılan sürümdür.

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --requirement requirements.txt
python -m pip check
```

Fiyat ve snapshot workflow'ları yalnız ihtiyaç duydukları kilitli bağımlılık dosyasını kurar:

```text
requirements/jobs/binance.txt
requirements/jobs/bist.txt
requirements/jobs/fx.txt
requirements/jobs/nasdaq.txt
requirements/jobs/portfolio-snapshots.txt
requirements/jobs/tefas.txt
```

### Ortam değişkenleri

`.env.example` dosyasını temel alın:

```env
APP_NAME=FinBot
TIMEZONE=Europe/Istanbul
PORT=8000
BIST_MOVER_MAX_ABS_CHANGE_PERCENT=15.0

SUPABASE_DATABASE_URL=postgresql://...

EMAIL_USER=...
EMAIL_APP_PASSWORD=...
EMAIL_TO=...
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
```

Gerçek DSN, parola, SMTP token'ı veya başka bir secret kaynak koda ve loglara yazılmamalıdır.

## HTTP API

Sunucuyu çalıştırmak için:

```bash
uvicorn app.main:app --reload
```

API salt okunurdur; veri yenilemez ve e-posta göndermez.

### Genel piyasa

| Endpoint | Amaç |
|---|---|
| `GET /health` | Uygulama health check |
| `GET /api/market-summary` | Güncel piyasa özeti; `?format=text` destekler |
| `GET /api/bist-movers` | En çok yükselen/düşen üç BIST hissesi |

### Ekonomik takvim

| Endpoint | Amaç |
|---|---|
| `GET /api/economic-calendar/today` | Bugünün olayları |
| `GET /api/economic-calendar/week?offset=0` | Seçilen hafta |
| `GET /api/economic-calendar/month?offset=0` | Seçilen ay |
| `GET /api/economic-calendar?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD` | En fazla 91 günlük özel aralık |

### Bilanço takvimi

`{market}` değeri `bist100` veya `nasdaq100` olabilir:

| Endpoint | Amaç |
|---|---|
| `GET /api/earnings/{market}/today` | İlgili piyasanın bugünkü takvimi |
| `GET /api/earnings/{market}/week?offset=0` | İlgili piyasanın haftalık takvimi |
| `GET /api/earnings/{market}/month?offset=0` | İlgili piyasanın aylık takvimi |
| `GET /api/earnings/{market}?start_date=...&end_date=...` | İlgili piyasanın özel tarih aralığı |
| `GET /api/earnings/today` | BIST ve NASDAQ ortak görünümü |
| `GET /api/earnings/week?offset=0` | Ortak haftalık görünüm |
| `GET /api/earnings/month?offset=0` | Ortak aylık görünüm |
| `GET /api/earnings?start_date=...&end_date=...` | Ortak özel tarih aralığı |

Hafta, ay veya özel aralık istekleri Supabase'deki 91 günlük kapsamla hiç kesişmiyorsa `422`; bugünkü veri kullanılamıyorsa `503` dönebilir. Ortak endpoint, piyasalardan biri hatalı olduğunda sağlıklı piyasanın verisini `partial` sonuç içinde korur.

## Production Job'ları

| Job | GitHub Actions zamanlaması | Bildirim politikası |
|---|---|---|
| `economic_calendar_refresh_job` | Her gün 07:30 Türkiye | Her tamamlanmada özet |
| `earnings_refresh_job` | Her gün 08:00 Türkiye | Her tamamlanmada özet |
| `morning_job` | Hafta içi 10:25 Türkiye | Her tamamlanmada özet |
| `closing_job` | Hafta içi 18:30 Türkiye | Her tamamlanmada özet |
| `binance_crypto_latest_prices_job` | Her gün 08:17–00:17, iki saatte bir | Partial/failed |
| `bist_latest_prices_job` | Hafta içi 10:30, 12:30, 14:30, 16:30, 18:30 | Partial/failed |
| `nasdaq_latest_prices_job` | Hafta içi `14:30–21:30 UTC`, saat başı aday tetikleme | Partial/failed |
| `tefas_latest_prices_job` | Hafta içi 21:00 Türkiye | Partial/failed |
| `fx_latest_price_job` | Hafta içi 08:27–00:27, iki saatte bir | Partial/failed |
| `portfolio_snapshots_job` | Her gün 02:00 Türkiye | Partial/failed |

Ek işler:

- `bist_earnings_backfill_job`: Manuel BIST mali dönem arşiv backfill'i.
- `database_health_check`: Manuel ve salt-okunur şema kontrolü.

### Fiyat, snapshot ve piyasa özeti job komutları

Fiyat ve snapshot job'larında varsayılan davranış dry-run'dır:

```bash
python -m app.jobs.binance_crypto_latest_prices_job --dry-run
python -m app.jobs.bist_latest_prices_job --dry-run
python -m app.jobs.nasdaq_latest_prices_job --dry-run
python -m app.jobs.tefas_latest_prices_job --dry-run
python -m app.jobs.fx_latest_price_job --dry-run
python -m app.jobs.portfolio_snapshots_job --dry-run
```

Canlı veritabanı yazımı için ilgili komuta `--apply-db`; e-postayı kapatmak için `--no-email` eklenir.

```bash
python -m app.jobs.nasdaq_latest_prices_job --apply-db
python -m app.jobs.portfolio_snapshots_job --apply-db --snapshot-date 2026-08-28
```

Snapshot işleyişi: Supabase `lucrari-portfolio-chart-snapshots` görevi her saat `:55`'te
o günün değerlemesini kaydeder. Gece 02:00 Türkiye saatinde planlanan GitHub job'ı,
önceki gün içinde kaydedilmiş son değerlemeyi `private.resolve_daily_portfolio_snapshot`
ile doğrular ve yeniden kullanır. Bu, tam gece yarısı kapanış fiyatı garantisi değil,
`captured_at` anında alınmış portföy görüntüsüdür. GitHub gecikse veya tekrar çalışsa da
geçmiş snapshot'ın nakdi, fiyatları, kuru ve kayıt zamanı değiştirilmez.

İstenen günün kaydı yoksa job `partial` ve `missing_snapshot_count` uyarısı üretir;
bugünkü verilerle geçmiş kayıt doldurmaz. Gerçek DB hataları `failed` olarak kalır.
Bugünün tarihi açıkça verilirse mevcut güncel değerleme motoru kullanılmaya devam eder.
Reused kayıtlar takip metriklerinde `unchanged_count` sayılır; veri yazımı olarak gösterilmez.

Dağıtım sırası: önce `20261121000000_daily_snapshot_recorded_state.sql` migration'ını
uygula, ardından Python job değişikliğini yayınla. Mevcut geçmiş veriler değişmez.
Geri dönüşte önce önceki Python sürümüne dön, ardından yeni özel fonksiyonu kaldır;
güncel değerleme fonksiyonu ve saatlik görev bu değişiklikten etkilenmez.

Yerel gerçek PostgreSQL kabul testi (geçici cluster, canlı bağlantı yok):

```bash
RUN_SNAPSHOT_POSTGRES=1 python -m pytest --allow-hosts=127.0.0.1 -q tests/test_daily_snapshot_postgres.py
```

Piyasa özeti ve takvim job'ları:

```bash
python -m app.jobs.morning_job
python -m app.jobs.morning_job --dry-run
python -m app.jobs.closing_job
python -m app.jobs.closing_job --dry-run
python -m app.jobs.economic_calendar_refresh_job --dry-run
python -m app.jobs.earnings_refresh_job --dry-run
python -m app.jobs.bist_earnings_backfill_job --year 2026
python -m app.jobs.database_health_check
```

### NASDAQ gecikme dayanıklılığı

NASDAQ workflow'u GitHub Actions gecikmelerinde geçerli çalışmayı kaçırmamak için sabit slot kullanmaz.

- Başlangıç: `market_open + 30 dakika`
- Bitiş: `market_close + 6 saat`
- Seans içi: `execution_window=regular_session`
- Kapanış sonrası: `execution_window=post_close`
- Açılışın ilk 30 dakikası: `market_not_ready`
- Pencere sonrası: `outside_market_window`
- Tatil/hafta sonu: `market_closed`

Yalnız normal seans barları kabul edilir; pre-market ve after-hours barları reddedilir. Yaz/kış saati ve erken kapanışlar `pandas_market_calendars` NASDAQ takviminden çözülür.

### TEFAS katalog mutabakatı

TEFAS fiyat job'u aynı çalışmada yatırım fonu kataloğunu da gözlemler:

- Yeni ve pozitif fiyatlı fonu aktif ekler ve fiyatını yazar.
- Temiz `0.0` fiyatlı ve hiç fiyat geçmişi olmayan fonu pasif tutar/yapar.
- Daha önce fiyatı bulunan fonda geçici fiyat yoksa fonu aktif bırakır ve mevcut fiyatı stale yapar.
- Katalogdan kaybolan fonu soft-deactivate eder.
- Bozuk, çelişkili veya kimliği belirsiz satırlarda güvenli davranır; belirsiz veriye dayanarak fon pasifleştirmez.
- Gözlemlenen katalog aktif kataloğun `%80` altına düşerse mutabakatı fail-closed durdurur.

## Merkezî Job Takibi ve Bildirimler

On ana production job `TrackedJobRunner` üzerinden takip edilir.

### `refresh_runs`

Her job çalışması için tek ana kayıt tutar:

- Job adı, işlem türü ve kapsam
- `running`, `success`, `partial`, `failed` veya `skipped` durumu
- Başlangıç/bitiş zamanı ve metrikler
- GitHub Actions run kimliği ve idempotency bilgileri
- Hata ve uyarılar
- E-posta teslimat durumu

### `provider_run_results`

Yalnız gerçekten çok sağlayıcılı işler için alt kırılım tutar:

- Ekonomik takvim: TÜİK, TCMB, BLS, BEA ve Fed
- Bilanço takvimi: KAP, resmî IR ve Yahoo Finance

Tek sağlayıcılı fiyat job'ları ile sabah/kapanış piyasa özeti job'ları bu tabloda gereksiz alt kayıt oluşturmaz; yalnızca `refresh_runs` kullanılır.

### Bildirim politikaları

`ALERT_ON_PARTIAL_OR_FAILED`:

- `success`: E-posta yok
- `partial`: Uyarı e-postası
- `failed`: Hata e-postası
- `skipped`: E-posta yok

`ALWAYS_ON_FINISH` sabah/kapanış piyasa özetleri ile ekonomik ve bilanço takvimi job'larında kullanılır; başarılı, kısmi ve hatalı tamamlanmalarda e-posta gönderir.

Ortak kurallar:

- `--dry-run`: DB tracking kaydı ve gerçek e-posta yok
- `--no-email`: E-posta kapalı
- Teslimat sonucu `refresh_runs.notification_status`, `notification_error` ve `notified_at` alanlarında tutulur.
- Log, DB ve e-posta içerikleri secret sızıntısına karşı sanitize edilir.

Takip yaşam döngüsü başlangıç, iş mantığı, finalization ve bildirim için ayrı transaction sınırları kullanır. İş mantığı rollback olduğunda başlangıç kaydı kaybolmaz; finalization problemi oluşursa compensation akışı kaydı terminal `failed` durumuna getirmeyi dener.

### Mobil Sistem Sağlığı RPC (`get_mobile_system_health`)

Mobil uygulamanın arka plan işlerinin durumunu güvenli ve deterministik izlemesi için `public.get_mobile_system_health()` RPC fonksiyonu sunulur:

- **Güvenlik**: `SECURITY DEFINER`, `SET search_path = pg_catalog`, sahibi `postgres`.
- **Rol Yetkileri**: `anon` ve `PUBLIC` rolleri tamamen kapatılmıştır; yalnızca anonim/kullanıcı oturumuyla `authenticated` rolüne sahip ve `auth.uid()` içeren istemciler çağırabilir.
- **Fail-Closed**: `auth.uid() IS NULL` durumunda sabit `authentication_required` hatası fırlatılır.
- **Tablo İzolasyonu**: `refresh_runs` ve `provider_run_results` tablolarına doğrudan `SELECT` erişimi kapalı kalır.
- **Hassas Veri Koruması**: Teknik `job_name`, ham hata/uyarı metinleri, GitHub run ID'leri, bildirim durumları ve UUID'ler mobil sözleşmeye dahil edilmez.
- **Kapsam**: Tabloda hiç kayıt olmasa dahi allowlist'teki 10 job'ın her biri için tam 1 satır üretir.

#### Mobil Sözleşme Kolonları (13 Kolon)

1. `job_code` (text)
2. `category` (text)
3. `display_name` (text)
4. `sort_order` (integer)
5. `execution_status` (text: `running` \| `success` \| `partial` \| `failed` \| `skipped` \| `never_run`)
6. `health_status` (text: `healthy` \| `warning` \| `critical` \| `running` \| `inactive` \| `unknown`)
7. `last_started_at` (timestamptz)
8. `last_finished_at` (timestamptz)
9. `last_success_at` (timestamptz)
10. `duration_seconds` (numeric)
11. `is_overdue` (boolean)
12. `message_code` (text: `JOB_OK` \| `JOB_PARTIAL` \| `JOB_FAILED` \| `JOB_OVERDUE` \| `JOB_RUNNING` \| `JOB_SKIPPED` \| `JOB_NEVER_RUN`)
13. `checked_at` (timestamptz)

| job_code | category | display_name | `run_overdue_after` | `success_stale_after` |
|---|---|---|---|---|
| `BIST_PRICES` | `prices` | `BIST 100 Fiyatları` | 72 saat | 96 saat |
| `NASDAQ_PRICES` | `prices` | `ABD Hisse Fiyatları` | 72 saat | 96 saat |
| `CRYPTO_PRICES` | `prices` | `Kripto Para Fiyatları` | 14 saat | 24 saat |
| `TEFAS_PRICES` | `prices` | `TEFAS Fon Fiyatları` | 76 saat | 96 saat |
| `USDTRY_PRICE` | `prices` | `USD/TRY Kuru` | 76 saat | 96 saat |
| `EARNINGS_CALENDAR` | `calendar` | `Finansal Sonuç Takvimi` | 36 saat | 48 saat |
| `ECONOMIC_CALENDAR` | `calendar` | `Ekonomik Takvim` | 36 saat | 48 saat |
| `PORTFOLIO_SNAPSHOTS` | `portfolio` | `Portföy Değerleme` | 36 saat | 48 saat |
| `MORNING_SUMMARY` | `summary` | `Sabah Piyasa Özeti` | 76 saat | 96 saat |
| `CLOSING_SUMMARY` | `summary` | `Kapanış Piyasa Özeti` | 76 saat | 96 saat |

## Supabase Şeması

`supabase/migrations/` dizini şu yapıların kaynak kontrollü geçmişidir:

- Tablolar, kolonlar, constraint ve indexler
- Trigger ve SQL yardımcı fonksiyonları
- RLS policy'leri
- Portföy mutation/read RPC'leri
- FIFO ve snapshot hesaplama fonksiyonları
- Merkezî job takip şeması

Yeni bir şema değişikliği mevcut migration değiştirilerek değil, ileri yönlü yeni migration eklenerek yapılmalıdır.

```bash
supabase link --project-ref PROJECT_REF
supabase migration list
supabase db push
```

`20260825000000_baseline_schema.sql`, mevcut şemanın başlangıç noktasıdır. Mevcut production ortamında baseline migration geçmişe `applied` olarak kaydedilmiştir; sonraki migration'lar `supabase db push` ile sırayla uygulanır.

### Referans verisi seed'i

`supabase/seed.sql` yalnız şu referans verilerini içerir:

- `data_providers`
- `instruments`
- `kap_member_mappings`

Kullanıcı, portföy, fiyat, takvim olayı veya operasyon logu içermez. Doğal anahtarlarla upsert yaptığı için tekrar çalıştırılabilir.

Mevcut bağlantılı veritabanından salt-okunur şekilde yeniden üretmek için:

```bash
python scripts/export_reference_seed.py
```

## Mobil Portföy RPC'leri

Mobil uygulama Anonymous Sign-In sonrasında `authenticated` rolüyle çalışır. RLS, `auth.uid() = portfolios.user_id` kontrolüyle kullanıcıları birbirinden ayırır.

Temel RPC fonksiyonları:

```text
record_portfolio_transaction(...)
record_cash_flow(...)
reverse_portfolio_transaction(...)
reverse_cash_flow(...)
reset_my_investments(...)
get_portfolio_positions(...)
get_portfolio_cash_balances(...)
```

Ek okuma ve hesaplama fonksiyonları ilgili migration'larda tanımlıdır. RPC'ler:

- Portföy sahipliğini doğrular.
- `client_request_id` ile çift gönderimi engeller.
- İşlemleri aynı portföy kapsamında kilitler.
- BUY işleminde ilgili para biriminde yeterli nakit bulunmasını zorunlu tutar.
- WITHDRAWAL işleminde nakit bakiyesinin negatife düşmesini engeller.
- Pozisyonu aşan SELL'i reddeder.
- FIFO eşleştirmesi ve gerçekleşmiş kâr/zararı atomik hesaplar.
- Hata durumunda bütün değişiklikleri rollback eder.

Yeni bir portföyde TRY ve USD nakit hesapları oluşturulur. Kullanıcı BUY işleminden önce ilgili para biriminde `record_cash_flow(..., DEPOSIT, ...)` ile yeterli nakit yatırmalıdır. Kullanılabilir bakiye `açılış bakiyesi + DEPOSIT - WITHDRAWAL - BUY + SELL` mantığıyla hesaplanır.

`reset_my_investments`, doğru confirmation değeri olmadan çalışmaz. Kullanıcı ve portföy kayıtlarını korur; yatırım hareketlerini, FIFO eşleşmelerini ve snapshot verilerini atomik temizler.

### Portföy Hareketleri RPC (`get_my_portfolio_activity`)

Mobil uygulama, alış/satış işlemleri ile para yatırma/çekme hareketlerini
`public.get_my_portfolio_activity(...)` üzerinden tek bir kronolojik akışta
okur. RPC yalnız oturumdaki kullanıcının aktif portföyünü kabul eder; doğrudan
tablo erişimi açılmaz.

- Sıralama ve sonraki sayfa cursor'ı `occurred_at`, `created_at`,
  `source_kind`, `activity_id` alanlarının tamamını kullanır.
- Filtreler `ALL`, `BUY`, `SELL`, `DEPOSIT`, `WITHDRAWAL` ile sınırlıdır.
- Arama yalnız işlem satırlarının sembol ve şirket adında çalışır; nakit
  hareketlerine uygulanmaz.
- İşlem tutarı tarihsel `quantity * unit_price`, nakit tutarı ise kaydedilmiş
  `amount` değeridir. Güncel fiyat veya döviz kuru kullanılmaz.
- İptal edilen kayıtlar iptal alanlarıyla görünür; pasif enstrümanların geçmiş
  işlemleri saklanır.

`reset_my_investments` işlem ve nakit hareketlerini fiziksel olarak sildiği
için hareket RPC'si reset öncesi geçmişi arşivlemez; yalnız veritabanında kalan
reset sonrası hareketleri döndürür.

## Testler

Projenin hedef ve production Python sürümü 3.11'dir. Yerel geliştirme/test
ortamını tam uygulama bağımlılıkları ve test araçlarıyla kurmak için:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --requirement requirements/dev.txt
python -m pip check
```

Ruff kod kalite kontrolü:

```bash
python -m ruff check .
```

Tüm hızlı Python testleri gerçek internete, SMTP'ye veya production
veritabanına bağlanmadan çalışacak şekilde tasarlanmıştır:

```bash
PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest -p no:cacheprovider -q
```

Satır ve karar dallarını birlikte ölçen coverage taban raporu:

```bash
PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest -p no:cacheprovider -q \
  --cov=app --cov-branch --cov-report=term-missing \
  --cov-report=html --cov-report=xml
```

Coverage yalnız `app/` altındaki production Python kodunu ölçer ve satırlarla
birlikte karar dallarını da hesaba katar. Zorunlu global eşik `%80`'dir. HTML
raporu `htmlcov/index.html`, makine tarafından okunabilir rapor ise
`coverage.xml` altında oluşturulur ve bu dosyalar Git'e eklenmez.

GitHub Actions'taki `CI` workflow'u her pull request'te ve `main` branch'ine
gönderilen her değişiklikte üç kalite kapısı çalıştırır:

```text
CI / Ruff
CI / Python tests
CI / Database integration
```

Ruff ve hızlı Python testleri paralel çalışır. İkisi de geçtikten sonra
veritabanı işi, yerel Supabase'i Docker üzerinde sıfırdan kurar; migration ve
seed verisini uygular, yedi SQL dosyasını ve eşzamanlılık testini çalıştırır.
Tüm işler Python 3.11 kullanır. Python test işi `%80` coverage eşiğini uygular
ve oluşan XML/HTML raporlarını 14 gün saklanan bir workflow artifact'i olarak
yükler.

CI yalnız geçici ve yerel PostgreSQL kullanır. Production Supabase'e bağlanmaz;
production secret'ı, Supabase erişim anahtarı veya `.env` dosyası gerektirmez.
Supabase CLI sürümü tekrarlanabilir sonuçlar için `2.109.1` olarak sabittir.

Whitespace kontrolü:

```bash
git diff --check
```

Supabase portföy entegrasyon kontrolleri ayrıca şu dizinlerde bulunur:

```text
supabase/tests/
tests/integration/
tests/sql/
```

Bu gerçek PostgreSQL/Supabase testleri hızlı pytest çalışmasına dahil değildir.
Migration, seed, yedi SQL testi ve eşzamanlılık kontrolünü güvenli bir yerel
veritabanında tek komutla çalıştırmak için:

```bash
./scripts/test_db_integration.sh
```

Koşucu uzak veritabanı adreslerini reddeder. Katmanların ayrıntılı açıklaması,
gereksinimler ve servis yaşam döngüsü seçenekleri `tests/README.md` içindedir.

Canlı provider smoke kontrolleri de hızlı testlerden ayrıdır. Canlı
`--apply-db`, SMTP, migration veya seed işlemleri yalnız doğru ortam ve
secret'larla bilinçli olarak çalıştırılmalıdır.

## Push notifications

Lucrari için özel Supabase tabloları, cihaz/tercih RPC'leri ve bağımsız EC2
bildirim worker'ı eklendi. V2 tercih sözleşmesi iki kartta yedi ayrı seçeneği destekler:
portföy bilanço özeti 10.00, şirket hatırlatmaları 13.30'dan itibaren 15 dakika
arayla, takip listesi 14.30; genel bilanço takvimi 10.15 ve yarının takvimi 20.00.
Ekonomik göstergeler için 3 saat, 1 saat ve 15 dakika önce hatırlatma hazırlanır.
Portföy ve genel sabah özetleri güncel kaynak doğrulanırsa boş günlerde de gönderilir.
Günlük portföy özeti 00.00 Türkiye saatinde yalnız kâr/zararı gösterir; toplam
portföy değerini içermez. Tam ve ardışık değerleme verisi gerekir.
Varsayılan saat dilimi Europe/Istanbul. Mobil kartların arayüzü ayrı entegrasyon gerektirir.

Kurulum, mobil veri sözleşmesi, gönderim sınırları ve yerel PostgreSQL testleri:
[Push notification sözleşmesi](docs/push-notifications.md).
Bu kodun eklenmesi canlı bildirimi başlatmaz; migration ve ayrı worker dağıtımı gerekir.
