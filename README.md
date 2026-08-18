# Jotform MCP Pulseboard

Jotform Workflow MCP agent çalıştırmalarını session → turn → model step → tool call → Jotform HTTP call düzeyinde izleyen bağımsız observability dashboard’ı.

## Neler hazır?

- Legacy agent, MCP audit ve revision JSONL backfill
- Canonical event v1 sözleşmesi ve yüksek güvenli trace correlation
- Offset checkpoint, idempotency, process lock ve quarantine
- Jotform turuncusu ve lacivertinden türetilmiş açık/koyu tema, mobil navigasyon ve mobil session kartları
- Overview, Session Explorer, konuşma balonları, request-response eşleşmeli işlem kartları, tür/hata filtreleri, trace waterfall, Tool Intelligence, Findings, Cost ve Data Health ekranları
- Duplicate/redundant read, retry storm ve incomplete span kuralları
- Django Admin ve DRF read API’leri
- PostgreSQL + Redis + Celery Worker + Celery Beat + Gunicorn Docker ortamı
- SQLite ile bağımlılıksız lokal geliştirme

## En hızlı başlangıç — lokal

```bash
cd /home/avci/Desktop/jotform-mcp-phase1/jotform-observability-dashboard
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python manage.py migrate
.venv/bin/python manage.py ingest_logs
.venv/bin/python manage.py runserver
```

Ardından `http://127.0.0.1:8000` adresini açın. `DATABASE_URL` verilmezse SQLite, verilirse PostgreSQL kullanılır. Varsayılan `MCP_LOG_ROOT`, sibling `jotform-workflow-mcp` reposudur.

## Tek komutla Docker yönetimi

Kurulu `dashboard` komutuyla bütün Docker ortamını yönetebilirsiniz:

```bash
dashboard up       # build eder, servisleri başlatır ve healthy olmasını bekler
dashboard down     # durdurur; PostgreSQL/Redis verilerini silmez
dashboard status   # servis durumları
dashboard logs     # canlı web/worker/beat logları; Ctrl+C ile çıkılır
dashboard restart  # servisleri yeniden başlatır
dashboard open     # dashboard adresini yazdırır
```

Komut PATH’e kurulmamışsa repo içinden aynı script çalıştırılabilir:

```bash
./dashboard up
./dashboard down
```

## Docker ile tam ortam

```bash
cp .env.example .env
docker compose up --build
```

`web`, `worker` ve `beat` sibling MCP reposunu `/data/mcp:ro` olarak bağlar. Beat her 5 saniyede yeni byte aralığını ingest eder; PostgreSQL kalıcı sorgu katmanı, Redis ise Celery broker/result backend'i olur. Sessions tablosu da HTMX ile 5 saniyede bir yenilenir. İstemci sohbet metnini paylaşmıyorsa kayıt "MCP aktivitesi" olarak görünür; detayda tool ve HTTP işlemleri `MCP client -> MCP server`, `MCP server -> Jotform API` ve dönüş yanıtlarıyla çift taraflı listelenir.

## Önemli komutlar

```bash
make test                       # dashboard testleri
make check                      # Django check + migration drift
make ingest                     # bütün bilinen JSONL source’ları
.venv/bin/python manage.py ingest_logs /absolute/file.jsonl
.venv/bin/python manage.py createsuperuser
```

## API

- `GET /api/v1/overview/`
- `GET /api/v1/tools/`
- `GET /api/v1/sessions/<uuid>/trace/`

## Dokümantasyon

- Ekran → view → ORM query → metrik → model zinciri, Mermaid akışları, runtime topolojisi ve eksiksiz dosya rehberi: [docs/MIMARI_AKIS_VE_KOD_REHBERI_TR.md](docs/MIMARI_AKIS_VE_KOD_REHBERI_TR.md)
- İlk teknik teslim, producer instrumentation geçmişi ve operasyon notları: [docs/TEKNIK_RAPOR_TR.md](docs/TEKNIK_RAPOR_TR.md)
- Canonical event sözleşmesi: [contracts/canonical-event-v1.schema.json](contracts/canonical-event-v1.schema.json)
