# marp_slides ExApp image: pinned marpteam/marp-cli base (kept per contract;
# its bundled Chromium renders the pdf output format, the one output path
# that uses the browser, while the PPTX is built natively with python-pptx
# and marp-cli's HTML gate/render are Node-only) plus a Python FastAPI
# worker. Layer order rationale: the heavy, rarely changing base first; then
# OS packages and the hashed lock install (change with requirements.lock,
# regenerated from requirements.txt — see README "Releases"); the application
# code last, so day-to-day edits rebuild only the cheap COPY layers.
FROM marpteam/marp-cli:v4.5.1

# The base image ships neither python3 nor a `marp` command on PATH (it only
# has the gosu entrypoint), and Debian bookworm is PEP 668 managed, so deps
# go into a venv instead of system site-packages.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

# requirements.lock is the full transitive closure of requirements.txt with
# per-artifact sha256 hashes (CRA: reproducible, auditable installs).
# --require-hashes makes pip refuse any artifact whose hash is absent from
# the lock, so the image cannot pick up an unresolved or tampered wheel.
COPY requirements.lock /tmp/requirements.lock
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock \
    && rm /tmp/requirements.lock
ENV PATH=/opt/venv/bin:$PATH

# Expose marp-cli as a plain `marp` command for the worker and for ad-hoc use
# (`docker run --rm marp-slides:dev marp --version`).
RUN printf '#!/bin/sh\nexec node /home/marp/.cli/marp-cli.js "$@"\n' > /usr/local/bin/marp \
    && chmod +x /usr/local/bin/marp

WORKDIR /app
COPY marp_slides marp_slides
COPY main.py .

# AppAPI persistent-storage convention path: pre-created with marp
# ownership so a first-use named volume inherits it (docker copies the
# image dir's ownership into a fresh volume mounted here). The draft
# checkpoint store writes under this directory.
RUN mkdir -p /nc_app_marp_slides_data && chown marp:marp /nc_app_marp_slides_data

# The stock entrypoint gosu-drops to the `marp` user and execs marp-cli.js;
# we cleanly override it: switch to the marp user directly and run uvicorn.
# Chromium renders fine as this non-root user (verified against the base image).
USER marp
ENTRYPOINT []
CMD ["python3", "main.py"]
EXPOSE 9090
