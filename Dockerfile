# marp_slides ExApp image: pinned marpteam/marp-cli base (kept per contract;
# its bundled Chromium renders the pdf output format, the one output path
# that uses the browser, while the PPTX is built natively with python-pptx
# and marp-cli's HTML gate/render are Node-only) plus a Python FastAPI
# worker. Layer order rationale: the heavy, rarely changing base first; then
# OS packages and pinned pip deps (change with requirements.txt only); the
# application code last, so day-to-day edits rebuild only the cheap COPY
# layers.
FROM marpteam/marp-cli:v4.5.1

# The base image ships neither python3 nor a `marp` command on PATH (it only
# has the gosu entrypoint), and Debian bookworm is PEP 668 managed, so deps
# go into a venv instead of system site-packages.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt
ENV PATH=/opt/venv/bin:$PATH

# Expose marp-cli as a plain `marp` command for the worker and for ad-hoc use
# (`docker run --rm marp-slides:dev marp --version`).
RUN printf '#!/bin/sh\nexec node /home/marp/.cli/marp-cli.js "$@"\n' > /usr/local/bin/marp \
    && chmod +x /usr/local/bin/marp

WORKDIR /app
COPY marp_slides marp_slides
COPY main.py .

# The stock entrypoint gosu-drops to the `marp` user and execs marp-cli.js;
# we cleanly override it: switch to the marp user directly and run uvicorn.
# Chromium renders fine as this non-root user (verified against the base image).
USER marp
ENTRYPOINT []
CMD ["python3", "main.py"]
EXPOSE 9090
