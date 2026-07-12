# Voice-transcription worker — faster-whisper (no PyTorch) with the `small` model baked in, so there is
# no per-invocation download and the runtime/CI stay lean. Build context is the cleavis.eu/ dir.
#   docker build --provenance=false --sbom=false --platform linux/arm64 \
#     -f transcribe_worker/Dockerfile -t <ecr>:<tag> .
FROM public.ecr.aws/lambda/python:3.12

COPY transcribe_worker/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Bake the whisper `small` weights into the image (no runtime download).
RUN python -c "from faster_whisper.utils import download_model; download_model('small', output_dir='/opt/models/small')"

COPY transcribe_worker/handler.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.handler"]
