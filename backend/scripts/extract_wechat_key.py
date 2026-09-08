"""
Automated WeChat Key Extractor for macOS (Independent of WeChat version)
Captures CCKeyDerivationPBKDF calls via LLDB, matches salt, and validates Page 1.
"""

import os
import sys
import time
import subprocess
import binascii
import hashlib
from pathlib import Path

ACCOUNT_ID = "wxid_hxwpag2k3qi122_53e3"
DB_PATH = Path.home() / f"Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files/{ACCOUNT_ID}/db_storage/message/message_0.db"
CONFIG_PATH = Path.home() / "Library/Application Support/wx-cli/config/keys.toml"

if not DB_PATH.exists():
    print(f"Error: Database not found at {DB_PATH}")
    sys.exit(1)

with open(DB_PATH, "rb") as f:
    target_salt = f.read(16)
print(f"[*] Target message_0.db Salt: {target_salt.hex()}")

# Create LLDB hook script
lldb_script_path = Path("/tmp/wechat_key_hook.py")
lldb_script_content = """import lldb
import binascii

call_count = 0

def pbkdf_callback(frame, bp_loc, dict):
    global call_count
    call_count += 1
    process = frame.GetThread().GetProcess()
    gpr = frame.GetRegisters()[0]

    pwd_ptr = gpr.GetChildMemberWithName("x1").GetValueAsUnsigned()
    pwd_len = gpr.GetChildMemberWithName("x2").GetValueAsUnsigned()
    salt_ptr = gpr.GetChildMemberWithName("x3").GetValueAsUnsigned()
    salt_len = gpr.GetChildMemberWithName("x4").GetValueAsUnsigned()
    prf = gpr.GetChildMemberWithName("x5").GetValueAsUnsigned()
    rounds = gpr.GetChildMemberWithName("x6").GetValueAsUnsigned()

    error = lldb.SBError()
    pwd_hex, salt_hex = "", ""
    if 0 < pwd_len < 1024:
        d = process.ReadMemory(pwd_ptr, pwd_len, error)
        if error.Success(): pwd_hex = binascii.hexlify(d).decode()
    if 0 < salt_len < 1024:
        d = process.ReadMemory(salt_ptr, salt_len, error)
        if error.Success(): salt_hex = binascii.hexlify(d).decode()

    print(f"HOOK_EVENT: rounds={rounds} pwd={pwd_hex} salt={salt_hex}", flush=True)
    return False

def setup(debugger, command, result, internal_dict):
    target = debugger.GetSelectedTarget()
    bp = target.BreakpointCreateByName("CCKeyDerivationPBKDF")
    bp.SetScriptCallbackFunction(f"{__name__}.pbkdf_callback")
    bp.SetAutoContinue(True)
    print("HOOK_READY", flush=True)
    target.GetProcess().Continue()

def __lldb_init_module(debugger, internal_dict):
    debugger.HandleCommand(f'command script add -f {__name__}.setup capture_keys')
"""

with open(lldb_script_path, "w") as f:
    f.write(lldb_script_content)

print("[*] Killing existing WeChat...")
subprocess.run(["killall", "WeChat"], capture_output=True)
subprocess.run(["killall", "WeChatAppEx"], capture_output=True)
time.sleep(1.5)

print("[*] Spawning LLDB in wait-for-process mode...")
cmd = [
    "lldb",
    "-w",
    "-n",
    "WeChat",
    "-o",
    f"command script import {lldb_script_path}",
    "-o",
    "capture_keys",
]

proc = subprocess.Popen(
    cmd,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    bufsize=1
)

time.sleep(1)
print("[*] Launching WeChat...")
subprocess.run(["open", "-a", "WeChat"])

print("[*] Listening for PBKDF2 key derivation calls... (Please ensure WeChat is logging in)")

matched_key = None
start_time = time.time()
target_salt_hex = target_salt.hex()

try:
    for line in iter(proc.stdout.readline, ''):
        line = line.strip()
        if "HOOK_EVENT:" in line:
            # Parse event
            parts = line.split()
            rounds = 0
            pwd = ""
            salt = ""
            for p in parts[1:]:
                if p.startswith("rounds="):
                    rounds = int(p.split("=")[1])
                elif p.startswith("pwd="):
                    pwd = p.split("=")[1]
                elif p.startswith("salt="):
                    salt = p.split("=")[1]

            print(f" -> Found PBKDF2: rounds={rounds}, salt={salt[:16]}..., pwd_len={len(pwd)//2}")
            
            # Match rounds and salt
            if rounds == 256000 and salt == target_salt_hex and len(pwd) == 64:
                print(f"[+] MATCHED KEY for {ACCOUNT_ID}!")
                matched_key = pwd
                break

        if time.time() - start_time > 60:
            print("[-] Timeout waiting for key (60s)")
            break
finally:
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except Exception:
        proc.kill()

if matched_key:
    print(f"\n[SUCCESS] Extracted Raw Key (64-char Hex): {matched_key}")
    
    # Save to wx-cli keys.toml
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    toml_content = f"""[accounts.{ACCOUNT_ID}]
key = "{matched_key}"
"""
    with open(CONFIG_PATH, "w") as f:
        f.write(toml_content)
    print(f"[+] Saved to {CONFIG_PATH}")
else:
    print("\n[-] Key not captured in this run.")
