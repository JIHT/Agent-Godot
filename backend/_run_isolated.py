"""临时：逐个运行测试并带超时，定位卡死的用例。"""
import subprocess
import sys

TESTS = [
    "test_voice_transcribe_text",
    "test_voice_transcribe_srt",
    "test_voice_transcribe_json_carries_features",
    "test_voice_analyze_features_only",
    "test_voice_analyze_with_report",
    "test_voice_analyze_missing_file",
    "test_voice_chat_from_wav",
    "test_voice_chat_respects_privacy_no_audio_kept",
]

for t in TESTS:
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", f"tests/test_voice/test_cli.py::{t}",
             "-q", "--no-header"],
            capture_output=True, text=True, timeout=40,
            encoding="utf-8", errors="replace")
        tail = [l for l in r.stdout.strip().splitlines() if l.strip()]
        print(f"{t:52s} -> {tail[-1][:120] if tail else r.stderr[-200:]}")
    except subprocess.TimeoutExpired:
        print(f"{t:52s} -> TIMEOUT (40s)")
