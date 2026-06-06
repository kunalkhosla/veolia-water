# Debian/glibc base (NOT the HA Alpine base): Alpine/musl can't run Chromium and
# has no pip here. patchright drives a stealth-patched Chromium that clears the
# portal's Cloudflare Turnstile — but only headed, so we run it under Xvfb.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends xvfb xauth \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir -r requirements.txt \
    && patchright install --with-deps chromium

COPY veolia_water.py ./

# Headed Chromium needs a display; Xvfb provides a virtual one.
CMD ["xvfb-run", "-a", "--server-args=-screen 0 1280x1024x24", \
     "python3", "veolia_water.py"]
