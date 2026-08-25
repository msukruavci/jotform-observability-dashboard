from django.core.management.base import BaseCommand
from apps.ingestion.services import reclassify_sessions


class Command(BaseCommand):
    help = "Mevcut session kayıtlarını loglar, user-agent'lar ve metadata üzerinden Claude / Gemini / GPT / MCP olarak yeniden sınıflandırır."

    def handle(self, *args, **options):
        self.stdout.write("Geçmiş session kayıtları taranıyor ve sınıflandırılıyor...")
        summary = reclassify_sessions()
        self.stdout.write(self.style.SUCCESS(
            f"Tamamlandı! Güncellenen oturum sayısı: {summary['updated']}\n"
            f"🟣 Claude (Anthropic): {summary['claude']}\n"
            f"🔵 Gemini (Google): {summary['gemini']}\n"
            f"🟢 ChatGPT / GPT: {summary['gpt']}\n"
            f"⚙️ MCP Direct: {summary['mcp']}"
        ))
