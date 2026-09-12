"""Local launcher note.

Production architecture intentionally has no Telethon client here.
Run `python worker.py` on exactly ONE persistent worker host.
The Vercel bot is implemented in `api/index.py` and talks to that worker over HTTP.
"""
print("Production bot: deploy api/index.py to Vercel and worker.py to a persistent host.")
