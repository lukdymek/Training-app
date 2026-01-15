from django.core.management.base import BaseCommand
from django.db import connection

class Command(BaseCommand):
    help = "Print DB fingerprint"

    def handle(self, *args, **options):
        with connection.cursor() as cur:
            cur.execute("SELECT inet_server_addr(), inet_server_port(), current_database(), current_user, version()")
            row = cur.fetchone()
        self.stdout.write(f"DB_FINGERPRINT: ip={row[0]} port={row[1]} db={row[2]} user={row[3]}")
        self.stdout.write(f"DB_VERSION: {row[4]}")
