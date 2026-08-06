FROM python:3.12-slim
RUN useradd -m -u 10001 sandbox \
 && pip install --no-cache-dir pytest pytest-json-report>=1.5.0
RUN mkdir -p /work && chown sandbox:sandbox /work
WORKDIR /work
USER sandbox
COPY run_tests.py /usr/local/bin/run_tests.py
ENTRYPOINT ["python", "/usr/local/bin/run_tests.py"]
