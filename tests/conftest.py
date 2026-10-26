import os

os.environ.setdefault("LOCALAGENT_NO_KEYCHAIN", "1")  # tests must never write to the real OS keychain
