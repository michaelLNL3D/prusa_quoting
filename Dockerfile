# ── Stage 1: extract AppImages (runs on native builder arch) ─────────────────
# We use unsquashfs to extract without executing the x86_64 AppImage binary.
# This avoids Exec format errors on Apple Silicon / ARM builders.
FROM ubuntu:22.04 AS slicer-extract

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y wget squashfs-tools python3 \
  && rm -rf /var/lib/apt/lists/*

# Helper: find squashfs offset inside an AppImage and extract it
# AppImages embed a squashfs filesystem after the ELF runtime.
# squashfs magic: 'hsqs' (little-endian) or 'sqsh' (big-endian)
COPY find_squashfs_offset.py /tmp/find_squashfs_offset.py

# PrusaSlicer 2.8.1 (last release with Linux AppImage)
RUN wget -q \
    "https://github.com/prusa3d/PrusaSlicer/releases/download/version_2.8.1/PrusaSlicer-2.8.1%2Blinux-x64-newer-distros-GTK3-202409181416.AppImage" \
    -O /tmp/prusaslicer.AppImage \
  && OFFSET=$(python3 /tmp/find_squashfs_offset.py /tmp/prusaslicer.AppImage) \
  && echo "PrusaSlicer squashfs offset: $OFFSET" \
  && unsquashfs -dest /opt/prusaslicer -offset $OFFSET /tmp/prusaslicer.AppImage \
  && rm /tmp/prusaslicer.AppImage

# OrcaSlicer 2.3.1
RUN wget -q \
    "https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v2.3.1/OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.3.1.AppImage" \
    -O /tmp/orcaslicer.AppImage \
  && OFFSET=$(python3 /tmp/find_squashfs_offset.py /tmp/orcaslicer.AppImage) \
  && echo "OrcaSlicer squashfs offset: $OFFSET" \
  && unsquashfs -dest /opt/orcaslicer -offset $OFFSET /tmp/orcaslicer.AppImage \
  && rm /tmp/orcaslicer.AppImage


# ── Stage 2: runtime (linux/amd64 — QEMU on Apple Silicon, native on x86 Linux) ──
# Ubuntu 24.04 required: PrusaSlicer newer-distros and OrcaSlicer 2.3.1 both need
# libwebkit2gtk-4.1 which is not in 22.04.
FROM --platform=linux/amd64 ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    python3 python3-pip \
    libgtk-3-0 libglib2.0-0 libdbus-1-3 \
    libgl1 libglu1-mesa \
    libxrender1 libxrandr2 libxss1 libxcursor1 libxcomposite1 \
    libxi6 libxtst6 libfontconfig1 libfreetype6 \
    libatk1.0-0 libcairo2 libpango-1.0-0 libpangocairo-1.0-0 \
    libwebkit2gtk-4.1-0 \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r /tmp/requirements.txt

COPY --from=slicer-extract /opt/prusaslicer /opt/prusaslicer
COPY --from=slicer-extract /opt/orcaslicer  /opt/orcaslicer

# Wrapper scripts: set bundled lib path, then exec the actual binary
# The AppImage's own prusa-slicer script correctly sets LD_LIBRARY_PATH=$DIR/bin
RUN printf '#!/bin/sh\nexec /opt/prusaslicer/usr/bin/prusa-slicer "$@"\n' \
      > /usr/local/bin/prusa-slicer && chmod +x /usr/local/bin/prusa-slicer

RUN printf '#!/bin/sh\nexport LC_ALL=C\nexport LD_LIBRARY_PATH="/opt/orcaslicer/bin${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"\nexec /opt/orcaslicer/bin/orca-slicer "$@"\n' \
      > /usr/local/bin/orca-slicer && chmod +x /usr/local/bin/orca-slicer

WORKDIR /app
COPY app.py quote.py ./

RUN mkdir -p /data/prusaslicer_config /data/exports

ENV PRUSASLICER_PATH=/usr/local/bin/prusa-slicer
ENV ORCASLICER_PATH=/usr/local/bin/orca-slicer
ENV PRUSA_VENDOR_DIR=/opt/prusaslicer/usr/bin/resources/profiles
ENV PRUSA_USER_DIR=/root/.config/PrusaSlicer
ENV PRESETS_FILE=/data/presets.json
ENV PORT=5111
ENV HOME=/root
ENV DOCKER=1

VOLUME ["/data"]
EXPOSE 5111

CMD ["python3", "app.py"]
