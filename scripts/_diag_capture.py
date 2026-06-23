import os, subprocess, asyncio, asyncpg
r = subprocess.run(["python", "scripts/hebcal_block.py"], capture_output=True, text=True)
out = (r.stdout or "") + "\n--- STDERR ---\n" + (r.stderr or "") + f"\n--- EXIT {r.returncode} ---"
print(out)
async def w():
    c = await asyncpg.connect(os.environ["DATABASE_URL"], statement_cache_size=0)
    try:
        await c.execute(
            "insert into sched.hebcal_diag (spans,created,skipped,failed,note) values (0,0,0,0,$1)",
            "DIAG-CAPTURE\n" + out[-6000:])
    finally:
        await c.close()
asyncio.run(w())
print("diag-capture row written")
