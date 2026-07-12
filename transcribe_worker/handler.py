"""Voice-transcription worker (container Lambda). Invoked SYNCHRONOUSLY by the Slack handler when a
user sends an audio clip — the transcript becomes the user's prompt, so this must return before the
runtime turn can start (RequestResponse, not fire-and-forget like the doc worker).

Uses faster-whisper (CTranslate2 — no PyTorch) with the `small` model BAKED INTO THE IMAGE, so there
is no per-invocation model download and the runtime/CI stay lean. PyAV (bundled) decodes m4a/aac/ogg
directly — no separate ffmpeg needed.

Event: {"bucket": <uploads bucket>, "key": <s3 key of the staged audio>}
Returns: {"text": <transcript>, "language": <detected>, "duration": <seconds>}
"""
import os
import boto3
from faster_whisper import WhisperModel

_s3 = boto3.client("s3", region_name=os.environ.get("AWS_REGION", "eu-central-1"))
_MODEL_DIR = os.environ.get("MODEL_DIR", "/opt/models/small")
_model = None


def _model_get() -> WhisperModel:
    global _model
    if _model is None:                       # load once per warm container; weights are baked in
        _model = WhisperModel(_MODEL_DIR, device="cpu", compute_type="int8")
    return _model


def handler(event, context):
    bucket = event["bucket"]
    key = event["key"]
    dst = "/tmp/" + os.path.basename(key)
    _s3.download_file(bucket, key, dst)
    segments, info = _model_get().transcribe(dst, beam_size=1)
    text = " ".join(s.text for s in segments).strip()
    print(f"[transcribe] {key} lang={info.language} dur={info.duration:.1f}s -> {text[:160]!r}", flush=True)
    return {"text": text, "language": info.language, "duration": info.duration}
