ARG BUILD_FROM
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

COPY requirements.txt ./
# Playwright pulls in Chromium + its OS deps (glibc base, so Chromium runs fine).
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium

COPY veolia_water.py ./

CMD ["python3", "veolia_water.py"]
