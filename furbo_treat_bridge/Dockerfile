# Furbo Treat Bridge add-on image.
#
# Debian-based rather than Home Assistant's Alpine base: the TUTK P2P library is
# built against glibc and will not load on musl.
#
# The TUTK library is closed source and is NOT stored in this repository. It is
# downloaded when Home Assistant builds this image on your own machine, from the
# docker-wyze-bridge project (the same file the ha-furbo project uses), pinned to
# an exact commit and checked against a SHA-256 per architecture. If the bytes
# ever differ, the build stops.
FROM python:3.12-slim-bookworm

ARG TUTK_LIB_COMMIT="bf893749b748f142199c9bce14fac44f8a661d6e"
ARG TUTK_SHA256_AMD64="f7ec63dcb589e1b6bf0b8921af80d9d29ea0ddc4b2437170cbec28b08faa516e"
ARG TUTK_SHA256_ARM64="cc3542f50c0c3f433f7083de4666ed126bbe64e02a91753462c25af20e713737"

# tzdata: Home Assistant passes its time zone as TZ, and without zone files the
# daily treat count would roll over at UTC midnight instead of local midnight.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl ca-certificates tzdata; \
    case "$(uname -m)" in \
      x86_64) arch=amd64; sha="$TUTK_SHA256_AMD64" ;; \
      aarch64) arch=arm64; sha="$TUTK_SHA256_ARM64" ;; \
      *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 5 --retry-delay 2 \
      -o /usr/local/lib/libIOTCAPIs_ALL.so \
      "https://raw.githubusercontent.com/mrlt8/docker-wyze-bridge/${TUTK_LIB_COMMIT}/app/lib/lib.${arch}"; \
    echo "${sha}  /usr/local/lib/libIOTCAPIs_ALL.so" | sha256sum -c -; \
    ldconfig; \
    apt-get purge -y curl; \
    apt-get autoremove -y; \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/
WORKDIR /app
ENV PYTHONUNBUFFERED=1
CMD ["python3", "/app/main.py"]
