import asyncio
import os

from http.server import BaseHTTPRequestHandler

from scheduler import process_tasks


CRON_SECRET = os.getenv("CRON_SECRET")


class handler(BaseHTTPRequestHandler):

    def do_GET(self):

        # Защита endpoint
        auth = self.headers.get(
            "Authorization"
        )

        if CRON_SECRET:

            expected = (
                f"Bearer {CRON_SECRET}"
            )

            if auth != expected:

                self.send_response(401)

                self.end_headers()

                self.wfile.write(
                    b"Unauthorized"
                )

                return

        try:

            asyncio.run(
                process_tasks()
            )

            self.send_response(200)

            self.end_headers()

            self.wfile.write(
                b"Scheduler executed"
            )

        except Exception as e:

            print(
                f"Scheduler error: {e}"
            )

            self.send_response(500)

            self.end_headers()

            self.wfile.write(
                str(e).encode()
            )