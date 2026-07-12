# Heavy upload worker — markitdown conversion + KB indexing, offloaded from the responder.
# Build context is the cleavis.eu/ dir so we can bundle the shared config.py + user_files.py.
#   docker build -f user_files_worker/Dockerfile -t <ecr>:<tag> .
FROM public.ecr.aws/lambda/python:3.12

COPY user_files_worker/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Shared runtime modules (single source of truth for conversion + indexing) + the worker entrypoint.
COPY config.py user_files.py ${LAMBDA_TASK_ROOT}/
COPY user_files_worker/handler.py ${LAMBDA_TASK_ROOT}/

CMD ["handler.handler"]
