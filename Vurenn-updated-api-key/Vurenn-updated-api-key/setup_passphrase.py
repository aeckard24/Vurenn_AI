import hashlib

print("Starting passphrase setup...")

PASSPHRASE_HASH_PATH = "delta_passphrase.hash"

phrase = input("Set Delta's unlock passphrase: ").strip().lower()
confirm = input("Confirm passphrase: ").strip().lower()

if phrase != confirm:
    print("Passphrases didn't match. Try again.")
else:
    digest = hashlib.sha256(phrase.encode()).hexdigest()
    with open(PASSPHRASE_HASH_PATH, "w") as f:
        f.write(digest)
    print("Passphrase saved.")

print("Done.")
