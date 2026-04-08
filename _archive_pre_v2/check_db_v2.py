import sqlite3

conn = sqlite3.connect('data_mine.db')
cur = conn.cursor()

print("--- DATABASE TRUTH DIAGNOSTIC ---")

# 1. Are hashes actually saving?
cur.execute("SELECT COUNT(*) FROM file_index WHERE full_path LIKE '%TEST Data V2%'")
total_test = cur.fetchone()[0]

cur.execute("SELECT COUNT(*) FROM file_index WHERE full_path LIKE '%TEST Data V2%' AND sha256_hash IS NOT NULL")
hashed_test = cur.fetchone()[0]
print(f"Test Files Found: {total_test} | Hashes Saved: {hashed_test}")

# 2. Are there ANY duplicate hashes?
cur.execute("SELECT sha256_hash, COUNT(*) FROM file_index WHERE full_path LIKE '%TEST Data V2%' AND sha256_hash IS NOT NULL GROUP BY sha256_hash HAVING COUNT(*) > 1")
dups = cur.fetchall()
print(f"\nUnique Hashes with Copies: {len(dups)}")
for hash_val, count in dups:
    print(f" - Hash {hash_val[:8]}... has {count} identical files")

# 3. Did the SQL flag them?
cur.execute("SELECT is_golden, COUNT(*) FROM file_index WHERE full_path LIKE '%TEST Data V2%' GROUP BY is_golden")
flags = cur.fetchall()
print(f"\nGolden Flags (0 = Dup, 1 = Unique): {flags}")

conn.close()