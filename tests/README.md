# FinBot Test Katmanları

FinBot testleri, hızlı ve deterministik kontroller ile gerçek veritabanı veya
dış servis gerektiren kontrolleri birbirinden ayırır.

## Hızlı Python testleri

Kök `tests/test_*.py` dosyaları birim, mock, fixture ve sözleşme testleridir.
Bu testlerde gerçek internet, PostgreSQL ve SMTP erişimi yasaktır. Her pull
request için çalıştırılması planlanan ana test kapısı budur.

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q
```

Branch coverage raporu:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest -p no:cacheprovider -q \
  --cov=app --cov-branch --cov-report=term-missing \
  --cov-report=html --cov-report=xml
```

Coverage yalnız `app/` altındaki production Python kodunu ölçer. Testler,
migration dosyaları ve manuel scriptler bu orana dahil değildir. Satır ve dal
coverage toplamı `%80` altına düşerse komut başarısız olur.

## Ruff kod kalite kontrolü

Python kaynakları ve testleri için lint kapısı:

```bash
python -m ruff check .
```

Başlangıç kural grupları `E`, `F`, `I`, `B` ve `UP`'dir. Mevcut kodu tek
seferde davranış değişikliğine zorlamamak için satır uzunluğu, eski tip
anotasyonları, `datetime.UTC`, `StrEnum` ve bazı tanısal ara değişkenler ilk
geçişte ertelenmiştir. Ruff formatter henüz zorunlu değildir.

## PostgreSQL ve Supabase entegrasyon testleri

Aşağıdaki testler izole bir yerel Supabase/PostgreSQL ortamında çalışır ve
hızlı Python testlerine dahil değildir:

```text
supabase/tests/
tests/sql/
tests/integration/
```

Bu testler RLS, RPC yetkileri, transaction atomikliği, FIFO, reversal,
snapshot ve eşzamanlı satış davranışını gerçek PostgreSQL üzerinde doğrular.

Gereksinimler:

- Çalışan Docker Desktop veya Docker Engine
- Supabase CLI
- PostgreSQL `psql` istemcisi
- `psycopg` kurulmuş proje sanal ortamı

Bütün migration'ları ve seed verisini sıfırdan uygulayıp yedi SQL dosyasını ve
eşzamanlılık testini çalıştırmak için:

```bash
./scripts/test_db_integration.sh
```

Koşucu Supabase'i kendisi başlatırsa işlem sonunda kapatır ve yerel veri
volume'ünü siler. Supabase önceden çalışıyorsa mevcut geliştirici ortamını
kapatmaz. Koşucunun başlattığı ortamı incelemek için açık bırakmak isterseniz:

```bash
./scripts/test_db_integration.sh --keep-running
```

Koşucu yalnız `localhost`, `127.0.0.1` veya `::1` PostgreSQL adreslerini kabul
eder; production ve diğer uzak veritabanlarına karşı çalışmayı reddeder. SQL
dosyaları sabit sırada ve ilk hatada duracak şekilde çalıştırılır. Her dosya
transaction sonunda `ROLLBACK` yapar. Eşzamanlılık testi de başarılı veya
başarısız olmasından bağımsız olarak yalnız kendi sabit fixture UUID'lerini
temizler.

## GitHub Actions CI

`.github/workflows/ci.yml`, her pull request'te ve `main` branch'ine yapılan
her push'ta üç iş çalıştırır:

- `Ruff`: etkin lint kurallarını kontrol eder.
- `Python tests`: hızlı testleri branch coverage ile çalıştırır ve `%80`
  eşiğini uygular.
- `Database integration`: migration ve seed verisini sıfırdan uygular; yedi
  SQL testi ile eşzamanlılık kontrolünü gerçek PostgreSQL üzerinde çalıştırır.

Ruff ve hızlı Python testleri Python 3.11 üzerinde paralel çalışır. Veritabanı
işi bu iki hızlı kapı geçtikten sonra başlar; böylece başarısız bir lint veya
birim test için gereksiz Docker/Supabase ortamı kurulmaz. Coverage XML ve HTML
çıktıları GitHub Actions çalışmasının artifact bölümünde 14 gün saklanır.

Veritabanı işi `supabase/setup-cli@v3` ile sabit `2.109.1` CLI sürümünü kurar ve
aynı yerel `./scripts/test_db_integration.sh` komutunu çağırır. Docker Engine ve
`psql`, GitHub'ın Ubuntu runner'ından gelir. Test veritabanı yalnız runner
üzerindeki geçici `localhost` servisidir; production Supabase, SMTP veya
provider secret'ları kullanılmaz.

## Canlı sağlayıcı smoke testleri

Yahoo Finance, Binance, TEFAS, KAP ve resmî takvim kaynaklarına yapılan canlı
çağrılar hızlı test grubuna ait değildir. Bu kontroller zamanlanmış ve ayrı bir
smoke workflow'unda çalıştırılmalıdır; geçici dış servis sorunları pull request
testlerini kararsız hale getirmemelidir.

## Güvenlik sınırları

`tests/conftest.py` gerçek PostgreSQL ve SMTP bağlantılarını engeller.
`pytest-socket` ile etkinleştirilen `--disable-socket` seçeneği de hızlı testler
boyunca TCP/UDP ağ socketlerini kapatır. `asyncio` ve bellekte çalışan FastAPI
TestClient için yalnız yerel Unix socketlerine izin verilir. Ağ gerektiren bir
test eklenirse hızlı test grubuna sessizce dahil edilmemeli; açıkça `smoke` veya
`integration` katmanına yerleştirilmelidir.

## Bilinen bağımlılık uyarısı

Mevcut FastAPI/Starlette `TestClient`, `httpx` kullanımı için bir
`StarletteDeprecationWarning` üretiyor ve ileride `httpx2` geçişi istiyor. Uyarı
bilinçli olarak gizlenmez: test sonucu başarılıdır, fakat istemci geçişi ayrı bir
bağımlılık uyumluluğu değişikliği olarak ele alınmalıdır.
