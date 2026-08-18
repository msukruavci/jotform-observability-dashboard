# Jotform MCP Pulseboard — Baştan Sona Teknik ve Mimari Rapor

> Güncel ekran → view → query → model haritası, Mermaid akış diyagramları ve eksiksiz dosya rehberi için ayrıca [`MIMARI_AKIS_VE_KOD_REHBERI_TR.md`](MIMARI_AKIS_VE_KOD_REHBERI_TR.md) belgesine bakın.

> Bu belge projenin ana teknik kaynağıdır. Yalnızca bu dosya okunarak sistemin amacı, veri akışı, tabloları, ingestion davranışı, ekranları, API’leri, deployment topolojisi, test stratejisi ve her kaynak dosyanın sorumluluğu anlaşılabilir.

## 1. Yönetici özeti

Pulseboard, `jotform-workflow-mcp` uygulamasından bağımsız çalışan bir gözlemlenebilirlik ürünüdür. MCP runtime’ının görevi telemetry üretmek; Pulseboard’ın görevi bu telemetry’yi güvenilir biçimde saklamak, ilişkilendirmek, sorgulamak, görselleştirmek ve performans/verimlilik bulgularına dönüştürmektir.

Repo yerleşimi:

```text
jotform-mcp-phase1/
├── jotform-workflow-mcp/               # telemetry producer
│   └── .git/
└── jotform-observability-dashboard/    # query + analysis + UI
    └── .git/
```

Temel teknoloji seçimi:

| Katman | Teknoloji | Neden |
|---|---|---|
| Web ve domain | Django 5.2 LTS | Kararlı, admin/migration/template desteği güçlü |
| JSON API | Django REST Framework | İç chart API’leri ve gelecekte dış ingestion |
| Query store | PostgreSQL | İlişkisel drill-down, JSONB, aggregation ve indeksler |
| Queue/scheduler | Celery + Redis | Periyodik ingestion ve rule jobs |
| UI | Django Templates + HTMX hazırlığı | Ayrı SPA build zinciri olmadan hızlı panel |
| Charts | Apache ECharts | Zaman serisi, latency, pie ve waterfall desteği |
| Dev fallback | SQLite | Docker olmadan tek komutla geliştirme/test |
| Runtime | Docker Compose + Gunicorn + WhiteNoise | Tekrarlanabilir yerel/production-benzeri kurulum |

Uygulama şu an şunları yapar:

1. Mevcut agent turn JSONL kayıtlarını okur.
2. MCP tool ve Jotform HTTP started/completed çiftlerini birleştirir.
3. Revision JSONL kayıtlarını raw archive/query katmanına alır.
4. Canonical event v1 kayıtlarını işler.
5. Aynı session altında turn, model, tool ve HTTP span’lerini parent-child bağlar.
6. Exact duplicate, redundant read, retry storm ve incomplete span finding’leri üretir.
7. Overview, explorer, waterfall, tool, cost, findings ve data-health ekranlarını sunar.
8. Aynı dosya yeniden okutulduğunda duplicate üretmez.
9. Hatalı satırları tüm ingestion’ı durdurmadan quarantine’a taşır.
10. Celery Beat ile yalnızca dosyaya sonradan eklenen byte’ları işler.

## 2. Mimari sınır

MCP ve dashboard arasında kaynak kod bağımlılığı yoktur. Dashboard hiçbir MCP Python modülünü import etmez. Aralarındaki tek sözleşme append-only event verisidir.

```text
┌──────────────────────────── TELEMETRY PRODUCER ────────────────────────────┐
│ User → QA terminal → Model step → MCP tool → Jotform HTTP                 │
│                       contextvars ile correlation                          │
│                                   ↓                                        │
│                    immutable JSONL events                                  │
└───────────────────────────────────┬────────────────────────────────────────┘
                                    │ read-only mount
                                    ▼
┌────────────────────────────── PULSEBOARD ──────────────────────────────────┐
│ JSONL → checkpoint reader → adapter → raw_events → normalized PostgreSQL   │
│                                                   ↓                         │
│                                      rules + metrics + DRF                  │
│                                                   ↓                         │
│                             Django templates + ECharts                     │
└────────────────────────────────────────────────────────────────────────────┘
```

Bu sınırın sonuçları:

- Dashboard çökerse MCP ve Jotform işlemleri devam eder.
- Dashboard dependency’leri MCP runtime’ını büyütmez.
- JSONL immutable recovery/archive katmanı olarak kalır.
- PostgreSQL kaybedilirse JSONL yeniden backfill edilebilir.
- İleride bir dashboard birden fazla MCP workspace’i ingest edebilir.

## 3. Domain terminolojisi

```text
Workspace
└── Task
    └── Session
        └── Turn
            ├── Model Step Span
            │   └── Tool Call Span
            │       └── External HTTP Span
            └── Final answer
```

- **Workspace:** İzole ürün/ortam. Şu an varsayılanı `Jotform Workflow MCP`.
- **Task:** Kullanıcının yüksek seviyeli hedefi. Birden fazla turn içerebilir.
- **Session:** Bir agent konuşma/run sınırı. Dashboard’da ana explorer birimidir.
- **Turn:** Tek kullanıcı sorusu ve buna verilen nihai cevap.
- **Model step:** Agent loop’undaki tek provider model çağrısı.
- **Span:** Başlangıcı, bitişi, parent’ı, status’u ve süresi olan genel operasyon.
- **Tool call:** MCP tool adı, argümanı, sonucu ve hash’leri olan span detayı.
- **External call:** Jotform HTTP method, normalize URL, status ve byte ölçüleri.
- **Raw event:** Kaynak JSONL satırının kayıpsız kopyası.
- **Finding:** Evidence, confidence, severity ve estimated waste içeren kural sonucu.
- **Annotation:** Finding’in insan tarafından kabul/false-positive/snooze edilmesi.

## 4. Correlation tasarımı

Yeni telemetry zincirinde şu kimlikler taşınır:

| Alan | Yaşam süresi | İşlev |
|---|---|---|
| `task_id` | Kullanıcı hedefi | Birden fazla turn’ü birleştirir |
| `session_id` | Terminal konuşması | Agent ve MCP kayıtlarını aynı session yapar |
| `turn_id` | Tek soru | Model/tool/HTTP’yi doğru kullanıcı isteğine bağlar |
| `model_step_id` | Tek model çağrısı | Tool’u hangi reasoning adımının istediğini gösterir |
| `trace_id` | Tek turn trace’i | Dağıtık trace korelasyon anahtarıdır |
| `span_id` | Tek operasyon | Started/completed event çiftinin operation kimliğidir |
| `parent_span_id` | Parent operasyon | Model → tool → HTTP ağacını kurar |
| `request_id` | Started/completed çifti | Legacy eşleştirme ve recovery için kullanılır |
| `sequence_no` | Event akışı | Aynı timestamp durumunda deterministik sıra sağlar |

Producer’daki `contextvars` seçiminin nedeni async görevlerde global değişkenlerin context sızdırmasıdır. `ContextVar`, aynı process içindeki eşzamanlı coroutine’lerin session/turn/span değerlerini birbirinden ayırır. Nested `bind_context` çıkarken önceki token’ı geri yüklediği için HTTP span bittikten sonra context tekrar tool span’e, tool bittikten sonra model span’e döner.

Eski kayıtlarda bu alanlar bulunmadığından dashboard:

- Agent turn içindeki embedded tool listesini tahmini timing ile span’e dönüştürür.
- MCP `request_id` üzerinden started/completed eşleştirir.
- Açık tool sırasında gelen Jotform request’i nearest-open-tool heuristiğiyle bağlar.
- Bu kayıtları `correlation_confidence=low` işaretler.
- Yeni trace ID’li kayıtları `high` işaretler.

## 5. Canonical event v1

Sözleşmenin makine tarafından doğrulanabilir hali `contracts/canonical-event-v1.schema.json` dosyasındadır. Örnek iki satır `contracts/canonical-event-v1.example.jsonl` içindedir.

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "workspace_id": "jotform-workflow-mcp",
  "task_id": "task-id",
  "session_id": "session-id",
  "turn_id": "turn-id",
  "model_step_id": "model-step-id",
  "trace_id": "trace-id",
  "span_id": "span-id",
  "parent_span_id": "parent-span-or-null",
  "request_id": "request-id",
  "sequence_no": 14,
  "event_name": "mcp.tool_call.completed",
  "timestamp": "2026-08-14T10:00:00.486Z",
  "duration_ms": 486,
  "status": "ok",
  "attributes": {}
}
```

Event adları terminal faz ile biter: `.started`, `.completed`, `.failed`. Started satırında süre boş olabilir; completed satırı aynı `span_id` ile gelir ve `duration_ms` taşır.

## 6. Veritabanı modeli

### 6.1 Core

`Workspace` UUID primary key kullanır. `slug` ve `name` unique’tir. Environment ve timezone workspace kapsamındaki analizlerin ileride ayrıştırılması içindir.

### 6.2 Trace tabloları

| Model | Ana alanlar | İlişki |
|---|---|---|
| `Task` | external_id, name, kind, status, tags | workspace altında |
| `Session` | external_session_id, provider, model, status, confidence, duration | workspace/task altında |
| `Turn` | sequence_no, question, answer, tokens, cost | session altında |
| `Span` | trace/span/request/parent, kind, timing, status, attributes | session/turn altında |
| `ModelStep` | step_no, stop_reason, token breakdown | Span ile one-to-one |
| `ToolCall` | name, args/result JSON, hashes, error, bytes | Span ile one-to-one |
| `ExternalCall` | service, method, URL template, HTTP status, bytes | Span ile one-to-one |
| `UsageRecord` | metric, quantity, unit, cost, pricing version, estimated | Turn veya Span altında |

`Span` generic tutulur; ortak waterfall sorgusu için model/tool/external tablolarını UNION etmek gerekmez. Tip özel alanlar one-to-one detail tablosundadır. Bu, ortak timing kolonlarını tek yerde tutarken geniş JSON payload’ların her span satırını şişirmesini engeller.

`Session(workspace, external_session_id)` unique constraint’i agent ve MCP katmanlarının aynı kimlikle tek kayıtta birleşmesini garanti eder. `Turn(session, sequence_no)` unique constraint’i sıralamayı korur.

### 6.3 Ingestion tabloları

- `IngestionSource`: path + inode identity, byte checkpoint, parser version, status ve sayaçlar.
- `RawEvent`: source + source_offset unique, payload hash, event metadata ve tam JSON.
- `QuarantinedEvent`: parse/normalization hatasının offset’i, türü, mesajı ve sınırlı raw preview’su.

Path tek başına identity değildir; log rotation aynı path’e yeni inode getirebilir. Bu nedenle source constraint `path + inode` üzerindedir.

### 6.4 Finding tabloları

- `Finding`: rule code, severity, confidence, status, evidence, waste ve deterministic fingerprint.
- `Annotation`: finding’e verilen insan feedback’i.
- `RuleSetting`: kuralı aç/kapatma, varsayılan severity ve ileride threshold parametreleri.

Fingerprint unique olduğu için aynı kural tekrar çalıştığında finding çoğalmaz.

## 7. Ingestion algoritması

Tek dosya için gerçek akış:

```text
resolve absolute path
  → non-blocking file lock
  → stat(path): inode + size
  → get/create source(path, inode)
  → seek(last_offset)
  → complete newline oku
  → UTF-8 + JSON object doğrula
  → adapter türünü seç
  → transaction başlat
      → raw event get_or_create(source, offset)
      → yeni ise normalize et
    transaction commit
  → checkpoint'i sonraki byte'a taşı
  → EOF'te source health/sayaçlarını yaz
```

### 7.1 Neden byte offset?

JSONL append-only olduğu için her Beat turunda dosyayı baştan parse etmek gereksizdir. Binary stream offset’i satır sayısından daha güvenlidir; Unicode karakterlerin byte uzunluğu farklı olsa da `seek` kesin konuma gider.

### 7.2 Eksik son satır

Writer bir JSON satırını henüz tamamlamadıysa satır newline ile bitmez. Reader o satırı ingest etmez ve checkpoint’i ilerletmez. Bir sonraki tur tamamlanmış satırı yeniden okur.

### 7.3 Idempotency

`RawEvent(source_id, source_offset)` unique constraint’i DB düzeyinde son savunmadır. Aynı dosya aynı checkpoint’ten tekrar okunsa bile `get_or_create` normalization’ı ikinci kez çalıştırmaz.

### 7.4 Concurrency

Management command ile Celery Beat aynı dosyayı eşzamanlı okuyabilir. `ingestion_lock` path hash’inden `/tmp` lock dosyası üretir ve `fcntl.flock(LOCK_NB)` kullanır. Lock doluysa ikinci okuyucu `locked` döner. Celery task ayrıca bütün tree çevresinde global root lock alır; uzun ilk backfill Beat interval’ini aşsa bile queued tick’ler source’ları bölmez ve partial data üzerinde rule çalıştırmaz. `OperationalError: database is locked` bozuk input sayılmaz; checkpoint ilerletilmeden retry’a bırakılır.

Çok hostlu production’da local file lock yerine Redis distributed lock veya PostgreSQL advisory lock kullanılmalıdır. Mevcut Compose topolojisinde source mount ve host tek olduğundan file lock yeterlidir.

### 7.5 Truncate/rotation

Aynı inode’lu dosya checkpoint’ten daha küçükse otomatik reset veri çakışmasına yol açabileceği için source `truncated` işaretlenir. Yeni inode ile rotation otomatik olarak yeni source kaydı açar.

### 7.6 Quarantine

JSON decode, object-shape veya adapter hataları ilgili satıra özgü quarantine oluşturur; diğer satırlar devam eder. DB lock gibi transient infrastructure hataları quarantine yapılmaz.

## 8. Adapter davranışları

### 8.1 Agent turn adapter

Tanıma koşulu: payload’da `question` ve `tool_calls` bulunması.

- Timestamp turn sonu kabul edilir; başlangıç `timestamp - duration_ms` hesaplanır.
- Session provider/model ile enrich edilir.
- Task ID varsa Task oluşturulur ve Session’a bağlanır.
- Token ve cost Turn’a yazılır.
- Yeni trace ID’li kayıtta MCP span’leri zaten gerçek timing taşıyacağı için embedded tool listesi ikinci kez normalize edilmez.
- Legacy kayıtta embedded tool listesi tahmini ardışık span’lere çevrilir; `timing_is_estimated=true` yazılır.

### 8.2 MCP audit adapter

Tanıma koşulu: `event_type` alanı.

- `mcp.tool_call.started/completed/failed` request ID ile eşleşir.
- Started argümanları span attributes’a, completed sonucu `ToolCall.result` alanına yazılır.
- `jotform.request.*` ExternalCall’a dönüşür; started tarafı method/url/params/body bilgisini, completed/failed tarafı status/response/error bilgisini span attributes içinde de taşır.
- Yeni kayıtta explicit `parent_span_id`, legacy kayıtta nearest open tool kullanılır.
- `model.step.*` ModelStep ve model span’e dönüşür.
- Terminal event’i started olmadan geldiyse veri atılmaz; `missing_started_event=true` ile sentetik başlangıç oluşturulur.

### 8.3 Canonical v1 adapter

Tanıma koşulu: `schema_version` ve `event_name` bulunması.

Explicit span ID ve parent ID kullanır. Event adına göre kind belirler; completed kaydında tool/model detail oluşturur. Bu adapter gelecekte HTTP ingestion endpoint’i veya OTLP bridge tarafından da kullanılabilir.

### 8.4 Revision adapter

Revision kayıtları raw archive’a kayıpsız alınır. İlk sürümde workflow revision domain tablolarına açılmaz; bunun nedeni dashboard’ın ana sorgu biriminin agent execution olmasıdır. İleride mutation span ile revision ID bağlanabilir.

## 9. Rule engine

Kurallar deterministiktir; LLM judge kullanılmaz.

### `DUPLICATE_EXACT_CALL`

Aynı turn/session scope içinde `tool_name + argument_hash` birden fazla kez görülür. İlk çağrı gerekli kabul edilir; sonraki çağrıların süre toplamı wasted milliseconds olur.

### `REDUNDANT_READ`

Duplicate exact call’ın read-only tool setine özel adıdır. Öneri turn-scoped cache ve mutation sonrası invalidation’dır. Current tool set: `list_workflows`, `get_workflow`, `list_step_types`, `get_step_schema`, `get_form_fields`, revision read’leri.

### `RETRY_STORM`

Aynı scope/tool/argument hash için iki dakika içinde en az üç error çağrısıdır. Öneri exponential backoff, jitter ve retry limitidir.

### `INCOMPLETE_SPAN`

Beş dakikadan eski, hâlâ `running` olan span’dir. Process crash, timeout veya eksik terminal event ihtimalini gösterir.

Her finding:

- Evidence içinde tool, count, span IDs ve hash taşır.
- `high|medium|low` confidence taşır.
- `high|medium|low` severity taşır.
- `open|accepted|false_positive|snoozed` lifecycle taşır.
- Human feedback annotation olarak saklanır.

## 10. Metrikler

### Overview

- Session count
- `status=ok` session / total session success rate
- Session duration p95
- Bilinen turn cost toplamı
- Cost coverage: fiyatı bilinen turn / tüm turn
- Raw event ve quarantine sayısı
- Açık finding sayısı

### Tool Intelligence

Her tool için call count, p50, p95, error rate, average result bytes ve ilişkili açık finding sayısı hesaplanır. Percentile interpolation formülü sıralı dizi üzerinde `(n-1)*p` indeksini lineer interpolate eder.

### Cost

Turn cost’ları provider/model bazında gruplanır. Unknown fiyatlar `0` sayılmaz; coverage metriğinde açıkça görünür. `UsageRecord.pricing_version` historical repricing için veri zeminidir.

## 11. UI ekranları

### Overview `/`

KPI kartları, günlük session line chart, açık finding özeti ve en yavaş session tablosu. Amaç sistemin genel durumunu 10 saniyede göstermektir.

### Sessions `/sessions/`

Query string tabanlı arama, status/provider/tool filtreleri ve pagination. Filtreler URL’de kaldığı için link paylaşılabilir. Aktivitesi olmayan revision-artifact session’ları listeden çıkarılır; PostgreSQL `NULLS LAST` ve `ended_at → started_at` sıralamasıyla en son aktivite üreten session en üste gelir. Bu özellikle uzun süre açık kalan ve aynı session kimliğine yeni tool çağrıları ekleyen MCP prosesleri için önemlidir. Sonuç bölümü HTMX polling ile her 5 saniyede yenilenir. Tür sütunu, konuşma turn’ü bulunan kayıtları `Sohbet`, yalnızca MCP tool/API telemetrisi taşıyanları `MCP aktivitesi` olarak ayırır.

### Session detail `/sessions/<uuid>/`

- Session KPI strip
- `Tümü / MCP tool / Jotform API / Model / Hatalar` istemci tarafı operasyon filtreleri
- Görünen operasyonları topluca açma ve bütün operasyonları kapatma kontrolleri
- Kullanıcı ve asistan mesajlarını ayıran konuşma balonları
- Güvenli Markdown ile başlık, liste, kod ve bağlantı gösterimi
- Model, MCP tool ve Jotform HTTP çağrılarını özetleyen açılır işlem kartları
- Tool argümanlarını JSON yerine okunabilir anahtar/değer alanları olarak gösterme
- Tool kartlarında `MCP client -> MCP server` isteği ve `MCP server -> MCP client` yanıtını aynı işlem altında gösterme
- HTTP kartlarında `MCP server -> Jotform API` isteği ve `Jotform API -> MCP server` yanıtını aynı işlem altında gösterme
- Mevcut eski kayıtlar için aynı `request_id` değerine sahip RawEvent started/completed satırlarından request-response çiftini ekranda yeniden kurma
- Sohbet turn’ü bulunmayan doğrudan MCP session’larında en son 50 tool/API operasyonunu görünür aktivite akışı olarak gösterme
- Model/tool/HTTP renkli waterfall
- Parent span indentation
- Rule finding paneli
- Accept ve false-positive POST aksiyonları
- Ham telemetry verisini varsayılan olarak kapalı tutan geliştirici paneli
- İlk 50 raw event için sınırlandırılmış ve biçimlendirilmiş inspector

Waterfall bar konumu:

```text
offset_pct = (span.started_at - session.started_at) / session.duration * 100
width_pct  = span.duration / session.duration * 100
```

Çok kısa span’lerin görünür kalması için minimum genişlik `%0.35` uygulanır.

### Tool Intelligence `/tools/`

Call volume bar chart ve p95 line chart aynı grafikte; altta exact değer tablosu.

### Findings `/findings/`

Status, severity ve rule filtreli finding listesi. Evidence’e giden session detail linki bulunur.

### Cost & Capacity `/costs/`

Known total cost, pricing coverage, provider/model pie chart ve kesin değer tablosu.

### Data Health `/data-health/`

Her source için parser, status, byte offset, event/quarantine sayısı ve last ingest; ayrıca son parser failures.

### Admin `/admin/`

Workspace, trace, source, raw event, finding, annotation ve rule setting modelleri yönetilebilir.

## 12. JSON API

### `GET /api/v1/overview/`

```json
{"metrics": {"session_count": 30, "success_rate": 93.3}, "timeseries": []}
```

### `GET /api/v1/tools/`

Tool metrics array döner. Chart dışı entegrasyonlar aynı hesapları yeniden yazmaz.

### `GET /api/v1/sessions/<uuid>/trace/`

Session summary ve ordered span array döner. Her span parent ID, turn ID, kind, timestamps, duration, status ve attributes içerir.

API şu an iç ağ/read-only MVP kabulüyle anonymous’tur. Production öncesi DRF authentication ve permissions zorunludur.

## 13. Deployment

Compose servisleri:

```text
                    ┌─────────────┐
browser ───────────▶│ web:8000    │──┐
                    │ Gunicorn    │  │
                    └─────────────┘  │
                                     ▼
┌────────────────┐             ┌────────────┐
│ MCP JSONL :ro  │◀── worker ─▶│ PostgreSQL │
└────────────────┘      ▲      └────────────┘
                        │
                 ┌──────┴─────┐
                 │ Redis      │
                 └──────▲─────┘
                        │
                 ┌──────┴─────┐
                 │ Celery Beat│ every 5s
                 └────────────┘
```

- `db`: PostgreSQL 17, persistent volume ve healthcheck.
- `redis`: Redis 7, AOF persistent volume.
- `web`: `RUN_MIGRATIONS=1` ile migration owner’ı, ardından 3 Gunicorn worker ve HTTP healthcheck.
- `worker`: web healthy olduktan sonra başlayan iki Celery concurrency slot’u.
- `beat`: web healthy olduktan sonra periyodik ingestion task’ını gönderir.
- MCP repo üç application container’ına `/data/mcp:ro` bağlanır.

Production’da secret’lar `.env` yerine secret manager’dan gelmeli; DB/Redis dış dünyaya publish edilmemelidir.

## 14. Yerel geliştirme

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py ingest_logs
.venv/bin/python manage.py runserver
```

`DATABASE_URL` yoksa SQLite kullanılır. `MCP_LOG_ROOT` yoksa dashboard’ın sibling MCP reposu otomatik seçilir.

Admin:

```bash
.venv/bin/python manage.py createsuperuser
```

Tek dosya ingest:

```bash
.venv/bin/python manage.py ingest_logs /absolute/path/events.jsonl
```

Finding çalıştırmadan backfill:

```bash
.venv/bin/python manage.py ingest_logs --skip-findings
```

## 15. Test stratejisi

Dashboard testleri:

1. Agent turn normalize ve aynı dosyanın ikinci kez idempotent olması.
2. MCP tool ve HTTP started/completed pairing.
3. HTTP span’in tool parent’ına bağlanması.
4. URL’de uzun numeric ID’nin `{id}` ile normalize edilmesi.
5. Invalid JSON quarantine davranışı.
6. Finding fingerprint nedeniyle rule rerun idempotency.
7. Instrumented agent+MCP’nin aynı Session/Turn’da birleşmesi.
8. Model → tool parent chain ve trace ID bütünlüğü.
9. Yedi HTML sayfasının render smoke testi.
10. Üç DRF endpoint'inin response shape testi.
11. Konuşma görünümü ve okunabilir tool kartı render testi.
12. Markdown içindeki güvensiz HTML'in escape edilmesi ve sunum yardımcılarının boyut sınırı testi.
13. Boş session kayıtlarının listeden çıkarılması ve `NULLS LAST` ile yeni aktivitenin önce gelmesi.
14. HTMX session polling partial response ve MCP-only tool aktivitesi görünümü.
15. Tool/HTTP request-response çiftlerinin detail ekranında birlikte render edilmesi.
16. Operasyon filtre/aç-kapat veri nitelikleri ile tema ve mobil navigasyon kontrollerinin base layout'ta bulunması.

Producer testleri correlation context’in event’e `schema_version`, session, turn, trace, span, parent ve sequence eklediğini doğrular. Mevcut tool/client regresyon testleri de instrumentation sonrası çalıştırılmıştır.

Komutlar:

```bash
# Dashboard
.venv/bin/python manage.py test
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check --dry-run

# MCP producer
.venv/bin/python -m pytest tests/test_audit_log.py -q
.venv/bin/python -m pytest tests/test_jotform_client.py tests/test_building_tools.py tests/test_reading_tools.py tests/test_risky_tools.py -q
```

## 16. Güvenlik ve PII

Hazır savunmalar:

- MCP producer secret-key ve bearer/basic/token pattern’lerini recursive redact eder.
- Çok büyük payload’lar 12.000 karakter preview ile truncate edilir.
- MCP repo dashboard container’larına read-only mount edilir.
- Django CSRF middleware Finding POST’larını korur.
- Raw archive DB’de ayrı tabloda tutulur.
- Unknown cost yanlışlıkla zero yapılmaz.

Production öncesi gerekenler:

1. Authentication, RBAC ve SSO.
2. E-posta, soru, cevap ve form content’i için field-level PII masking.
3. Retention/purge policy ve audit trail.
4. TLS ve secure cookie ayarları.
5. DRF permission sınıfları ve rate limit.
6. PostgreSQL encrypted backup/restore testi.
7. CDN bağımlılıklarını self-host etmek veya CSP allowlist uygulamak.
8. `DJANGO_SECRET_KEY`, DB password ve Redis credentials secret manager’a almak.

## 17. Dosya dosya ve satır bloğu rehberi

Bu bölüm bütün kaynak dosyaları kapsar. Boş `__init__.py` dosyaları package marker’dır; runtime logic içermez. `__pycache__`, `.venv`, `db.sqlite3` ve `staticfiles` generated/ignored olduğu için kaynak tasarımının parçası değildir.

### Kök dosyalar

#### `manage.py` (1–15)

- 1: executable shebang.
- 2–3: environment ve argv importları.
- 6–10: default settings module’ünü seçip Django CLI dispatcher’ını çalıştırır.
- 13–15: script entry guard.

#### `requirements.txt` (1–8)

Her satır deploy’un deterministic dependency pinidir: Django, DRF, Celery, Redis client, psycopg binary, Gunicorn ve WhiteNoise. Major/minor drift’in sessiz davranış değişikliği üretmemesi için exact version kullanılır.

#### `.env.example` (1–9)

Secret/debug/allowed-host, PostgreSQL, Celery broker/result, MCP mount ve Beat interval contract’ını gösterir. Gerçek secret içermez.

#### `.gitignore` (1–13)

Secret, virtualenv, Python cache, SQLite, test coverage, collected static ve OS metadata’yı repository dışında tutar.

#### `.dockerignore` (1–9)

Git metadata, secret `.env`, local virtualenv/cache/SQLite, collected static ve uzun dokümantasyonun Docker build context’ine girmesini engeller. Böylece image küçük kalır ve local secret katmana yanlışlıkla kopyalanmaz.

#### `Dockerfile` (1–16)

- 1: Python 3.12 slim runtime.
- 3–5: bytecode ve buffered log davranışı.
- 7–10: `/app`, non-root `app` user ve dependency install.
- 11–13: source copy, collectstatic, ownership.
- 14: runtime non-root’a düşer.
- 15–16: migration entrypoint ve Gunicorn default command.

#### `docker-entrypoint.sh` (1–6)

Shell fail-fast modunu açar, yalnız `RUN_MIGRATIONS=1` verilen web servisinde migration uygular ve `exec` ile verilen process’i PID 1 yapar. Böylece migration yarışı oluşmaz ve container signals Gunicorn/Celery’ye doğru gider.

#### `docker-compose.yml` (1–67)

- 2–15: PostgreSQL credentials, volume ve readiness healthcheck.
- 17–27: Redis AOF ve ping healthcheck.
- Web bloğu: build, migration-owner environment, port, read-only MCP mount ve HTTP healthcheck.
- Worker bloğu: Celery worker ve `web: service_healthy` bağımlılığı.
- Beat bloğu: scheduler ve `web: service_healthy` bağımlılığı.
- 67 sonrası volume identity’leri.

#### `Makefile` (1–28)

Virtualenv install, migration, ingestion, run, test, drift check ve Compose lifecycle komutlarının kısa alias’larıdır.

#### `dashboard`

Kullanıcı dostu Docker lifecycle wrapper’ıdır. Script kendi gerçek konumunu symlink üzerinden çözer; bu nedenle `~/.local/bin/dashboard` içinden çağrılsa bile doğru Compose dosyasını bulur. `up` eksik `.env` dosyasını örnekten oluşturur, build/start yapar ve healthcheck’leri bekler. `down` named volume’ları silmez. Ayrıca `restart`, `status`, `logs` ve `open` komutlarını sunar.

### Config package

#### `config/settings.py` (1–101)

- 1–11: standard library, base path, environment flags.
- 13–25: Django/DRF/local app registry.
- 27–36: security, session, auth, CSRF ve WhiteNoise middleware sırası.
- 38–49: URL, templates, WSGI/ASGI.
- 52–68: `DATABASE_URL` parser; yoksa SQLite, varsa PostgreSQL.
- 71–88: locale/timezone/static/default key ve debug-aware storage backend.
- 90–95: DRF JSON/pagination defaults.
- 97–101: Celery broker/result/timezone/Beat schedule ve MCP log root.

#### `config/urls.py` (1–17)

Admin, yedi HTML ekranı ve `/api/v1/` router’ını tek root URLConf altında bağlar.

#### `config/celery.py` (1–10)

Django settings’i Celery namespace ile yükler ve installed app task’larını autodiscover eder.

#### `config/asgi.py`, `config/wsgi.py` (1–7)

ASGI ve WSGI server entrypoint’leridir. Mevcut Gunicorn WSGI kullanır; ileride SSE/WebSocket ASGI’ye geçirilebilir.

#### `config/__init__.py` (1–4)

Celery app’i package import’unda expose eder; Django boot sırasında task registry hazır olur.

### Core app

#### `apps/core/models.py` (1–16)

Tek `Workspace` modelini tanımlar. UUID external guess edilemez primary key, slug routing/API için stabil kimlik, environment ve timezone analiz boyutudur.

#### `apps/core/metrics.py` (1–71)

- 13–22: percentile sorting ve linear interpolation.
- 25–41: overview aggregate’leri; unknown cost coverage ayrımı.
- 44–46: günlük session timeseries.
- 49–65: ToolCall’ları tool adına göre memory’de gruplayıp exact p50/p95/error/byte/finding hesaplar.
- 68–71: provider/model cost aggregation.

MVP veri hacminde Python percentile yeterlidir. Büyük production hacminde PostgreSQL `percentile_cont` ve materialized rollup kullanılmalıdır.

#### `apps/core/views.py`

- 19–29: Overview context.
- Session list: URL-persisted filtreler, aktivite zorunluluğu, `NULLS LAST` sıralama, annotation/pagination ve HTMX partial response.
- 58–71: Finding feedback POST lifecycle.
- Session detail: waterfall offset/width, turn bazlı konuşma akışı, ilişkili model/tool/HTTP kartları, biçimlendirilmiş raw events ve finding context.
- 90–99: Tool Intelligence.
- 102–117: Finding filters.
- 120–130: Cost page.
- 133–144: Source/quarantine/correlation Data Health.

#### `apps/core/presentation.py`

Veritabanındaki yapılandırılmış telemetry'yi UI için güvenli ve okunabilir bir sunum modeline dönüştürür. JSON string'lerini kontrollü biçimde ayrıştırır, uzun payload'ları sınırlar, primitive/list/object değerlerini insan okunur hale getirir ve her span için model, MCP tool veya HTTP işlem kartı üretir. Bu katman yalnızca sunumla ilgilenir; ham veriyi ya da domain modellerini değiştirmez.

#### `apps/core/templatetags/core_extras.py`

Asistan yanıtlarını önce HTML-escape edip sonra sınırlı Markdown özellikleriyle render eden `conversation_markdown` filtresidir. Böylece başlıklar, listeler ve kod blokları okunurken kayıt içindeki HTML/script içeriği çalıştırılamaz.

#### `apps/core/api_views.py` (1–33)

Overview ve tool metrics’i JSON’a açar; trace endpoint session ve ordered span shape’ini serialize eder.

#### `apps/core/api_urls.py` (1–10)

Üç endpoint’in path/name eşlemesidir.

#### `apps/core/admin.py`, `apps/core/apps.py`

Workspace admin registration ve Django app metadata.

### Trace app

#### `apps/traces/models.py` (1–128)

- 8–18: Task hiyerarşisi, status ve tags.
- 21–42: Session identity, provider/model, correlation, timing; workspace+external unique.
- 45–62: Turn conversation, tokens, cost ve sequence unique.
- 65–85: Generic Span, parent tree, trace fields, kind/timing/status ve kritik indeksler.
- 88–95: ModelStep token breakdown.
- 98–106: ToolCall JSON/hash/error/size.
- 109–116: ExternalCall service/HTTP/bytes.
- 119–128: Usage ledger ve pricing version.

#### `apps/traces/admin.py`, `apps/traces/apps.py`

Bütün trace modellerini Django Admin’e açar ve app config sağlar.

### Ingestion app

#### `apps/ingestion/models.py` (1–46)

- 4–23: Source checkpoint state ve `(path,inode)` uniqueness.
- 29–41: RawEvent immutable payload ve `(source,offset)` idempotency.
- 44–50 civarı: Quarantine error envelope.

#### `apps/ingestion/services.py` (1–yaklaşık 460)

- 1–23: imports/parser version.
- 26–40: same-file concurrency lock.
- 43–66: canonical JSON, SHA-256, datetime ve result parsing helpers.
- 69–122: Workspace/Task/Session upsert ve enrichment.
- 125–139: Session bounds/status rollup.
- 142–193: agent turn normalization; legacy embedded tool fallback.
- 196–205: HTTP URL templating ve legacy parent heuristic.
- 208–yaklaşık 315: MCP tool, HTTP ve model event state machine.
- Sonraki canonical block: explicit span/parent normalization.
- `detect_kind`/`normalize`: adapter router.
- `ingest_file`/`_ingest_locked_file`: lock, checkpoint, transaction, quarantine core.
- Son blok: known path discovery ve tree ingestion.

Bu dosya sistemin en kritik domain service’idir. Değişiklik yapılırken idempotency, checkpoint ve raw+normalized aynı transaction invariant’ı korunmalıdır.

#### `apps/ingestion/tasks.py` (1–14)

Celery task configured root’u ingest eder, ardından rules çalıştırır ve özet sayaç döner.

#### `apps/ingestion/management/commands/ingest_logs.py` (1–24)

CLI path listesi veya default root kabul eder; her source sonucunu yazdırır ve opsiyonel finding üretir.

#### `apps/ingestion/admin.py`, `apps/ingestion/apps.py`

Source/raw/quarantine admin registration ve app metadata.

### Findings app

#### `apps/findings/models.py` (1–42)

- 7–27: Finding scope, classification, lifecycle, waste, evidence ve fingerprint.
- 30–35: Human Annotation.
- 38–45 civarı: RuleSetting enable/severity/parameters.

#### `apps/findings/engine.py` (1–118)

- 13–20: read tool allowlist ve rule catalog.
- 23–31: setting bootstrap ve deterministic SHA fingerprint.
- 34–52: bütün kuralların ortak create/idempotency kapısı.
- 55–82: duplicate ve redundant read grouping.
- 85–106: retry storm zaman penceresi.
- 109–yaklaşık 130: incomplete span recovery finding’i.
- Son fonksiyon: bütün kuralları sıralı çalıştırır.

#### `apps/findings/admin.py`, `apps/findings/apps.py`

Finding/annotation/settings admin registration ve app metadata.

### Templates

#### `templates/layouts/base.html`

HTML head, Inter/IBM Plex Mono fontları, ECharts/HTMX asset’leri, fixed sidebar, workspace topbar, mobil menü, tema anahtarı, mesaj alanı ve content/script block’larını tanımlar. Açık/koyu tema tercihi `localStorage` üzerinde `pulse-theme` anahtarıyla korunur; ilk paint öncesi `html[data-theme]` değerine uygulanarak tema parlaması önlenir. Mobil sidebar aç/kapat davranışı ve Escape ile kapanma da burada küçük, bağımlılıksız JavaScript ile yönetilir. Bütün sayfalar buradan extend eder; navigation aktif durumu `active_nav` context’iyle belirlenir.

#### `templates/dashboard/overview.html`

KPI kartları, ECharts activity line, bulgular ve yavaş session tablosu. Son script block server’dan gelen güvenli JSON array’i chart option’a map eder. Grafik renkleri CSS design token’larından okunur ve `pulse-theme-change` event’inde yeniden çizilir; bu nedenle açık/koyu temada chart ile sayfa aynı renk sistemini kullanır.

#### `templates/sessions/list.html`

GET filter formu ve `sessions/_results.html` canlı sonuç partial’ını içerir. Partial tabloyu, session türünü, reusable pagination bileşenini ve beş saniyelik HTMX polling davranışını taşır. Hücrelerdeki `data-label` alanları 620 px altında tabloyu yatay kaydırma gerektirmeyen bilgi kartlarına dönüştürmek için CSS tarafından kullanılır.

#### `templates/sessions/detail.html`

Ana deneyim konuşma geçmişidir: kullanıcı/asistan balonları ve her turn altında açılır model, MCP tool ve Jotform HTTP işlem kartları bulunur. İşlem kartı açıldığında önce karşılıklı iletişim blokları görünür; tool için client/server isteği ve yanıtı, HTTP için MCP server/Jotform API isteği ve yanıtı aynı kartta eşleşir. Toolbar üzerindeki tür ve hata filtreleri `data-operation-kind/status` alanlarıyla tamamen istemci tarafında çalışır; ağ isteği üretmez. `Tümünü aç/kapat` kontrolleri yalnızca görünür `<details>` elemanlarını yönetir. İstemci konuşma metnini MCP protokolüne vermiyorsa boş sohbet yerine son 50 bağımsız MCP operasyonu gösterilir. Waterfall ikinci analiz katmanıdır. Ham telemetry JSON'u geliştirici panelinde varsayılan kapalıdır. Django `csrf_token` bütün mutation formlarında bulunur.

#### `templates/tools/overview.html`

Dual-axis ECharts bar+line ve exact metrics table. Chart CSS tema token’larını kullanır ve tema değişiminde yeniden çizilir.

#### `templates/findings/list.html`

Finding filtreleri, severity rail, recommendation ve estimated waste kartları.

#### `templates/costs/index.html`

Known cost/coverage cards, tema-duyarlı donut chart ve model tablosu.

#### `templates/data_health/index.html`

Correlation oranı, checkpoint table ve quarantine details.

#### `templates/components/status_badge.html`

Status değerini CSS class’a map eden tek reusable badge.

#### `templates/components/pagination.html`

Previous/page/next navigation. Query param koruması ileride urlencode helper ile genişletilebilir.

### Static

#### `static/css/app.css`

Tek kaynak stylesheet’tir. Mantıksal selector sırası:

1. Açık tema ve `html[data-theme="dark"]` design token’ları.
2. Erişilebilir focus/skip-link kuralları ve reset.
3. Jotform turuncusu + lacivert omurgalı sidebar, CSS brand mark ve navigation.
4. Topbar, canlı veri durumu, tema ve mobil menü kontrolleri.
5. Metrics, panels, grids, chart kapsayıcıları, tables, filters, badges ve pagination.
6. Konuşma balonları, operasyon toolbar’ı, request-response kartları ve ham payload alanları.
7. Waterfall, finding kartları, developer inspector ve data-health bileşenleri.
8. 1160/820/620 px responsive kırılımları; 620 px altında session tablosunun kart görünümüne dönüşümü.

Renk yaklaşımı Jotform ürün ailesinden iz taşır fakat dashboard fonksiyonlarına göre genişletilmiştir: `--orange` ana aksiyon ve aktif vurgu, `--navy` navigasyon/başlık, mavi MCP tool, turkuaz Jotform HTTP, mor model operasyonu, yeşil başarı ve kırmızı hata semantiğidir. Boyutlar, gölgeler ve radius değerleri tokenlaştırıldığı için ekranlar arasında rastgele görsel karar oluşmaz. Bütün etkileşimli elemanlarda `:focus-visible` tanımı, mobil sidebar için scrim ve Escape desteği, grafiklerde tema güncellemesi vardır.

Renk semantiği: green success, red error/high severity, amber running/medium, cyan external call, blue tool, purple model/brand.

### Contracts

#### `contracts/canonical-event-v1.schema.json`

JSON Schema 2020-12 kullanır, required alanları ve status enum’unu sabitler, unknown top-level alanları `additionalProperties=false` ile reddeder.

#### `contracts/canonical-event-v1.example.jsonl`

Aynı tool span için started ve completed golden sample’dır; parser/manual integration testlerinde kullanılabilir.

### Tests

#### `tests/test_ingestion.py`

Dört temel ingestion senaryosu, HTTP response attribute saklama, rule idempotency ve full correlation parent chain testini içerir. Temporary directory içindeki gerçek JSONL dosyalarını okuyarak sadece unit helper değil byte reader/checkpoint katmanını da test eder.

#### `tests/test_views.py`

Ana HTML routes, DRF response’ları, session ordering/polling, detail ekranındaki tool/HTTP request-response eşleşmesi, operasyon filtreleri ve base layout tema/mobil navigasyon kontrollerini Django test client ile smoke test eder.

### Migrations

- `core/0001_initial.py`: Workspace.
- `traces/0001_initial.py`: trace hierarchy ve indeksler.
- `traces/0002_*.py`: Session uniqueness’i provider’dan bağımsız hale getirir.
- `ingestion/0001_initial.py`: source/raw/quarantine.
- `findings/0001_initial.py`: rule/finding/annotation.

Migration dosyaları Django tarafından üretilmiştir; model source of truth ile beraber commit edilmelidir. Elle düzenleme yerine yeni migration oluşturulmalıdır.

### Package marker’ları

`apps/__init__.py`, her app’in `__init__.py`, migration ve management package `__init__.py` dosyaları Python import package sınırlarını tanımlar. İş mantığı içermezler.

## 18. MCP producer’da yapılan instrumentation değişiklikleri

Dashboard repo bağımsızdır; fakat tam correlation için sibling producer’da aşağıdaki hedefli değişiklikler yapılmıştır.

### `mcp_server/telemetry_context.py` (1–43)

- 9–11: yedi correlation ContextVar ve sequence counter.
- 14–15: UUID üretimi.
- 18–19: dolu context alanlarını event için toplama.
- 22–25: monoton sequence.
- 28–39: nested, exception-safe context binding/reset.
- 42–43: session başında sequence reset.

### `mcp_server/audit_log.py`

- `write_event`: schema/event/sequence ve aktif context’i her event’e ekler.
- `call_tool`: yeni tool span açar; model span’i parent yapar; success/failure’da aynı context ile terminal event yazar.
- `log_jotform_request`: yeni HTTP span açar; aktif tool span’i parent yapar.
- Mevcut secret redaction ve truncation aynen korunur.

### `agent/qa_terminal.py`

Session ve Task UUID’lerini konuşma başında; Turn ve Trace UUID’lerini her soruda üretir. Provider loop’un tamamını bu context altında çalıştırır ve turn JSONL satırına aynı kimlikleri yazar.

### `agent/run.py`

Her Anthropic model çağrısı için `model.step.started/completed/failed` yazar. Tool çağrısı sırasında model span context’ini parent olarak taşır. Usage input/output token’ları event’e ekler.

### `agent/gemini_run.py`

Anthropic ile aynı model-step lifecycle’ını Gemini retry loop’unun çevresinde uygular. Usage extraction best-effort kalır; bulunmayan token `null` olur.

### `agent/logging_.py`

Turn satırına schema version, task/turn/trace, pricing version ve `cost_estimated` ekler. Eski caller’lar optional default’lar sayesinde bozulmaz.

### `tests/test_audit_log.py`

Nested context’in event JSON’una eksiksiz yansımasını doğrulayan regresyon testi eklenmiştir.

## 19. Operasyon rehberi

### Dashboard boş

1. `MCP_LOG_ROOT` doğru mu?
2. `manage.py ingest_logs` çıktısında dosyalar listeleniyor mu?
3. Data Health’te source offset ilerliyor mu?
4. Docker mount gerçekten `:ro` ile `/data/mcp` altında mı?

### Source `truncated`

Aynı inode’lu dosya küçülmüştür. Dosyanın nasıl truncate edildiğini inceleyin; güvenilir bir yeni source identity/rotation sonrası yeniden ingest edin. Checkpoint’i körlemesine sıfırlamayın.

### Quarantine artıyor

Data Health’te error type/message ve raw preview’yu okuyun. Schema değiştiyse yeni adapter/parser version yazın; eski adapter’ı sessizce değiştirmek yerine versionlayın.

### Session `running` kalıyor

Started span terminal event almamıştır. Beş dakika sonra `INCOMPLETE_SPAN` finding’i oluşur. Producer crash ve log flush davranışını kontrol edin.

### Cost coverage düşük

Legacy loglarda token veya pricing bilinmiyordur. Unknown değerleri zero’ya çevirmeyin. Model fiyat tablosunu versionlayıp yalnız bilinen usage’a uygulayın.

### Beat duplicate çalışıyor

Tek hostta file lock ikinci okuyucuyu `locked` döndürür. Çok hostta Redis/PostgreSQL distributed lock ekleyin.

## 20. Bilinen sınırlar ve sonraki üretim adımları

Bu teslim çalışan ve testli bir tam MVP’dir; aşağıdakiler production hardening/gelişmiş analytics kapsamıdır:

1. PostgreSQL-native percentile/materialized rollup.
2. Çok hostlu distributed ingestion lock.
3. SSE ile live trace push; mevcut UI refresh/poll modelindedir.
4. Authentication/RBAC/SSO.
5. Field-level PII detection ve masking.
6. Retention, purge, backup ve restore automation.
7. Rule thresholds için admin UI ve historical re-evaluation.
8. Read-after-write, overfetch, result-ignored ve serializable-calls kuralları.
9. Benchmark trace comparison ve regression reports.
10. OTLP ingestion bridge.
11. PostgreSQL partitioning yalnız veri hacmi yüz milyonlar düzeyine gelirse.
12. CDN asset’lerini self-host etme ve CSP.

## 21. Önerilen okuma sırası

Yeni geliştirici için en hızlı hakimiyet sırası:

1. Bu raporun 1–8. bölümleri.
2. `contracts/canonical-event-v1.schema.json`.
3. `apps/traces/models.py`.
4. `apps/ingestion/models.py`.
5. `apps/ingestion/services.py` — önce `ingest_file`, sonra adapter’lar.
6. `apps/findings/engine.py`.
7. `apps/core/views.py` ve `metrics.py`.
8. `templates/sessions/detail.html`.
9. `docker-compose.yml` ve `config/settings.py`.
10. `tests/test_ingestion.py` — invariant’ların executable özeti.
11. Producer tarafında `telemetry_context.py`, `qa_terminal.py`, `audit_log.py`.

Bu sırayla okunduğunda önce sözleşme ve domain, sonra data pipeline, ardından analiz/UI ve en son runtime ayrıntıları oturur.

## 22. Tasarım invariant’ları

Gelecekte kod değişirken şu kurallar kırılmamalıdır:

1. Raw event kaydı ile normalize kayıt aynı transaction içinde oluşur.
2. Checkpoint yalnız tam newline ve başarılı/karantinaya alınmış satır sonrasında ilerler.
3. Transient DB hatası input quarantine sayılmaz.
4. Unknown cost `0` değildir.
5. Legacy correlation yüksek güven diye sunulmaz.
6. Dashboard MCP kodunu import etmez.
7. MCP log mount read-only kalır.
8. Finding deterministic fingerprint ile idempotent kalır.
9. Started/completed aynı span/request identity ile eşleşir.
10. Parent span explicit ise heuristic onun üzerine yazamaz.
11. Secret redaction raw log yazılmadan önce yapılır.
12. Yeni schema değişikliği `schema_version`/`parser_version` ile görünür olmalıdır.

Bu invariant’lar projenin mimari omurgasıdır; framework veya UI değişse bile korunmalıdır.
